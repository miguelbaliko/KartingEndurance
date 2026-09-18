#!/usr/bin/env python3
"""Analyze driver performance across recorded practice sessions."""

import json
import os
from collections import defaultdict
from pathlib import Path


def parse_lap_time(lap_str: str) -> float:
    """Convert lap time string (MM:SS.SSS) to seconds."""
    if not lap_str or lap_str == "?":
        return float("inf")
    try:
        parts = lap_str.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
    except (ValueError, AttributeError):
        pass
    return float("inf")


def load_all_sessions():
    """Load all recorded session JSON files."""
    sessions = []
    for json_file in Path("testdata").glob("apex-*.json"):
        try:
            with open(json_file) as f:
                data = json.load(f)
            sessions.append({
                "file": json_file.name,
                "event": data.get("url", "").split("/")[-2],
                "recorded": data.get("recorded", ""),
                "drivers": data.get("sample_rows", [])
            })
        except Exception as e:
            print(f"Skipped {json_file.name}: {e}")
    return sessions


def analyze_drivers(sessions: list):
    """Build driver performance summary across all sessions."""
    drivers = defaultdict(list)

    for session in sessions:
        event = session["event"]
        recorded = session["recorded"]

        for driver_data in session["drivers"]:
            name = driver_data.get("driver", "")
            if not name:
                continue

            kart = driver_data.get("kart", "")
            best = driver_data.get("best_lap", "")
            last = driver_data.get("last_lap", "")
            laps = int(driver_data.get("total_laps", 0))

            drivers[name].append({
                "event": event,
                "recorded": recorded,
                "kart": kart,
                "best_lap": best,
                "best_seconds": parse_lap_time(best),
                "last_lap": last,
                "total_laps": laps
            })

    return drivers


def report_drivers(drivers: dict):
    """Print driver performance report."""
    if not drivers:
        print("No driver data recorded yet.")
        return

    print("\n" + "=" * 80)
    print("DRIVER PERFORMANCE ACROSS PRACTICE SESSIONS")
    print("=" * 80)

    # Sort by best lap time across all sessions
    sorted_drivers = sorted(
        drivers.items(),
        key=lambda x: min(d["best_seconds"] for d in x[1])
    )

    for rank, (name, sessions) in enumerate(sorted_drivers, 1):
        best_overall = min(s["best_seconds"] for s in sessions)
        total_laps = sum(s["total_laps"] for s in sessions)

        print(f"\n{rank}. {name}")
        print(f"   Best overall: {sessions[0]['best_lap'] if best_overall < float('inf') else 'N/A'}")
        print(f"   Sessions: {len(sessions)}, Total laps: {total_laps}")

        for session in sessions:
            print(f"     • {session['event']} ({session['recorded']}): "
                  f"best {session['best_lap']} ({session['total_laps']} laps) "
                  f"on Kart {session['kart']}")

    print("\n" + "=" * 80)


if __name__ == "__main__":
    sessions = load_all_sessions()
    if sessions:
        print(f"Loaded {len(sessions)} recorded session(s)")
        drivers = analyze_drivers(sessions)
        report_drivers(drivers)
    else:
        print("No sessions recorded yet. Run monitor_sessions.py to capture practice data.")
