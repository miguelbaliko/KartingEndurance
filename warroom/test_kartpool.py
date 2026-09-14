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
