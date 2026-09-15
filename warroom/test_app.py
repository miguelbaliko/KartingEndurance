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
import time
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
        # Without this the suite reads whatever config.json the last real run
        # saved, so the regulation defaults under test are silently overridden.
        os.environ["WARROOM_CONFIG"] = os.path.join(self._tmp.name, "config.json")
        self.addCleanup(os.environ.pop, "WARROOM_CONFIG", None)

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

    def test_entering_the_lane_is_enough_to_start_the_box_clock(self):
        """No button, no pit counter — the feed showing us in the lane does it."""
        self.feed(head=True)
        self.client.post("/api/race/start")
        self.lap(n=2)
        self.state["1"]["cls"] = "pit"
        self.feed()
        state = self.snap()
        self.assertEqual(state["status"], "pitting")
        self.assertGreater(state["pit_remaining"], 0)

        self.state["1"]["cls"] = ""
        self.feed()
        self.assertEqual(self.snap()["status"], "racing")

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


CONFIG_JS = """
var configPort = 10110;
var configHost = 'live-data.apex-timing.com';
var configRequestUrl = 'https://live-data.apex-timing.com/live-timing/commonv2/functions/';
"""

EVENT_PAGE = """
<html><head>
<script type="text/javascript" src="../commonv2/javascript/javascript_live_timing.min.js"></script>
<script type="text/javascript" src="javascript/config.js"></script>
</head><body></body></html>
"""


class TestFeedDiscovery(AppCase):
    """Where the timing data lives, worked out the way the site's own JS does."""

    URL = "https://live.apex-timing.com/kip-palmela/"

    def serve(self, pages):
        """Stand in for the network: a URL -> body map, no sockets involved."""
        def fake_get(url, referer="", timeout=5):
            for frag, body in pages.items():
                if frag in url:
                    return body
            raise OSError(f"unexpected fetch: {url}")
        self.app._http_get = fake_get
        self.app._ws_url_checked_at = 0.0
        self.app._reset_ajax_state()

    def test_the_feed_host_comes_from_config_not_the_page(self):
        # The page is served from live.apex-timing.com but the feed is not:
        # reading configHost is the whole point, so guessing the page host back
        # would be the bug this test exists to catch.
        self.serve({"config.js": CONFIG_JS, "kip-palmela": EVENT_PAGE})
        ep = self.app._find_apex_endpoints(self.URL)
        self.assertEqual(ep["ws"], "wss://live-data.apex-timing.com:10113/")
        self.assertNotIn("www.apex-timing.com", ep["ws"])

    def test_the_ajax_endpoint_is_the_one_the_site_uses(self):
        self.serve({"config.js": CONFIG_JS, "kip-palmela": EVENT_PAGE})
        ep = self.app._find_apex_endpoints(self.URL)
        self.assertTrue(ep["ajax"].endswith("/live_ajax.php"))
        self.assertEqual(ep["port"], 10110)

    def test_discovery_is_cached_so_every_poll_is_not_two_fetches(self):
        self.serve({"config.js": CONFIG_JS, "kip-palmela": EVENT_PAGE})
        self.app._find_apex_endpoints(self.URL)
        self.app._http_get = lambda *a, **k: self.fail("refetched inside the cache window")
        self.assertEqual(self.app._find_apex_endpoints(self.URL)["port"], 10110)

    def test_a_page_with_no_config_yields_no_endpoints(self):
        self.serve({"kip-palmela": "<html><body>nothing here</body></html>"})
        self.assertEqual(self.app._find_apex_endpoints(self.URL), {})
        self.assertIsNone(self.app._find_ws_url(self.URL))


class TestAjaxFallback(AppCase):
    """The polling transport: same payload as the socket, over plain HTTPS."""

    URL = "https://live.apex-timing.com/kip-palmela/"

    def serve(self, replies):
        self.replies, self.asked = list(replies), []

        def fake_get(url, referer="", timeout=5):
            if "config.js" in url:
                return CONFIG_JS
            if "live_ajax.php" not in url:
                return EVENT_PAGE
            self.asked.append(url)
            return self.replies.pop(0) if self.replies else ""
        self.app._http_get = fake_get
        self.app._ws_url_checked_at = 0.0
        self.app._reset_ajax_state()

    def test_a_payload_becomes_karts(self):
        self.serve(["0@57@" + "grid||" + grid_html(self.state, head=True)])
        self.assertTrue(self.app._consume_pipe(self.app._fetch_http(self.URL)))
        self.assertEqual(len(self.snap()["teams"]), len(TEAMS))

    def test_the_cursor_advances_so_each_poll_asks_for_what_changed(self):
        self.serve(["1@57@", "1@61@"])
        self.app._fetch_http(self.URL)
        self.assertIn("index=0", self.asked[0])      # first poll starts cold
        self.app._fetch_http(self.URL)
        self.assertIn("index=57", self.asked[1])     # then resumes where it left off
        self.assertEqual(self.app._ajax_state["index"], "61")

    def test_the_poll_port_is_four_above_the_config_port(self):
        # +3 is the WebSocket, +4 is the AJAX feed. Mixing them up returns
        # nothing at all, which is indistinguishable from an idle track.
        self.serve(["0@0@"])
        self.app._fetch_http(self.URL)
        self.assertIn("port=10114", self.asked[-1])

    def test_refresh_browser_resets_the_cursor(self):
        self.serve(["0@57@REFRESH_BROWSER"])
        self.assertEqual(self.app._fetch_http(self.URL), "")
        self.assertEqual(self.app._ajax_state["init"], "1")
        self.assertEqual(self.app._ajax_state["index"], "0")

    def test_an_idle_track_is_quiet_not_broken(self):
        # Between sessions Apex answers with an empty payload. That must not
        # look like a parse failure, and must not wipe the grid.
        self.serve(["0@57@"])
        self.assertFalse(self.app._consume_pipe(self.app._fetch_http(self.URL)))

    def test_a_truncated_reply_is_ignored(self):
        self.serve(["garbage"])
        self.assertEqual(self.app._fetch_http(self.URL), "")

    def test_a_network_error_is_not_fatal(self):
        def boom(url, referer="", timeout=5):
            if "live_ajax.php" in url:
                raise OSError("connection reset")
            return CONFIG_JS if "config.js" in url else EVENT_PAGE
        self.app._http_get = boom
        self.app._ws_url_checked_at = 0.0
        self.app._reset_ajax_state()
        self.assertEqual(self.app._fetch_http(self.URL), "")


class TestSwitchingEvent(AppCase):
    def test_changing_the_url_drops_the_old_cursor(self):
        self.app._ajax_state.update({"init": "0", "index": "57", "counter": 9})
        self.app._ws_blocked = True
        self.client.post("/api/settings",
                         json={"apex_url": "https://live.apex-timing.com/kartalcanede/"})
        self.assertEqual(self.app._ajax_state["index"], "0")
        self.assertFalse(self.app._ws_blocked)


class TestArchivedSessions(AppCase):
    """Apex keeps finished sessions; this is how qualifying gets reviewed."""

    def fake_request(self, answers):
        def go(page_url, request, timeout=20):
            return answers.get(request, "")
        self.app._apex_request = go

    LIST = "16#Session 14\n14#Session 12"
    RESULT = ('0@0@grid||<tbody>'
              '<tr data-id="r0" class="head" data-pos="0">'
              '<td data-id="c1" data-type="rk"></td>'
              '<td data-id="c2" data-type="no"></td>'
              '<td data-id="c3" data-type="dr"></td>'
              '<td data-id="c4" data-type="blp"></td>'
              '<td data-id="c5" data-type="tlp"></td></tr>'
              '<tr data-id="r1" data-pos="2">'
              '<td data-id="r1c1">2</td><td data-id="r1c2">308</td>'
              '<td data-id="r1c3">BAPTISTE L.</td>'
              '<td data-id="r1c4">1:07.232</td><td data-id="r1c5">11</td></tr>'
              '<tr data-id="r2" data-pos="1">'
              '<td data-id="r2c1">1</td><td data-id="r2c2">309</td>'
              '<td data-id="r2c3">OOSTERMAN B.</td>'
              '<td data-id="r2c4">1:05.123</td><td data-id="r2c5">12</td></tr>'
              '</tbody>\nlight|lf|\n')

    def test_the_history_list_is_read(self):
        self.fake_request({"S#": self.LIST})
        got = self.client.get("/api/apex/sessions").get_json()["sessions"]
        self.assertEqual(got, [{"id": "16", "name": "Session 14"},
                               {"id": "14", "name": "Session 12"}])

    def test_no_history_is_an_empty_list_not_an_error(self):
        for answer in ("", "error"):
            self.fake_request({"S#": answer})
            self.assertEqual(
                self.client.get("/api/apex/sessions").get_json()["sessions"], [])

    def test_a_finished_session_comes_back_in_order(self):
        self.fake_request({"S#16": self.RESULT})
        got = self.client.get("/api/apex/session/16").get_json()
        self.assertTrue(got["finished"], "chequered flag")
        self.assertEqual([r["pos"] for r in got["rows"]], ["1", "2"])
        self.assertEqual(got["rows"][0]["kart"], "309")
        self.assertEqual(got["rows"][0]["best_lap"], "1:05.123")
        self.assertEqual(got["rows"][0]["total_laps"], "12")

    def test_a_session_that_cannot_be_read_is_empty_not_a_crash(self):
        self.fake_request({})
        got = self.client.get("/api/apex/session/99").get_json()
        self.assertEqual(got["rows"], [])

    def test_reading_a_result_does_not_disturb_the_live_grid(self):
        """The archive must not overwrite what is on the wall right now."""
        self.app._reset_sector_best()
        self.app._process_rows([{"pos": "1", "kart": "7", "team": "ALPHA",
                                 "last_lap": "1:03.000"}])
        self.fake_request({"S#16": self.RESULT})
        self.client.get("/api/apex/session/16")
        live = self.client.get("/api/state").get_json()["teams"]
        self.assertEqual([t["kart"] for t in live], ["7"])


class TestFindingOurselvesInTheFeed(AppCase):
    """Apex does not have to spell a team the way the entry list does.

    We are "TPC CIAO CUORE" on the organisers' list and may be plain "TPC" on
    the timing screen.  Exact equality would leave the wall with no team for
    twenty-five hours: no strategy, no pace, no box clock, nothing.
    """

    FIELD = ["MATRAX", "TPC CIAO CUORE", "STF PERFORMANCE", "JURASSIC KART"]

    def test_a_longer_name_in_the_feed_is_still_us(self):
        self.assertEqual(self.app.match_team("TPC", self.FIELD), "TPC CIAO CUORE")

    def test_a_shorter_name_in_the_feed_is_still_us(self):
        self.assertEqual(self.app.match_team("TPC CIAO CUORE", ["TPC", "MATRAX"]),
                         "TPC")

    def test_case_accents_and_punctuation_do_not_matter(self):
        for spelt in ("microagua", "MICRO-AGUA", "Microágua"):
            self.assertEqual(self.app.match_team(spelt, ["MICROÁGUA", "STCAR"]),
                             "MICROÁGUA", spelt)

    def test_an_exact_name_beats_a_longer_one_that_starts_the_same(self):
        """JURASSIC KART and JURASSIC KART RAPTOR are two different teams."""
        field = ["JURASSIC KART", "JURASSIC KART RAPTOR"]
        self.assertEqual(self.app.match_team("JURASSIC KART", field),
                         "JURASSIC KART")
        self.assertEqual(self.app.match_team("JURASSIC KART RAPTOR", field),
                         "JURASSIC KART RAPTOR")

    def test_it_refuses_to_pick_between_two(self):
        """Three STF teams are entered. The wrong team's pace is worse than none."""
        field = ["STF BY KARTCUP", "STF PERFORMANCE", "STF ENDURANCE"]
        self.assertIsNone(self.app.match_team("STF", field))

    def test_roman_numerals_are_not_run_together(self):
        """TFD I and TFD II are two entries, one character apart."""
        field = ["TFD I", "TFD II"]
        self.assertEqual(self.app.match_team("TFD I", field), "TFD I")
        self.assertEqual(self.app.match_team("TFD II", field), "TFD II")

    def test_a_name_that_is_not_there_is_not_forced_onto_someone(self):
        self.assertIsNone(self.app.match_team("TPC", ["MATRAX", "STCAR"]))
        self.assertIsNone(self.app.match_team("", self.FIELD))

    def test_our_row_is_marked_when_the_feed_spells_us_differently(self):
        self.app.CFG["team_name"] = "TPC"
        self.app._reset_sector_best()
        self.app._process_rows([
            {"pos": "1", "kart": "7", "team": "TPC CIAO CUORE",
             "last_lap": "1:03.000"},
            {"pos": "2", "kart": "9", "team": "MATRAX", "last_lap": "1:04.000"}])
        rows = {t["kart"]: t for t in
                self.client.get("/api/state").get_json()["teams"]}
        self.assertTrue(rows["7"]["is_my_team"])
        self.assertFalse(rows["9"]["is_my_team"])

    def test_nobody_is_marked_when_we_are_not_in_the_feed(self):
        self.app.CFG["team_name"] = "TPC"
        self.app._reset_sector_best()
        self.app._process_rows([
            {"pos": "1", "kart": "9", "team": "MATRAX", "last_lap": "1:04.000"}])
        rows = self.client.get("/api/state").get_json()["teams"]
        self.assertFalse(any(t["is_my_team"] for t in rows))

    def test_our_laps_are_looked_up_under_the_feeds_spelling(self):
        """The pool files laps under the feed's name, so that is what to ask for."""
        self.app.CFG["team_name"] = "TPC"
        self.app._reset_sector_best()
        self.app._process_rows([{"pos": "1", "kart": "7",
                                 "team": "TPC CIAO CUORE", "last_lap": "1:03.000"}])
        self.assertEqual(self.app.my_team_name(), "TPC CIAO CUORE")


class TestCategoryFromTheEntryList(AppCase):
    """PRO or AM when the feed's own class column is blank."""

    def test_the_entry_list_fills_a_blank(self):
        self.assertEqual(self.app.category_of("TPC CIAO CUORE"), "AM")
        self.assertEqual(self.app.category_of("MATRAX"), "PRO")

    def test_a_team_not_on_the_list_gets_nothing_invented(self):
        self.assertEqual(self.app.category_of("SOME OTHER TEAM"), "")
        self.assertEqual(self.app.category_of(""), "")

    def test_an_ambiguous_name_is_left_blank(self):
        """Two of the three STF teams are PRO, but guessing is still guessing."""
        self.assertEqual(self.app.category_of("STF"), "")

    def test_the_feed_wins_when_it_says_anything(self):
        self.app._reset_sector_best()
        self.app._process_rows([
            {"pos": "1", "kart": "7", "team": "TPC CIAO CUORE",
             "category": "PRO", "last_lap": "1:03.000"},
            {"pos": "2", "kart": "9", "team": "MATRAX", "last_lap": "1:04.000"}])
        rows = {t["kart"]: t for t in
                self.client.get("/api/state").get_json()["teams"]}
        self.assertEqual(rows["7"]["category"], "PRO", "the feed is the authority")
        self.assertEqual(rows["9"]["category"], "PRO", "filled from the entry list")

    def test_every_entry_has_a_category_we_recognise(self):
        import entries
        for name, cat in entries.ENTRIES:
            self.assertIn(cat, ("PRO", "AM"), name)
        self.assertFalse(entries.COMPLETE, "one entry was obscured; say so")

    def test_no_two_entries_collapse_into_the_same_name(self):
        """If two entries normalise alike, neither could ever be matched."""
        import entries
        seen = {}
        for name, _cat in entries.ENTRIES:
            key = self.app.norm_team(name)
            self.assertNotIn(key, seen, f"{name} collides with {seen.get(key)}")
            seen[key] = name

    def test_our_own_entry_is_findable_from_the_name_we_ship_with(self):
        """The default in the settings has to find us on the entry list.

        Read off the shipped default, not the live config: the test harness
        renames the team, and this is a check on what we ship.
        """
        import entries, re as _re
        shipped = _re.search(r'"team_name":\s*"([^"]*)"',
                             open(self.app.__file__).read()).group(1)
        self.assertEqual(
            self.app.match_team(shipped, [n for n, _c in entries.ENTRIES]),
            entries.OURS)


class TestHowLongThisStopCanWait(AppCase):
    """One number, and which rule is holding it.

    Two deadlines run at once: the stint ceiling (§3.10, and §15.4 charges 20s
    per started 10s over it) and the schedule (every stop still owed needs its
    three minutes before the lane shuts at 24:30, §3.8). The earlier one is
    the only one that matters, and the wall needs to know which it is.
    """

    def by(self, stint_min, elapsed_h, pits, **over):
        race = dict(self.app.CFG["race"])
        race.update(over)
        return self.app.pit_by_seconds(stint_min * 60, elapsed_h * 3600,
                                       pits, race)

    def test_early_on_the_ceiling_is_what_binds(self):
        got = self.by(stint_min=20, elapsed_h=2, pits=3)
        self.assertEqual(got["reason"], "stint limit")
        self.assertAlmostEqual(got["seconds"], 40 * 60, delta=1)

    def test_it_counts_down_with_the_stint(self):
        a = self.by(stint_min=20, elapsed_h=2, pits=3)["seconds"]
        b = self.by(stint_min=35, elapsed_h=2, pits=3)["seconds"]
        self.assertAlmostEqual(a - b, 15 * 60, delta=1)

    def test_the_ceiling_still_wins_while_there_is_race_left(self):
        """At 23h with four stops owed the schedule allows 78 min — but the
        stint ceiling only 55, so the ceiling is still the binding rule."""
        got = self.by(stint_min=5, elapsed_h=23, pits=30)
        self.assertEqual(got["reason"], "stint limit")
        self.assertAlmostEqual(got["seconds"], 55 * 60, delta=1)

    def test_late_on_the_schedule_is_what_binds(self):
        """An hour later the window is 30 min and four stops need twelve."""
        got = self.by(stint_min=5, elapsed_h=24, pits=30)
        self.assertEqual(got["reason"], "schedule")
        self.assertAlmostEqual(got["seconds"], 18 * 60, delta=1)
        self.assertIn("4 stop", got["detail"])

    def test_it_never_goes_negative(self):
        for args in ((70, 2, 3), (5, 24, 30), (70, 24, 33)):
            self.assertGreaterEqual(self.by(*args)["seconds"], 0.0)

    def test_once_the_lane_shuts_it_says_so(self):
        got = self.by(stint_min=10, elapsed_h=24.9, pits=34)
        self.assertEqual(got["reason"], "lane shut")
        self.assertEqual(got["seconds"], 0)

    def test_with_every_stop_served_only_the_ceiling_is_left(self):
        got = self.by(stint_min=10, elapsed_h=20, pits=34)
        self.assertEqual(got["reason"], "stint limit")
        self.assertAlmostEqual(got["seconds"], 50 * 60, delta=1)
        self.assertIn("mandatory stop served", got["detail"])

    def test_it_agrees_with_box_now_at_the_ceiling(self):
        """The countdown and the call must not disagree on the wall."""
        race = self.app.CFG["race"]
        at_limit = race["stint_max_minutes"] * 60 - 30
        got = self.app.pit_by_seconds(at_limit, 3600, 2, race)
        self.assertLess(got["seconds"], 60)
        strat = self.app.compute_strategy(at_limit, 3600, 2, None, None, "lg")
        self.assertEqual(strat["label"], "BOX NOW")

    def test_the_shape_a_real_race_actually_had(self):
        """Read off a 24h Palmela board: 10h14 gone, teams on 8-10 stops.

        Our own race is 25h with 34 stops, so the schedule bites far harder
        than it did for them — this is the arithmetic that says by how much.
        """
        ours = self.by(stint_min=0, elapsed_h=10.24, pits=10)
        self.assertEqual(ours["reason"], "stint limit",
                         "ten hours in, the ceiling is still the binding rule")
        # 24 stops still owed at three minutes is 72 minutes of box time, and
        # there are about 13 hours of open pit lane left to fit it in.
        late = self.by(stint_min=0, elapsed_h=10.24, pits=10)
        self.assertGreater(late["seconds"], 0)

    def test_it_reaches_the_wall(self):
        self.app.kv_set("status", "racing")
        got = self.client.get("/api/state").get_json()["pit_by"]
        self.assertIn(got["reason"], ("stint limit", "schedule", "lane shut"))
        self.assertGreaterEqual(got["seconds"], 0)


class TestSectorColours(AppCase):
    """Purple, green, yellow — what every timing screen in the paddock means."""

    def setUp(self):
        super().setUp()
        self.session, self.by_kart = {}, {}

    def mark(self, kart, s1=None, s2=None, s3=None):
        row = {k: v for k, v in (("s1", s1), ("s2", s2), ("s3", s3))
               if v is not None}
        return self.app.mark_sectors(kart, row, self.session, self.by_kart)

    def test_the_first_time_through_is_the_session_best(self):
        self.assertEqual(self.mark("17", s1="24.176")["s1"]["mark"], "sb")

    def test_beating_the_field_is_purple(self):
        self.mark("17", s1="24.176")
        self.assertEqual(self.mark("21", s1="23.900")["s1"]["mark"], "sb")

    def test_beating_only_yourself_is_green(self):
        self.mark("17", s1="24.176")
        self.mark("21", s1="23.900")
        self.assertEqual(self.mark("17", s1="24.000")["s1"]["mark"], "pb")

    def test_slower_than_your_own_best_is_yellow(self):
        self.mark("17", s1="24.176")
        self.assertEqual(self.mark("17", s1="24.900")["s1"]["mark"], "slow")

    def test_purple_moves_when_someone_beats_it(self):
        self.mark("17", s1="24.176")
        self.mark("21", s1="23.900")
        # 17 goes quicker than its own best but not quicker than 21.
        self.assertEqual(self.mark("17", s1="23.950")["s1"]["mark"], "pb")
        # Now it does.
        self.assertEqual(self.mark("17", s1="23.800")["s1"]["mark"], "sb")

    def test_each_sector_is_judged_on_its_own(self):
        self.mark("17", s1="24.0", s2="18.0", s3="21.0")
        got = self.mark("17", s1="23.5", s2="18.4", s3="21.6")
        self.assertEqual([got[k]["mark"] for k in ("s1", "s2", "s3")],
                         ["sb", "slow", "slow"])

    def test_matching_the_best_keeps_the_purple(self):
        """Joint fastest is fastest — nobody has been quicker through there."""
        self.mark("17", s3="21.0")
        self.assertEqual(self.mark("17", s3="21.0")["s3"]["mark"], "sb")

    def test_the_purple_moves_off_a_kart_that_has_been_beaten(self):
        """The first kart through owns the purple; it must not keep it.

        Live at Palmela this showed the slowest kart on track in purple: it was
        first through the sector, and its mark was frozen at the moment it was
        set instead of being read off the bests as they stand.
        """
        self.assertEqual(self.mark("102", s1="39.137")["s1"]["mark"], "sb")
        self.assertEqual(self.mark("222", s1="22.997")["s1"]["mark"], "sb")
        # Same time, same kart, next frame: the purple is not theirs any more.
        self.assertEqual(self.mark("102", s1="39.137")["s1"]["mark"], "pb")

    def test_the_order_rows_arrive_in_cannot_decide_the_purple(self):
        """Apex sends rows in its own order, not in race order.

        Live at Palmela the slowest kart on track was shown in purple: its row
        came first, and the quicker kart further down the same grid could not
        take the colour back from it.
        """
        grid = [{"pos": "6", "kart": "102", "team": "SLOW", "s1": "34.804"},
                {"pos": "1", "kart": "222", "team": "QUICK", "s1": "23.030"}]
        self.app._reset_sector_best()
        self.app._process_rows(grid)
        rows = {t["kart"]: t for t in
                self.client.get("/api/state").get_json()["teams"]}
        self.assertEqual(rows["222"]["sectors"]["s1"]["mark"], "sb")
        self.assertEqual(rows["102"]["sectors"]["s1"]["mark"], "pb",
                         "the slow kart cannot keep a purple it has lost")

    def test_the_table_comes_out_in_race_order(self):
        """Apex sends rows in its own order; a timing screen is read top down."""
        self.app._reset_sector_best()
        self.app._process_rows([
            {"pos": p, "kart": k, "team": k, "last_lap": "1:03.000"}
            for p, k in (("6", "102"), ("3", "202"), ("1", "222"), ("4", "214"))])
        got = [t["pos"] for t in self.client.get("/api/state").get_json()["teams"]]
        self.assertEqual(got, ["1", "3", "4", "6"])

    def test_a_kart_with_no_position_sorts_to_the_back(self):
        self.app._reset_sector_best()
        self.app._process_rows([
            {"pos": "", "kart": "9", "team": "NEW", "last_lap": "1:03.000"},
            {"pos": "2", "kart": "7", "team": "OLD", "last_lap": "1:03.000"}])
        got = [t["kart"] for t in self.client.get("/api/state").get_json()["teams"]]
        self.assertEqual(got, ["7", "9"])

    def test_a_sector_not_yet_set_is_absent_not_zero(self):
        got = self.mark("17", s1="24.0", s2="", s3="-")
        self.assertEqual(set(got), {"s1"})

    def test_the_raw_text_is_carried_through_for_display(self):
        self.assertEqual(self.mark("17", s1="24.176")["s1"]["t"], "24.176")

    def test_sectors_reach_the_timing_table(self):
        """End to end: the feed's sector cells come out coloured."""
        self.app._reset_sector_best()
        self.app._process_rows([
            {"pos": "1", "kart": "7", "team": "ALPHA", "last_lap": "1:03.000",
             "s1": "24.176", "s2": "18.000"},
            {"pos": "2", "kart": "9", "team": "BRAVO", "last_lap": "1:04.000",
             "s1": "24.500", "s2": "17.800"}])
        rows = {t["kart"]: t for t in
                self.client.get("/api/state").get_json()["teams"]}
        self.assertEqual(rows["7"]["sectors"]["s1"]["mark"], "sb")
        self.assertEqual(rows["7"]["sectors"]["s1"]["t"], "24.176")
        # 9 is slower than 7 through S1, so green, not purple — and quicker
        # through S2, so it takes the purple there.
        self.assertEqual(rows["9"]["sectors"]["s1"]["mark"], "pb")
        self.assertEqual(rows["9"]["sectors"]["s2"]["mark"], "sb")


class TestDiscoverySurvivesABlip(AppCase):
    """Twenty-five hours means the network will drop a handshake sooner or later."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.app._apex_endpoints = {}
        self.app._ws_url_checked_at = 0.0

    def fail_once_then_work(self):
        def fake_get(url, referer="", timeout=5):
            self.calls.append(url)
            if len(self.calls) == 1:
                raise OSError("handshake operation timed out")
            if url.endswith("config.js"):
                return "var configHost='live-data.apex-timing.com';var configPort=10110;"
            return '<script src="config.js"></script>'
        self.app._http_get = fake_get

    def test_a_failed_discovery_is_retried_within_seconds(self):
        self.fail_once_then_work()
        url = "https://apex-timing.com/live-timing/kip-palmela/"
        self.assertEqual(self.app._find_apex_endpoints(url), {},
                         "the blip must not be papered over")
        # A failure is held for seconds, not for the five minutes a working
        # answer earns — otherwise one dropped handshake blacks out the feed.
        self.app._ws_url_checked_at = time.time() - self.app.DISCOVERY_RETRY_S - 1
        got = self.app._find_apex_endpoints(url)
        self.assertEqual(got.get("port"), 10110)
        self.assertEqual(got.get("ws"), "wss://live-data.apex-timing.com:10113/")

    def test_a_working_answer_is_not_re_fetched_every_poll(self):
        self.fail_once_then_work()
        self.calls.append("burn the failure")
        url = "https://apex-timing.com/live-timing/kip-palmela/"
        first = self.app._find_apex_endpoints(url)
        self.assertTrue(first)
        before = len(self.calls)
        for _ in range(5):
            self.assertEqual(self.app._find_apex_endpoints(url), first)
        self.assertEqual(len(self.calls), before, "cached, not re-fetched")


class TestRegulation(AppCase):
    """The 2026 KIP regulation, as numbers the strategy engine has to obey."""

    def test_the_race_is_twenty_five_hours(self):
        self.assertEqual(self.app.CFG["race"]["duration_minutes"], 25 * 60)

    def test_we_are_entered_as_am(self):
        R = self.app.CFG["race"]
        self.assertEqual((R["category"], R["mandatory_pits"], R["stint_max_minutes"]),
                         ("AM", 34, 60))

    def test_category_sets_the_stops_and_the_stint_ceiling(self):
        for cat, pits, stint in (("PRO", 28, 80), ("AM", 34, 60)):
            race = {"category": cat, "mandatory_pits": 0, "stint_max_minutes": 0}
            self.app.apply_category(race)
            self.assertEqual((race["mandatory_pits"], race["stint_max_minutes"]),
                             (pits, stint), cat)

    def test_an_unknown_category_leaves_the_numbers_alone(self):
        race = {"category": "", "mandatory_pits": 28, "stint_max_minutes": 80}
        self.app.apply_category(race)
        self.assertEqual(race["mandatory_pits"], 28)

    def test_the_pit_lane_shuts_before_the_flag_not_at_it(self):
        # §3.8: every stop must be done by 24:30 of a 25:00 race.
        R = self.app.CFG["race"]
        after_shut = (R["duration_minutes"] - 20) * 60
        s = self.app.compute_strategy(60, after_shut, R["mandatory_pits"], None, None)
        self.assertEqual(s["label"], "HOLD")
        self.assertIn("30 min", s["detail"])

    def test_stops_still_owed_when_the_lane_shuts_are_called_out(self):
        # §3.13.1 is five laps per missing stop, so this is not a quiet HOLD.
        R = self.app.CFG["race"]
        after_shut = (R["duration_minutes"] - 20) * 60
        s = self.app.compute_strategy(60, after_shut, R["mandatory_pits"] - 6,
                                      None, None)
        self.assertEqual(s["label"], "STOPS MISSED")
        self.assertIn("6", s["detail"])

    def test_the_stops_are_paced_into_the_window_that_allows_them(self):
        # Pacing across the full 25h would call a team on plan when it is a
        # stop down, because the last 30 min cannot absorb one.
        R = self.app.CFG["race"]
        R["mandatory_pits"] = 34
        close_s = (R["duration_minutes"] - R["no_pit_last_minutes"]) * 60
        # Two hours of pit window left: on schedule is quiet, behind is a warning.
        early = close_s - 2 * 3600
        self.assertEqual(
            self.app.compute_strategy(60, early, 30, None, None)["label"], "HOLD")
        self.assertEqual(
            self.app.compute_strategy(60, early, 25, None, None)["label"], "PREPARE")
        # One minute left and four stops owed is not a warning, it is now.
        self.assertEqual(
            self.app.compute_strategy(60, close_s - 60, 30, None, None)["label"],
            "BOX NOW")

    def test_a_driver_short_of_the_minimum_is_shown_what_is_owed(self):
        self.client.post("/api/driver/add", json={"name": "Dinis"})
        d = self.snap()["drivers"][0]
        self.assertEqual(d["owed_seconds"], 120 * 60)
        self.assertIn("owed_fmt", d)


class TestQualifyingSurvivesIntoTheRace(AppCase):
    """§3.2.1: the race starts on exactly the karts qualifying finished on.

    So the pace measured in qualifying is worth something from lap one, and
    clearing the race clock before the start must not throw it away.
    """

    def qualifying(self):
        """A kart that has turned laps, and a team sitting in it."""
        self.app.POOL.set_kart("1", "TPC", "17")
        for lap in range(2, 8):
            self.app.POOL.observe([{"kart": "1", "team": "TPC", "pits": "0",
                                    "total_laps": str(lap), "last_lap_s": 62.5,
                                    "driver": "Dinis", "in_pit": False}])
        with self.app.POOL._con() as con:
            return con.execute("SELECT COUNT(*) FROM kart_lap").fetchone()[0]

    def laps_kept(self):
        with self.app.POOL._con() as con:
            return con.execute("SELECT COUNT(*) FROM kart_lap").fetchone()[0]

    def test_resetting_the_race_keeps_what_qualifying_measured(self):
        laps = self.qualifying()
        self.assertGreater(laps, 0, "qualifying recorded nothing to keep")
        self.client.post("/api/race/reset")
        self.assertEqual(self.laps_kept(), laps)
        self.assertEqual(self.app.POOL.kart_of().get("1"), "17",
                         "the race starts on the kart qualifying finished on")

    def test_resetting_the_race_still_clears_the_race(self):
        self.qualifying()
        self.client.post("/api/driver/add", json={"name": "Dinis"})
        self.client.post("/api/race/reset")
        state = self.snap()
        self.assertEqual(state["status"], "idle")
        self.assertEqual(state["stints_done"], 0)
        self.assertEqual(state["drivers"][0]["total_seconds"], 0)

    def test_wiping_the_fleet_is_its_own_deliberate_action(self):
        self.qualifying()
        self.client.post("/api/karts/reset")
        self.assertEqual(self.laps_kept(), 0)
        self.assertIsNone(self.app.POOL.kart_of().get("1"))

    def graded_qualifying(self):
        """Long enough to actually grade the karts, not just record laps.

        Two teams swapping two karts is the least that lets the model tell a
        kart apart from whoever is driving it, which is the whole point of
        carrying the grades forward.
        """
        pool = self.app.POOL
        pairs, karts, lap, ts = [("7", "ALPHA"), ("9", "BRAVO")], \
            {"7": "17", "9": "19"}, 0, 1000.0
        for _swap in range(6):
            # Told directly which kart each team is in, the way the pit wall
            # does it in qualifying — the pit counter never ticks, so no stop
            # is opened and there is nothing waiting on an answer.
            for team_no, team in pairs:
                pool.set_kart(team_no, team, karts[team_no])
            for _ in range(14):
                lap, ts = lap + 1, ts + 65
                pool.observe([
                    {"kart": t, "team": nm, "pits": "0",
                     "total_laps": str(lap), "pos": str(i + 1), "gap": "8.0",
                     # 17 is the quick kart whoever is sitting in it, and
                     # ALPHA the quick team whatever they are sitting in.
                     "last_lap_s": (62.5 if karts[t] == "17" else 63.7)
                                   + (0.0 if nm == "ALPHA" else 0.4)}
                    for i, (t, nm) in enumerate(pairs)], now=ts)
            karts = {"7": karts["9"], "9": karts["7"]}
        return pool

    def test_qualifying_is_long_enough_to_grade_the_fleet(self):
        rated = self.graded_qualifying().ratings(force=True)["karts"]
        self.assertTrue(rated["17"]["rated"], "nothing to carry forward")
        self.assertLess(rated["17"]["effect"], rated["19"]["effect"],
                        "17 was quicker whoever drove it")

    def test_the_grades_themselves_survive_the_reset(self):
        """Not just the laps: the score on the screen has to be the same one."""
        before = {k: v["effect"] for k, v in
                  self.graded_qualifying().ratings(force=True)["karts"].items()}
        self.client.post("/api/race/reset")
        after = {k: v["effect"] for k, v in
                 self.app.POOL.ratings(force=True)["karts"].items()}
        self.assertEqual(before, after)
        self.assertTrue(self.app.POOL.ratings()["karts"]["17"]["rated"])

    def test_the_grades_survive_switching_into_race_mode(self):
        before = {k: v["effect"] for k, v in
                  self.graded_qualifying().ratings(force=True)["karts"].items()}
        self.client.post("/api/session/mode", json={"mode": "race"})
        self.assertEqual(
            {k: v["effect"] for k, v in
             self.app.POOL.ratings(force=True)["karts"].items()}, before)


class TestPenaltyLadder(AppCase):
    """§15.1/15.4/15.5 all charge 20 seconds per started block of ten short."""

    def test_the_ladder(self):
        for short, penalty in [(0, 0), (-5, 0), (0.5, 20), (10, 20),
                               (10.1, 40), (11, 40), (20, 40), (21, 60), (45, 100)]:
            self.assertEqual(self.app.penalty_for_shortfall(short), penalty,
                             f"{short}s short")

    def test_the_box_clock_says_what_leaving_now_costs(self):
        self.client.post("/api/pit/start")
        s = self.snap()
        # Three minutes still owed is 20s per ten of them.
        self.assertGreater(s["pit_penalty_now"], 0)
        self.assertEqual(s["pit_penalty_now"],
                         self.app.penalty_for_shortfall(s["pit_remaining"]))

    def test_no_penalty_once_the_minimum_is_served(self):
        self.assertEqual(self.app.penalty_for_shortfall(0), 0)


class TestGapReading(AppCase):
    def test_seconds(self):
        self.assertAlmostEqual(self.app.gap_seconds("1.034", 60.0), 1.034)
        self.assertAlmostEqual(self.app.gap_seconds("+2.6", 60.0), 2.6)
        self.assertAlmostEqual(self.app.gap_seconds("1:02.500", 60.0), 62.5)

    def test_whole_laps_need_a_lap_time(self):
        self.assertAlmostEqual(self.app.gap_seconds("2 Laps", 61.0), 122.0)
        # Without a lap time a lapped gap is unknown, not zero — calling it
        # zero would rank a lapped team as if it were on the leader's tail.
        self.assertIsNone(self.app.gap_seconds("2 Laps", None))

    def test_nothing_useful(self):
        for g in ("", "-", "--", "abc", None):
            self.assertIsNone(self.app.gap_seconds(g, 60.0))


class TestVirtualPosition(AppCase):
    """Track position lies while stops are still owed."""

    def teams(self, rows):
        return [{"kart": k, "pos": str(p), "pits": str(d), "gap": g}
                for p, (k, d, g) in enumerate(rows, start=1)]

    def test_a_team_that_has_skipped_its_stops_is_not_really_leading(self):
        # P1 has taken 10 stops, P2 has taken 14, of 34. P1 owes four more,
        # each costing 200s, against a 30s lead on the road.
        v = self.app.virtual_positions(
            self.teams([("11", 10, ""), ("22", 14, "30.0")]), 34, 200.0, 60.0)
        self.assertEqual(v["22"]["virtual_pos"], 1)
        self.assertEqual(v["11"]["virtual_pos"], 2)
        self.assertEqual(v["11"]["stops_owed"], 24)
        self.assertAlmostEqual(v["11"]["debt_s"], 24 * 200.0)   # absolute, not relative
        self.assertAlmostEqual(v["22"]["debt_s"], 20 * 200.0)
        # P1 leads by 30s on the road but owes four more stops at 200s each,
        # which is what puts it behind once the debt is counted.
        self.assertGreater(v["11"]["debt_s"], v["22"]["debt_s"])

    def test_debt_is_never_negative(self):
        v = self.app.virtual_positions(
            self.teams([("11", 10, ""), ("22", 14, "30.0"), ("33", 34, "60.0")]),
            34, 200.0, 60.0)
        self.assertTrue(all(r["debt_s"] >= 0 for r in v.values()), v)

    def test_the_positions_run_from_one_with_no_gaps(self):
        v = self.app.virtual_positions(
            self.teams([("11", 10, ""), ("22", 14, "30.0"), ("33", 12, "45.0")]),
            34, 200.0, 60.0)
        self.assertEqual(sorted(r["virtual_pos"] for r in v.values()), [1, 2, 3])

    def test_equal_stops_keeps_the_road_order(self):
        v = self.app.virtual_positions(
            self.teams([("11", 12, ""), ("22", 12, "5.0"), ("33", 12, "9.0")]),
            34, 200.0, 60.0)
        self.assertEqual([v[k]["virtual_pos"] for k in ("11", "22", "33")], [1, 2, 3])

    def test_a_team_whose_gap_cannot_be_read_is_left_out_not_guessed(self):
        v = self.app.virtual_positions(
            self.teams([("11", 12, ""), ("22", 12, "?")]), 34, 200.0, 60.0)
        self.assertIn("11", v)
        self.assertNotIn("22", v)

    def test_stops_owed_never_goes_negative(self):
        v = self.app.virtual_positions(
            self.teams([("11", 40, "")]), 34, 200.0, 60.0)
        self.assertEqual(v["11"]["stops_owed"], 0)

    def test_an_empty_grid_is_not_a_crash(self):
        self.assertEqual(self.app.virtual_positions([], 34, 200.0, 60.0), {})


class TestLapHistory(AppCase):
    """Clicking a team opens every lap we have for it."""

    def laps_for(self, team_no):
        return json.loads(self.client.get(f"/api/laps/{team_no}").data)

    def record(self, team_no, kart, driver, times):
        import time as _t
        with self.app.POOL._con() as con:
            for i, t in enumerate(times):
                con.execute("INSERT INTO kart_lap(ts,team_no,pilot,kart,lap_s) "
                            "VALUES(?,?,?,?,?)",
                            (_t.time() + i, team_no, f"ALPHA|{driver}", kart, t))

    def test_laps_come_back_newest_first_with_the_best_marked(self):
        self.record("1", "17", "Dinis", [63.5, 62.1, 64.0])
        d = self.laps_for("1")
        self.assertEqual(d["count"], 3)
        self.assertEqual(d["best"], "1:02.100")
        self.assertEqual(d["laps"][0]["lap"], "1:04.000")   # newest first

    def test_the_driver_is_shown_not_the_storage_key(self):
        self.record("1", "17", "Dinis", [62.0])
        self.assertEqual(self.laps_for("1")["laps"][0]["pilot"], "Dinis")

    def test_the_kart_each_lap_was_driven_in_is_kept(self):
        self.record("1", "17", "Dinis", [62.0])
        self.record("1", "31", "Lobo", [61.5])
        self.assertEqual({l["kart"] for l in self.laps_for("1")["laps"]}, {"17", "31"})

    def test_a_team_with_no_laps_is_empty_not_an_error(self):
        d = self.laps_for("99")
        self.assertEqual((d["count"], d["laps"], d["best"]), (0, [], "-"))


class TestBoxNowVerdict(AppCase):
    """§3.13: the draw picks the lane, so the two queue fronts are the offer."""

    def lanes(self, *fronts):
        return [{"lane": i, "name": f"Lane {i}", "color": "#f00",
                 "karts": [{"num": n, "label": lbl, "delta": d}]}
                for i, (n, lbl, d) in enumerate(fronts, start=1)]

    def test_the_offer_is_the_front_of_each_queue(self):
        out = self.app.next_karts_out(self.lanes(("12", "Good", 0.1),
                                                 ("31", "Poor", 0.9)))
        self.assertEqual([k["num"] for k in out], ["12", "31"])
        self.assertEqual(out[0]["lane_name"], "Lane 1")

    def test_an_empty_lane_offers_nothing(self):
        self.assertEqual(self.app.next_karts_out([{"lane": 1, "karts": []}]), [])

    def test_both_better_is_a_clear_upgrade(self):
        out = self.app.next_karts_out(self.lanes(("12", "Good", 0.1), ("31", "Good", 0.2)))
        self.assertEqual(self.app.box_now_verdict(out, 0.5)["verdict"], "better")

    def test_both_worse_is_a_clear_downgrade(self):
        out = self.app.next_karts_out(self.lanes(("12", "Poor", 0.8), ("31", "Poor", 0.9)))
        self.assertEqual(self.app.box_now_verdict(out, 0.5)["verdict"], "worse")

    def test_one_each_way_is_a_gamble(self):
        # We cannot pick the lane, so this is the honest answer, not an average.
        out = self.app.next_karts_out(self.lanes(("12", "Good", 0.1), ("31", "Poor", 0.9)))
        self.assertEqual(self.app.box_now_verdict(out, 0.5)["verdict"], "mixed")

    def test_unrated_karts_say_so_rather_than_pretending(self):
        out = self.app.next_karts_out(self.lanes(("12", "Unknown", None),
                                                 ("31", "Unknown", None)))
        self.assertEqual(self.app.box_now_verdict(out, 0.5)["verdict"], "unknown")

    def test_not_knowing_our_own_kart_is_not_a_verdict(self):
        out = self.app.next_karts_out(self.lanes(("12", "Good", 0.1), ("31", "Poor", 0.9)))
        self.assertEqual(self.app.box_now_verdict(out, None)["verdict"], "unknown")


class TestPitClockIsOurOwnStopwatch(AppCase):
    """Apex times the box electronically; we only run a stopwatch beside it.

    Ours starts when the feed notices the stop or someone presses the button,
    which is always a little after the pit entry beam that scores us.  So the
    countdown holds the kart past the minimum, while the penalty shown is still
    measured against the regulation minimum itself.
    """

    def test_the_countdown_targets_the_minimum_plus_the_margin(self):
        R = self.app.CFG["race"]
        self.client.post("/api/pit/box", json={"offset_seconds": 0})
        s = self.snap()
        self.assertAlmostEqual(
            s["pit_remaining"],
            R["pit_duration_seconds"] + R["pit_safety_seconds"], delta=2)

    def test_the_minimum_is_not_called_met_at_the_bare_minimum(self):
        R = self.app.CFG["race"]
        # Entered the box exactly the regulation minimum ago: their clock may
        # not agree with ours, so we do not wave the kart out yet.
        self.client.post("/api/pit/box",
                         json={"offset_seconds": R["pit_duration_seconds"]})
        self.assertFalse(self.snap()["pit_min_met"])

    def test_it_is_met_once_the_margin_is_served(self):
        R = self.app.CFG["race"]
        self.client.post("/api/pit/box", json={"offset_seconds":
                         R["pit_duration_seconds"] + R["pit_safety_seconds"] + 1})
        self.assertTrue(self.snap()["pit_min_met"])

    def test_the_penalty_is_charged_against_the_rule_not_our_margin(self):
        R = self.app.CFG["race"]
        # Served the regulation minimum but not our margin: nothing is owed.
        self.client.post("/api/pit/box",
                         json={"offset_seconds": R["pit_duration_seconds"] + 1})
        self.assertEqual(self.snap()["pit_penalty_now"], 0)


class TestRaceControlLog(AppCase):
    """Apex sends race control's own messages; they used to be discarded."""

    def test_a_flagged_entry_is_read(self):
        got = self.app.parse_control_log(
            '<p><b>21:05</b><span data-flag="green"></span>Start</p>')
        self.assertEqual(got, [{"at": "21:05", "flag": "green", "text": "Start"}])

    def test_several_entries_keep_their_order(self):
        got = self.app.parse_control_log(
            '<p><b>21:05</b><span data-flag="green"></span>Start</p>'
            '<p><b>22:10</b><span data-flag="yellow"></span>SC deployed</p>')
        self.assertEqual([e["flag"] for e in got], ["green", "yellow"])
        self.assertEqual(got[1]["text"], "SC deployed")

    def test_an_entry_with_no_flag_still_counts(self):
        got = self.app.parse_control_log('<p><b>23:00</b>Kart 12 black flag</p>')
        self.assertEqual(got[0]["text"], "Kart 12 black flag")
        self.assertEqual(got[0]["flag"], "")

    def test_nothing_useful_is_nothing(self):
        self.assertEqual(self.app.parse_control_log(""), [])
        self.assertEqual(self.app.parse_control_log("<p></p>"), [])

    def test_it_reaches_the_session_state(self):
        self.app._process_meta({"com": None} if False else
                               {"control": [{"at": "21:05", "flag": "green",
                                             "text": "Start"}]})
        self.assertEqual(self.snap()["apex_session"]["control"][0]["text"], "Start")


class TestClassPositions(AppCase):
    """We are classified against AM, not against the PRO team leading on track."""

    def grid(self, rows):
        return [{"kart": k, "pos": str(p), "gap": g, "category": c}
                for p, (k, g, c) in enumerate(rows, start=1)]

    def test_position_is_counted_inside_the_category(self):
        v = self.app.class_positions(self.grid([
            ("11", "",      "PRO"), ("22", "10.0", "AM"),
            ("33", "15.0",  "PRO"), ("44", "25.0", "AM")]), 60.0)
        self.assertEqual((v["22"]["class"], v["22"]["class_pos"]), ("AM", 1))
        self.assertEqual(v["44"]["class_pos"], 2)
        self.assertEqual(v["11"]["class_pos"], 1)          # PRO has its own count

    def test_the_gap_that_matters_is_to_the_car_ahead_in_class(self):
        v = self.app.class_positions(self.grid([
            ("11", "",     "PRO"), ("22", "10.0", "AM"),
            ("33", "15.0", "PRO"), ("44", "25.0", "AM")]), 60.0)
        # 44 is 25s off the overall leader but only 15s off the AM team ahead.
        self.assertAlmostEqual(v["44"]["ahead_s"], 15.0)

    def test_the_class_leader_leads_by_nothing(self):
        v = self.app.class_positions(self.grid([
            ("11", "", "PRO"), ("22", "10.0", "AM")]), 60.0)
        self.assertIsNone(v["22"]["ahead_s"])   # nobody ahead in class

    def test_an_event_with_no_categories_is_one_class(self):
        v = self.app.class_positions(self.grid([
            ("11", "", ""), ("22", "10.0", ""), ("33", "20.0", "")]), 60.0)
        self.assertEqual(v["33"]["class_pos"], 3)
        self.assertAlmostEqual(v["33"]["ahead_s"], 10.0)

    def test_an_unreadable_gap_is_left_out(self):
        v = self.app.class_positions(self.grid([
            ("11", "", "AM"), ("22", "?", "AM")]), 60.0)
        self.assertNotIn("22", v)


class TestPitPlanCheck(AppCase):
    """A plan that cannot be driven should say so on Friday, not at 4am."""

    def race(self, **over):
        r = dict(self.app.CFG["race"])
        r.update(over)
        return r

    def plan(self, ids):
        return [{"driver_id": d, "note": ""} for d in ids]

    def drivers(self, *names):
        return [{"id": i, "name": n} for i, n in enumerate(names, start=1)]

    def test_our_own_am_numbers_are_drivable(self):
        # 25h, 34 stops, 60 min ceiling, six drivers sharing evenly.
        ids = [(i % 6) + 1 for i in range(34)]
        probs = self.app.check_pit_plan(
            self.plan(ids), self.race(),
            self.drivers("a", "b", "c", "d", "e", "f"))
        self.assertEqual([p for p in probs if p["level"] == "blocker"], [])

    def test_too_few_stops_for_the_stint_ceiling_is_a_blocker(self):
        # 25 hours over 6 stints is four hours a stint; the ceiling is one.
        probs = self.app.check_pit_plan(
            self.plan([1] * 5), self.race(mandatory_pits=5), self.drivers("a"))
        self.assertTrue(any(p["level"] == "blocker" and "limit" in p["text"]
                            for p in probs), probs)

    def test_a_driver_planned_under_their_minimum_is_a_blocker(self):
        # One stop for the joker, everything else to the other five.
        ids = [1] * 33 + [7]
        probs = self.app.check_pit_plan(
            self.plan(ids), self.race(),
            self.drivers("Balikó", "b", "c", "d", "e", "f", "Casinha"))
        self.assertTrue(any("Casinha" in p["text"] and p["level"] == "blocker"
                            for p in probs), probs)

    def test_unassigned_stops_are_flagged_but_not_fatal(self):
        probs = self.app.check_pit_plan(
            self.plan([1] * 30 + [None] * 4), self.race(), self.drivers("a"))
        self.assertTrue(any(p["level"] == "warn" and "no driver" in p["text"]
                            for p in probs), probs)

    def test_stops_that_cannot_fit_before_the_lane_shuts(self):
        probs = self.app.check_pit_plan(
            self.plan([1] * 34),
            self.race(duration_minutes=60, no_pit_last_minutes=30),
            self.drivers("a"))
        self.assertTrue(any("shuts" in p["text"] for p in probs), probs)

    def test_blockers_are_listed_before_warnings(self):
        ids = [1] * 33 + [None]
        probs = self.app.check_pit_plan(
            self.plan(ids), self.race(mandatory_pits=5), self.drivers("a"))
        levels = [p["level"] for p in probs]
        self.assertEqual(levels, sorted(levels, key=lambda l: l != "blocker"))

    def test_the_check_reaches_the_screen(self):
        self.client.post("/api/driver/add", json={"name": "Solo"})
        self.assertIn("plan_problems", self.snap())


class TestStrategyCalls(AppCase):
    """The call, and what overrules what."""

    def call(self, **kw):
        R = self.app.CFG["race"]
        a = dict(stint_s=10 * 60, race_elapsed_s=3 * 3600,
                 pits_done=4, my_avg5=None, prev_avg5=None)
        a.update(kw)
        return self.app.compute_strategy(**a)

    def offer(self, verdict):
        return {"verdict": verdict, "detail": "d"}

    def test_a_neutralised_track_is_the_cheapest_stop_of_the_race(self):
        for flag in ("ly", "lsc", "lr"):
            s = self.call(light=flag)
            self.assertEqual(s["label"], "BOX NOW", flag)
            self.assertIn("cheap", s["detail"])

    def test_a_flag_does_not_send_a_kart_in_that_just_came_out(self):
        # §3.10: a turn under 10 minutes is itself a penalty.
        self.assertNotEqual(self.call(light="lsc", stint_s=60)["label"], "BOX NOW")

    def test_a_flag_is_ignored_once_every_stop_is_served(self):
        s = self.call(light="lsc", pits_done=self.app.CFG["race"]["mandatory_pits"])
        self.assertNotEqual(s["label"], "BOX NOW")

    def test_the_stint_limit_outranks_a_good_kart_waiting(self):
        R = self.app.CFG["race"]
        s = self.call(stint_s=R["stint_max_minutes"] * 60 - 30,
                      box_now=self.offer("worse"))
        self.assertEqual(s["label"], "BOX NOW")
        self.assertIn("STINT LIMIT", s["detail"])

    def test_two_better_karts_waiting_pulls_a_due_stop_forward(self):
        R = self.app.CFG["race"]
        near = R["stint_max_minutes"] * 60 - 11 * 60
        self.assertEqual(self.call(stint_s=near, box_now=self.offer("better"))["label"],
                         "BOX NOW")

    def test_two_worse_karts_waiting_holds_a_stop_that_can_wait(self):
        R = self.app.CFG["race"]
        near = R["stint_max_minutes"] * 60 - 11 * 60
        self.assertEqual(self.call(stint_s=near, box_now=self.offer("worse"))["label"],
                         "WAIT")

    def test_the_kart_lottery_is_ignored_when_a_stop_is_not_due(self):
        self.assertNotEqual(self.call(stint_s=60, box_now=self.offer("better"))["label"],
                            "BOX NOW")

    def test_collapsed_pace_boxes_it(self):
        s = self.call(my_avg5=63.0, prev_avg5=62.0)
        self.assertEqual(s["label"], "BOX NOW")

    def test_every_call_says_why(self):
        for kw in ({}, {"light": "lsc"}, {"my_avg5": 63.0, "prev_avg5": 62.0},
                   {"stint_s": self.app.CFG["race"]["stint_max_minutes"] * 60}):
            self.assertTrue(self.call(**kw)["why"], kw)


class TestKartLapHistory(AppCase):
    """Clicking a kart shows the evidence behind its score."""

    def record(self, kart, pilots):
        import time as _t
        with self.app.POOL._con() as con:
            for i, (team, drv, t) in enumerate(pilots):
                con.execute("INSERT INTO kart_lap(ts,team_no,pilot,kart,lap_s) "
                            "VALUES(?,?,?,?,?)",
                            (_t.time() + i, team, f"T{team}|{drv}", kart, t))

    def test_it_reports_laps_best_and_average(self):
        self.record("17", [("1", "Ana", 62.0), ("1", "Ana", 61.0)])
        d = json.loads(self.client.get("/api/kart/17/laps").data)
        self.assertEqual(d["count"], 2)
        self.assertEqual(d["best"], "1:01.000")
        self.assertEqual(d["avg"], "1:01.500")

    def test_it_breaks_the_laps_down_by_driver(self):
        # A score resting on one driver is weaker than one several agree on.
        self.record("17", [("1", "Ana", 62.0), ("1", "Ana", 62.4),
                           ("2", "Lobo", 61.0)])
        d = json.loads(self.client.get("/api/kart/17/laps").data)
        by = {x["pilot"]: x for x in d["drivers"]}
        self.assertEqual(by["Ana"]["laps"], 2)
        self.assertEqual(by["Lobo"]["best"], "1:01.000")

    def test_a_kart_nobody_has_driven_is_empty_not_an_error(self):
        d = json.loads(self.client.get("/api/kart/99/laps").data)
        self.assertEqual((d["count"], d["best"], d["drivers"]), (0, "-", []))
        self.assertEqual(d["label"], "Unknown")


class TestDriverDetail(AppCase):
    """Clicking a driver shows their record, split by the kart they were in."""

    def setUp(self):
        super().setUp()
        self.client.post("/api/driver/add", json={"name": "Dinis"})
        self.did = self.snap()["drivers"][0]["id"]

    def detail(self, did=None):
        return json.loads(self.client.get(f"/api/driver/{did or self.did}/laps").data)

    def record(self, kart, times, who="Dinis"):
        import time as _t
        with self.app.POOL._con() as con:
            for i, t in enumerate(times):
                con.execute("INSERT INTO kart_lap(ts,team_no,pilot,kart,lap_s) "
                            "VALUES(?,?,?,?,?)",
                            (_t.time() + i, "1", f"TPC|{who}", kart, t))

    def test_laps_best_and_average(self):
        self.record("17", [62.0, 61.0, 63.0])
        d = self.detail()
        self.assertEqual((d["count"], d["best"]), (3, "1:01.000"))
        self.assertEqual(d["avg"], "1:02.000")

    def test_the_karts_they_had_are_broken_out(self):
        # A driver who looks slow may simply have drawn badly.
        self.record("17", [63.0, 63.2])
        self.record("31", [61.0, 61.1])
        by = {k["kart"]: k for k in self.detail()["karts"]}
        self.assertEqual(by["31"]["best"], "1:01.000")
        self.assertEqual(by["17"]["laps"], 2)

    def test_another_driver_s_laps_are_not_counted(self):
        self.record("17", [62.0], who="Dinis")
        self.record("17", [50.0], who="Lobo")
        self.assertEqual(self.detail()["count"], 1)

    def test_it_carries_the_time_still_owed(self):
        d = self.detail()
        self.assertEqual(d["owed_seconds"],
                         self.app.CFG["race"]["driver_min_minutes"] * 60)
        self.assertIn("owed_fmt", d)

    def test_a_driver_with_no_laps_is_empty_not_an_error(self):
        d = self.detail()
        self.assertEqual((d["count"], d["best"], d["karts"]), (0, "-", []))

    def test_an_unknown_driver_is_a_404(self):
        self.assertEqual(self.client.get("/api/driver/9999/laps").status_code, 404)


class TestBoxTimeCalibration(AppCase):
    """pit_loss_seconds starts as a guess; every stop measures part of it."""

    def hist(self, *box):
        return [{"box_s": b} for b in box]

    def test_nothing_served_yet_says_so(self):
        s = self.app.box_time_summary([], 200.0, 180.0)
        self.assertEqual(s["n"], 0)
        self.assertIn("estimate", s["note"])

    def test_it_reports_the_median(self):
        s = self.app.box_time_summary(self.hist(185, 190, 240), 200.0, 180.0)
        self.assertEqual((s["n"], s["median_s"]), (3, 190.0))

    def test_the_note_says_how_far_over_the_minimum_we_run(self):
        # The note is what the pit log shows, so that is what is pinned.
        s = self.app.box_time_summary(self.hist(190, 190), 200.0, 180.0)
        self.assertIn("+10s", s["note"])
        self.assertIn("3:10", s["note"])

    def test_stints_with_no_box_time_are_skipped(self):
        s = self.app.box_time_summary([{"box_s": None}, {"box_s": 190}], 200.0, 180.0)
        self.assertEqual(s["n"], 1)

    def test_a_stop_records_its_box_time_against_the_stint(self):
        self.client.post("/api/driver/add", json={"name": "Dinis"})
        did = self.snap()["drivers"][0]["id"]
        self.client.post("/api/driver/set", json={"driver_id": did})
        self.client.post("/api/race/start")
        self.client.post("/api/pit/box", json={"offset_seconds": 0})
        self.client.post("/api/pit/done", json={"driver_id": did})
        hist = self.snap()["pit_history"]
        self.assertTrue(hist, "no stint recorded")
        self.assertIsNotNone(hist[-1]["box_s"])


class TestKartWatch(AppCase):
    """Spotting a good kart by who is driving it unusually well."""

    def grid(self):
        return [{"kart": "7", "team": "MATRAX", "last_lap_s": 62.40},
                {"kart": "31", "team": "LAST", "last_lap_s": 63.40},
                {"kart": "19", "team": "MIDDLE", "last_lap_s": 64.90}]

    LEVELS = {"MATRAX": 0.0, "LAST": 2.50, "MIDDLE": 1.10}

    def test_the_surprising_kart_is_listed_first_not_the_fastest(self):
        # MATRAX is quicker on the road, but that is just MATRAX being MATRAX.
        # LAST a second off the reference, when they normally run 2.5s off,
        # is the kart doing the work.
        w = self.app.kart_watch(self.grid(), 62.40, 1.0, self.LEVELS)
        self.assertEqual(w["karts"][0]["kart"], "31")
        self.assertAlmostEqual(w["karts"][0]["beat_own_s"], 1.5)

    def test_teams_outside_the_window_are_not_listed(self):
        w = self.app.kart_watch(self.grid(), 62.40, 1.0, self.LEVELS)
        self.assertNotIn("19", [k["kart"] for k in w["karts"]])

    def test_the_reference_is_what_was_passed_not_the_race_best(self):
        # Over 25 hours a race best becomes unreachable, and a rule anchored to
        # it never fires again. The caller supplies a recent window.
        # At 62.40 the midfield kart is 2.5s away and invisible; once the
        # track slows and the reference moves to 64.00 it is only 0.9s off and
        # belongs in the list. Same laps, different reference.
        tight = self.app.kart_watch(self.grid(), 62.40, 1.0, self.LEVELS)
        self.assertNotIn("19", [k["kart"] for k in tight["karts"]])
        later = self.app.kart_watch(self.grid(), 64.00, 1.0, self.LEVELS)
        self.assertEqual(later["reference_s"], 64.0)
        self.assertIn("19", [k["kart"] for k in later["karts"]])

    def test_without_a_reference_it_says_nothing(self):
        w = self.app.kart_watch(self.grid(), None, 1.0, self.LEVELS)
        self.assertEqual(w["karts"], [])

    def test_without_levels_it_still_lists_who_is_close(self):
        w = self.app.kart_watch(self.grid(), 62.40, 1.0, {})
        self.assertEqual({k["kart"] for k in w["karts"]}, {"7", "31"})
        self.assertIsNone(w["karts"][0]["beat_own_s"])

    def test_a_team_with_no_lap_yet_is_skipped(self):
        w = self.app.kart_watch([{"kart": "5", "team": "X"}], 62.4, 1.0, {})
        self.assertEqual(w["karts"], [])


class TestSectorReview(AppCase):
    """A lap time says a driver is four tenths off; sectors say where."""

    def rows(self, pilot, s1, s2, s3=None, n=8):
        return [{"pilot": pilot, "s1": s1, "s2": s2, "s3": s3} for _ in range(n)]

    def test_it_names_the_sector_the_time_goes_in(self):
        team = (self.rows("TPC|Balikó", 21.6, 17.0, 28.0)
                + self.rows("TPC|Caxi", 21.2, 17.4, 28.0))
        mine = [r for r in team if r["pilot"].endswith("Balikó")]
        got = {x["sector"]: x for x in self.app.sector_review(mine, team)}
        self.assertAlmostEqual(got["Sector 1"]["loss"], 0.4, places=3)
        self.assertEqual(got["Sector 2"]["loss"], 0.0)   # theirs is the best
        self.assertEqual(got["Sector 3"]["loss"], 0.0)   # dead level

    def test_a_sector_with_too_few_laps_is_left_out(self):
        team = self.rows("TPC|Balikó", 21.6, 17.0, None)
        got = [x["sector"] for x in self.app.sector_review(team, team)]
        self.assertEqual(got, ["Sector 1", "Sector 2"])

    def test_one_blocked_lap_does_not_invent_a_weakness(self):
        team = self.rows("TPC|Balikó", 21.2, 17.0) + self.rows("TPC|Caxi", 21.2, 17.0)
        team[0] = dict(team[0], s1=40.0)          # a lap spent behind someone
        mine = [r for r in team if r["pilot"].endswith("Balikó")]
        got = {x["sector"]: x for x in self.app.sector_review(mine, team)}
        self.assertLess(got["Sector 1"]["loss"], 0.01)

    def test_no_sectors_at_all_is_an_empty_review_not_a_crash(self):
        self.assertEqual(self.app.sector_review([], []), [])

    def test_sectors_reach_the_driver_view_from_the_feed(self):
        """End to end: the feed's sector columns land on the finished lap."""
        self.client.post("/api/driver/add", json={"name": "Balikó"})
        did = self.client.get("/api/state").get_json()["drivers"][0]["id"]
        pool = self.app.POOL
        pool.set_kart("7", "ALPHA", "17")
        for lap in range(1, 12):
            pool.observe([{"kart": "7", "team": "ALPHA", "driver": "Balikó",
                           "pits": "0", "total_laps": str(lap),
                           "last_lap_s": 63.0, "pos": "1", "gap": "0.0",
                           "s1": "21.5", "s2": "17.1", "s3": "24.4"}],
                         now=1000.0 + lap * 65)
        got = self.client.get(f"/api/driver/{did}/laps").get_json()
        self.assertTrue(got["sectors"], "the feed sent sectors; they must arrive")
        self.assertEqual({x["sector"] for x in got["sectors"]},
                         {"Sector 1", "Sector 2", "Sector 3"})
        self.assertAlmostEqual(
            next(x for x in got["sectors"] if x["sector"] == "Sector 2")["median"],
            17.1, places=3)


class TestDriverFatigue(AppCase):
    """At 4am the rota is decided on who is rested, not on who is quickest."""

    def rest(self, stints, now="2026-09-20T04:00:00"):
        from datetime import datetime
        return self.app.rest_seconds(stints, datetime.fromisoformat(now))

    def test_rest_is_measured_from_the_last_stint_that_ended(self):
        got = self.rest([{"driver_id": 1, "end_ts": "2026-09-20T01:00:00"},
                         {"driver_id": 1, "end_ts": "2026-09-20T03:30:00"},
                         {"driver_id": 2, "end_ts": "2026-09-20T02:00:00"}])
        self.assertAlmostEqual(got["1"], 1800.0)
        self.assertAlmostEqual(got["2"], 7200.0)

    def test_a_driver_who_has_not_driven_has_no_reading(self):
        self.assertEqual(self.rest([]), {})

    def test_a_stint_still_running_is_not_rest(self):
        self.assertEqual(self.rest([{"driver_id": 1, "end_ts": None}]), {})

    def test_a_broken_timestamp_is_skipped_not_guessed(self):
        self.assertEqual(self.rest([{"driver_id": 1, "end_ts": "soon"}]), {})

    def test_fade_compares_the_last_laps_to_the_opening_ones(self):
        laps = [62.0] * 5 + [62.5] * 10 + [63.0] * 5
        self.assertAlmostEqual(self.app.stint_fade(laps), 1.0, places=3)

    def test_a_driver_finding_the_pace_reads_negative(self):
        self.assertLess(self.app.stint_fade([64.0] * 5 + [62.0] * 5), 0)

    def test_one_lap_in_traffic_is_not_fatigue(self):
        """A median, so a single blocked lap does not read as a driver dying."""
        laps = [62.0] * 9 + [75.0]
        self.assertAlmostEqual(self.app.stint_fade(laps), 0.0, places=3)

    def test_too_few_laps_says_nothing_rather_than_guessing(self):
        self.assertIsNone(self.app.stint_fade([62.0] * 9))
        self.assertIsNone(self.app.stint_fade([]))

    def test_nothing_is_said_early_in_a_stint(self):
        """Scruffy opening laps are not tiredness — wait for the stint to run."""
        self.assertIsNone(self.app.fatigue_note(1.2, stint_s=600,
                                                cfg=self.app.CFG["race"]))

    def test_a_real_drop_late_in_a_stint_is_called_out(self):
        note = self.app.fatigue_note(0.9, stint_s=2400, cfg=self.app.CFG["race"])
        self.assertEqual(note["level"], "warn")
        self.assertIn("+0.90s", note["text"])

    def test_holding_pace_is_worth_saying_too(self):
        note = self.app.fatigue_note(0.05, stint_s=2400, cfg=self.app.CFG["race"])
        self.assertEqual(note["level"], "good")

    def test_it_never_names_a_replacement(self):
        """The rota is the team's; the app reports the laps and stops there."""
        for fade in (-1.0, 0.0, 0.5, 2.0):
            note = self.app.fatigue_note(fade, 3000, self.app.CFG["race"])
            self.assertNotIn("swap", (note or {}).get("text", "").lower())
            self.assertNotIn("put ", (note or {}).get("text", "").lower())

    def test_the_driver_on_track_is_not_shown_as_rested(self):
        self.app.kv_set("status", "racing")
        self.client.post("/api/driver/add", json={"name": "Balikó"})
        did = self.client.get("/api/state").get_json()["drivers"][0]["id"]
        self.client.post("/api/driver/set", json={"driver_id": did})
        row = self.client.get("/api/state").get_json()["drivers"][0]
        self.assertTrue(row["active"])
        self.assertIsNone(row["rested_s"])


class TestPages(AppCase):
    def test_war_room_renders(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_pit_phone_renders(self):
        res = self.client.get("/pit")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Lane", res.data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
