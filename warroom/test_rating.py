#!/usr/bin/env python3
"""Rating model checks against a synthetic race whose kart truth is known.

The interesting failure mode is not "does it produce numbers" but "does it
still get the kart right when the fast teams happen to draw the good karts".
The unbalanced test below is built to punish exactly that.
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rating import rate, Baseline, fit_effects


def simulate(kart_eff, pilot_eff, draw, stints=14, laps=32, seed=7,
             drift=0.8, noise=0.25, start=0.0):
    """Run a mock endurance race and return lap samples.

    ``draw(stint, pilot_index) -> kart`` decides who gets which kart, which is
    how a test injects a deliberately unbalanced allocation.
    """
    rng = random.Random(seed)
    samples = []
    ts = start
    for stint in range(stints):
        # Track ramps in over the race, like rubber going down.
        base = 63.0 + drift * (stint / max(1, stints - 1))
        for pi, pilot in enumerate(pilot_eff):
            kart = draw(stint, pi)
            for lap in range(laps):
                lap_s = (base + pilot_eff[pilot] + kart_eff[kart]
                         + rng.gauss(0, noise))
                samples.append((ts + lap * 65 + pi * 0.7, pilot, kart, lap_s))
        ts += laps * 65
    return samples


class TestBaseline(unittest.TestCase):
    def test_tracks_drift_and_ignores_sparse_windows(self):
        samples = [(t * 60.0, "p", "k", 60.0 + t * 0.01) for t in range(600)]
        base = Baseline(samples, bucket_s=600, min_laps=5)
        self.assertAlmostEqual(base.at(0), 60.05, delta=0.2)
        self.assertAlmostEqual(base.at(35999), 65.9, delta=0.3)
        # Outside the sampled range the ends clamp rather than extrapolate.
        self.assertAlmostEqual(base.at(-10_000), base.at(0), delta=0.01)

    def test_empty(self):
        self.assertEqual(Baseline([], 600, 5).at(0), 0.0)


class TestFit(unittest.TestCase):
    def test_separates_two_factors(self):
        obs = [("fast", "good", -1.5, 30), ("fast", "bad", 0.5, 30),
               ("slow", "good", 0.5, 30), ("slow", "bad", 2.5, 30)]
        pilots, karts = fit_effects(obs, 0.01, 0.01, 60)
        self.assertAlmostEqual(karts["bad"] - karts["good"], 2.0, delta=0.05)
        self.assertAlmostEqual(pilots["slow"] - pilots["fast"], 2.0, delta=0.05)


class TestPilotKeys(unittest.TestCase):
    """A driver only earns their own effect once they have laps to back it."""

    def test_a_fresh_driver_is_scored_as_their_team(self):
        from rating import _pilot_keys
        samples = [(0.0, "ALPHA|Ana", "K1", 63.0)] * 5
        self.assertEqual(_pilot_keys(samples, 30)["ALPHA|Ana"], "ALPHA")

    def test_a_driver_with_a_real_sample_keeps_their_own(self):
        from rating import _pilot_keys
        samples = [(0.0, "ALPHA|Ana", "K1", 63.0)] * 40
        self.assertEqual(_pilot_keys(samples, 30)["ALPHA|Ana"], "ALPHA|Ana")

    def test_events_without_driver_names_are_untouched(self):
        from rating import _pilot_keys
        samples = [(0.0, "ALPHA", "K1", 63.0)] * 3
        self.assertEqual(_pilot_keys(samples, 30)["ALPHA"], "ALPHA")

    def test_a_race_of_one_stint_per_driver_is_still_rateable(self):
        """Without the fold this is eight unconnected pairs and no ratings."""
        karts = [f"K{i}" for i in range(8)]
        samples, ts = [], 0.0
        for stint in range(6):
            for team in range(8):
                kart = karts[(stint + team) % 8]
                for lap in range(20):
                    samples.append((ts + lap * 65, f"T{team}|D{stint}", kart,
                                    63.0 + 0.1 * team + 0.2 * ((stint + team) % 8)))
            ts += 1300
        res = rate(samples)["karts"]
        self.assertEqual(sum(1 for k in res.values() if k["rated"]), 8)


class TestRate(unittest.TestCase):
    KARTS = {"K1": -0.45, "K2": -0.30, "K3": -0.10, "K4": 0.0,
             "K5": 0.15, "K6": 0.40, "K7": 0.75, "K8": 1.30}
    PILOTS = {"PRO-A": -0.90, "PRO-B": -0.70, "MID-C": -0.10,
              "MID-D": 0.05, "AM-E": 0.85, "AM-F": 1.40,
              "AM-G": 1.10, "MID-H": 0.20}

    def _rotating(self):
        karts = list(self.KARTS)
        return lambda stint, pi: karts[(stint + pi) % len(karts)]

    def test_recovers_kart_order(self):
        res = rate(simulate(self.KARTS, self.PILOTS, self._rotating()))
        got = sorted(res["karts"], key=lambda k: res["karts"][k]["effect"])
        self.assertEqual(got, sorted(self.KARTS, key=self.KARTS.get))

    def test_spread_is_not_crushed_by_shrinkage(self):
        res = rate(simulate(self.KARTS, self.PILOTS, self._rotating()))
        got = res["karts"]["K8"]["effect"] - res["karts"]["K1"]["effect"]
        true = self.KARTS["K8"] - self.KARTS["K1"]
        self.assertGreater(got, true * 0.75)

    def test_best_kart_reads_zero_and_labels_are_ordered(self):
        res = rate(simulate(self.KARTS, self.PILOTS, self._rotating()))
        self.assertAlmostEqual(res["karts"]["K1"]["delta"], 0.0, delta=0.01)
        self.assertEqual(res["karts"]["K1"]["label"], "Rocket")
        self.assertEqual(res["karts"]["K8"]["label"], "Bad")
        for k in res["karts"].values():
            self.assertGreaterEqual(k["delta"], -0.001)

    def test_good_karts_handed_to_slow_teams_are_still_rated_good(self):
        """The whole point of the model.

        The two quickest karts go to the three slowest teams most of the time,
        and the two worst karts to the quickest teams.  On raw lap time K1 and
        K2 then look like the worst karts on the grid; the model has to see
        past that.  The allocation stays leaky — every kart reaches a mid-field
        team now and then — because that is what makes the two effects
        separable at all, and what the pit-lane queues produce in a real race.
        """
        slow = ["AM-E", "AM-F", "AM-G"]
        fast = ["PRO-A", "PRO-B"]
        pilots = list(self.PILOTS)
        all_karts = list(self.KARTS)
        mid_karts = ["K3", "K4", "K5", "K6"]

        def draw(stint, pi):
            pilot = pilots[pi]
            # Every fourth stint the field mixes, which is what links the graph.
            if stint % 4 == 3:
                return all_karts[(stint + pi) % len(all_karts)]
            if pilot in slow:
                return ["K1", "K2"][(stint + pi) % 2]
            if pilot in fast:
                return ["K7", "K8"][(stint + pi) % 2]
            return mid_karts[(stint + pi) % len(mid_karts)]

        samples = simulate(self.KARTS, self.PILOTS, draw)

        raw = {}
        for _ts, _p, kart, lap_s in samples:
            raw.setdefault(kart, []).append(lap_s)
        raw_rank = sorted(raw, key=lambda k: sum(raw[k]) / len(raw[k]))
        self.assertGreater(raw_rank.index("K1"), 3,
                           "raw lap time should be fooled — otherwise the test is void")

        res = rate(samples)["karts"]
        self.assertIn(res["K1"]["label"], ("Rocket", "Very Good"))
        self.assertLess(res["K1"]["effect"], res["K7"]["effect"])
        self.assertLess(res["K2"]["effect"], res["K8"]["effect"])
        self.assertEqual(res["K8"]["label"], "Bad")

    def test_karts_never_shared_are_not_ranked(self):
        """No swaps means no way to tell kart from driver — say so, don't guess.

        Each team keeps one kart for the whole run, so the pilot↔kart graph
        falls into eight disconnected pairs.  A model that ranked them would be
        ranking the drivers.
        """
        pilots = list(self.PILOTS)
        karts = list(self.KARTS)
        res = rate(simulate(self.KARTS, self.PILOTS,
                            lambda stint, pi: karts[pi]))["karts"]
        for kart, info in res.items():
            self.assertEqual(info["label"], "Unknown", kart)
            self.assertEqual(info["reason"], "not yet shared with the field")
        # No kart is comparable to any other, so none is treated as linked.
        self.assertEqual(sum(1 for i in res.values() if i["linked"]), 0)

    def test_thin_karts_report_unknown(self):
        samples = simulate(self.KARTS, self.PILOTS, self._rotating())
        samples += [(300_000.0 + i * 65, "PRO-A", "K9", 63.5) for i in range(3)]
        res = rate(samples)["karts"]
        self.assertFalse(res["K9"]["rated"])
        self.assertEqual(res["K9"]["label"], "Unknown")
        self.assertIsNone(res["K9"]["delta"])
        self.assertIn("clean laps", res["K9"]["reason"])

    def test_kart_whose_laps_are_all_outliers_is_still_listed(self):
        samples = simulate(self.KARTS, self.PILOTS, self._rotating())
        samples += [(300_000.0 + i * 65, "PRO-A", "K9", 140.0) for i in range(4)]
        res = rate(samples)["karts"]
        self.assertEqual(res["K9"]["laps"], 0)
        self.assertEqual(res["K9"]["raw_laps"], 4)
        self.assertEqual(res["K9"]["label"], "Unknown")

    def test_single_driver_kart_is_flagged_weak(self):
        samples = simulate(self.KARTS, self.PILOTS, self._rotating())
        samples += [(300_000.0 + i * 65, "PRO-A", "K9", 63.5) for i in range(40)]
        res = rate(samples)["karts"]
        self.assertTrue(res["K9"]["weak"])
        self.assertFalse(res["K1"]["weak"])

    def test_traffic_laps_do_not_move_a_kart(self):
        base = simulate(self.KARTS, self.PILOTS, self._rotating())
        clean = rate(base)["karts"]["K1"]["effect"]
        dirty = list(base)
        dirty += [(t * 65.0, "PRO-A", "K1", 95.0) for t in range(30)]
        self.assertAlmostEqual(rate(dirty)["karts"]["K1"]["effect"], clean,
                               delta=0.08)

    def test_empty_input(self):
        res = rate([])
        self.assertEqual(res["karts"], {})
        self.assertEqual(res["n_laps"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
