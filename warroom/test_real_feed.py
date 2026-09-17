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


BIGKIP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "testdata", "apex-kip-13karts-lapcount.raw")


class TestTheBiggestKipFieldWeHaveSeen(RealFeedCase):
    """KIP Session 46, 2026-09-16: thirteen karts, and a lap counter.

    Every other KIP capture is two to ten karts, and the ten-kart one sends no
    "tlp" at all.  This one does, so it is the closest thing to the event's
    own configuration we have been able to record — and the only KIP fixture
    where the live cell updates carry enough for the whole board to fill.
    """

    def setUp(self):
        super().setUp()
        self.app._global_col_types.clear()
        self.app._row_kart_map.clear()
        self.app._reset_sector_best()
        self.cells = 0
        with open(BIGKIP, encoding="utf-8") as f:
            for frame in f.read().split("\n\x00\n"):
                r, c, m = self.app._parse_apex_pipe(frame)
                if m: self.app._process_meta(m)
                if c:
                    self.cells += len(c)
                    self.app._apply_cell_updates(c)
                if r: self.app._process_rows(r)
        # Straight off the parsed rows, not through make_snapshot: the other
        # cases here never touch the database, and borrowing whichever one the
        # last test module left behind is how this passed alone and failed in
        # the suite.
        self.teams = sorted(self.app._teams,
                            key=lambda t: int(t.get("pos") or 99))

    def test_the_whole_field_arrives(self):
        self.assertEqual(len(self.teams), 13)
        self.assertEqual([t["pos"] for t in self.teams[:3]], ["1", "2", "3"])

    def test_this_one_does_carry_a_lap_counter(self):
        """The distinction from the sprint fixture, which carries none."""
        laps = [t["total_laps"] for t in self.teams if t.get("total_laps")]
        self.assertEqual(len(laps), 13)
        self.assertEqual(laps[0], "7")

    def test_the_live_cell_updates_were_read(self):
        """Forty of them across twelve frames — the board filled from these."""
        self.assertGreater(self.cells, 20)

    def test_every_kart_has_all_three_sectors_marked(self):
        for t in self.teams:
            self.assertEqual(sorted(t["sectors"]), ["s1", "s2", "s3"], t["kart"])

    def test_the_three_mark_states_all_appear(self):
        """Purple for the session's best, green for a personal best, yellow
        for a sector slower than that driver's own."""
        marks = {v["mark"] for t in self.teams for v in t["sectors"].values()}
        self.assertEqual(marks, {"sb", "pb", "slow"})

    def test_a_purple_is_never_shared_and_may_be_absent(self):
        """The column shows the driver's LAST sector, coloured for what it was.

        So the purple sits on a kart only while its most recent sector is
        still the session's best — once that driver posts a slower one it
        leaves the table entirely, which is what F1 does and what stops a
        purple sticking to a kart that has long since dropped off.  In this
        capture the best S1 is 22.699 and the quickest current one is 23.010,
        so S1 has no purple at all and that is correct.
        """
        for sector in ("s1", "s2", "s3"):
            best = [t["kart"] for t in self.teams
                    if t["sectors"].get(sector, {}).get("mark") == "sb"]
            self.assertLessEqual(len(best), 1, f"{sector}: {best}")
        held = sum(1 for sector in ("s1", "s2", "s3")
                   for t in self.teams
                   if t["sectors"].get(sector, {}).get("mark") == "sb")
        self.assertGreater(held, 0, "no purple anywhere means it never marks one")

    def test_a_purple_belongs_to_the_quickest_current_sector(self):
        for sector in ("s1", "s2", "s3"):
            times = {t["kart"]: float(t["sectors"][sector]["t"]) for t in self.teams}
            marked = [t["kart"] for t in self.teams
                      if t["sectors"][sector]["mark"] == "sb"]
            if marked:
                self.assertEqual(marked[0], min(times, key=times.get), sector)

    def test_the_race_clock_is_readable_from_it(self):
        import apex_dump
        self.assertTrue(apex_dump._clock_reads(["136781", "106729", "76725"]))


LEMANS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "testdata", "apex-lemans-22karts-french.raw")


class TestABigFrenchFieldLapsItsBackmarkers(RealFeedCase):
    """Le Mans, 2026-09-17: twenty-two karts, and a French board.

    Twice the field of any other capture, and the first one where a lapped
    kart appears at all: the gap column reads "1 Tour" / "2 Tours" instead of
    a time.  We already read "Lap"/"Volta"; the French plural went unread, so
    those two karts dropped out of the virtual order entirely.
    """

    def setUp(self):
        super().setUp()
        self.app._global_col_types.clear()
        with open(LEMANS, encoding="utf-8") as f:
            self.rows = []
            for frame in f.read().split("\n\x00\n"):
                parsed, _c, _m = self.app._parse_apex_pipe(frame)
                self.rows.extend(parsed)

    def test_all_twenty_two_arrive(self):
        self.assertEqual(len(self.rows), 22)
        self.assertEqual(len(self.field("kart")), 22)

    def test_lapped_karts_are_read_as_laps_not_dropped(self):
        lapped = [r["gap"] for r in self.rows
                  if "Tour" in str(r.get("gap", "")) and r["gap"][0].isdigit()]
        self.assertEqual(sorted(lapped), ["1 Tour", "2 Tours"])
        self.assertAlmostEqual(self.app.gap_seconds("1 Tour", 62.0), 62.0)
        self.assertAlmostEqual(self.app.gap_seconds("2 Tours", 62.0), 124.0)

    def test_the_leaders_lap_count_is_still_not_a_gap(self):
        """The leader's cell reads "Tour 10" — the race lap, not a deficit."""
        self.assertIsNone(self.app.gap_seconds("Tour 10", 62.0))


REAL_STOPS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "testdata", "apex-rkc-36karts-real-stops.raw")


class TestARealPitStop(RealFeedCase):
    """RKC "COURSE 1", 2026-09-17: thirty-six karts and a pit window.

    Every other fixture carries the pit column but never sees it move.  This
    one was recorded across eight minutes of a live endurance race in which
    twenty-four counters ticked, so it is the only evidence we have that a
    stop is detected from the thing the regulation is actually judged on —
    the organiser's own count — rather than from a lap-time spike.
    """

    def setUp(self):
        super().setUp()
        import kartpool, tempfile
        self.app._global_col_types.clear()
        self.app._global_head_cols.clear()
        self.app._row_kart_map.clear()
        self.app._reset_sector_best()
        self.app._teams = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pool = kartpool.KartPool(os.path.join(self.tmp.name, "s.db"),
                                      {"lanes": 2})
        self.ticks = {}
        with open(REAL_STOPS, encoding="utf-8") as fh:
            frames = fh.read().split("\n\x00\n")
        first = self.app._parse_apex_pipe(frames[0])[0]
        for r in first:
            if r.get("kart"):
                self.pool.set_kart(r["kart"], r.get("team") or r["kart"], r["kart"])
        for f in frames:
            rows, cells, _m = self.app._parse_apex_pipe(f)
            for kart, upd in cells.items():
                if "pits" in upd:
                    self.ticks.setdefault(kart, set()).add(upd["pits"])
            for r in rows:
                if r.get("pits"):
                    self.ticks.setdefault(r.get("kart"), set()).add(r["pits"])
            if cells: self.app._apply_cell_updates(cells)
            if rows: self.app._process_rows(rows)
            if self.app._teams:
                self.pool.observe([dict(t) for t in self.app._teams])

    def test_the_counters_really_move_in_this_one(self):
        moved = {k for k, v in self.ticks.items() if len(v) > 1}
        self.assertGreaterEqual(len(moved), 20,
                                "the fixture exists for the ticks; without "
                                "them it is just another grid")

    def test_every_tick_becomes_a_stop(self):
        moved = {k for k, v in self.ticks.items() if len(v) > 1}
        stops = {str(p["team_no"]) for p in self.pool.pending()}
        missed = moved - stops
        self.assertFalse(missed, f"counters ticked but no stop opened: {missed}")

    def test_the_stops_came_from_the_feed_not_a_lap_spike(self):
        """§3.8 is judged on the organiser's count, so that is what we follow."""
        with self.pool._con() as con:
            sources = {r["source"] for r in
                       con.execute("SELECT DISTINCT source FROM kart_stop")}
        self.assertEqual(sources, {"feed"})

    def test_the_field_and_its_laps_come_through(self):
        self.assertEqual(len(self.app._teams), 36)
        with self.pool._con() as con:
            karts = con.execute(
                "SELECT COUNT(DISTINCT kart) FROM kart_lap").fetchone()[0]
        self.assertGreaterEqual(karts, 30, "laps must reach the rating model")


if __name__ == "__main__":
    unittest.main(verbosity=2)
