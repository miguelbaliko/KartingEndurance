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
import re
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


PALMELA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "testdata", "apex-kip-palmela-live.raw")


class TestPalmelaLayout(RealFeedCase):
    """Frames from KIP Palmela itself, captured 2026-09-15 before a session.

    Our track sends three sector columns that kartplanet does not, which shifts
    everything after them.  The grid was empty — a countdown to a 15 minute
    session under a red light — so the header is the real part, and the header
    is exactly what the shift depends on.
    """

    def setUp(self):
        super().setUp()
        with open(PALMELA, encoding="utf-8") as f:
            self.raw = f.read()
        self.head = re.search(r'<tr[^>]*class="head".*?</tr>', self.raw).group(0)

    def palmela_row(self, vals):
        ids = re.findall(r'data-id="(c\d+)"', self.head)
        body = "".join(f'<td data-id="r17{cid}" class="{cls}">{v}</td>'
                       for cid, (cls, v) in zip(ids, vals))
        rows, _c, _m = self.app._parse_apex_pipe(
            "grid||" + self.head + f'<tr data-id="r17">{body}</tr>')
        return rows[0] if rows else {}

    def test_the_columns_our_track_sends(self):
        self.assertEqual(
            re.findall(r'data-type="([^"]*)"', self.head),
            ["grp", "sta", "rk", "no", "dr", "s1", "s2", "s3",
             "llp", "blp", "tlp", "gap"])

    def test_sectors_do_not_shift_the_columns_after_them(self):
        # The three sector cells carry the meaningless class "in", the same as
        # gap and lap count, so only the header can say which is which.
        got = self.palmela_row([("gf", ""), ("in", ""), ("rk", "4"),
                                ("no", "17"), ("dr", "DINIS"),
                                ("in", "21.4"), ("in", "19.8"), ("in", "22.1"),
                                ("ti", "1:03.312"), ("ib", "1:02.998"),
                                ("in", "41"), ("in", "12.4")])
        self.assertEqual(got.get("last_lap"), "1:03.312")
        self.assertEqual(got.get("best_lap"), "1:02.998")
        self.assertEqual(got.get("total_laps"), "41")
        self.assertEqual(got.get("gap"), "12.4")
        self.assertEqual((got.get("pos"), got.get("kart")), ("4", "17"))

    def test_a_sector_time_is_never_read_as_a_lap_time(self):
        got = self.palmela_row([("gf", ""), ("in", ""), ("rk", "1"),
                                ("no", "17"), ("dr", "DINIS"),
                                ("in", "21.4"), ("in", "19.8"), ("in", "22.1"),
                                ("ti", "1:03.312"), ("ib", "1:02.998"),
                                ("in", "41"), ("in", "")])
        for field in ("last_lap", "best_lap"):
            self.assertNotIn(got.get(field), ("21.4", "19.8", "22.1"))

    def test_the_session_header_is_read(self):
        meta = {}
        for frame in self.raw.split("\n\x00\n"):
            _r, _c, m = self.app._parse_apex_pipe(frame)
            meta.update({k: v for k, v in m.items() if v})
        self.assertEqual(meta.get("track"), "KIP (1270m)")
        self.assertEqual(meta.get("light"), "lr")          # red, session not out

    def test_an_empty_grid_yields_no_karts(self):
        rows, _c, _m = self.app._parse_apex_pipe(self.raw)
        self.assertEqual(rows, [])


SECTORS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "testdata", "apex-kip-sectors-live.raw")


class TestPalmelaWithKartsRunning(RealFeedCase):
    """KIP Palmela mid-session, captured 2026-09-15 with three karts circulating.

    This is the frame that caught it: the sector cells carry the very classes
    the parser treats as a lap time (``tn``, ``ti``, ``tb``), and they come
    before the lap columns, so the class fallback was reading S1 as the last
    lap — 21.303 where the board said 1:06.822.  The header says which column
    is which, and it has to be believed over the class.
    """

    def setUp(self):
        super().setUp()
        with open(SECTORS, encoding="utf-8") as f:
            self.raw = f.read()
        self.rows = {r["kart"]: r for r in self.app._parse_apex_pipe(self.raw)[0]}

    def test_all_three_karts_parse(self):
        self.assertEqual(set(self.rows), {"308", "309", "310"})

    def test_the_lap_time_is_the_lap_not_the_first_sector(self):
        self.assertEqual(self.rows["309"]["last_lap"], "1:06.822")
        self.assertEqual(self.rows["309"]["best_lap"], "1:05.123")
        # The sector times are in the frame and must not reach any field.
        for row in self.rows.values():
            for field in ("last_lap", "best_lap", "gap"):
                self.assertNotIn(row.get(field),
                                 ("21.303", "21.611", "21.745", "17.138"))

    def test_a_lap_time_a_sector_could_never_be(self):
        """Sanity the other way: a real lap here is over a minute."""
        for kart, row in self.rows.items():
            self.assertGreater(self.app.parse_laptime(row["last_lap"]), 60.0, kart)

    def test_the_gap_to_the_kart_ahead_survives(self):
        """0.034s is the tow the kart rating now has to know about."""
        self.assertEqual(self.rows["310"]["gap"], "0.034")
        import kartpool
        ahead = kartpool.KartPool.gaps_ahead(list(self.rows.values()))
        self.assertIsNone(ahead["309"])           # the leader
        self.assertAlmostEqual(ahead["310"], 0.034)
        self.assertAlmostEqual(ahead["308"], 2.075)

    def test_the_session_is_named_and_the_light_is_green(self):
        meta = {}
        for frame in self.raw.split("\n\x00\n"):
            meta.update({k: v for k, v in self.app._parse_apex_pipe(frame)[2].items()
                         if v})
        self.assertEqual(meta.get("light"), "lg")
        self.assertEqual(meta.get("track"), "KIP (1270m)")


SPRINT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "testdata", "apex-kip-sprint-no-lapcount.raw")


class TestSprintWithoutLapCount(RealFeedCase):
    """KIP Palmela mid-race, captured 2026-09-16: a sprint, not an endurance.

    The layout is our own track's minus one column: a ten kart race with no
    ``tlp``, so no lap count reaches us at all, and the gap is written in laps
    ("Lap 16", "1 Lap") rather than seconds.  Every other fixture we have
    carries a lap counter, so this is the one that fails if somebody assumes
    the number is always there.
    """

    def setUp(self):
        super().setUp()
        self.app._global_col_types.clear()
        self.rows = []
        with open(SPRINT, encoding="utf-8") as f:
            for frame in f.read().split("\n\x00\n"):
                parsed, _c, _m = self.app._parse_apex_pipe(frame)
                self.rows.extend(parsed)

    def test_the_event_sends_no_lap_counter(self):
        self.assertEqual(self.field("total_laps"), [],
                         "the fixture stops being the no-lap-count one")

    def test_the_grid_still_parses_without_it(self):
        self.assertEqual(len(self.rows), 10)
        self.assertEqual([r["pos"] for r in self.rows[:3]], ["1", "2", "3"])
        self.assertEqual([r["kart"] for r in self.rows[:3]], ["17", "16", "29"])
        self.assertEqual(self.rows[0]["driver"], "FRED L.")

    def test_sectors_and_lap_times_come_through(self):
        first = self.rows[0]
        self.assertEqual(first["s1"], "22.465")
        self.assertEqual(first["s3"], "28.825")
        self.assertEqual(first["last_lap"], "1:09.109")
        self.assertEqual(first["best_lap"], "1:08.242")

    def test_a_gap_written_in_laps_is_not_read_as_seconds(self):
        """Without a lap time to price them, laps down are unknown, not zero."""
        self.assertIsNone(self.app.gap_seconds("1 Lap", None))
        self.assertAlmostEqual(self.app.gap_seconds("1 Lap", 69.1), 69.1)

    def test_the_leaders_lap_number_is_not_a_gap(self):
        """Apex writes the lap they are on in the leader's gap cell.

        Our leader's cell reads "Lap 16" while the kart a lap down reads
        "1 Lap".  Reading the first as sixteen laps down would drop the leader
        to the back of the road order, which is the one thing that column must
        never do.
        """
        self.assertEqual(self.rows[0]["gap"], "Lap 16")
        self.assertIsNone(self.app.gap_seconds("Lap 16", 69.1))

    def test_the_board_ranks_without_a_lap_count(self):
        """No lap counter must not collapse the order or the virtual position."""
        order = self.app.kartpool.KartPool.road_order(self.rows)
        self.assertEqual(order[0][0], "17")
        virtual = self.app.virtual_positions(self.rows, mandatory_pits=0,
                                             pit_loss_s=200.0, lap_s=69.1)
        self.assertEqual(virtual["17"]["virtual_pos"], 1)


RKC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "testdata", "apex-rkc-endurance-pitcount.raw")


class TestEnduranceWithPitCounter(RealFeedCase):
    """RKC mid-session, captured 2026-09-16: 25 karts, teams of three.

    The closest thing we have to our own race, and the only fixture that
    carries a pit counter or an interval column — both of which the wall reads
    and neither of which any other real frame had ever exercised.  It also
    names a whole crew in the driver cell and writes its control log in
    French, which is what a Portuguese event will look like in September too.
    """

    def setUp(self):
        super().setUp()
        self.app._global_col_types.clear()
        self.rows, self.meta = [], {}
        with open(RKC, encoding="utf-8") as f:
            for frame in f.read().split("\n\x00\n"):
                parsed, _c, m = self.app._parse_apex_pipe(frame)
                self.rows.extend(parsed)
                self.meta.update({k: v for k, v in m.items() if v})

    def test_the_whole_grid_parses(self):
        self.assertEqual(len(self.rows), 25)

    def test_the_pit_counter_is_read(self):
        """It decides stops owed, so a feed that has one must be believed."""
        pits = [r["pits"] for r in self.rows if r.get("pits")]
        self.assertTrue(pits, "this is the fixture with a pit counter")
        self.assertTrue(all(p.isdigit() for p in pits))

    def test_the_interval_column_is_read(self):
        """GAP is to the leader, INT is to the kart in front — not the same."""
        row = next(r for r in self.rows if r.get("interval"))
        self.assertNotEqual(row["interval"], row.get("gap"))

    def test_a_whole_crew_in_the_driver_cell_is_left_alone(self):
        """Three names with slashes is a driver cell, not something to split."""
        crew = [r["driver"] for r in self.rows if "/" in (r.get("driver") or "")]
        self.assertTrue(crew)
        self.assertIn("/", crew[0])

    def test_an_accented_name_survives_the_parser(self):
        self.assertTrue(any("É" in (r.get("driver") or "") or
                            "Ã" in (r.get("driver") or "") for r in self.rows))

    def test_the_control_log_is_read_in_french(self):
        self.assertEqual(self.meta["control"][0]["flag"], "green")
        self.assertEqual(self.meta["control"][0]["text"], "Départ")

    def test_otr_is_a_column_we_looked_at_and_chose_to_skip(self):
        """"En piste" — the rival's stint clock, or "in" while they are boxed.

        Useful, but our own track does not send it, so it stays unparsed and
        the watch must not keep reporting it as an unknown layout.
        """
        import apex_dump
        self.assertIn("otr", apex_dump.KNOWN_SKIPPED)
        with open(RKC, encoding="utf-8") as f:
            self.assertIn('data-type="otr"', f.read())


PENALTIES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "testdata", "apex-rkc-control-penalties.raw")


class TestControlLogPenalties(RealFeedCase):
    """RKC at the flag, 2026-09-16: race control naming names.

    Five karts warned for a stop under the minimum — "15 Avertissement -
    Passage au stand en 00:56 (Tour 18)" is kart 15, boxed for 56 seconds,
    which is the rule §15.1 charges us 20s a block for.  No other fixture has
    race control saying anything but "Start", so this is the one that proves a
    message can be traced to a competitor at all.
    """

    def setUp(self):
        super().setUp()
        self.control = []
        with open(PENALTIES, encoding="utf-8") as f:
            for frame in f.read().split("\n\x00\n"):
                m = self.app._parse_apex_pipe(frame)[2]
                if m.get("control"):
                    self.control = m["control"]

    def tagged(self):
        field = {"5", "13", "14", "15", "16", "17", "21", "22"}
        return self.app.control_for_kart(self.control, field)

    def test_the_log_came_through(self):
        self.assertGreaterEqual(len(self.control), 6)

    def test_every_warning_is_traced_to_its_kart(self):
        warned = {e["kart"] for e in self.tagged() if e["flag"] == "warning"}
        self.assertEqual(warned, {"13", "14", "15", "17", "21"})

    def test_messages_for_the_whole_field_are_left_alone(self):
        both = {e["text"]: e["kart"] for e in self.tagged()}
        self.assertEqual(both["Départ"], "")
        self.assertEqual(both["Arrivée"], "")

    def test_the_flags_either_side_of_the_race_are_read(self):
        flags = [e["flag"] for e in self.control]
        self.assertIn("green", flags)
        self.assertIn("chequered", flags)


if __name__ == "__main__":
    unittest.main(verbosity=2)
