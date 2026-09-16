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


class TestABareMillisecondCounter(unittest.TestCase):
    """KIP sends the clock as a bare number of milliseconds, not h:mm:ss.

    Both Apex installs we have sampled do it — KIP Palmela's dyn1 went
    928012 -> 897863 -> 867412 thirty seconds apart, which is a thousand a
    second counting down.  Before this the colon pattern saw nothing there and
    we ran the whole race on our own clock, which drifts and knows nothing
    about a red flag stopping the countdown.
    """

    def feed(self, values, step=30.0, clock=None):
        c, t = clock or ApexClock(), 1000.0
        reads = []
        for v in values:
            reads.append(c.update(v, now=t))
            t += step
        return c, reads

    def test_kips_real_sequence_reads_as_time_remaining(self):
        c, reads = self.feed(["928012", "897863", "867412"])
        self.assertEqual(reads, [False, True, True],
                         "the first sighting proves nothing on its own")
        self.assertAlmostEqual(c.remaining, 867.412, places=3)
        self.assertIsNone(c.elapsed)

    def test_the_cadence_kip_actually_sends(self):
        """Taken off the live feed: 22s, then 47s, then 11s between frames.

        The tower steps the counter down by a flat 30,000 each time it
        republishes, but the frames arrive whenever the poll lands, so
        consecutive readings gave 1368, 641 and 2727 a second for one clock.
        Judging pairs reads the polling jitter; judging against a held anchor
        reads the clock.
        """
        c = ApexClock()
        for at, v in ((0, "358036"), (22, "327941"), (69, "297801"), (80, "267802")):
            c.update(v, now=1000.0 + at)
        self.assertAlmostEqual(c.remaining, 267.802, places=3)

    def test_a_slow_counter_at_that_cadence_is_still_refused(self):
        c = ApexClock()
        for at, v in ((0, "118"), (22, "119"), (69, "121"), (80, "122"), (300, "130")):
            self.assertFalse(c.update(v, now=1000.0 + at), f"at {at}s")
        self.assertIsNone(c.remaining)

    def test_a_counter_going_up_is_elapsed(self):
        c, _ = self.feed(["60000", "90000", "120000"])
        self.assertAlmostEqual(c.elapsed, 120.0, places=3)

    def test_a_lap_count_is_not_a_clock(self):
        """It moves, but nowhere near a thousand a second."""
        c, reads = self.feed(["118", "119", "120", "121"])
        self.assertEqual(reads, [False] * 4)
        self.assertIsNone(c.remaining)
        self.assertIsNone(c.elapsed)

    def test_a_big_number_at_the_wrong_rate_is_not_a_clock(self):
        c, reads = self.feed(["152400", "156210", "160020"])
        self.assertEqual(reads, [False] * 3)
        self.assertIsNone(c.elapsed)

    def test_a_red_flag_freezing_it_does_not_lose_the_clock(self):
        """Confirmed once, kept — a stopped clock is when it matters most."""
        c, _ = self.feed(["928012", "897863", "897863", "897863"])
        self.assertAlmostEqual(c.remaining, 897.863, places=3)

    def test_the_written_out_form_is_untouched(self):
        c = ApexClock()
        self.assertTrue(c.update("0:45:00 / 25:00:00", now=1000.0))
        self.assertEqual(c.total, 90000)
        self.assertAlmostEqual(c.elapsed, 2700.0)

    def test_a_lap_time_is_still_never_a_race_clock(self):
        c = ApexClock()
        self.assertFalse(c.update("Best lap 1:02.478", now=1000.0))
