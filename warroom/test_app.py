#!/usr/bin/env python3
"""End-to-end checks: real Flask app, real parser, a fake Apex feed.

Everything here goes through the same path a race does — pipe-protocol HTML in
at one end, the war room's snapshot out at the other.
"""

import importlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEAMS = [("1", "ALPHA", "Ana"), ("2", "BRAVO", "Bea"), ("3", "CHARLIE", "Caio")]


def grid_html(state, head=True, team_column=True):
    """A grid the way Apex sends it: a head row of data-types, then the field."""
    cols = ["rk", "no"] + (["name"] if team_column else []) + \
           ["dr", "llp", "blp", "tlp", "pit"]
    out = []
    if head:
        cells = "".join(f'<th data-id="c{i}" data-type="{c}">{c}</th>'
                        for i, c in enumerate(cols))
        out.append(f'<tr class="head">{cells}</tr>')
    for pos, (no, team, driver) in enumerate(TEAMS, start=1):
        s = state[no]
        vals = [pos, no] + ([team] if team_column else []) + \
               [driver, s["last"], s["best"], s["laps"], s["pits"]]
        cells = "".join(f'<td data-id="r{no}c{i}" data-type="{c}">{v}</td>'
                        for i, (c, v) in enumerate(zip(cols, vals)))
        out.append(f'<tr data-id="r{no}" class="{s.get("cls", "")}">{cells}</tr>')
    return "".join(out)


class AppCase(unittest.TestCase):
    def setUp(self):
        # A race per test: the app keeps its state in SQLite, so tests that
        # shared a file would inherit each other's karts and stops.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        os.environ["WARROOM_DB"] = os.path.join(self._tmp.name, "race.db")
        self.addCleanup(os.environ.pop, "WARROOM_DB", None)

        import app
        importlib.reload(app)
        self.app = app
        app.CFG["team_name"] = "ALPHA"
        app.app.config["TESTING"] = True
        self.client = app.app.test_client()
        self.state = {no: {"last": "1:03.000", "best": "1:02.500", "laps": 10,
                           "pits": 0} for no, _, _ in TEAMS}

    def feed(self, head=False):
        self.app._process_rows(self.app._parse_apex_pipe(
            "grid||" + grid_html(self.state, head=head))[0])

    def lap(self, times=None, n=1):
        for _ in range(n):
            for no, _team, _driver in TEAMS:
                self.state[no]["laps"] += 1
                if times and no in times:
                    self.state[no]["last"] = times[no]
            self.feed()

    def snap(self):
        return json.loads(self.client.get("/api/state").data)


class TestFeedParsing(AppCase):
    def test_grid_reaches_the_snapshot(self):
        self.feed(head=True)
        teams = self.snap()["teams"]
        self.assertEqual([t["kart"] for t in teams], ["1", "2", "3"])
        self.assertEqual([t["team"] for t in teams], ["ALPHA", "BRAVO", "CHARLIE"])

    def test_events_without_a_team_column_fall_back_to_the_driver(self):
        self.app._process_rows(self.app._parse_apex_pipe(
            "grid||" + grid_html(self.state, head=True, team_column=False))[0])
        self.assertEqual([t["team"] for t in self.snap()["teams"]],
                         ["Ana", "Bea", "Caio"])

    def test_driver_column_is_kept_separately(self):
        self.feed(head=True)
        self.assertEqual(self.snap()["teams"][0]["driver"], "Ana")

    def test_in_pit_is_read_off_the_row(self):
        self.feed(head=True)
        self.state["2"]["cls"] = "pit"
        self.feed()
        by_no = {t["kart"]: t for t in self.snap()["teams"]}
        self.assertTrue(by_no["2"]["in_pit"])
        self.assertFalse(by_no["1"]["in_pit"])


class TestKartCounting(AppCase):
    def setUp(self):
        super().setUp()
        self.feed(head=True)
        for no, team, _ in TEAMS:
            self.client.post("/api/kart/assign",
                             json={"team_no": no, "kart": str(10 + int(no))})
        for kart in ("20", "21"):
            self.client.post("/api/kart/add", json={"lane": 1, "kart": kart})
        for kart in ("30", "31"):
            self.client.post("/api/kart/add", json={"lane": 2, "kart": kart})

    def karts(self):
        return json.loads(self.client.get("/api/karts").data)

    def test_a_stop_asks_which_lane_and_nothing_else(self):
        self.lap()
        self.state["2"]["pits"] = 1
        self.lap()
        pending = self.karts()["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["team_no"], "2")
        self.assertEqual(pending[0]["kart_in"], "12")

    def test_answering_the_lane_completes_the_swap(self):
        self.lap()
        self.state["2"]["pits"] = 1
        self.lap()
        stop = self.karts()["pending"][0]["id"]
        self.client.post("/api/kart/lane", json={"stop_id": stop, "lane": 1})
        pool = self.karts()
        self.assertEqual(pool["kart_of"]["2"], "20")
        self.assertEqual(pool["pending"], [])
        lane1 = [k["num"] for k in pool["lanes"][0]["karts"]]
        self.assertEqual(lane1, ["21", "12"])

    def test_the_timing_table_shows_the_kart_each_team_is_in(self):
        by_no = {t["kart"]: t for t in self.snap()["teams"]}
        self.assertEqual(by_no["1"]["my_kart"], "11")
        self.assertEqual(by_no["3"]["my_kart"], "13")

    def test_undo_reverses_a_mis_tapped_lane(self):
        self.lap()
        self.state["2"]["pits"] = 1
        self.lap()
        stop = self.karts()["pending"][0]["id"]
        self.client.post("/api/kart/lane", json={"stop_id": stop, "lane": 2})
        self.assertEqual(self.karts()["kart_of"]["2"], "30")
        self.client.post("/api/kart/undo")
        self.assertEqual(self.karts()["kart_of"]["2"], "12")
        self.assertEqual(len(self.karts()["pending"]), 1)

    def test_a_kart_can_be_corrected_by_hand(self):
        self.client.post("/api/kart/assign", json={"team_no": "1", "kart": "99"})
        self.assertEqual(self.karts()["kart_of"]["1"], "99")

    def test_manual_stop_for_one_the_feed_missed(self):
        res = json.loads(self.client.post("/api/kart/stop",
                                          json={"team_no": "3"}).data)
        self.assertTrue(res["ok"])
        self.assertEqual(self.karts()["pending"][0]["team_no"], "3")


class TestMyTeamAutoPit(AppCase):
    def test_the_feed_starts_and_ends_our_stop(self):
        self.feed(head=True)
        self.client.post("/api/driver/add", json={"name": "Ana"})
        self.client.post("/api/driver/set", json={"driver_id": 1})
        self.client.post("/api/race/start")
        self.lap(n=2)
        self.assertEqual(self.snap()["status"], "racing")

        self.state["1"]["pits"] = 1           # our kart enters the pit lane
        self.lap()
        self.assertEqual(self.snap()["status"], "pitting")

        self.lap()                            # and comes back out
        state = self.snap()
        self.assertEqual(state["status"], "racing")
        self.assertEqual(state["stints_done"], 1, "the stint was closed and logged")
        self.assertEqual(state["pits_done"], 1, "counted from the Apex pit column")

    def test_a_rival_stop_leaves_our_clock_alone(self):
        self.feed(head=True)
        self.client.post("/api/race/start")
        self.lap()
        self.state["2"]["pits"] = 1
        self.lap()
        self.assertEqual(self.snap()["status"], "racing")


class TestRaceClock(AppCase):
    def test_apex_clock_is_preferred(self):
        self.app._process_meta({"dyn1": "10:15:40 / 24:00:00"})
        state = self.snap()
        self.assertEqual(state["clock_source"], "apex")
        self.assertAlmostEqual(state["race_remaining"], 13 * 3600 + 44 * 60 + 20,
                               delta=2)
        self.assertAlmostEqual(state["race_elapsed"], 10 * 3600 + 15 * 60 + 40,
                               delta=2)

    def test_our_own_clock_carries_on_when_the_feed_is_quiet(self):
        state = self.snap()
        self.assertEqual(state["clock_source"], "local")
        self.assertIn("race_remaining_fmt", state)

    def test_a_lap_time_in_the_header_is_not_a_race_clock(self):
        self.app._process_meta({"dyn1": "Best lap 1:02.478"})
        self.assertEqual(self.snap()["clock_source"], "local")


class TestPages(AppCase):
    def test_war_room_renders(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_pit_phone_renders(self):
        res = self.client.get("/pit")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Lane", res.data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
