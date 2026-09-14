#!/usr/bin/env python3
"""Checks for the feed capture tool.

The network half cannot be tested from here — that is the whole reason the tool
exists — so everything that reasons about a feed is kept apart from everything
that fetches one, and only the first half is tested.  The frames below are
shaped like the Apex pipe protocol the war room already parses.
"""

import os
import sys
import tempfile
import unittest

os.environ.setdefault("WARROOM_DB", tempfile.mktemp(suffix=".db"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import apex_dump

COLS = [("rk", "1"), ("no", "7"), ("name", "TPC"), ("dr", "Ana"),
        ("llp", "1:02.478"), ("blp", "1:01.900"), ("tlp", "42"), ("pit", "8")]


def grid(rows=1, extra_cols=(), lap="1:02.478"):
    head = "".join(f'<th data-id="c{i}" data-type="{t}">{t}</th>'
                   for i, (t, _v) in enumerate(COLS + list(extra_cols)))
    body = ""
    for n in range(rows):
        cells = "".join(
            f'<td data-type="{t}">{lap if t == "llp" else v}</td>'
            for t, v in COLS + list(extra_cols))
        body += f'<tr data-id="r{n}" class="">{cells}</tr>'
    return f'grid||<tr class="head">{head}</tr>{body}'


class TestSummarise(unittest.TestCase):
    def test_a_running_session_reads_as_live(self):
        s = apex_dump.summarise([grid(rows=12), "dyn1||10:15:40 / 24:00:00",
                                 "light|lg|"])
        self.assertTrue(s["live"])
        self.assertEqual(s["karts"], 12)
        self.assertEqual(s["on_track"], 12)
        self.assertEqual(s["light"], "GREEN")
        self.assertEqual(s["clock"], "10:15:40 / 24:00:00")

    def test_an_empty_grid_is_not_live(self):
        s = apex_dump.summarise([grid(rows=0)])
        self.assertFalse(s["live"])
        self.assertEqual(s["karts"], 0)

    def test_nothing_at_all_is_not_live(self):
        s = apex_dump.summarise([])
        self.assertFalse(s["live"])

    def test_cars_parked_in_the_pits_are_counted_but_not_on_track(self):
        s = apex_dump.summarise([grid(rows=5, lap="-")])
        self.assertEqual(s["karts"], 5)
        self.assertEqual(s["on_track"], 0)

    def test_it_names_the_columns_we_would_ignore(self):
        s = apex_dump.summarise([grid(extra_cols=[("sr", "GOLD")])])
        self.assertEqual(s["columns_ignored"], ["sr"])

    def test_it_says_whether_the_pit_counter_is_there(self):
        self.assertTrue(apex_dump.summarise([grid()])["has_pit_counter"])

    def test_it_spots_a_category_column(self):
        s = apex_dump.summarise([grid(extra_cols=[("cat", "PRO")])])
        self.assertTrue(s["has_category"])
        self.assertEqual(s["columns_ignored"], [])

    def test_light_states_are_translated(self):
        for code, name in (("lr", "RED"), ("ly", "YELLOW"), ("lsc", "SAFETY CAR")):
            s = apex_dump.summarise([grid(), f"light|{code}|"])
            self.assertEqual(s["light"], name)


class TestUrls(unittest.TestCase):
    def test_a_slug_becomes_an_event_url(self):
        self.assertEqual(apex_dump.event_url("kip-palmela"),
                         "https://live.apex-timing.com/kip-palmela/")

    def test_a_url_is_left_alone(self):
        url = "https://live.apex-timing.com/kartalcanede/#live"
        self.assertEqual(apex_dump.event_url(url), url)

    def test_the_event_name_comes_back_out(self):
        self.assertEqual(
            apex_dump.event_name("https://live.apex-timing.com/kip-palmela/#live"),
            "kip-palmela")

    def test_palmela_is_watched_by_default(self):
        self.assertIn("kip-palmela", apex_dump.KNOWN_EVENTS)


class TestAnalyse(unittest.TestCase):
    def test_it_reports_what_the_parser_read(self):
        r = apex_dump.analyse([grid(rows=3)])
        self.assertEqual(r["rows_parsed"], 3)
        self.assertIn("driver", r["fields_we_read"])
        self.assertIn("pits", r["fields_we_read"])
        self.assertEqual(r["commands"], ["grid"])

    def test_it_confirms_the_race_clock_parses(self):
        self.assertTrue(
            apex_dump.analyse([grid(), "dyn1||10:15:40 / 24:00:00"])["clock_parsed"])

    def test_a_lap_time_in_the_header_is_not_a_clock(self):
        self.assertFalse(
            apex_dump.analyse([grid(), "dyn1||Best 1:02.478"])["clock_parsed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
