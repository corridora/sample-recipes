#!/usr/bin/env python3
"""Scheduled browser watchdog. Only a successful UI journey writes heartbeat."""
from datetime import datetime, timezone, timedelta
import json
import os
import subprocess
import urllib.request

def needs_replay(state, now, force=False):
    hour, minute = map(int, state["daily_start_utc"].split(":"))
    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < start:
        start -= timedelta(days=1)
    end = start + timedelta(seconds=state.get("window_seconds", 600))
    if end.date() < now.date():
        return force
    heartbeat = state.get("heartbeat") or {}
    return force or (now >= end and heartbeat.get("utc_date") != now.date().isoformat())


def main():
    url = os.environ.get("BASE_URL", "").rstrip("/")
    token = os.environ.get("DEMO_CONTROL_TOKEN", "")
    if not url or not token:
        raise SystemExit("BASE_URL and dedicated DEMO_CONTROL_TOKEN are required")
    with urllib.request.urlopen(url + "/api/demo/status", timeout=20) as response:
        state = json.load(response)
    if not needs_replay(state, datetime.now(timezone.utc), os.environ.get("FORCE_REPLAY") == "true"):
        print("No replay needed; outside watchdog time or browser heartbeat already recorded.")
    else:
        print("Browser heartbeat missing; replaying bounded healthy/regression/control/recovery journey.")
        subprocess.run(["npm", "run", "test:e2e"], check=True, timeout=180)


if __name__ == "__main__":
    main()
