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
        # lf is the chequered flag: without it a finished session printed its
        # raw code and read as one still running.
        for code, name in (("lr", "RED"), ("ly", "YELLOW"), ("lsc", "SAFETY CAR"),
                           ("lg", "GREEN"), ("lf", "CHEQUERED")):
            s = apex_dump.summarise([grid(), f"light|{code}|"])
            self.assertEqual(s["light"], name)


class TestASweepThatCannotReachAnythingSaysSo(unittest.TestCase):
    """The failure that looks exactly like success.

    This watch runs hourly and stays quiet when nothing is live.  A dropped TLS
    handshake made every event read as "nothing broadcasting", so a sweep that
    reached no feed at all reported a quiet paddock and said nothing — while
    KIP Palmela had three karts on track.
    """

    def setUp(self):
        import io, contextlib
        self.warroom = apex_dump.warroom
        self._listen = apex_dump.listen
        self._state = dict(self.warroom._ajax_state)
        self.addCleanup(setattr, apex_dump, "listen", self._listen)
        self.addCleanup(self.warroom._ajax_state.update, self._state)
        self._find_ep = self.warroom._find_apex_endpoints
        self.io, self.ctx = io, contextlib

    def sweep(self, listen):
        apex_dump.listen = listen
        buf = self.io.StringIO()
        with self.ctx.redirect_stdout(buf):
            apex_dump.find(["kip-palmela", "kartplanet"], seconds=1)
        return buf.getvalue()

    def test_a_failed_poll_is_not_reported_as_a_quiet_track(self):
        self.warroom._find_apex_endpoints = lambda url: {"ws": "wss://x/"}
        self.addCleanup(setattr, self.warroom, "_find_apex_endpoints",
                        self._find_ep)

        def failing(url, seconds):
            self.warroom._ajax_state["errors"] = \
                self.warroom._ajax_state.get("errors", 0) + 1
            return []
        out = self.sweep(failing)
        self.assertIn("could not reach the feed", out)
        self.assertNotIn("nothing broadcasting", out)
        self.assertNotIn("Nothing running", out)
        self.assertIn("network result", out)

    def test_discovery_failing_is_not_a_quiet_track_either(self):
        """Without endpoints we never reached the feed, so we heard nothing."""
        self.warroom._find_apex_endpoints = lambda url: {}
        self.addCleanup(setattr, self.warroom, "_find_apex_endpoints",
                        self._find_ep)
        out = self.sweep(lambda url, seconds: [])
        self.assertIn("could not reach the feed", out)
        self.assertNotIn("Nothing running", out)

    def test_a_track_we_did_reach_and_that_was_quiet_still_reads_as_quiet(self):
        self.warroom._find_apex_endpoints = lambda url: {"ws": "wss://x/"}
        self.addCleanup(setattr, self.warroom, "_find_apex_endpoints",
                        self._find_ep)
        out = self.sweep(lambda url, seconds: [])
        self.assertIn("nothing broadcasting", out)
        self.assertIn("Nothing running", out)
        self.assertNotIn("could not reach", out)

    def test_a_live_session_is_still_reported_as_live(self):
        """A running session sends the grid and then keeps sending updates."""
        out = self.sweep(lambda url, seconds: [grid(rows=2), "r1c9|tb|1:02.478"])
        self.assertIn("LIVE", out)
        self.assertNotIn("Nothing running", out)

    def test_a_board_left_up_after_the_flag_is_not_live(self):
        """Apex leaves the final grid up, green light and all.

        kartplanet read as "LIVE · 12 karts" for three hourly sweeps with
        nobody on track: the session had ended and the board was frozen.
        Polled ninety seconds apart, not one lap count had moved.
        """
        out = self.sweep(lambda url, seconds: [grid(rows=12) + "\nlight|lg|"])
        self.assertIn("nothing moved", out)
        self.assertIn("session likely over", out)
        self.assertNotIn("LIVE", out)

    def test_a_static_board_still_reports_what_is_on_it(self):
        s = apex_dump.summarise([grid(rows=12)])
        self.assertFalse(s["live"])
        self.assertTrue(s["grid_up"], "the karts are still worth naming")
        self.assertEqual(s["karts"], 12)
        self.assertEqual(s["updates"], 0)

    def test_movement_is_what_makes_it_live(self):
        s = apex_dump.summarise([grid(rows=2), "r1c9|tb|1:02.1", "r2c9|tn|1:03.9"])
        self.assertTrue(s["live"])
        self.assertEqual(s["updates"], 2)


class TestAFailedPollBuysBackItsTime(unittest.TestCase):
    """A short probe must not spend its whole window on one timeout."""

    def setUp(self):
        self.warroom = apex_dump.warroom
        self._fetch = self.warroom._fetch_http
        self._state = dict(self.warroom._ajax_state)
        self.addCleanup(setattr, self.warroom, "_fetch_http", self._fetch)
        self.addCleanup(self.warroom._ajax_state.update, self._state)

    def test_the_poll_after_a_failure_still_gets_a_turn(self):
        calls = []

        def fetch(url):
            calls.append(url)
            if len(calls) == 1:              # the dropped handshake
                self.warroom._ajax_state["errors"] = \
                    self.warroom._ajax_state.get("errors", 0) + 1
                return ""
            return "grid||<tbody></tbody>"

        self.warroom._fetch_http = fetch
        self.warroom._ajax_state["errors"] = 0
        # A window already spent, so the only polls left are the retries.
        frames = apex_dump.listen_ajax("http://x/", seconds=0, interval=0.0)
        self.assertGreaterEqual(len(calls), 2, "the retry never happened")
        self.assertTrue(frames, "the frame after the failure was lost")

    def test_retries_are_not_unlimited(self):
        """A track that is genuinely unreachable must not hold the sweep up."""
        calls = []

        def fetch(url):
            calls.append(url)
            self.warroom._ajax_state["errors"] = \
                self.warroom._ajax_state.get("errors", 0) + 1
            return ""

        self.warroom._fetch_http = fetch
        self.warroom._ajax_state["errors"] = 0
        apex_dump.listen_ajax("http://x/", seconds=0, interval=0.0, retries=3)
        # The first poll, then three retries, then it gives up.
        self.assertEqual(len(calls), 4)


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


class TestTheClockReport(unittest.TestCase):
    """What the recorder says about a feed's race clock.

    It reported "race clock NO" for KIP, whose dyn1 is a bare millisecond
    counter, because it only ever showed the clock one value.  A single bare
    number proves nothing by design — the clock has to watch it move.
    """

    def test_kips_millisecond_counter_reads_as_a_clock(self):
        # Taken off the live feed, 2026-09-16.
        self.assertTrue(apex_dump._clock_reads(["448197", "418145", "388082"]))

    def test_the_written_out_form_reads_as_a_clock(self):
        self.assertTrue(apex_dump._clock_reads(["0:45:00 / 25:00:00"]))

    def test_a_lap_count_does_not(self):
        self.assertFalse(apex_dump._clock_reads(["118", "119", "120"]))

    def test_nothing_at_all_does_not(self):
        self.assertFalse(apex_dump._clock_reads([]))
