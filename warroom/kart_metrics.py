#!/usr/bin/env python3
"""Rate today's practice karts from every recorded session, kart numbers and
all -- tomorrow's race uses the same physical fleet, so a kart that ran fast
today is worth knowing about before the pool even starts moving.

Bypasses KartPool.observe(): it assumes one continuous session, and these are
six disconnected ones where a kart's lap count resets between recordings.
Fresh-lap detection is done locally, per file, instead.
"""
import glob
import os
import sys
import tempfile

os.environ.setdefault("WARROOM_NO_WORKER", "1")
os.environ.setdefault("WARROOM_DB", os.path.join(tempfile.gettempdir(), "kart-metrics-scratch.db"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as warroom
import rating
from testdata.kip_official_results import SESSIONS

# These Apex captures are partial recordings of sessions the official KIP
# result sheets cover completely (session 29 = 1425/1427, session 35 =
# 1539) -- the official data supersedes them, so counting both would
# double-weight the same laps.
SUPERSEDED_BY_OFFICIAL = {"20260918-1425", "20260918-1427", "20260918-1539"}

# A single verified lap outside any of the three transcribed sessions: KIP's
# own "track records" board credits it to a kart number the sessions above
# never show, so it is its own sample rather than folded into one of them.
TRACK_RECORD_EXTRA = [("MIGUEL_SILVA_75", "84", 63.152)]


def samples_from(raw_path: str, ts_start: float) -> tuple:
    """(samples, next_ts) for one recording. ts only needs to sort in order --
    bucket_minutes=0 below makes the actual values irrelevant otherwise."""
    with open(raw_path, encoding="utf-8", errors="replace") as f:
        frames = f.read().split("\n\x00\n")
    seen_laps = {}
    out = []
    ts = ts_start
    for frame in frames:
        rows, cells, meta = warroom._parse_apex_pipe(frame)
        if meta:
            warroom._process_meta(meta)
        if cells:
            warroom._apply_cell_updates(cells)
        if rows:
            warroom._process_rows(rows)
        for t in warroom._teams:
            kart = str(t.get("kart") or "").strip()
            lap_s = t.get("last_lap_s")
            if not kart or not lap_s:
                continue
            try:
                laps_n = int(t.get("total_laps"))
            except (TypeError, ValueError):
                continue
            prev = seen_laps.get(kart)
            if prev is not None and laps_n > prev:
                pilot = (t.get("team") or t.get("driver") or kart).strip()
                ts += 1
                out.append((ts, pilot, kart, lap_s, None, None))
            seen_laps[kart] = laps_n
    return out, ts


def official_samples(ts_start: float) -> tuple:
    """Every lap from the official KIP result sheets: already in run order,
    so no fresh-lap dedup is needed -- just hand them to rate() as-is."""
    out = []
    ts = ts_start
    for session in SESSIONS:
        for kart, (pilot, laps) in session.items():
            for lap_s in laps:
                ts += 1
                out.append((ts, pilot, kart, lap_s, None, None))
    for pilot, kart, lap_s in TRACK_RECORD_EXTRA:
        ts += 1
        out.append((ts, pilot, kart, lap_s, None, None))
    return out, ts


def main():
    samples = []
    ts = 0.0
    for path in sorted(glob.glob(os.path.join(
            os.path.dirname(__file__), "testdata",
            "apex-kip-palmela-20260918-*.raw"))):
        if any(tag in path for tag in SUPERSEDED_BY_OFFICIAL):
            print(f"{os.path.basename(path):45} skipped (superseded by official sheet)")
            continue
        warroom._global_col_types.clear()
        warroom._row_kart_map.clear()
        warroom._teams.clear()
        new_samples, ts = samples_from(path, ts)
        print(f"{os.path.basename(path):45} {len(new_samples):4} laps")
        samples.extend(new_samples)

    official, ts = official_samples(ts)
    print(f"{'official KIP result sheets':45} {len(official):4} laps")
    samples.extend(official)

    result = rating.rate(samples, {"bucket_minutes": 0})
    print(f"\n{result['n_laps']} laps total, {result['linked_karts']} kart(s) linked "
          f"across drivers\n")
    karts = sorted(result["karts"].items(),
                    key=lambda kv: (kv[1]["delta"] is None, kv[1]["delta"] or 0))
    print(f"{'kart':>6}  {'label':<12} {'delta':>7}  {'laps':>5}  {'pilots':>6}  reason")
    for kart, info in karts:
        delta = info["delta"]
        delta_s = f"+{delta:.2f}" if delta is not None else "   -"
        print(f"{kart:>6}  {info['label']:<12} {delta_s:>7}  {info['raw_laps']:>5}  "
              f"{info['pilots']:>6}  {info['reason']}")


if __name__ == "__main__":
    main()
