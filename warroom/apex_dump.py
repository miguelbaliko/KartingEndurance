#!/usr/bin/env python3
"""Find and record real Apex Timing sessions, so the parser can be checked.

Run this on a machine that can reach live.apex-timing.com.  To see which of the
events we care about have cars on track right now:

    python3 apex_dump.py --find
    python3 apex_dump.py --find kip-palmela kartalcanede other-event

To record one during a session — practice, qualifying, anything with karts on
track:

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
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# We drive the feed ourselves below; the app's own poller would race us for the
# AJAX cursor.  --replay starts the server afterwards, which does not need it.
os.environ.setdefault("WARROOM_NO_WORKER", "1")
# Looking for a session is read-only work, but importing the app opens a race
# database.  Point it at a scratch file so probing never writes to the real one.
os.environ.setdefault("WARROOM_DB", os.path.join(
    tempfile.gettempdir(), "apex-dump-probe.db"))

import app as warroom


# The events this war room follows.  Add a slug and --find will watch it too.
#
# The first two are ours.  The rest are other Apex tracks, kept here only so
# there is something running to test the parser against midweek — ours are dark
# except on race weekends, and a layout we have never parsed is the main risk.
KNOWN_EVENTS = ["kip-palmela", "kartalcanede"]

OTHER_TRACKS = ["kartplanet", "kartodromodeviana", "wsk", "rgmmc", "rgmmc2",
                "ligue-karting-op", "korridas", "rkc", "lemans-karting",
                "elk-motorsport"]


def event_name(url: str) -> str:
    m = re.search(r'apex-timing\.com/([^/#?]+)', url)
    return m.group(1) if m else "event"


def event_url(slug: str) -> str:
    """Accept either a slug or a full URL, so paste-what-you-have works."""
    return slug if slug.startswith("http") else f"https://live.apex-timing.com/{slug}/"


# Apex's light command: lr red, lg green, ly yellow, lsc safety car, lf the
# chequered flag.  Without lf a finished session reports its raw code and reads
# as if it were still running.
# Columns we have looked at and deliberately do not read, so that a recording
# carrying them is not reported as a layout we cannot parse.
#
#   sta  Apex's on-track status flag.  Every fixture carries it and it reads
#        "sr" for every kart; pit state comes from the row class.  Teaching it
#        other values needs a recording that actually contains one.
#   otr  "En piste" at RKC: how long this kart's current run has been, as
#        m:ss, or the literal "in" while it is in the pits.  Genuinely useful
#        — it is every rival's stint clock — but KIP Palmela does not send the
#        column, so parsing it would be building for a track we do not race
#        at.  Revisit if a feed we actually use starts carrying it.
KNOWN_SKIPPED = frozenset({"sta", "otr"})

LIGHTS = {"lg": "GREEN", "ly": "YELLOW", "lr": "RED", "lsc": "SAFETY CAR",
          "lf": "CHEQUERED"}


def summarise(frames: list) -> dict:
    """Is this session actually running, and what is on track?

    Kept apart from the network so it can be tested against recorded frames.
    """
    report = analyse(frames)
    meta = {}
    for frame in frames:
        _rows, _cells, m = warroom._parse_apex_pipe(frame)
        meta.update({k: v for k, v in m.items() if v})
    return {
        "session": meta.get("name", ""),
        "light": LIGHTS.get(meta.get("light", ""), meta.get("light", "")),
        "clock": meta.get("dyn1") or meta.get("dyn2") or "",
        "karts": report["rows_parsed"],
        "on_track": report["rows_on_track"],
        # Karts on the board is not a session running.  Apex leaves the final
        # grid up after the flag, green light and all, so a finished session
        # looks exactly like a live one in a single frame — kartplanet read as
        # LIVE for three hourly sweeps with nobody on track.  What tells them
        # apart is movement: a running session sends cell updates every few
        # seconds, a finished one sends the grid and then nothing.
        "updates": max(0, len(frames) - 1),
        "live": report["rows_parsed"] > 0 and len(frames) > 1,
        "grid_up": report["rows_parsed"] > 0,
        "columns_ignored": report["columns_ignored"],
        "has_pit_counter": report["has_pit_counter"],
        "has_category": report["has_category"],
    }


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
        # Lap times on the board mean karts actually circulating, as opposed to
        # a grid sitting in parc fermé.
        "rows_on_track": sum(1 for r in rows
                             if r.get("last_lap") not in (None, "", "-", "--")),
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


def listen(url: str, seconds: float, tries: int = 1) -> list:
    """Collect whatever the event sends for a few seconds.

    Tries the WebSocket first, then falls back to Apex's AJAX feed.  The two
    carry the same payload, and the timing ports sit on a different host to the
    event page — so on a network that only lets 443 out, polling is the one
    that works.  Same frames either way.
    """
    warroom.CFG["apex_url"] = url
    warroom._ws_url_cache, warroom._ws_url_checked_at = None, 0.0
    warroom._reset_ajax_state()
    # A dropped handshake during discovery is not an answer, the same way a
    # dropped poll is not one in listen_ajax.  The sweep takes the first answer
    # and says "not a verdict" when it fails, so it stays on one try; a
    # recording gets one shot at a session that is live right now, so it asks
    # again.  Clearing the timestamp is what makes the retry immediate —
    # _find_apex_endpoints otherwise sits on a failure for fifteen seconds.
    for _ in range(max(1, tries)):
        if warroom._find_apex_endpoints(url):
            break
        warroom._ws_url_checked_at = 0.0
    else:
        return []

    frames = listen_ws(url, seconds)
    return frames if frames else listen_ajax(url, seconds)


def listen_ws(url: str, seconds: float) -> list:
    ws_url = warroom._find_ws_url(url)
    if not ws_url:
        return []
    try:
        import websocket
        import ssl
    except ImportError:
        return []

    frames, stop_at = [], time.time() + seconds

    def on_message(_ws, msg):
        frames.append(msg)
        if time.time() > stop_at:
            _ws.close()

    ws = websocket.WebSocketApp(ws_url, on_message=on_message)
    # Nothing may ever arrive, so the socket needs its own deadline too.
    threading.Timer(seconds + 5, ws.close).start()
    ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE}, ping_interval=30)
    return frames


def listen_ajax(url: str, seconds: float, interval: float = 2.0,
                retries: int = 3) -> list:
    """Poll the AJAX feed and keep every non-empty payload as a frame.

    A dropped TLS handshake is routine here and must not eat the budget: a
    failed poll buys back the time it cost, up to ``retries`` times, because
    otherwise a short probe spends its whole window on one timeout and calls
    a live track dark.
    """
    frames, stop_at = [], time.time() + seconds
    left = retries
    while True:
        before = warroom._ajax_state.get("errors", 0)
        payload = warroom._fetch_http(url)
        if payload.strip():
            frames.append(payload)
        failed = warroom._ajax_state.get("errors", 0) > before
        if time.time() >= stop_at:
            # Out of time.  A probe that has heard nothing but failures gets a
            # few more goes — a dropped handshake is not an answer — but a
            # track that is genuinely unreachable must not hold up the sweep.
            if not (failed and left and not frames):
                return frames
            left -= 1
        time.sleep(interval)


def find(slugs: list, seconds: float):
    """Probe each event and say which ones have karts on track."""
    print(f"\nProbing {len(slugs)} event(s), {seconds:g}s each\n")
    live, unreachable = [], []
    for slug in slugs:
        url = event_url(slug)
        print(f"  {event_name(url):<22}", end="", flush=True)
        errs_before = warroom._ajax_state.get("errors", 0)
        try:
            frames = listen(url, seconds)
        except Exception as e:
            unreachable.append(slug)
            print(f"unreachable — {e}")
            continue
        if not frames:
            # Nothing came back — but only silence from a feed we actually
            # reached means the track is dark.  Reporting a timeout as "nothing
            # broadcasting" is how this watch missed a live session.
            # Discovery failing is the same kind of silence: without endpoints
            # we never reached the feed, so we have not heard it say anything.
            if (warroom._ajax_state.get("errors", 0) > errs_before
                    or not warroom._find_apex_endpoints(url)):
                unreachable.append(slug)
                print("could not reach the feed (polls failed) — not a verdict")
            else:
                print("no feed (event page up, nothing broadcasting)")
            continue
        s = summarise(frames)
        if not s["live"]:
            if s["grid_up"]:
                print(f"grid up, nothing moved in {seconds:g}s — "
                      f"{s['karts']} karts, session likely over")
            else:
                print("connected, empty grid")
            continue
        live.append((url, s))
        print(f"LIVE · {s['karts']} karts, {s['on_track']} with lap times"
              f"{' · ' + s['session'] if s['session'] else ''}"
              f"{' · ' + s['light'] if s['light'] else ''}"
              f"{' · ' + s['clock'] if s['clock'] else ''}")
        unseen = sorted(set(s["columns_ignored"]) - KNOWN_SKIPPED)
        if unseen:
            print(f"  {'':<22}columns we have never seen: {', '.join(unseen)}")
        if not s["has_pit_counter"]:
            print(f"  {'':<22}no pit counter — stops fall back to lap-time spikes")

    if live:
        print("\n  Record one with:\n")
        for url, _s in live:
            print(f"    python3 apex_dump.py {url} --seconds 120")
    elif unreachable:
        # Never report a sweep that could not reach anything as a quiet track:
        # the whole point of the watch is to notice a live session, and this is
        # the shape of a failure that looks exactly like success.
        print(f"\n  Reached nothing: {len(unreachable)} of {len(slugs)} events "
              f"failed to answer ({', '.join(unreachable[:6])}"
              f"{'…' if len(unreachable) > 6 else ''}).")
        print("  This is a network result, not a verdict on what is running.")
    else:
        print("\n  Nothing running. Try again during a race, practice or qualifying.")
    if live and unreachable:
        print(f"  ({len(unreachable)} other event(s) could not be reached.)")
    print()


def record(url: str, seconds: float, out_dir: str):
    print(f"recording {event_name(url)} for {seconds:g}s…")
    frames = listen(url, seconds, tries=3)
    if not frames:
        # Same distinction the sweep makes: never having reached the feed is a
        # network result, not a verdict on whether anything is running.
        if not warroom._find_apex_endpoints(url):
            sys.exit(f"Could not reach the feed behind {url} — "
                     f"that is the network, not the event.  Try again.")
        sys.exit(f"Nothing came back from {url} — is the event live?")
    ep = warroom._find_apex_endpoints(url)

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    base = os.path.join(out_dir, f"apex-{event_name(url)}-{stamp}")
    with open(base + ".raw", "w") as f:
        f.write("\n\x00\n".join(frames))
    report = {"url": url, "endpoints": ep, "recorded": stamp, **analyse(frames)}
    with open(base + ".json", "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n  {base}.raw\n  {base}.json\n")
    print(f"  rows parsed      {report['rows_parsed']}")
    print(f"  columns read     {', '.join(report['columns_recognised']) or 'NONE'}")
    print(f"  columns ignored  {', '.join(report['columns_ignored']) or 'none'}")
    print(f"  pit counter      {'yes' if report['has_pit_counter'] else 'NO'}")
    print(f"  driver names     {'yes' if report['has_driver'] else 'no'}")
    print(f"  race clock       {'yes' if report['clock_parsed'] else 'NO'}")
    new_cols = sorted(set(report["columns_ignored"]) - KNOWN_SKIPPED)
    if new_cols:
        print(f"\n  Send both files over — {', '.join(new_cols)} is a column we "
              f"have never seen.")


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
    ap.add_argument("--find", nargs="*", metavar="EVENT",
                    help="probe events for a live session; no names means "
                         + ", ".join(KNOWN_EVENTS))
    ap.add_argument("--anywhere", action="store_true",
                    help="also probe other Apex tracks, to find any live "
                         "session to test the parser against")
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--replay", help="a .raw file recorded earlier")
    ap.add_argument("--speed", type=float, default=20.0, help="replay frames per second")
    args = ap.parse_args()

    if args.find is not None:
        slugs = args.find or KNOWN_EVENTS + (OTHER_TRACKS if args.anywhere else [])
        find(slugs, min(args.seconds, 15.0))
    elif args.replay:
        replay(args.replay, args.speed)
    elif args.url:
        record(args.url, args.seconds, args.out)
    else:
        ap.error("--find to look for a live session, an event URL to record it, "
                 "or --replay a recording")


if __name__ == "__main__":
    main()
