#!/usr/bin/env python3
"""Monitor Apex events throughout the day and auto-record driver data from practice sessions."""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apex_dump
import app as warroom


def load_recorded_sessions() -> set:
    """Load set of already-recorded session timestamps to avoid re-recording."""
    try:
        with open("testdata/recorded_sessions.json") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_recorded_sessions(sessions: set):
    """Save set of recorded session timestamps."""
    with open("testdata/recorded_sessions.json", "w") as f:
        json.dump(sorted(sessions), f, indent=2)


def extract_drivers(json_path: str) -> list:
    """Extract driver data from recorded session."""
    try:
        with open(json_path) as f:
            data = json.load(f)
        return data.get("sample_rows", [])
    except Exception as e:
        print(f"Could not extract drivers from {json_path}: {e}")
        return []


def run_monitor(interval: int = 600, duration: int = None):
    """Monitor KNOWN_EVENTS and auto-record live sessions.

    Args:
        interval: seconds between probes (default 10 minutes)
        duration: total runtime in seconds (None = run forever)
    """
    print(f"Starting monitor at {datetime.now().isoformat()}")
    print(f"Probe interval: {interval}s, monitoring: {', '.join(apex_dump.KNOWN_EVENTS)}")

    recorded = load_recorded_sessions()
    start_time = time.time()

    while duration is None or (time.time() - start_time) < duration:
        try:
            live, unreachable = apex_dump.find(apex_dump.KNOWN_EVENTS, seconds=8)

            for url, session_info in live:
                event = apex_dump.event_name(url)
                clock = session_info.get("clock", "")
                karts = session_info.get("karts", 0)

                # Use clock as session identifier if available
                session_id = f"{event}@{clock}" if clock else f"{event}@{datetime.now().strftime('%H%M')}"

                if session_id not in recorded and karts > 0:
                    print(f"\n✓ LIVE: {event} with {karts} karts (clock: {clock})")
                    try:
                        # Record for 120 seconds
                        apex_dump.record(url, 120, "testdata")
                        recorded.add(session_id)
                        save_recorded_sessions(recorded)

                        # Extract and display driver info
                        stamp = datetime.now().strftime("%Y%m%d-%H%M")
                        json_file = f"testdata/apex-{event}-{stamp}.json"
                        if os.path.exists(json_file):
                            drivers = extract_drivers(json_file)
                            if drivers:
                                print(f"\n  Drivers in session:")
                                for i, driver in enumerate(drivers, 1):
                                    d_name = driver.get("driver", "?")
                                    d_kart = driver.get("kart", "?")
                                    best = driver.get("best_lap", "?")
                                    print(f"    {i}. {d_name:15} (Kart {d_kart}) - {best}")
                    except Exception as e:
                        print(f"  Error recording: {e}")

            elapsed = time.time() - start_time
            if duration and elapsed >= duration:
                break

            # Wait for next probe
            print(f"\nNext probe in {interval}s ({datetime.now().isoformat()})")
            time.sleep(interval)

        except KeyboardInterrupt:
            print("\nMonitor stopped.")
            break
        except Exception as e:
            print(f"Probe error: {e}")
            time.sleep(interval)

    print(f"Monitor finished. Recorded {len(recorded)} sessions.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Monitor and record Apex sessions")
    parser.add_argument("--interval", type=int, default=600,
                        help="Probe interval in seconds (default 600)")
    parser.add_argument("--duration", type=int, default=None,
                        help="Run duration in seconds (None = forever)")
    parser.add_argument("--once", action="store_true",
                        help="Run single probe and exit")
    args = parser.parse_args()

    if args.once:
        live, unreachable = apex_dump.find(apex_dump.KNOWN_EVENTS, seconds=8)
        for url, s in live:
            print(f"{apex_dump.event_name(url)}: {s['karts']} karts")
    else:
        run_monitor(interval=args.interval, duration=args.duration)
