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

import rating
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

    def _towed(self, kart, n, clean_every, quicker=0.8, start=1000.0):
        """Laps in ``kart`` by four drivers, most of them run in a tow.

        One lap in ``clean_every`` is run in clear air; ``clean_every`` of 0
        means the kart was never once seen on its own.  A towed lap is
        ``quicker`` seconds faster than the kart deserves, which is what makes
        a merely average kart look like a rocket.
        """
        rng = random.Random(11)
        out = []
        for i in range(n):
            pilot = ["PRO-A", "PRO-B", "MID-C", "MID-D"][i % 4]
            clean = bool(clean_every) and i % clean_every == 0
            out.append((start + i * 65, pilot, kart,
                        63.0 + self.PILOTS[pilot] - (0.0 if clean else quicker)
                        + rng.gauss(0, 0.15),
                        3.5 if clean else 0.25))
        return out

    def test_a_kart_only_ever_seen_in_a_tow_is_not_read_as_quick(self):
        """The whole point of the gap check.

        Every lap of K9 was run within a length of the kart ahead, so every one
        of them is 0.8s quicker than the kart is.  Without the gap the model
        calls it the best kart on the island; with it, it admits it has not
        seen the kart run on its own.
        """
        base = simulate(self.KARTS, self.PILOTS, self._rotating())
        base = [(t, p, k, s, None) for t, p, k, s in base]
        towed = self._towed("K9", 60, clean_every=0)

        blind = rate(base + [(t, p, k, s, None) for t, p, k, s, _ in towed])
        self.assertIn(blind["karts"]["K9"]["label"], ("Rocket", "Very Good"),
                      "test is void unless the tow really does fool it")

        seeing = rate(base + towed)["karts"]["K9"]
        self.assertEqual(seeing["label"], "Unknown")
        self.assertEqual(seeing["reason"], "only ever seen in a tow")
        self.assertEqual(seeing["clean_laps"], 0)
        self.assertEqual(seeing["tow_laps"], 60)

    def test_a_kart_with_clean_laps_is_read_on_those(self):
        """Two thirds of K9's laps are towed; the clean third is the truth."""
        base = simulate(self.KARTS, self.PILOTS, self._rotating())
        base = [(t, p, k, s, None) for t, p, k, s in base]
        mixed = self._towed("K9", 60, clean_every=3)

        blind = rate(base + [(t, p, k, s, None) for t, p, k, s, _ in mixed])
        seeing = rate(base + mixed)["karts"]["K9"]

        # K9 was built at the same pace as a mid-pack kart, so the honest
        # reading is near the middle, not at the front.
        self.assertLess(blind["karts"]["K9"]["effect"], -0.3)
        self.assertGreater(seeing["effect"], -0.1)
        self.assertTrue(seeing["rated"])
        self.assertEqual(seeing["clean_laps"], 20)
        self.assertEqual(seeing["tow_laps"], 40)
        # Only the clean laps count towards knowing the kart.
        self.assertEqual(seeing["laps"], 20)

    def test_a_feed_without_gaps_rates_exactly_as_before(self):
        """Nobody loses anything when the gap column is missing."""
        base = simulate(self.KARTS, self.PILOTS, self._rotating())
        four = rate(base)["karts"]
        five = rate([(t, p, k, s, None) for t, p, k, s in base])["karts"]
        for kart in four:
            self.assertAlmostEqual(four[kart]["effect"], five[kart]["effect"],
                                   places=6, msg=kart)

    def test_a_grade_on_thin_evidence_is_marked(self):
        """Measured: a genuinely bad kart reads OK until about thirty clean laps.

        Shrinkage pulls a thinly-sampled kart towards the middle of the fleet,
        so the ones that move are the extremes — and the direction that costs
        us is the bad kart flattering itself into an OK a driver gets sent out
        in.  The grade is still shown; it is shown as provisional.
        """
        base = simulate(self.KARTS, self.PILOTS, self._rotating())
        thin = base + [(300_000.0 + i * 65, p, "K9", 63.9)
                       for i, p in enumerate(["PRO-A", "PRO-B", "MID-C"] * 4)]
        got = rate(thin)["karts"]["K9"]
        self.assertTrue(got["rated"], "twelve clean laps is still a grade")
        self.assertTrue(got["thin"], "…but one that can still move")

    def test_a_grade_the_whole_race_backs_is_not_marked(self):
        res = rate(simulate(self.KARTS, self.PILOTS, self._rotating()))["karts"]
        self.assertFalse(res["K1"]["thin"])
        self.assertFalse(res["K8"]["thin"])

    def test_thin_is_not_the_same_as_unrated(self):
        """An Unknown kart has no grade at all; a thin one has a provisional one."""
        samples = simulate(self.KARTS, self.PILOTS, self._rotating())
        samples += [(300_000.0 + i * 65, "PRO-A", "K9", 63.5) for i in range(3)]
        got = rate(samples)["karts"]["K9"]
        self.assertFalse(got["rated"])
        self.assertFalse(got["thin"], "nothing to qualify — there is no grade")

    def test_towed_laps_do_not_make_a_grade_look_solid(self):
        """The evidence that counts is clean, here as everywhere else."""
        base = [(t, p, k, s, None) for t, p, k, s in
                simulate(self.KARTS, self.PILOTS, self._rotating())]
        twelve_clean = [(300_000.0 + i * 65, p, "K9", 63.9, 3.5)
                        for i, p in enumerate(["PRO-A", "PRO-B", "MID-C"] * 4)]
        towed = [(400_000.0 + i * 65, "PRO-A", "K9", 63.1, 0.3) for i in range(80)]
        got = rate(base + twelve_clean + towed)["karts"]["K9"]
        self.assertTrue(got["thin"], "eighty tows are not thirty clean laps")

    def test_the_tow_threshold_sits_where_the_tow_does(self):
        """Not a free parameter — both ends of it cost something measurable.

        Karting's published slipstream range is two to five kart lengths for
        the full effect, nothing left by ten to fifteen; at Palmela's speeds
        that is 0.14-0.35s of gap, gone by 0.70-1.06s.  Too narrow and a kart
        seen only in traffic is graded anyway; too wide and clean laps are
        thrown away for an effect that is already zero out there.
        """
        from rating import DEFAULTS
        self.assertGreaterEqual(DEFAULTS["tow_gap_s"], 0.5,
                                "under this, an all-tow kart still gets a grade")
        self.assertLessEqual(DEFAULTS["tow_gap_s"], 1.06,
                             "past this the slipstream is gone; the laps are clean")

    def test_a_kart_seen_only_in_traffic_is_refused_at_this_threshold(self):
        """The floor of the range above, checked rather than asserted."""
        base = [(t, p, k, s, None) for t, p, k, s in
                simulate(self.KARTS, self.PILOTS, self._rotating())]
        towed = self._towed("K9", 40, clean_every=0)
        self.assertEqual(rate(base + towed)["karts"]["K9"]["label"], "Unknown")

    def test_a_kart_a_length_or_two_clear_is_not_called_towed(self):
        """The ceiling: a kart running its own race must keep its clean laps."""
        base = [(t, p, k, s, None) for t, p, k, s in
                simulate(self.KARTS, self.PILOTS, self._rotating())]
        clear = [(300_000.0 + i * 65, p, "K9", 63.4, 1.2)
                 for i, p in enumerate(["PRO-A", "PRO-B", "MID-C"] * 12)]
        got = rate(base + clear)["karts"]["K9"]
        self.assertEqual(got["tow_laps"], 0, "1.2s back is not a tow")
        self.assertTrue(got["rated"])

    def test_a_shove_from_behind_is_counted(self):
        """The other half of the interval: who is close behind, not in front."""
        base = [(t, p, k, s, None, None) for t, p, k, s in
                simulate(self.KARTS, self.PILOTS, self._rotating())]
        shoved = [(300_000.0 + i * 65, p, "K9", 63.4, 5.0, 0.2)
                  for i, p in enumerate(["PRO-A", "PRO-B", "MID-C"] * 12)]
        got = rate(base + shoved)["karts"]["K9"]
        self.assertEqual(got["push_laps"], 36)
        self.assertEqual(got["tow_laps"], 0, "nobody was in front of them")

    def test_a_shove_never_moves_the_grade(self):
        """Counted and shown, never scored.

        A shove does make a lap quicker, but we have no measurement of by how
        much — so discounting those laps would be a guess, and it would cost
        clean evidence to make it.  The number is there to be read; the grade
        comes out of the same laps either way.
        """
        base = [(t, p, k, s, None, None) for t, p, k, s in
                simulate(self.KARTS, self.PILOTS, self._rotating())]
        laps = [(300_000.0 + i * 65, p, "K9", 63.4, 5.0)
                for i, p in enumerate(["PRO-A", "PRO-B", "MID-C"] * 12)]
        alone = rate(base + [(t, p, k, s, a, 9.0) for t, p, k, s, a in laps])
        shoved = rate(base + [(t, p, k, s, a, 0.2) for t, p, k, s, a in laps])
        self.assertEqual(alone["karts"]["K9"]["effect"],
                         shoved["karts"]["K9"]["effect"])
        self.assertEqual(alone["karts"]["K9"]["laps"],
                         shoved["karts"]["K9"]["laps"])

    def test_a_lap_can_be_towed_and_shoved_at_once(self):
        """The middle of a train is both, and each is counted on its own."""
        base = [(t, p, k, s, None, None) for t, p, k, s in
                simulate(self.KARTS, self.PILOTS, self._rotating())]
        train = [(300_000.0 + i * 65, p, "K9", 63.0, 0.3, 0.2)
                 for i, p in enumerate(["PRO-A", "PRO-B", "MID-C"] * 12)]
        got = rate(base + train)["karts"]["K9"]
        self.assertEqual(got["tow_laps"], 36)
        self.assertEqual(got["push_laps"], 36)

    def test_a_feed_that_sends_neither_gap_still_rates(self):
        base = simulate(self.KARTS, self.PILOTS, self._rotating())
        got = rate(base)["karts"]["K1"]
        self.assertTrue(got["rated"])
        self.assertEqual((got["tow_laps"], got["push_laps"]), (0, 0))

    def test_the_shove_threshold_is_closer_than_a_tow(self):
        """Contact, not air: a kart two lengths back is touching you."""
        from rating import DEFAULTS
        self.assertLess(DEFAULTS["push_gap_s"], DEFAULTS["tow_gap_s"])

    def test_empty_input(self):
        res = rate([])
        self.assertEqual(res["karts"], {})
        self.assertEqual(res["n_laps"], 0)



def noisy_race(seed, n_laps=160):
    """A race whose kart truth is known, driven by steady and erratic teams.

    The erratic teams are no slower on average, only less repeatable, so pace
    alone cannot tell them apart — only the spread of their laps can.
    """
    import random
    rng = random.Random(seed)
    karts = {f"K{i}": round(rng.uniform(-0.8, 0.8), 3) for i in range(1, 11)}
    teams = [("STEADY1", -0.3, 0.08), ("STEADY2", 0.0, 0.09), ("STEADY3", 0.2, 0.07),
             ("WILD1", -0.1, 0.75), ("WILD2", 0.1, 0.85), ("WILD3", 0.3, 0.80)]
    samples, t = [], 0.0
    for lap in range(n_laps):
        for name, pace, noise in teams:
            kart = f"K{(lap // 8 + hash(name) % 10) % 10 + 1}"
            t += 3
            samples.append((t, name, kart, 62.0 + karts[kart] + pace
                            + rng.gauss(0, noise)))
    return karts, samples


def mean_error(truth, res):
    import statistics
    got = {k: res["karts"][k]["effect"] for k in truth if k in res["karts"]}
    if not got:
        return 99.0
    level = statistics.mean(got[k] - truth[k] for k in got)
    return statistics.mean(abs((got[k] - truth[k]) - level) for k in got)


class TestWhoseLapsCount(unittest.TestCase):
    """A kart's score is only as good as the driving that measured it."""

    def test_steady_drivers_sharpen_the_scores(self):
        import statistics
        off, on = [], []
        for seed in range(6):
            truth, samples = noisy_race(seed)
            off.append(mean_error(truth, rating.rate(
                samples, {"weight_by_consistency": False, "min_laps": 5})))
            on.append(mean_error(truth, rating.rate(
                samples, {"weight_by_consistency": True, "min_laps": 5})))
        self.assertLess(statistics.mean(on), statistics.mean(off),
                        f"weighted {statistics.mean(on):.3f} vs "
                        f"unweighted {statistics.mean(off):.3f}")

    def test_excluding_the_erratic_teams_costs_more_than_it_saves(self):
        """Documented on purpose: the exclude list is a foot-gun.

        Dropping a team removes its laps *and* the kart-to-driver links that
        let the model separate a slow kart from a slow driver.  Measured, that
        is worse than leaving the noisy laps in and down-weighting them.
        """
        import statistics
        keep, drop = [], []
        for seed in range(6):
            truth, samples = noisy_race(seed)
            keep.append(mean_error(truth, rating.rate(
                samples, {"weight_by_consistency": True, "min_laps": 5})))
            drop.append(mean_error(truth, rating.rate(
                samples, {"exclude_teams": ["WILD1", "WILD2", "WILD3"],
                          "weight_by_consistency": True, "min_laps": 5})))
        self.assertLess(statistics.mean(keep), statistics.mean(drop))

    def test_an_excluded_team_contributes_nothing(self):
        _truth, samples = noisy_race(1)
        res = rating.rate(samples, {"exclude_teams": ["WILD1"], "min_laps": 5})
        self.assertNotIn("WILD1", res["pilots"])

    def test_exclusion_is_case_and_space_insensitive(self):
        _truth, samples = noisy_race(1)
        res = rating.rate(samples, {"exclude_teams": ["  wild1 "], "min_laps": 5})
        self.assertNotIn("WILD1", res["pilots"])

    def test_the_team_is_read_out_of_the_pilot_key(self):
        self.assertEqual(rating.team_of("TPC|Dinis"), "TPC")
        self.assertEqual(rating.team_of("TPC"), "TPC")
        self.assertEqual(rating.team_of(""), "")

    def test_nobody_is_silenced_and_nobody_decides_alone(self):
        _truth, samples = noisy_race(2)
        cfg = rating.cfg_with_defaults({})
        base = rating.Baseline(samples, cfg["bucket_minutes"] * 60.0,
                               cfg["min_bucket_laps"])
        seg = {}
        for ts, pilot, kart, lap_s in samples:
            seg.setdefault((pilot, kart), []).append(lap_s - base.at(ts))
        w = rating.consistency_weights(seg, cfg)
        self.assertTrue(w)
        self.assertTrue(all(cfg["weight_floor"] <= v <= cfg["weight_ceiling"]
                            for v in w.values()), w)
        # The steady teams must outweigh the wild ones, which is the point.
        self.assertGreater(min(w[p] for p in w if p.startswith("STEADY")),
                           max(w[p] for p in w if p.startswith("WILD")))

    def test_turning_the_weighting_off_leaves_it_alone(self):
        _truth, samples = noisy_race(3)
        cfg = rating.cfg_with_defaults({"weight_by_consistency": False})
        self.assertEqual(rating.consistency_weights({}, cfg), {})



class TestFade(unittest.TestCase):
    """A kart's average cannot say whether it holds pace across a stint."""

    def race(self, fade_per_lap, laps=15, runs=6, seed=3):
        import random
        rng = random.Random(seed)
        samples, t = [], 0.0
        for _ in range(runs):
            for team in ("A", "B", "C"):
                for kart, f in (("STEADY", 0.0), ("FADER", fade_per_lap)):
                    for lap in range(laps):
                        t += 3
                        samples.append((t, team, kart,
                                        62.0 - (0.6 if kart == "FADER" else 0)
                                        + lap * f + rng.gauss(0, 0.08)))
        return rating.rate(samples, {"min_laps": 5})["karts"]

    def test_a_kart_that_goes_off_is_caught(self):
        k = self.race(0.08)
        self.assertGreater(k["FADER"]["fade_s"], 0.5)
        self.assertLess(abs(k["STEADY"]["fade_s"]), 0.15)

    def test_the_average_alone_would_not_have_shown_it(self):
        # Both karts are set up to rate the same overall; only fade separates
        # them, which is the whole reason the number exists.
        k = self.race(0.08)
        self.assertLess(abs(k["FADER"]["delta"] - k["STEADY"]["delta"]), 0.15)

    def test_a_steady_fleet_shows_no_fade(self):
        k = self.race(0.0)
        self.assertLess(abs(k["FADER"]["fade_s"]), 0.15)

    def test_short_runs_say_nothing_rather_than_guessing(self):
        k = self.race(0.08, laps=5)
        self.assertIsNone(k["FADER"]["fade_s"])
        self.assertEqual(k["FADER"]["fade_runs"], 0)

    def test_a_run_ends_when_the_kart_or_the_driver_changes(self):
        cfg = rating.cfg_with_defaults({})
        samples = ([(i, "A", "K1", 62.0) for i in range(1, 11)]
                   + [(i, "A", "K2", 62.0) for i in range(11, 21)]
                   + [(i, "B", "K2", 62.0) for i in range(21, 31)])
        base = rating.Baseline(samples, cfg["bucket_minutes"] * 60.0, 1)
        runs = rating.runs_from(samples, base, cfg)
        self.assertEqual([k for k, _ in runs], ["K1", "K2", "K2"])
        self.assertTrue(all(len(r) == 10 for _k, r in runs))



class TestASlowDriverDoesNotCondemnTheKart(unittest.TestCase):
    """The worry: a novice or a heavy driver from the last-placed team takes a
    good kart, posts poor laps, and the app calls the kart bad.

    It does not, and these pin why.  The model fits a per-driver effect, so it
    learns the team is slow instead of blaming the kart; and where it cannot
    tell the two apart it says Unknown rather than guessing.  The lap cap is
    there for the residue, not as the defence.
    """

    def race(self, cfg, shared=True, novice_off=3.0):
        import random
        rng = random.Random(11)
        karts = {"GOOD": -0.6, "MID": 0.0, "POOR": 0.7}
        fast = [("FAST1", -0.3), ("FAST2", -0.1), ("FAST3", 0.1)]
        samples, t = [], 0.0
        for block in range(8):
            for team, pace in fast:
                pool = list(karts) if shared else ["MID", "POOR"]
                kart = pool[(block + hash(team) % len(pool)) % len(pool)]
                for _ in range(12):
                    t += 3
                    samples.append((t, team, kart,
                                    62.0 + karts[kart] + pace + rng.gauss(0, 0.1)))
            for _ in range(12):      # the novice, always in the good kart
                t += 3
                samples.append((t, "LAST", "GOOD",
                                62.0 + karts["GOOD"] + novice_off + rng.gauss(0, 0.5)))
        return rating.rate(samples, dict(cfg, min_laps=5))["karts"]

    def test_the_good_kart_is_still_the_good_kart(self):
        k = self.race({})
        self.assertEqual(k["GOOD"]["label"], "Rocket")
        self.assertEqual(k["GOOD"]["delta"], 0.0)

    def test_the_slowness_lands_on_the_driver_not_the_kart(self):
        k = self.race({})
        self.assertLess(k["GOOD"]["delta"], k["POOR"]["delta"])

    def test_a_kart_only_one_team_has_driven_is_not_judged_at_all(self):
        # Here the driver and the kart genuinely cannot be told apart, so the
        # honest answer is to decline rather than to label it bad.
        k = self.race({}, shared=False)
        self.assertIsNone(k["GOOD"]["delta"])
        self.assertIn("shared", k["GOOD"]["reason"])

    def test_a_cap_keeps_the_teams_quick_laps_and_drops_the_rest(self):
        k = self.race({"team_lap_cap": {"LAST": "1:04.000"}})
        self.assertEqual(k["GOOD"]["label"], "Rocket")

    def test_the_cap_is_matched_loosely_on_the_team_name(self):
        k = self.race({"team_lap_cap": {"  last ": 64.0}})
        self.assertEqual(k["GOOD"]["label"], "Rocket")

    def test_lap_times_may_be_written_either_way(self):
        self.assertAlmostEqual(rating.lap_seconds("1:03.500"), 63.5)
        self.assertAlmostEqual(rating.lap_seconds("63.5"), 63.5)
        self.assertAlmostEqual(rating.lap_seconds(63.5), 63.5)
        for junk in (None, "", "abc", "1:aa"):
            self.assertIsNone(rating.lap_seconds(junk))

    def test_a_cap_nobody_set_changes_nothing(self):
        self.assertEqual(self.race({})["GOOD"], self.race({"team_lap_cap": {}})["GOOD"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
