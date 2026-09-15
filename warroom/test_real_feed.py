#!/usr/bin/env python3
"""Tests against frames recorded from a real Apex session.

testdata/apex-kartplanet-live.raw was captured live from
https://live.apex-timing.com/kartplanet/ on 2026-09-15.  It is the only thing
here that is not made up, so it is the only thing that can catch us assuming a
layout Apex does not actually send.

What that session showed, and the previous fixtures did not:

  * Data rows identify their columns by the ``cN`` suffix of ``data-id``, which
    refers back to the header's ``data-type``.  Two columns (gap, lap count)
    carry the class ``in``, which means nothing on its own — reading the class
    alone silently drops both.
  * ``rk`` and ``no`` cells carry no ``data-id`` at all, only a class, so
    neither route works for every column and both are needed.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "testdata", "apex-kartplanet-live.raw")


def frames():
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read().split("\n\x00\n")


class RealFeedCase(unittest.TestCase):
    def setUp(self):
        os.environ["WARROOM_NO_WORKER"] = "1"
        self.addCleanup(os.environ.pop, "WARROOM_NO_WORKER", None)
        import app
        app._global_col_types.clear()      # a fresh connection knows no columns
        self.app = app
        self.rows = []
        for frame in frames():
            parsed, _cells, _meta = app._parse_apex_pipe(frame)
            self.rows.extend(parsed)

    def field(self, name):
        return [r[name] for r in self.rows if r.get(name)]


class TestRealApexGrid(RealFeedCase):
    def test_the_grid_parses(self):
        self.assertEqual(len(self.rows), 12)

    def test_kart_numbers_and_drivers_come_through(self):
        self.assertEqual(len(self.field("kart")), 12)
        self.assertIn("IVO K.", self.field("driver"))

    def test_lap_times_come_through(self):
        self.assertEqual(len(self.field("last_lap")), 12)
        self.assertIn("50.437", self.field("last_lap"))

    def test_lap_counts_come_through(self):
        # Carried on a cell whose class is the meaningless "in", so this only
        # works by resolving the column through the header.  kartpool detects
        # stops from the lap count: lose it and no stop is ever seen.
        laps = self.field("total_laps")
        self.assertEqual(len(laps), 12, "lap counts were dropped")
        self.assertTrue(all(l.isdigit() for l in laps), laps)

    def test_gap_comes_through(self):
        gaps = self.field("gap")
        self.assertEqual(len(gaps), 11, "only the leader has no gap")

    def test_the_header_is_not_mistaken_for_a_kart(self):
        self.assertNotIn("Kart", self.field("kart"))
        self.assertNotIn("Driver", self.field("driver"))

    def test_positions_are_the_ranking_column(self):
        self.assertEqual(sorted(int(p) for p in self.field("pos")),
                         list(range(1, 13)))


class TestRealFeedReachesTheWarRoom(RealFeedCase):
    """The same frames, all the way through to what the screen shows."""

    def test_every_kart_becomes_a_team_with_a_lap_time(self):
        self.app._process_rows(self.rows)
        with self.app._lock:
            teams = list(self.app._teams)
        self.assertEqual(len(teams), 12)
        self.assertTrue(all(t.get("last_lap") for t in teams))

    def test_the_session_name_is_read(self):
        meta = {}
        for frame in frames():
            _r, _c, m = self.app._parse_apex_pipe(frame)
            meta.update({k: v for k, v in m.items() if v})
        self.assertEqual(meta.get("light"), "lg")     # green flag


if __name__ == "__main__":
    unittest.main(verbosity=2)
