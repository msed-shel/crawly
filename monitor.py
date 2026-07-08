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
from pathlib import Path

# --- Config (override via env / GitHub secrets) ---------------------------
API_URL = os.environ.get("API_URL", "https://meta.radio886.at/886/0")
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh/radio-alerts-886-emberscollide")
ARTIST_TO_WATCH = os.environ.get("ARTIST_TO_WATCH", "EMBERS COLLIDE").upper()

STATE_FILE = Path(__file__).parent / "notified.json"
MAX_REMEMBERED = 200  # prune old IDs so the state file doesn't grow forever
TIMEOUT = 20


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
        print(f"Error fetching API: {e}")
        # Don't fail the workflow on a transient network hiccup.
        return 0

    songs = data.get("data", [])
    # "Upcoming" = queued but not yet played and not currently playing.
    upcoming = [
        s for s in songs
        if s.get("played") is False and s.get("is_playing") is False
    ]

    new_alerts = 0
    for song in upcoming:
        artist = (song.get("name") or "").upper()
        if ARTIST_TO_WATCH not in artist:
            continue

        song_id = str(song.get("id"))
        if song_id in notified:
            continue

        message = (
            f"{ARTIST_TO_WATCH.title()} is coming up!\n\n"
            f"Time: {song.get('scheduled_time', '?')}\n"
            f"Song: {song.get('title', '?')}\n"
            f"Artist: {song.get('name', '?')}"
        )
        print(message)
        try:
            send_ntfy(message)
            notified.add(song_id)
            new_alerts += 1
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"Error sending ntfy: {e}")

    if new_alerts == 0:
        print(f"No new upcoming {ARTIST_TO_WATCH} tracks found.")

    # Keep the newest IDs only.
    state["notified_ids"] = list(notified)[-MAX_REMEMBERED:]
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
