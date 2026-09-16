#!/usr/bin/env python3
"""Kart-counting tests.

These lean on the two things that make or break the feature in the pit lane:
a stop must be detected exactly once, and a lane must hand out the kart that
has been waiting longest.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kartpool
from kartpool import KartPool
import rating


def row(team_no, team, pits=0, laps=1, lap_s=63.0, driver="", in_pit=False):
    return {"kart": str(team_no), "team": team, "pits": str(pits),
            "total_laps": str(laps), "last_lap_s": lap_s, "driver": driver,
            "in_pit": in_pit}


class PoolCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "t.db")
        self.pool = self.make()

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, **cfg):
        return KartPool(self.db, cfg)

    def seed(self, pairs):
        for team_no, team, kart in pairs:
            self.pool.set_kart(team_no, team, kart)


class TestAssignment(PoolCase):
    def test_set_and_read_back(self):
        self.seed([("1", "ALPHA", "10"), ("2", "BRAVO", "11")])
        self.assertEqual(self.pool.kart_of(), {"1": "10", "2": "11"})

    def test_reassignment_closes_the_previous_holding(self):
        self.seed([("1", "ALPHA", "10"), ("1", "ALPHA", "12")])
        self.assertEqual(self.pool.kart_of(), {"1": "12"})


class TestStopDetection(PoolCase):
    def test_first_snapshot_seeds_without_inventing_stops(self):
        self.pool.observe([row("1", "ALPHA", pits=9)])
        self.assertEqual(self.pool.pending(), [])

    def test_pit_counter_tick_opens_one_stop(self):
        self.pool.observe([row("1", "ALPHA", pits=0)])
        self.pool.observe([row("1", "ALPHA", pits=1, laps=2)])
        self.assertEqual(len(self.pool.pending()), 1)

    def test_repeated_snapshot_does_not_duplicate(self):
        self.pool.observe([row("1", "ALPHA", pits=0)])
        for _ in range(5):
            self.pool.observe([row("1", "ALPHA", pits=1, laps=2)])
        self.assertEqual(len(self.pool.pending()), 1)

    def test_lap_spike_catches_a_stop_the_pit_column_missed(self):
        pool = self.make(detect_by_pit_column=False)
        for lap in range(1, 8):
            pool.observe([row("1", "ALPHA", laps=lap, lap_s=63.0)])
        self.assertEqual(pool.pending(), [])
        pool.observe([row("1", "ALPHA", laps=8, lap_s=63.0 + 45)])
        self.assertEqual(len(pool.pending()), 1)

    def test_traffic_is_not_a_stop(self):
        pool = self.make(detect_by_pit_column=False)
        for lap in range(1, 8):
            pool.observe([row("1", "ALPHA", laps=lap, lap_s=63.0)])
        pool.observe([row("1", "ALPHA", laps=8, lap_s=63.0 + 6)])
        self.assertEqual(pool.pending(), [])


class TestLaneFlow(PoolCase):
    def setUp(self):
        super().setUp()
        self.seed([("1", "ALPHA", "10")])
        for kart in ("21", "22", "23"):
            self.pool.lane_add(1, kart)

    def _stop(self, team_no="1", team="ALPHA"):
        return self.pool.manual_stop(team_no, team)

    def test_lane_hands_out_the_kart_that_waited_longest(self):
        self.pool.resolve(self._stop(), lane=1)
        self.assertEqual(self.pool.kart_of()["1"], "21")

    def test_returned_kart_goes_to_the_back_of_the_queue(self):
        self.pool.resolve(self._stop(), lane=1)
        queue = self.pool.snapshot()["lanes"][0]["karts"]
        self.assertEqual([k["num"] for k in queue], ["22", "23", "10"])

    def test_a_team_never_gets_its_own_kart_straight_back(self):
        self.pool.lane_remove(1, "22")
        self.pool.lane_remove(1, "23")
        self.pool.resolve(self._stop(), lane=1)      # lane holds only 21
        self.assertEqual(self.pool.kart_of()["1"], "21")
        self.pool.resolve(self._stop(), lane=1)      # lane now holds only 10
        self.assertEqual(self.pool.kart_of()["1"], "10")

    def test_empty_lane_asks_for_the_kart_number(self):
        for kart in ("21", "22", "23"):
            self.pool.lane_remove(1, kart)
        stop = self._stop()
        self.pool.resolve(stop, lane=1)
        pending = self.pool.pending()
        self.assertEqual(pending[0]["state"], kartpool.AWAIT_KART)
        self.pool.resolve(stop, kart_out="44")
        self.assertEqual(self.pool.kart_of()["1"], "44")
        self.assertEqual(self.pool.pending(), [])

    def test_driver_change_only_keeps_the_kart(self):
        self.pool.resolve(self._stop(), no_change=True)
        self.assertEqual(self.pool.kart_of()["1"], "10")
        self.assertEqual(self.pool.pending(), [])

    def test_single_lane_needs_no_input_at_all(self):
        self.db = os.path.join(self._tmp.name, "one-lane.db")
        pool = self.make(lanes=1)
        pool.set_kart("1", "ALPHA", "10")
        pool.lane_add(1, "21")
        pool.observe([row("1", "ALPHA", pits=0)])
        pool.observe([row("1", "ALPHA", pits=1, laps=2)])
        self.assertEqual(pool.pending(), [])
        self.assertEqual(pool.kart_of()["1"], "21")

    def test_karts_stay_in_one_lane_only(self):
        self.pool.lane_add(2, "21")
        lanes = {l["lane"]: [k["num"] for k in l["karts"]]
                 for l in self.pool.snapshot()["lanes"]}
        self.assertEqual(lanes[1], ["22", "23"])
        self.assertEqual(lanes[2], ["21"])

    def test_undo_puts_a_mis_tapped_lane_back(self):
        before = self.pool.snapshot()["lanes"][0]["karts"]
        self.pool.resolve(self._stop(), lane=1)
        self.assertTrue(self.pool.undo())
        after = self.pool.snapshot()["lanes"][0]["karts"]
        self.assertEqual([k["num"] for k in before], [k["num"] for k in after])


class TestPendingStopsAgeOut(PoolCase):
    """One unresolved stop must not disable counting for the rest of the race.

    At a single lane the pool counts on its own, but only while nobody else is
    in the box — order decides who takes the front kart.  An unresolved stop
    used to count as "in the box" forever, so the first overlap switched
    automatic counting off permanently, for every team.
    """

    def setUp(self):
        super().setUp()
        self._real_time = kartpool.time.time
        self.addCleanup(setattr, kartpool.time, "time", self._real_time)
        self.now = [1_000_000.0]
        kartpool.time.time = lambda: self.now[0]

        self.db = os.path.join(self._tmp.name, "age.db")
        self.pool = self.make(lanes=1)
        for no, team, kart in (("1", "ALPHA", "10"), ("2", "BRAVO", "11")):
            self.pool.set_kart(no, team, kart)
        for k in ("21", "22", "23"):
            self.pool.lane_add(1, k)

    def stop(self, no, team, pits, laps, others=()):
        """One stop for a team, with anyone else the feed shows in the box."""
        base = [row(no, team, pits=pits - 1, laps=laps - 1)]
        self.pool.observe(base + list(others))
        self.pool.observe([row(no, team, pits=pits, laps=laps)] + list(others))

    def test_a_stop_while_someone_is_in_the_box_waits_to_be_told(self):
        # BRAVO is standing in the box, so who reached the front kart first is
        # not something the feed can answer.
        self.stop("1", "ALPHA", 1, 2, others=[row("2", "BRAVO", in_pit=True)])
        self.assertEqual([p["team_no"] for p in self.pool.pending()], ["1"])

    def test_an_old_unanswered_stop_stops_blocking(self):
        self.stop("1", "ALPHA", 1, 2, others=[row("2", "BRAVO", in_pit=True)])
        self.assertEqual(len(self.pool.pending()), 1)

        # An hour on, nobody has answered it. ALPHA stops again, alone this
        # time, and that stop must count itself rather than inherit the block.
        self.now[0] += 3600
        self.stop("1", "ALPHA", 2, 9)
        self.assertEqual(len(self.pool.pending()), 1,
                         "the later stop should have counted itself")

    def test_a_stop_moments_old_still_blocks(self):
        # The window is what keeps the fix honest: while someone really could
        # still be in the box, the question has to be asked.
        self.stop("1", "ALPHA", 1, 2, others=[row("2", "BRAVO", in_pit=True)])
        self.now[0] += 30
        self.stop("2", "BRAVO", 1, 2)
        self.assertEqual(len(self.pool.pending()), 2)

    def laps_recorded(self):
        with self.pool._con() as con:
            return con.execute("SELECT COUNT(*) FROM kart_lap").fetchone()[0]

    def test_ageing_never_credits_laps_to_a_kart_it_cannot_name(self):
        # Ageing relaxes "who is in the box", never "which kart is this".  A
        # team whose stop is unanswered is in an unknown kart, so its laps must
        # rate nothing — crediting them would poison the kart ratings, which is
        # the whole reason for tracking karts at all.
        self.stop("1", "ALPHA", 1, 2, others=[row("2", "BRAVO", in_pit=True)])
        self.assertIn("1", self.pool._pending_teams)
        before = self.laps_recorded()

        self.now[0] += 3600                # long past the blocking window
        for lap in range(3, 8):
            self.pool.observe([row("1", "ALPHA", pits=1, laps=lap)])
        self.assertIn("1", self.pool._pending_teams, "still unknown, just not blocking")
        self.assertEqual(self.laps_recorded(), before,
                         "laps were credited to a kart the pool cannot name")


class TestRatingNeverTakesTheScreenDown(PoolCase):
    def test_a_rater_that_trips_keeps_the_pool_usable(self):
        import rating
        real, calls = rating.rate, []

        def boom(*a, **k):
            calls.append(1)
            raise TypeError("unsupported operand type(s) for //: 'str' and 'float'")
        rating.rate = boom
        self.addCleanup(setattr, rating, "rate", real)

        self.pool._rating_dirty = True
        snap = self.pool.snapshot()          # must not raise
        self.assertTrue(calls, "the rater was not even reached")
        self.assertIn("lanes", snap)
        self.assertIn("fleet", snap)


class TestRetiringAKart(PoolCase):
    """Karts break. One that is stored must never be handed to anybody."""

    def test_a_retired_kart_leaves_the_lanes(self):
        self.pool.retire_kart("22", "engine")
        queues = [[k["num"] for k in l["karts"]]
                  for l in self.pool.snapshot()["lanes"]]
        self.assertNotIn("22", [n for q in queues for n in q])

    def test_it_is_marked_with_its_reason(self):
        self.pool.retire_kart("22", "engine")
        card = next(c for c in self.pool.snapshot()["fleet"] if c["num"] == "22")
        self.assertTrue(card["retired"])
        self.assertEqual(card["retired_reason"], "engine")

    def test_it_cannot_be_queued_again_by_mistake(self):
        self.pool.retire_kart("22", "chassis")
        self.pool.lane_add(1, "22")
        queues = [[k["num"] for k in l["karts"]]
                  for l in self.pool.snapshot()["lanes"]]
        self.assertNotIn("22", [n for q in queues for n in q])

    def test_its_laps_are_kept(self):
        # They are still evidence about every other kart; deleting them would
        # quietly move the rest of the fleet's scores.
        self.pool.observe([row("1", "ALPHA", laps=1)])
        self.pool.observe([row("1", "ALPHA", laps=2)])
        with self.pool._con() as con:
            before = con.execute("SELECT COUNT(*) FROM kart_lap").fetchone()[0]
        self.pool.retire_kart("10", "engine")
        with self.pool._con() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM kart_lap")
                             .fetchone()[0], before)

    def test_it_can_come_back(self):
        # Coming back does not put it anywhere — it left its lane when it broke
        # and somebody has to say where it is now.
        self.pool.retire_kart("22", "tyre")
        self.pool.unretire_kart("22")
        self.assertEqual(self.pool.snapshot()["retired"], [])
        self.pool.lane_add(1, "22")
        queues = [[k["num"] for k in l["karts"]]
                  for l in self.pool.snapshot()["lanes"]]
        self.assertIn("22", [n for q in queues for n in q])

    def test_a_retired_kart_stays_visible_so_it_can_be_brought_back(self):
        self.pool.retire_kart("22", "tyre")
        nums = [c["num"] for c in self.pool.snapshot()["fleet"]]
        self.assertIn("22", nums)

    def test_retiring_nothing_is_not_a_crash(self):
        self.pool.retire_kart("", "")
        self.assertEqual(self.pool.snapshot()["retired"], [])


class TestWipingClearsWhatIsOnScreen(PoolCase):
    def test_a_wipe_drops_the_scores_immediately(self):
        """The rating cache serves for a few seconds, so a wipe used to leave
        the old grades showing — long enough to make someone wipe twice."""
        self.pool.observe([row("1", "ALPHA", laps=1)])
        for lap in range(2, 14):
            self.pool.observe([row("1", "ALPHA", laps=lap)])
        self.pool.ratings()                      # warm the cache
        self.pool.reset()
        self.assertEqual(self.pool.ratings()["karts"], {})
        self.assertEqual(self.pool.snapshot()["fleet"], [])


class TestFullPitCycle(PoolCase):
    """Three teams, two lanes, karts round-tripping the way they do in a race."""

    def test_every_kart_is_accounted_for(self):
        teams = [("1", "ALPHA"), ("2", "BRAVO"), ("3", "CHARLIE")]
        self.seed([(no, name, k) for (no, name), k in zip(teams, ("10", "11", "12"))])
        for kart in ("20", "21"):
            self.pool.lane_add(1, kart)
        for kart in ("30", "31"):
            self.pool.lane_add(2, kart)

        pits = {no: 0 for no, _ in teams}
        self.pool.observe([row(no, name, pits=0) for no, name in teams])

        for stop_n in range(6):
            no, name = teams[stop_n % 3]
            pits[no] += 1
            self.pool.observe([row(n, t, pits=pits[n], laps=stop_n + 2)
                               for n, t in teams])
            pending = self.pool.pending()
            self.assertEqual(len(pending), 1, f"stop {stop_n}")
            self.pool.resolve(pending[0]["id"], lane=1 + stop_n % 2)

        held = set(self.pool.kart_of().values())
        queued = {k["num"] for lane in self.pool.snapshot()["lanes"]
                  for k in lane["karts"]}
        self.assertEqual(len(held), 3)
        self.assertEqual(held | queued,
                         {"10", "11", "12", "20", "21", "30", "31"})
        self.assertFalse(held & queued, "a kart cannot be out and queued at once")


class TestLapAttribution(PoolCase):
    def _laps(self):
        with self.pool._con() as con:
            return [(r["kart"], r["pilot"], r["lap_s"]) for r in
                    con.execute("SELECT kart,pilot,lap_s FROM kart_lap ORDER BY id")]

    def test_laps_land_on_the_kart_the_team_is_holding(self):
        self.seed([("1", "ALPHA", "10")])
        self.pool.observe([row("1", "ALPHA", laps=1, lap_s=63.0)])
        self.pool.observe([row("1", "ALPHA", laps=2, lap_s=63.2)])
        self.assertEqual([k for k, _, _ in self._laps()], ["10"])

    def test_a_repeated_snapshot_is_not_a_second_lap(self):
        self.seed([("1", "ALPHA", "10")])
        for _ in range(4):
            self.pool.observe([row("1", "ALPHA", laps=2, lap_s=63.0)])
        self.assertLessEqual(len(self._laps()), 1)

    def test_driver_name_sharpens_the_pilot_key(self):
        self.seed([("1", "ALPHA", "10")])
        self.pool.observe([row("1", "ALPHA", laps=1, driver="Ana")])
        self.pool.observe([row("1", "ALPHA", laps=2, lap_s=63.1, driver="Ana")])
        self.assertEqual(self._laps()[0][1], "ALPHA|Ana")

    def test_out_lap_is_not_blamed_on_the_new_kart(self):
        self.seed([("1", "ALPHA", "10")])
        self.pool.lane_add(1, "21")
        self.pool.observe([row("1", "ALPHA", pits=0, laps=1)])
        self.pool.observe([row("1", "ALPHA", pits=1, laps=2)])
        self.pool.resolve(self.pool.pending()[0]["id"], lane=1)
        self.pool.observe([row("1", "ALPHA", pits=1, laps=3, lap_s=70.0)])
        self.pool.observe([row("1", "ALPHA", pits=1, laps=4, lap_s=63.4)])
        on_new = [l for l in self._laps() if l[0] == "21"]
        self.assertEqual([round(l[2], 1) for l in on_new], [63.4])

    def test_unassigned_team_banks_no_laps(self):
        self.pool.observe([row("9", "GHOST", laps=1, lap_s=63.0)])
        self.pool.observe([row("9", "GHOST", laps=2, lap_s=63.0)])
        self.assertEqual(self._laps(), [])


class TestMyTeamHooks(PoolCase):
    def test_feed_drives_my_box_clock(self):
        fired = []
        pool = KartPool(self.db, {},
                        on_my_stop=lambda t: fired.append(("box", t)),
                        on_my_release=lambda t: fired.append(("out", t)))
        pool.observe([row("1", "ALPHA", pits=0, laps=1)], my_team="ALPHA")
        pool.observe([row("1", "ALPHA", pits=1, laps=2)], my_team="ALPHA")
        pool.observe([row("1", "ALPHA", pits=1, laps=3)], my_team="ALPHA")
        self.assertEqual(fired, [("box", "1"), ("out", "1")])

    def test_entering_the_lane_boxes_us_before_the_counter_ticks(self):
        """The box clock must start when the kart arrives, not a lap later."""
        fired = []
        pool = KartPool(self.db, {}, on_my_stop=lambda t: fired.append(t))
        pool.observe([row("1", "ALPHA", pits=0, laps=1)], my_team="ALPHA")
        pool.observe([row("1", "ALPHA", pits=0, laps=1, in_pit=True)],
                     my_team="ALPHA")
        self.assertEqual(fired, ["1"], "boxed on the in-pit flag alone")
        # The counter catching up must not box us a second time.
        pool.observe([row("1", "ALPHA", pits=1, laps=2, in_pit=True)],
                     my_team="ALPHA")
        self.assertEqual(fired, ["1", "1"], "one call per snapshot, not per signal")

    def test_leaving_the_lane_resumes_us(self):
        out = []
        pool = KartPool(self.db, {}, on_my_release=lambda t: out.append(t))
        pool.observe([row("1", "ALPHA", in_pit=True)], my_team="ALPHA")
        pool.observe([row("1", "ALPHA", in_pit=False)], my_team="ALPHA")
        self.assertEqual(out, ["1"])

    def test_other_teams_do_not_touch_my_clock(self):
        fired = []
        pool = KartPool(self.db, {}, on_my_stop=lambda t: fired.append(t))
        pool.observe([row("2", "BRAVO", pits=0)], my_team="ALPHA")
        pool.observe([row("2", "BRAVO", pits=1, laps=2)], my_team="ALPHA")
        self.assertEqual(fired, [])

    def test_auto_pit_off_leaves_the_buttons_in_charge(self):
        fired = []
        pool = KartPool(self.db, {"auto_pit": False},
                        on_my_stop=lambda t: fired.append(t))
        pool.observe([row("1", "ALPHA", pits=0)], my_team="ALPHA")
        pool.observe([row("1", "ALPHA", pits=1, laps=2)], my_team="ALPHA")
        self.assertEqual(fired, [])
        self.assertEqual(len(pool.pending()), 1, "the stop is still booked")


class TestGapToTheKartAhead(PoolCase):
    """Apex sends the gap to the leader; the tow is about the kart in front."""

    @staticmethod
    def grid(*pairs):
        return [{"pos": str(p), "kart": k, "gap": g} for p, k, g in pairs]

    def test_the_interval_is_the_difference_between_neighbours(self):
        got = KartPool.gaps_ahead(self.grid((1, "10", "0.0"),
                                            (2, "11", "0.4"),
                                            (3, "12", "6.9")))
        self.assertIsNone(got["10"], "the leader is behind nobody")
        self.assertAlmostEqual(got["11"], 0.4)
        self.assertAlmostEqual(got["12"], 6.5)

    def test_a_gap_in_minutes_is_read_as_seconds(self):
        got = KartPool.gaps_ahead(self.grid((1, "10", "0.0"), (2, "11", "1:05.5")))
        self.assertAlmostEqual(got["11"], 65.5)

    def test_lapped_karts_are_skipped_not_guessed(self):
        """"2 laps" is not a number of seconds, so that kart gets no reading."""
        got = KartPool.gaps_ahead(self.grid((1, "10", "0.0"),
                                            (2, "11", "2 laps"),
                                            (3, "12", "8.0")))
        self.assertNotIn("11", got)
        # 12 is measured against the last kart we could actually place, which
        # overstates its gap — erring towards calling a lap clean.
        self.assertAlmostEqual(got["12"], 8.0)

    def test_an_unordered_or_empty_grid_is_not_a_crash(self):
        self.assertEqual(KartPool.gaps_ahead([]), {})
        self.assertEqual(KartPool.gaps_ahead([{"kart": "10", "gap": "1.0"}]), {})

    def test_a_close_lap_is_stored_against_the_kart(self):
        """The gap has to survive the round trip, or the rating never sees it."""
        import sqlite3
        self.seed([("1", "ALPHA", "10"), ("2", "BRAVO", "11")])
        grid = [dict(row("1", "ALPHA", laps=n), pos="1", gap="0.0")
                for n in (1,)] + [dict(row("2", "BRAVO", laps=1), pos="2", gap="0.3")]
        self.pool.observe(grid)
        grid = [dict(row("1", "ALPHA", laps=2), pos="1", gap="0.0"),
                dict(row("2", "BRAVO", laps=2), pos="2", gap="0.3")]
        self.pool.observe(grid)
        con = sqlite3.connect(self.db)
        got = dict(con.execute("SELECT kart, ahead_s FROM kart_lap"))
        self.assertIsNone(got.get("10"), "the leader was in clear air")
        self.assertAlmostEqual(got["11"], 0.3)


class TestAnUnansweredStopIsNotFree(PoolCase):
    """A stop nobody answers costs a lap a minute, silently, for 25 hours.

    While the kart a team came out in is unknown, their laps cannot be filed
    against any kart — filing them against the one they walked out of would be
    a guess.  Dropping them is right; dropping them without saying so is not.
    """

    def stop_and_run(self, laps=6):
        self.seed([("1", "ALPHA", "10")])
        self.pool.observe([row("1", "ALPHA", pits=0)])
        self.pool.observe([row("1", "ALPHA", pits=1, laps=2)])
        for n in range(3, 3 + laps):
            self.pool.observe([row("1", "ALPHA", pits=1, laps=n)])

    def test_the_laps_it_costs_are_counted(self):
        self.stop_and_run(laps=6)
        self.assertEqual(self.pool.pending()[0]["laps_lost"], 6)

    def test_a_stop_answered_straight_away_costs_nothing(self):
        self.seed([("1", "ALPHA", "10")])
        self.pool.observe([row("1", "ALPHA", pits=0)])
        self.pool.observe([row("1", "ALPHA", pits=1, laps=2)])
        self.pool.resolve(self.pool.pending()[0]["id"], kart_out="21")
        self.assertEqual(self.pool.pending(), [])
        self.assertEqual(self.pool._dropped.get("1", 0), 0)

    def test_answering_it_late_stops_the_bleeding(self):
        self.stop_and_run(laps=6)
        self.pool.resolve(self.pool.pending()[0]["id"], kart_out="21")
        self.assertEqual(self.pool._dropped.get("1", 0), 0)

    def test_it_says_so_rather_than_only_counting(self):
        self.stop_and_run(laps=6)
        notes = " ".join(n["text"] for n in self.pool.snapshot()["log"])
        self.assertIn("not counted", notes)


class TestKnowingWhereWeStandBeforeTheLightsGoOut(PoolCase):
    """A kart cannot be told apart from its driver until a second driver has it.

    Measured on a thirty-team race with forty-lap stints, the first kart is not
    gradeable until fifty minutes in and the fleet not until an hour and forty
    — so a race started cold is blind through the opening stint, and the wall
    has to say so rather than let the pit crew find out at the first stop.
    """

    def running(self, teams=3, laps=6, start=1000.0):
        for i in range(teams):
            self.pool.set_kart(str(i + 1), f"T{i+1}", str(10 + i))
        ts = start
        for n in range(2, 2 + laps):
            self.pool.observe(
                [dict(row(str(i + 1), f"T{i+1}", laps=n, lap_s=63.0 + i * 0.4),
                      pos=str(i + 1), gap=f"{i * 4.0}")
                 for i in range(teams)], now=ts)
            ts += 65
        return ts

    def test_a_cold_fleet_says_it_is_cold(self):
        r = self.pool.snapshot()["ready"]
        self.assertEqual((r["graded"], r["solid"]), (0, 0))

    def test_laps_without_a_second_driver_do_not_grade_anything(self):
        self.running(laps=20)
        r = self.pool.snapshot()["ready"]
        self.assertEqual(r["graded"], 0, "one driver per kart is not evidence")
        self.assertEqual(r["total"], 3)

    def test_the_best_lap_hint_is_there_while_the_grade_is_not(self):
        """The one real signal in the blind hour, and it is not a grade."""
        self.running(laps=20)
        fleet = {c["num"]: c for c in self.pool.snapshot()["fleet"]}
        self.assertIsNone(fleet["10"]["delta"], "no grade yet")
        # 10 is the quick kart here, so it sets the fleet best and reads zero.
        self.assertEqual(fleet["10"]["best_delta_s"], 0.0)
        self.assertGreater(fleet["12"]["best_delta_s"], 0.5)

    def test_a_kart_nobody_has_driven_has_no_hint_either(self):
        self.running(laps=6)
        self.pool.lane_add(1, "30")
        fleet = {c["num"]: c for c in self.pool.snapshot()["fleet"]}
        self.assertIsNone(fleet["30"]["best_delta_s"])


class TestTheGapBehind(PoolCase):
    """The same interval read the other way: who is close enough to shove."""

    @staticmethod
    def grid(*pairs):
        return [{"pos": str(p), "kart": k, "gap": g} for p, k, g in pairs]

    def test_it_is_the_interval_to_the_kart_chasing(self):
        got = KartPool.gaps_behind(self.grid((1, "10", "0.0"),
                                             (2, "11", "0.4"),
                                             (3, "12", "6.9")))
        self.assertAlmostEqual(got["10"], 0.4)
        self.assertAlmostEqual(got["11"], 6.5)
        self.assertIsNone(got["12"], "nobody is chasing the last kart")

    def test_ahead_and_behind_are_the_same_interval(self):
        grid = self.grid((1, "10", "0.0"), (2, "11", "0.4"), (3, "12", "6.9"))
        ahead, behind = KartPool.gaps_ahead(grid), KartPool.gaps_behind(grid)
        self.assertAlmostEqual(behind["10"], ahead["11"])
        self.assertAlmostEqual(behind["11"], ahead["12"])

    def test_a_lapped_kart_drops_out_of_both(self):
        grid = self.grid((1, "10", "0.0"), (2, "11", "2 laps"), (3, "12", "8.0"))
        self.assertNotIn("11", KartPool.gaps_behind(grid))
        self.assertNotIn("11", KartPool.gaps_ahead(grid))

    def test_an_empty_grid_is_not_a_crash(self):
        self.assertEqual(KartPool.gaps_behind([]), {})
        self.assertEqual(KartPool.gaps_behind([{"kart": "10"}]), {})

    def test_the_shove_is_stored_against_the_lap(self):
        import sqlite3
        self.seed([("1", "ALPHA", "10"), ("2", "BRAVO", "11")])
        for n in (1, 2):
            self.pool.observe([
                dict(row("1", "ALPHA", laps=n), pos="1", gap="0.0"),
                dict(row("2", "BRAVO", laps=n), pos="2", gap="0.3")])
        con = sqlite3.connect(self.db)
        got = dict(con.execute("SELECT kart, behind_s FROM kart_lap"))
        self.assertAlmostEqual(got["10"], 0.3, msg="11 is right behind 10")
        self.assertIsNone(got["11"], "nobody behind the last kart")


class TestPersistence(PoolCase):
    def test_state_survives_a_restart(self):
        self.seed([("1", "ALPHA", "10")])
        self.pool.lane_add(2, "21")
        again = KartPool(self.db, {})
        self.assertEqual(again.kart_of(), {"1": "10"})
        self.assertEqual([k["num"] for k in again.snapshot()["lanes"][1]["karts"]],
                         ["21"])

    def test_reset_clears_the_book(self):
        self.seed([("1", "ALPHA", "10")])
        self.pool.lane_add(1, "21")
        self.pool.reset()
        self.assertEqual(self.pool.kart_of(), {})
        self.assertEqual(self.pool.snapshot()["lanes"][0]["karts"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPracticeSwitch(unittest.TestCase):
    """The switch is a setting like any other, and moving it must be felt."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pool = KartPool(os.path.join(self._tmp.name, "p.db"), {})

    def test_off_by_default_and_the_rating_cfg_is_untouched(self):
        self.assertFalse(self.pool.cfg["practice"])
        self.assertEqual(self.pool.rating_cfg(), {})

    def test_on_it_layers_practice_over_the_track_settings(self):
        self.pool.configure({"practice": True, "rating": {"tow_gap_s": 0.5}})
        cfg = self.pool.rating_cfg()
        self.assertEqual(cfg["bucket_minutes"], 0)
        self.assertEqual(cfg["min_pilot_laps"], rating.PRACTICE["min_pilot_laps"])
        self.assertEqual(cfg["tow_gap_s"], 0.5, "per-track tuning still applies")

    def test_moving_the_switch_stales_the_scores(self):
        """No new lap arrives when you flick it, so nothing else would."""
        self.pool.ratings(force=True)
        self.pool._rating_dirty = False
        self.pool.configure({"practice": True})
        self.assertTrue(self.pool._rating_dirty)

    def test_setting_it_to_what_it_already_is_changes_nothing(self):
        self.pool.ratings(force=True)
        self.pool._rating_dirty = False
        self.pool.configure({"practice": False})
        self.assertFalse(self.pool._rating_dirty)
