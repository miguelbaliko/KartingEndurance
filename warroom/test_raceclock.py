#!/usr/bin/env python3
"""Race clock tests — mostly about not mistaking a lap time for the race time."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from raceclock import ApexClock, parse_clocks


class TestParse(unittest.TestCase):
    def test_pair(self):
        self.assertEqual(parse_clocks("10:15:40 / 24:00:00"), [36940, 86400])

    def test_lap_times_are_not_race_times(self):
        self.assertEqual(parse_clocks("1:02.478"), [])
        self.assertEqual(parse_clocks("best 1:01.821 last 1:03.702"), [])

    def test_mm_ss(self):
        self.assertEqual(parse_clocks("45:00"), [2700])

    def test_nothing(self):
        self.assertEqual(parse_clocks(""), [])
        self.assertEqual(parse_clocks("Qualifying"), [])


class TestClock(unittest.TestCase):
    def test_elapsed_over_total(self):
        c = ApexClock()
        self.assertTrue(c.update("10:15:40 / 24:00:00", now=1000))
        self.assertEqual(c.total, 86400)
        self.assertEqual(c.elapsed, 36940)
        self.assertEqual(c.remaining, 86400 - 36940)

    def test_countdown_is_recognised_by_its_direction(self):
        c = ApexClock()
        c.update("13:44:20", now=1000)
        c.update("13:44:10", now=1010)
        self.assertEqual(c.remaining, 49450)

    def test_count_up_is_recognised_too(self):
        c = ApexClock()
        c.update("0:10:00", now=1000)
        c.update("0:10:10", now=1010)
        self.assertEqual(c.elapsed, 610)

    def test_wording_wins_over_guessing(self):
        c = ApexClock()
        c.update("Restant 2:00:00", now=1000)
        self.assertEqual(c.remaining, 7200)

    def test_config_total_completes_a_one_sided_feed(self):
        c = ApexClock()
        c.update("Remaining 1:00:00", now=1000)
        c.set_total(24 * 3600)
        self.assertEqual(c.elapsed, 23 * 3600)

    def test_ticks_between_feed_updates(self):
        c = ApexClock()
        c.update("10:00:00 / 24:00:00", now=1000)
        state = c.state(now=1030)
        self.assertAlmostEqual(state["elapsed"], 36030, delta=0.5)
        self.assertAlmostEqual(state["remaining"], 86400 - 36030, delta=0.5)

    def test_goes_not_ok_when_the_feed_dies(self):
        c = ApexClock(stale_after=60)
        c.update("10:00:00 / 24:00:00", now=1000)
        self.assertTrue(c.ok(now=1050))
        self.assertFalse(c.ok(now=1200))

    def test_never_counts_past_the_flag(self):
        c = ApexClock()
        c.update("0:00:10 / 24:00:00", now=1000)
        c.update("Remaining 0:00:10", now=1000)
        self.assertEqual(c.state(now=1100)["remaining"], 0.0)

    def test_no_data_is_not_ok(self):
        self.assertFalse(ApexClock().ok())
        self.assertFalse(ApexClock().update("Qualifying"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
