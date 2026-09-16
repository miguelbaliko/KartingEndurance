#!/usr/bin/env python3
"""The whole thing, against a simulated race whose truth we know.

A mock race hands out karts through its own pit lanes.  The war room sees only
what Apex would send — positions, laps, pit counts, lap times — plus the lane
each stop went to, which is the one thing a human tells it.  If the design
works, the war room's book matches the simulator's kart for kart at the flag,
and its kart ratings line up with the qualities the simulator invented.

This is slower than the unit tests (it drives several hours of racing) but it
is the test that would have caught every bug that mattered.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kartpool import KartPool
from mock_race import MockRace


def spearman(pairs) -> float:
    """Rank correlation, without pulling in scipy for one number."""
    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        for rank, i in enumerate(order):
            out[i] = float(rank)
        return out

    xs, ys = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    n = len(pairs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs) ** 0.5
    vy = sum((y - my) ** 2 for y in ys) ** 0.5
    return cov / (vx * vy) if vx and vy else 0.0


class Rehearsal:
    """Runs a mock race against a real KartPool, with an operator in the loop."""

    def __init__(self, db, hours=6.0, dt=15.0, lanes=2, seed=11,
                 operator="perfect"):
        self.race = MockRace(seed=seed, lanes=lanes)
        self.pool = KartPool(db, {"lanes": lanes})
        self.hours, self.dt, self.operator = hours, dt, operator
        self.mistakes = 0
        self.answers = 0
        for team in self.race.teams:
            self.pool.set_kart(team.number, team.name, team.kart)
        for lane, queue in self.race.queues.items():
            for kart in queue:
                self.pool.lane_add(lane, kart)

    def run(self):
        steps = int(self.hours * 3600 / self.dt)
        for _ in range(steps):
            self.race.step(self.dt)
            self.pool.observe(self.race.rows(), my_team="TPC")
            self.answer()
        return self

    def answer(self):
        """The person on the pit wall, tapping lanes — MockRace plays them.

        The simulator owns this because answering needs the one thing only it
        knows: which lane each kart really went to, and in what order.
        """
        fumble = 3 - self.mistakes if self.operator == "sloppy" else 0
        tapped, fumbled = self.race.answer(self.pool, fumble=fumble)
        self.answers += tapped + fumbled
        self.mistakes += fumbled

    def correct(self) -> int:
        book = self.pool.kart_of()
        return sum(1 for t in self.race.teams if book.get(t.number) == t.kart)

    def fleet(self) -> list:
        # Ratings are rate-limited in the live app; a test wants them now.
        self.pool.ratings(force=True)
        return self.pool.snapshot()["fleet"]


class TestRehearsal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.reh = Rehearsal(os.path.join(cls._tmp.name, "a.db")).run()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_every_kart_is_counted_correctly(self):
        self.assertEqual(self.reh.correct(), len(self.reh.race.teams))

    def test_the_race_actually_happened(self):
        self.assertGreater(self.reh.pool.snapshot()["stops_seen"], 100)
        self.assertGreater(min(t.laps for t in self.reh.race.teams), 150)

    def test_no_kart_is_in_two_places(self):
        pool = self.reh.pool.snapshot()
        held = list(self.reh.pool.kart_of().values())
        queued = [k["num"] for lane in pool["lanes"] for k in lane["karts"]]
        self.assertEqual(len(held + queued), len(set(held + queued)))

    def test_kart_ratings_follow_the_real_kart_quality(self):
        truth = self.reh.race.kart_quality
        rated = [(truth[c["num"]], c["delta"])
                 for c in self.reh.fleet()
                 if c["delta"] is not None and c["num"] in truth]
        self.assertGreater(len(rated), 25, "most of the fleet should be rated")
        self.assertGreater(spearman(rated), 0.75)

    def test_the_best_karts_are_actually_the_best_karts(self):
        truth = self.reh.race.kart_quality
        fleet = [c for c in self.reh.fleet() if c["delta"] is not None]
        picked = {c["num"] for c in fleet[:6]}
        really = set(sorted(truth, key=truth.get)[:12])
        self.assertGreaterEqual(len(picked & really), 4,
                                "at least four of our top six are truly top twelve")

    def test_driver_pace_does_not_leak_into_the_kart_score(self):
        """A pro team's kart must not be flattered by the pro team."""
        truth = self.reh.race.kart_quality
        fleet = {c["num"]: c for c in self.reh.fleet()}
        book = self.reh.pool.kart_of()
        pro_karts = [book[t.number] for t in self.reh.race.teams
                     if t.is_pro and book.get(t.number) in fleet]
        rated = [fleet[k]["delta"] for k in pro_karts if fleet[k]["delta"] is not None]
        if len(rated) < 5:
            self.skipTest("not enough rated karts under pro teams")
        overall = [c["delta"] for c in fleet.values() if c["delta"] is not None]
        self.assertAlmostEqual(sum(rated) / len(rated),
                               sum(overall) / len(overall), delta=0.35)


class TestOperatorMistakes(unittest.TestCase):
    def test_a_wrong_lane_is_recoverable_and_does_not_spread(self):
        """A mis-tap costs those karts, not the whole book.

        Each one costs about two and a half teams: whoever got the wrong kart,
        whoever should have had it, and usually one more downstream.  Measured
        at 7 to 9 of thirty across five seeds, so the ceiling here is a third
        of the book — the point being that two thirds still come out right,
        not that any particular number does.
        """
        with tempfile.TemporaryDirectory() as tmp:
            reh = Rehearsal(os.path.join(tmp, "b.db"), hours=3.0,
                            operator="sloppy").run()
            self.assertEqual(reh.mistakes, 3)
            self.assertGreaterEqual(reh.correct(), len(reh.race.teams) - 10)

    def test_tapping_out_of_order_is_what_costs_you(self):
        """Answering in the feed's order instead of the real one loses karts.

        Two teams taking from the same lane within a minute arrive as two
        questions, and the feed's order is not necessarily theirs.  Answered
        the wrong way round they are handed each other's kart, and every kart
        behind them in that lane shifts by one.  That is the whole reason the
        crew answers oldest-stop-first, so it is worth pinning: without the
        sort this run scores 20 of 30, and at coarser steps it reaches zero.
        """
        with tempfile.TemporaryDirectory() as tmp:
            reh = Rehearsal(os.path.join(tmp, "d.db"), hours=3.0)
            race = reh.race

            def in_feed_order(pool, fumble=0):
                by_team = {t.number: t for t in race.teams}
                ready = []
                for q in pool.pending():
                    team = by_team.get(str(q["team_no"]))
                    if team is None or not team.taps:
                        continue
                    ready.append((team.taps.popleft(), q))
                for (_seq, lane, kart), q in ready:      # no sort: as listed
                    if q["asks"] == "kart":
                        pool.resolve(q["id"], kart_out=kart)
                    else:
                        pool.resolve(q["id"], lane=lane)
                return len(ready), 0

            race.answer = in_feed_order
            reh.run()
            self.assertLess(reh.correct(), len(race.teams) - 5,
                            "if order did not matter the sort would be dead code")


class TestSingleLane(unittest.TestCase):
    """One lane removes the lane question, and leaves only the order question.

    While a single kart is in the pit lane there is nothing to ask and the
    count is fully automatic.  When two are in there together, which of them
    takes the kart at the front is something only the person standing there can
    see, so the war room asks instead of guessing.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.reh = Rehearsal(os.path.join(self._tmp.name, "c.db"),
                             hours=3.0, lanes=1).run()

    def test_it_counts_all_but_the_odd_transposition(self):
        """One shared lane leaves an irreducible risk of swapping two karts.

        When two teams are in the pit lane together, whoever reaches the front
        kart first decides who gets it.  We ask whenever we can see the
        overlap, but a stop that looked unambiguous when we resolved it can
        still turn out to have been simultaneous.  Over a hundred-odd stops
        that costs about one kart; with two lanes it costs none.
        """
        teams = len(self.reh.race.teams)
        self.assertGreaterEqual(self.reh.correct(), teams - 1)

    def test_a_stop_on_its_own_needs_no_input(self):
        pool, race = self.reh.pool, self.reh.race
        pool._in_box_since.clear()
        team = race.teams[0]
        before = pool.kart_of()[team.number]
        pool.manual_stop(team.number, team.name)
        self.assertEqual(pool.pending(), [], "nothing to ask about")
        self.assertNotEqual(pool.kart_of()[team.number], before,
                            "the kart at the front of the lane was handed out")

    def test_what_it_asks_for_is_the_order_not_the_lane(self):
        race, pool = MockRace(seed=3, lanes=1), self.reh.pool
        stop = pool.manual_stop("1", "ALPHA")
        pool._in_box_since["2"] = __import__("time").time()
        pool.manual_stop("2", "BRAVO")
        asks = {s["team_no"]: s["asks"] for s in pool.pending()}
        self.assertEqual(asks.get("2"), "order")


if __name__ == "__main__":
    unittest.main(verbosity=2)
