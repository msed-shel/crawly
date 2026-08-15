#!/usr/bin/env python3
"""
Radio 88.6 monitor — GitHub Actions edition.

Polls the Radio 88.6 metadata API once per run, looks for upcoming songs by a
watched artist, and sends an ntfy push for any it hasn't alerted on before.

Because GitHub Actions runs are stateless, the set of already-notified song IDs
is persisted to notified.json, which the workflow commits back after each run.
"""

import json
import os
import ssl
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# --- Config (override via env / GitHub secrets) ---------------------------
# While debugging Embers Collide, alerts go to the TEST channel. Switch NTFY_URL
# back to "https://ntfy.sh/radio-alerts-886-emberscollide" once it's confirmed
# working (or set the NTFY_URL / ARTIST_TO_WATCH repo secrets, which override).
API_URL = os.environ.get("API_URL", "https://meta.radio886.at/886/0")
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh/radio-alerts-886-emberscollide")
ARTIST_TO_WATCH = os.environ.get("ARTIST_TO_WATCH", "EMBERS COLLIDE").upper()


def norm_name(s):
    """Normalize an artist name for matching: uppercase, letters+digits only.

    This makes matching insensitive to spaces and punctuation, so a watch value
    of "EMBERS COLLIDE" also matches a feed name like "EMBERSCOLLIDE".
    """
    return "".join(c for c in (s or "").upper() if c.isalnum())


WATCH_NORM = norm_name(ARTIST_TO_WATCH)

STATE_FILE = Path(__file__).parent / "notified.json"
LOG_FILE = Path(__file__).parent / "events.log"
FEED_LOG = Path(__file__).parent / "feed.log"
MAX_REMEMBERED = 200    # prune old alert IDs so the state file doesn't grow forever
MAX_LOG_LINES = 2000    # keep events.log bounded
MAX_FEED_LINES = 5000   # keep feed.log bounded
MAX_FEED_SEEN = 800     # how many track IDs to remember for feed-log dedup
TIMEOUT = 20
# When on, log a summary line for EVERY run (upcoming count + whether the
# watched artist was in the feed). Off by default so the committed log only
# changes when something noteworthy happens. Set env MONITOR_VERBOSE=1 to debug
# a suspected detection miss.
VERBOSE = os.environ.get("MONITOR_VERBOSE", "").lower() in ("1", "true", "yes")

# Feed logging: ON by default. To turn it off without editing this file, set the
# env var / repo variable MONITOR_FEED_LOG to 0 (or false/no/off). To disable it
# permanently in code, change the default below from "1" to "0".
FEED_LOG_ENABLED = os.environ.get("MONITOR_FEED_LOG", "1").lower() \
    not in ("0", "false", "no", "off")

# Skip TLS cert verification for the meta-API fetch (their cert keeps expiring).
# ON by default; set MONITOR_INSECURE_SSL=0 to re-enable strict verification.
INSECURE_SSL = os.environ.get("MONITOR_INSECURE_SSL", "1").lower() \
    not in ("0", "false", "no", "off")

# One alert per airing. A song's id is per-SONG (identical every time it plays),
# so we dedup on the ARTIST with a cooldown instead of the id: after alerting we
# suppress repeats for this many minutes (long enough to cover the ~25 min a
# track lingers across the feed's upcoming/playing/played window), then a later
# play of the band alerts again. Override via env MONITOR_COOLDOWN_MIN.
COOLDOWN_MIN = int(os.environ.get("MONITOR_COOLDOWN_MIN", "60"))


def log_event(event, **fields):
    """Append one JSON-line event (with a UTC timestamp) to events.log."""
    entry = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "event": event, **fields}
    line = json.dumps(entry, ensure_ascii=False)
    print("LOG " + line)
    try:
        lines = LOG_FILE.read_text().splitlines() if LOG_FILE.exists() else []
    except OSError:
        lines = []
    lines.append(line)
    LOG_FILE.write_text("\n".join(lines[-MAX_LOG_LINES:]) + "\n")


def append_feed_log(songs, feed_seen):
    """Record every not-yet-seen track (any state) to feed.log for diagnostics.

    Deduped by song id so the feed's repetition across polls doesn't bloat the
    file — each track is logged once, when first seen. This is what lets you
    confirm whether/how a band (e.g. Embers Collide) ever appears in the feed.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_lines = []
    for s in songs:
        sid = str(s.get("id"))
        if sid in feed_seen:
            continue
        feed_seen.add(sid)
        if s.get("is_playing"):
            state = "playing"
        elif s.get("played"):
            state = "played"
        else:
            state = "upcoming"
        new_lines.append(
            f"{now} | {s.get('scheduled_time', '?')} | {state:8} | "
            f"{s.get('name') or '?'} - {s.get('title') or '?'} | id={sid}"
        )
    if new_lines:
        try:
            existing = FEED_LOG.read_text().splitlines() if FEED_LOG.exists() else []
        except OSError:
            existing = []
        existing.extend(new_lines)
        FEED_LOG.write_text("\n".join(existing[-MAX_FEED_LINES:]) + "\n")
        for line in new_lines:
            print("FEED " + line)
    return feed_seen


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text() or "{}")
        except json.JSONDecodeError:
            print("Warning: notified.json unreadable, starting fresh.")
    return {"notified_ids": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "radio886-monitor/1.0"})
    # The station periodically lets the meta-API TLS certificate expire. Since
    # this is a read-only public endpoint, optionally skip cert verification for
    # THIS request only (ntfy calls below still verify normally). Turn strict
    # checking back on by setting env MONITOR_INSECURE_SSL=0 once they renew.
    ctx = None
    if INSECURE_SSL:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


def format_details(song):
    """Pull album/artist/fun-fact info out of prefetchedMetainfos, if present.

    Not every track carries this (lesser-known artists often don't), so every
    lookup is defensive and the function returns "" when there's nothing to add.
    """
    meta = song.get("prefetchedMetainfos")
    if not isinstance(meta, dict):
        return ""

    lines = []

    # Album / release
    songinfo = meta.get("song") or {}
    release = songinfo.get("release")
    if release:
        rel = f"Album: {release}"
        if songinfo.get("release_date"):
            rel += f" ({songinfo['release_date']})"
        lines.append(rel)

    # Main artist bio + a link if available
    artists = meta.get("artists") or []
    main = next((a for a in artists if isinstance(a, dict)
                 and a.get("relationship") == "main"), None)
    if main is None and artists and isinstance(artists[0], dict):
        main = artists[0]
    if isinstance(main, dict):
        bits = []
        if main.get("begin"):
            bits.append(f"since {main['begin']}")
        if main.get("country"):
            bits.append(main["country"])
        if bits:
            lines.append(f"About: {', '.join(bits)}")
        if main.get("web"):
            lines.append(f"Web: {main['web']}")

    # Fun facts — the API pre-flattens these into one list, but with duplicates.
    seen, facts = set(), []
    for f in (meta.get("funfacts") or []):
        if isinstance(f, str):
            t = f.strip()
            if t and t not in seen:
                seen.add(t)
                facts.append(t)
    if facts:
        lines.append("")
        lines.append("Fun facts:")
        lines.extend(f"• {t}" for t in facts)

    return "\n".join(lines)


def send_ntfy(message):
    data = message.encode("utf-8")
    req = urllib.request.Request(
        NTFY_URL,
        data=data,
        headers={
            "Title": "Radio 88.6 Alert",
            "Priority": "high",
            "Tags": "musical_note",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        resp.read()


def main():
    state = load_state()
    alerts = dict(state.get("artist_alerts", {}))   # normalized artist -> last alert epoch
    feed_seen = set(state.get("feed_seen_ids", []))
    now = time.time()

    try:
        data = fetch_json(API_URL)
    except (urllib.error.URLError, TimeoutError) as e:
        # Log the failure so a miss can be traced to the fetch, not ntfy.
        log_event("fetch_error", error=str(e))
        # Don't fail the workflow on a transient network hiccup.
        return 0

    songs = data.get("data", [])

    # Diagnostic: log every track the feed shows (deduped by id).
    if FEED_LOG_ENABLED:
        feed_seen = append_feed_log(songs, feed_seen)

    # The feed is a short rolling window: a few recently-played tracks, the one
    # currently playing, and a couple of upcoming ones. Matching only "upcoming"
    # is fragile — a track can slip from upcoming past playing to played between
    # two polls and never be seen in that single state. So we scan the WHOLE
    # window and dedup by song id; this also catches a track first seen while it
    # is playing or just after it played.

    if VERBOSE:
        present = any(WATCH_NORM in norm_name(s.get("name")) for s in songs)
        log_event("run", feed=len(songs), watched_present=present)

    new_alerts = 0
    for song in songs:
        # Normalized match — insensitive to spaces/punctuation/case.
        if WATCH_NORM not in norm_name(song.get("name")):
            continue

        song_id = str(song.get("id"))
        # Dedup by artist + cooldown (NOT by song id — see COOLDOWN_MIN note).
        artist_key = norm_name(song.get("name"))
        last = alerts.get(artist_key)
        if last is not None and (now - last) < COOLDOWN_MIN * 60:
            continue

        if song.get("is_playing"):
            status = "is playing now"
        elif song.get("played"):
            status = "was just played"
        else:
            status = "is coming up"

        message = (
            f"{ARTIST_TO_WATCH.title()} {status}!\n\n"
            f"Time: {song.get('scheduled_time', '?')}\n"
            f"Song: {song.get('title', '?')}\n"
            f"Artist: {song.get('name', '?')}"
        )
        details = format_details(song)
        if details:
            message += "\n\n" + details
        print(message)

        # Record detection FIRST — this is the "the crawler saw it" marker.
        log_event("match", id=song_id, artist=song.get("name"),
                  title=song.get("title"),
                  scheduled_time=song.get("scheduled_time"))
        try:
            send_ntfy(message)
            alerts[artist_key] = now      # start the cooldown for this artist
            new_alerts += 1
            # "the push went out" marker — its absence after a match = ntfy issue.
            log_event("notified", id=song_id, title=song.get("title"))
        except (urllib.error.URLError, TimeoutError) as e:
            # Cooldown not started, so the next run will retry the alert.
            log_event("ntfy_error", id=song_id, error=str(e))

    if new_alerts == 0:
        print(f"No new {ARTIST_TO_WATCH} plays to alert (in cooldown or not in feed).")

    # Persist: drop stale cooldown entries; keep the feed-seen set bounded.
    cutoff = now - 30 * 86400
    state["artist_alerts"] = {k: v for k, v in alerts.items() if v >= cutoff}
    state["feed_seen_ids"] = list(feed_seen)[-MAX_FEED_SEEN:]
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
