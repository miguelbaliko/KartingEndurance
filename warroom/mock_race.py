#!/usr/bin/env python3
"""A fake 24h race, so the pit crew can rehearse before the real one.

Runs the war room against a simulated Apex feed: thirty teams of differing
pace, a fleet of karts of differing quality, kart swaps through two pit lanes,
and a race clock in the header — everything the real feed sends, at whatever
speed you ask for.

    python3 mock_race.py                 # 60× real time, port 8080
    python3 mock_race.py --speed 200     # a 24h race in about seven minutes
    python3 mock_race.py --lanes 1       # fully automatic counting, no taps

The simulator knows which kart every team is really in.  The war room has to
work that out from the stops you resolve, so ``/mock/truth`` shows both side by
side and scores how many the war room has right — that is the number to watch
while rehearsing.
"""

import argparse
import os
import random
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEAM_NAMES = [
    "JYN-SPORTS 1", "STF BY KARTCUP", "TMSCK", "ESOTEAM", "SCUDERIA VAROISE",
    "GAMIT RACING", "MTF RACING", "DRIVING PASSION", "JYN-SPORT 2", "SYNERGIE 7",
    "JB#17 FOREVER", "APEX VAROISE", "BGB", "MTF RACING 2", "GAMIT RACING SPORT",
    "ITSB1", "JYN-PORC", "SYNERGIE 8", "ITSB2", "JYN-EnPeuPlus",
    "PORTOS", "KART ATTACK", "TEAM DELTA", "ROUGE VIF", "LES BLEUS",
    "NIGHT OWLS", "PALMELA RACING", "ALMADA KART", "SETUBAL SPEED", "APX GP",
]
FIRST = ["Pierre", "Remi", "Fabian", "Sylvain", "Mathieu", "Natale", "Giovanni",
         "Christophe", "Hervé", "Stéphane", "Kantin", "Martin", "Alex", "Kevin",
         "Ana", "Bea", "Caio", "Duarte", "Eva", "Filipe"]
LAST = ["Dujardin", "Loth", "Delage", "Audibert", "Pellegrino", "Spada", "Iovine",
        "Barret", "Schyns", "Planques", "Sanchez", "Raze", "Piercy", "Demartino",
        "Silva", "Costa", "Martins", "Lopes", "Ferreira", "Rocha"]


class Team:
    def __init__(self, rng, number, name, is_pro):
        self.number = str(number)
        self.name = name
        self.is_pro = is_pro
        # Pro teams are quick and consistent; amateurs are neither.
        self.pace = rng.gauss(-0.55, 0.25) if is_pro else rng.gauss(0.95, 0.45)
        self.noise = 0.18 if is_pro else 0.42
        self.drivers = [f"{rng.choice(FIRST)} {rng.choice(LAST)}" for _ in range(4)]
        self.driver = 0
        self.kart = None
        self.laps = 0
        self.pits = 0
        self.best = None
        self.last = None
        self.in_pit_until = 0.0
        self.next_stop_at = 0.0
        self.timer = 0.0

    @property
    def category(self):
        return "PRO" if self.is_pro else "AM"


class MockRace:
    """Ground truth: karts, teams, lanes and the clock."""

    BASE_LAP = 62.5

    def __init__(self, seed=11, n_karts=38, lanes=2, stint_minutes=42):
        self.rng = random.Random(seed)
        self.lanes = lanes
        self.stint_s = stint_minutes * 60
        self.t = 0.0
        self.teams = [
            Team(self.rng, i + 1, name, is_pro=(i % 3 != 2))
            for i, name in enumerate(TEAM_NAMES)
        ]
        # Kart quality is what the war room is trying to discover.
        self.kart_quality = {
            str(k): round(self.rng.gauss(0.0, 0.55), 3)
            for k in range(1, n_karts + 1)
        }
        karts = list(self.kart_quality)
        self.rng.shuffle(karts)
        for team in self.teams:
            team.kart = karts.pop()
            team.next_stop_at = self.rng.uniform(0.55, 1.0) * self.stint_s
        # Whatever is left over waits in the lanes.
        self.pit_order = []
        self.queues = {i + 1: [] for i in range(lanes)}
        for i, kart in enumerate(karts):
            self.queues[i % lanes + 1].append(kart)

    # ── simulation ────────────────────────────────────────────────────────────
    def step(self, dt: float):
        self.t += dt
        for team in self.teams:
            if self.t < team.in_pit_until:
                continue
            team.timer += dt
            while True:
                lap = self.lap_time(team)
                if team.timer < lap:
                    break
                team.timer -= lap
                team.laps += 1
                team.last = lap
                team.best = lap if team.best is None else min(team.best, lap)
                if self.t >= team.next_stop_at:
                    self.pit(team)
                    break

    def lap_time(self, team) -> float:
        # Track rubbers in over the first hours, then cools off at night.
        evolution = -0.8 * min(1.0, self.t / 7200) + 0.5 * max(0.0, (self.t - 36000) / 36000)
        return max(50.0, self.BASE_LAP + evolution + team.pace
                   + self.kart_quality[team.kart]
                   + self.rng.gauss(0, team.noise))

    def pit(self, team):
        """A stop: hand the kart to a lane, take the one at the front."""
        team.pits += 1
        self.pit_order.append(team.number)
        team.driver = (team.driver + 1) % len(team.drivers)
        team.in_pit_until = self.t + self.rng.uniform(150, 200)
        team.next_stop_at = self.t + self.rng.uniform(0.8, 1.05) * self.stint_s
        team.last = self.lap_time(team) + self.rng.uniform(25, 40)   # the in-lap
        lane = self.rng.randint(1, self.lanes)
        queue = self.queues[lane]
        taken = queue.pop(0) if queue else team.kart
        if taken != team.kart:
            queue.append(team.kart)
        team.kart = taken

    # ── the feed ──────────────────────────────────────────────────────────────
    def rows(self) -> list:
        out = []
        for pos, team in enumerate(
                sorted(self.teams, key=lambda x: (-x.laps, x.number)), start=1):
            out.append({
                "pos": str(pos),
                "kart": team.number,              # Apex "no" — the team's number
                "team": team.name,
                "driver": team.drivers[team.driver],
                "category": team.category,
                "last_lap": fmt(team.last),
                "last_lap_s": team.last,
                "best_lap": fmt(team.best),
                "total_laps": str(team.laps),
                "pits": str(team.pits),
                "gap": "",
                "in_pit": self.t < team.in_pit_until,
                "row_cls": "pit" if self.t < team.in_pit_until else "",
            })
        return out

    def header(self, total_s: float) -> str:
        return f"{hms(self.t)} / {hms(total_s)}"

    def truth(self) -> dict:
        return {t.number: t.kart for t in self.teams}


def fmt(secs):
    if not secs:
        return "-"
    m, s = divmod(secs, 60)
    return f"{int(m)}:{s:06.3f}"


def hms(secs):
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--speed", type=float, default=60.0, help="times real time")
    ap.add_argument("--hours", type=float, default=24.0, help="race length")
    ap.add_argument("--lanes", type=int, default=2)
    ap.add_argument("--karts", type=int, default=38)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--seed-karts", action="store_true",
                    help="tell the war room the starting allocation, as you would "
                         "type it in before the start")
    args = ap.parse_args()

    os.environ.setdefault("WARROOM_DB", os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "mock.db"))

    import app
    race = MockRace(seed=args.seed, n_karts=args.karts, lanes=args.lanes)
    total_s = args.hours * 3600

    app.CFG["team_name"] = "APX GP"
    app.CFG["karts"]["lanes"] = args.lanes
    app.CFG["race"]["duration_minutes"] = int(args.hours * 60)
    app.POOL.configure(app.CFG["karts"])
    app.POOL.reset()

    if args.seed_karts:
        for team in race.teams:
            app.POOL.set_kart(team.number, team.name, team.kart)
        for lane, queue in race.queues.items():
            for kart in queue:
                app.POOL.lane_add(lane, kart)

    @app.app.get("/mock/truth")
    def mock_truth():
        real = race.truth()
        guess = app.POOL.kart_of()
        rows = [{"team_no": t.number, "team": t.name, "real": real[t.number],
                 "war_room": guess.get(t.number, ""),
                 "ok": guess.get(t.number, "") == real[t.number]}
                for t in race.teams]
        right = sum(1 for r in rows if r["ok"])
        return app.jsonify({
            "counted_right": right, "of": len(rows),
            "kart_quality": race.kart_quality,
            "race_time": hms(race.t), "teams": rows,
        })

    def drive():
        tick = 0.5
        while race.t < total_s:
            race.step(tick * args.speed)
            app._process_meta({"dyn1": race.header(total_s),
                               "title1": f"{args.hours:g}h mock · Palmela (mock)"})
            app._process_rows(race.rows())
            app.broadcast()
            time.sleep(tick)

    threading.Thread(target=drive, daemon=True).start()
    print(f"\n  MOCK RACE  ->  http://localhost:{args.port}"
          f"   (pit phone: /pit, truth: /mock/truth)")
    print(f"  {args.speed:g}× speed · {args.lanes} lane(s) · {args.karts} karts\n")
    app.app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
