#!/usr/bin/env python3
"""Record a real Apex Timing feed, so the parser can be checked against it.

Run this on a machine that can reach live.apex-timing.com during a session —
practice, qualifying, anything with karts on track:

    python3 apex_dump.py https://live.apex-timing.com/kip-palmela/ --seconds 120

It writes two files next to itself:

    apex-<event>-<stamp>.raw    every WebSocket frame, exactly as it arrived
    apex-<event>-<stamp>.json   what our parser made of it, and what it missed

The .json is the interesting one: it lists the columns we recognised, the
columns we ignored, whether the pit counter and the race clock came through,
and the first parsed rows.  If a field is missing there, it will be missing in
the war room too — send the pair over and the parser can be taught the event's
layout without guessing.

Replay a recording into the war room later with:

    python3 apex_dump.py --replay apex-kip-palmela-1730.raw
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as warroom


def event_name(url: str) -> str:
    m = re.search(r'apex-timing\.com/([^/#?]+)', url)
    return m.group(1) if m else "event"


def analyse(frames: list) -> dict:
    """What our parser understood, and — more usefully — what it did not."""
    rows, metas, seen_types = [], [], set()
    for frame in frames:
        seen_types.update(re.findall(r'data-type="([^"]+)"', frame))
        parsed, _cells, meta = warroom._parse_apex_pipe(frame)
        rows.extend(parsed)
        if meta:
            metas.append(meta)
    unknown_types = {t for t in seen_types if t not in warroom._CELL_MAP}

    fields = sorted({k for r in rows for k in r})
    clock = [m.get("dyn1") or m.get("dyn2") for m in metas if m.get("dyn1") or m.get("dyn2")]
    return {
        "frames": len(frames),
        "rows_parsed": len(rows),
        "fields_we_read": fields,
        "columns_recognised": sorted(seen_types & set(warroom._CELL_MAP)),
        "columns_ignored": sorted(unknown_types),
        "has_pit_counter": any(r.get("pits") for r in rows),
        "has_driver": any(r.get("driver") for r in rows),
        "has_category": any(r.get("category") for r in rows),
        "clock_samples": clock[:5],
        "clock_parsed": bool(clock and warroom.ApexClock().update(clock[-1])),
        "sample_rows": rows[:5],
        "commands": sorted({l.split("|")[0] for f in frames
                            for l in f.replace("\r", "").split("\n") if "|" in l}),
    }


def record(url: str, seconds: float, out_dir: str):
    try:
        import websocket
        import ssl
    except ImportError:
        sys.exit("pip install websocket-client first")

    warroom.CFG["apex_url"] = url
    ws_url = warroom._find_ws_url(url)
    if not ws_url:
        sys.exit(f"No WebSocket URL found behind {url} — is the event live?")
    print(f"[apex] {ws_url}")

    frames, stop_at = [], time.time() + seconds

    def on_message(_ws, msg):
        frames.append(msg)
        if len(frames) % 25 == 0:
            print(f"  {len(frames)} frames…", flush=True)
        if time.time() > stop_at:
            _ws.close()

    ws = websocket.WebSocketApp(ws_url, on_message=on_message)
    ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE}, ping_interval=30)

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    base = os.path.join(out_dir, f"apex-{event_name(url)}-{stamp}")
    with open(base + ".raw", "w") as f:
        f.write("\n\x00\n".join(frames))
    report = {"url": url, "ws_url": ws_url, "recorded": stamp, **analyse(frames)}
    with open(base + ".json", "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n  {base}.raw\n  {base}.json\n")
    print(f"  rows parsed      {report['rows_parsed']}")
    print(f"  columns read     {', '.join(report['columns_recognised']) or 'NONE'}")
    print(f"  columns ignored  {', '.join(report['columns_ignored']) or 'none'}")
    print(f"  pit counter      {'yes' if report['has_pit_counter'] else 'NO'}")
    print(f"  driver names     {'yes' if report['has_driver'] else 'no'}")
    print(f"  race clock       {'yes' if report['clock_parsed'] else 'NO'}")
    if report["columns_ignored"] or not report["has_pit_counter"]:
        print("\n  Send both files over — the parser needs teaching for this event.")


def replay(path: str, speed: float):
    """Push a recording through the war room as if it were live."""
    with open(path) as f:
        frames = f.read().split("\n\x00\n")
    print(f"replaying {len(frames)} frames from {path}")
    for frame in frames:
        rows, cells, meta = warroom._parse_apex_pipe(frame)
        if meta:
            warroom._process_meta(meta)
        if cells:
            warroom._apply_cell_updates(cells)
        warroom._process_rows(rows)
        time.sleep(1.0 / max(0.1, speed))
    warroom.app.run(host="0.0.0.0", port=8080, use_reloader=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", help="the event's live timing page")
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--replay", help="a .raw file recorded earlier")
    ap.add_argument("--speed", type=float, default=20.0, help="replay frames per second")
    args = ap.parse_args()

    if args.replay:
        replay(args.replay, args.speed)
    elif args.url:
        record(args.url, args.seconds, args.out)
    else:
        ap.error("give an event URL to record, or --replay a recording")


if __name__ == "__main__":
    main()
