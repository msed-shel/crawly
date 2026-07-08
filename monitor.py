#!/usr/bin/env python3
"""
Radio 88.6 monitor — TEST version.

Unlike monitor.py, this ignores the watched artist and the dedup state.
On EVERY execution it fetches the playlist and pushes an ntfy message listing
all upcoming songs. Use it to confirm the API + ntfy pipeline works end to end,
then switch back to monitor.py for real alerting.
"""

import json
import os
import sys
import urllib.request
import urllib.error

API_URL = os.environ.get("API_URL", "https://meta.radio886.at/886/0")
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh/radio-alerts-886-emberscollide")
TIMEOUT = 20


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "radio886-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_ntfy(message, title="Radio 88.6 TEST"):
    req = urllib.request.Request(
        NTFY_URL,
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": "default", "Tags": "test_tube"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        resp.read()


def main():
    try:
        data = fetch_json(API_URL)
    except (urllib.error.URLError, TimeoutError) as e:
        msg = f"TEST: error fetching API: {e}"
        print(msg)
        try:
            send_ntfy(msg)
        except Exception as e2:
            print(f"Also failed to send ntfy: {e2}")
        return 0

    songs = data.get("data", [])
    upcoming = [
        s for s in songs
        if s.get("played") is False and s.get("is_playing") is False
    ]

    if upcoming:
        lines = [
            f"{s.get('scheduled_time', '?')}  {s.get('name', '?')} - {s.get('title', '?')}"
            for s in upcoming
        ]
        message = "Upcoming songs:\n\n" + "\n".join(lines)
    else:
        message = "No upcoming songs in the feed right now."

    print(message)
    try:
        send_ntfy(message)
        print(f"\nPushed {len(upcoming)} upcoming song(s) to ntfy.")
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"Error sending ntfy: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
