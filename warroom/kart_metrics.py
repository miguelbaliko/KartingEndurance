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
import statistics
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
    """(samples, next_ts) for one recording, each sample tagged with its
    session id for the fallback pace estimate. ts only needs to sort in
    order -- bucket_minutes=0 below makes the actual values irrelevant
    otherwise."""
    session_id = os.path.basename(raw_path)
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
                out.append((session_id, ts, pilot, kart, lap_s, None, None))
            seen_laps[kart] = laps_n
    return out, ts


def official_samples(ts_start: float) -> tuple:
    """Every lap from the official KIP result sheets: already in run order,
    so no fresh-lap dedup is needed -- just hand them to rate() as-is."""
    out = []
    ts = ts_start
    for i, session in enumerate(SESSIONS):
        session_id = f"official-{i}"
        for kart, (pilot, laps) in session.items():
            for lap_s in laps:
                ts += 1
                out.append((session_id, ts, pilot, kart, lap_s, None, None))
    for pilot, kart, lap_s in TRACK_RECORD_EXTRA:
        ts += 1
        out.append(("track-record", ts, pilot, kart, lap_s, None, None))
    return out, ts


def fallback_pace(tagged_samples: list, karts_rated: set) -> dict:
    """A rougher pace estimate for karts rating.rate() cannot place on the
    shared scale: this kart's median lap against its own session's median,
    averaged across sessions it appeared in, weighted by lap count.

    Conflates driver and kart -- it is not a substitute for a linked grade,
    only a "what did we actually see" number for karts the model is right
    to call Unknown.
    """
    session_laps = {}     # session_id -> [lap_s, ...] (all karts, all pilots)
    kart_session_laps = {}  # kart -> {session_id: [lap_s, ...]}
    for session_id, _ts, _pilot, kart, lap_s, _a, _b in tagged_samples:
        session_laps.setdefault(session_id, []).append(lap_s)
        kart_session_laps.setdefault(kart, {}).setdefault(session_id, []).append(lap_s)

    session_floor = {sid: min(laps) for sid, laps in session_laps.items()}
    session_median = {}
    for sid, laps in session_laps.items():
        clean = [l for l in laps if l <= session_floor[sid] * 1.15]
        session_median[sid] = statistics.median(clean or laps)

    out = {}
    for kart, by_session in kart_session_laps.items():
        if kart in karts_rated:
            continue
        deltas, weights = [], []
        for sid, laps in by_session.items():
            floor = min(laps)
            clean = [l for l in laps if l <= floor * 1.15]
            if not clean:
                continue
            deltas.append(statistics.median(clean) - session_median[sid])
            weights.append(len(clean))
        if not deltas:
            continue
        out[kart] = (sum(d * w for d, w in zip(deltas, weights)) / sum(weights),
                     sum(weights))
    return out


def main():
    tagged = []
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
        tagged.extend(new_samples)

    official, ts = official_samples(ts)
    print(f"{'official KIP result sheets':45} {len(official):4} laps")
    tagged.extend(official)

    samples = [s[1:] for s in tagged]   # strip session id for rate()
    # Lowered from the 25h-race default of 8: a 17-19 lap practice heat
    # never gives a kart that many clean laps in one session, and these
    # karts are already linked -- min_laps was gating on volume, not doubt.
    result = rating.rate(samples, {"bucket_minutes": 0, "min_laps": 4})
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

    rated_karts = {k for k, v in result["karts"].items() if v["rated"]}
    fallback = fallback_pace(tagged, rated_karts)
    print(f"\n{'-- unconfirmed: session-relative pace only, conflates driver & kart --':<70}")
    print(f"{'kart':>6}  {'vs session median':>18}  {'laps':>5}")
    for kart, (delta, n) in sorted(fallback.items(), key=lambda kv: kv[1][0]):
        sign = "+" if delta >= 0 else ""
        print(f"{kart:>6}  {sign}{delta:>17.2f}  {n:>5}")


if __name__ == "__main__":
    main()
