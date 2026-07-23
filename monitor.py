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
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# --- Config (override via env / GitHub secrets) ---------------------------
# NOTE: TEST configuration — Fall Out Boy (a frequently-played band) and the
# test ntfy channel. To go back to real monitoring, restore:
#   NTFY_URL default -> "https://ntfy.sh/radio-alerts-886-emberscollide"
#   ARTIST_TO_WATCH default -> "EMBERS COLLIDE"
# (or set the NTFY_URL / ARTIST_TO_WATCH repo secrets, which override these).
API_URL = os.environ.get("API_URL", "https://meta.radio886.at/886/0")
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh/radio_886_test")
ARTIST_TO_WATCH = os.environ.get("ARTIST_TO_WATCH", "REV THEORY").upper()

STATE_FILE = Path(__file__).parent / "notified.json"
LOG_FILE = Path(__file__).parent / "events.log"
MAX_REMEMBERED = 200   # prune old IDs so the state file doesn't grow forever
MAX_LOG_LINES = 2000   # keep the log bounded
TIMEOUT = 20
# When on, log a summary line for EVERY run (upcoming count + whether the
# watched artist was in the feed). Off by default so the committed log only
# changes when something noteworthy happens. Set env MONITOR_VERBOSE=1 to debug
# a suspected detection miss.
VERBOSE = os.environ.get("MONITOR_VERBOSE", "").lower() in ("1", "true", "yes")


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
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
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
    notified = set(state.get("notified_ids", []))

    try:
        data = fetch_json(API_URL)
    except (urllib.error.URLError, TimeoutError) as e:
        # Log the failure so a miss can be traced to the fetch, not ntfy.
        log_event("fetch_error", error=str(e))
        # Don't fail the workflow on a transient network hiccup.
        return 0

    songs = data.get("data", [])
    # The feed is a short rolling window: a few recently-played tracks, the one
    # currently playing, and a couple of upcoming ones. Matching only "upcoming"
    # is fragile — a track can slip from upcoming past playing to played between
    # two polls and never be seen in that single state. So we scan the WHOLE
    # window and dedup by song id; this also catches a track first seen while it
    # is playing or just after it played.

    if VERBOSE:
        present = any(ARTIST_TO_WATCH in (s.get("name") or "").upper()
                      for s in songs)
        log_event("run", feed=len(songs), watched_present=present)

    new_alerts = 0
    for song in songs:
        artist = (song.get("name") or "").upper()
        if ARTIST_TO_WATCH not in artist:
            continue

        song_id = str(song.get("id"))
        if song_id in notified:
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
            notified.add(song_id)
            new_alerts += 1
            # "the push went out" marker — its absence after a match = ntfy issue.
            log_event("notified", id=song_id, title=song.get("title"))
        except (urllib.error.URLError, TimeoutError) as e:
            # Not added to notified, so the next run will retry the alert.
            log_event("ntfy_error", id=song_id, error=str(e))

    if new_alerts == 0:
        print(f"No new upcoming {ARTIST_TO_WATCH} tracks found.")

    # Keep the newest IDs only.
    state["notified_ids"] = list(notified)[-MAX_REMEMBERED:]
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
