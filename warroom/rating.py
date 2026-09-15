#!/usr/bin/env python3
"""Kart rating — how fast is the kart, not the driver in it.

Raw lap times rank drivers, not karts: a mediocre kart under a pro team looks
better than a rocket under an amateur one.  To score the karts themselves we fit
an additive model to every lap the field has run,

    lap  ≈  baseline(t)  +  pilot_effect  +  kart_effect

where

* ``baseline(t)`` is the rolling median of the whole field at that moment.  It
  absorbs everything that hits everyone equally — track evolution, temperature,
  rubber, night running, safety cars — so what is left is relative pace.
* ``pilot_effect`` and ``kart_effect`` are fitted *jointly*, by ridge-penalised
  alternating least squares.  Because most karts are driven by several teams
  over a long race, the two effects separate: the model learns that a team is
  half a second slow and stops blaming its kart for it.

The ridge term shrinks effects toward zero in proportion to how little data
backs them, so a kart seen for three laps is reported as roughly neutral rather
than as the best or worst kart on the grid; below ``min_laps`` it is reported as
``Unknown`` outright.

The module is pure: it takes lap samples and returns numbers, which makes the
model straightforward to test against a synthetic race whose kart truth is known
(see test_rating.py).
"""

from collections import defaultdict
import bisect
import statistics

# Tuning.  Every value is overridable from config.json ("karts" block).
DEFAULTS = {
    "bucket_minutes": 10,     # width of a baseline window
    "min_bucket_laps": 5,     # laps needed before a window is trusted
    "trim_lo": 2.0,           # residuals faster than this are implausible
    "trim_hi": 4.0,           # residuals slower than this are traffic, not kart
    "lambda_pilot": 4.0,      # ridge strength, in laps, for driver effects
    "lambda_kart": 12.0,      # ridge strength, in laps, for kart effects
    "iters": 40,              # alternating least squares sweeps
    "min_laps": 8,            # laps before a kart is rated at all
    "min_pilot_laps": 30,     # laps before a driver is scored apart from their team
    "min_pilots": 2,          # distinct drivers before a rating is called solid
    "thresholds": [0.15, 0.40, 0.65, 1.00],
    "labels": ["Rocket", "Very Good", "Good", "OK", "Bad"],
    "unknown_label": "Unknown",
}


def cfg_with_defaults(cfg: dict = None) -> dict:
    out = dict(DEFAULTS)
    if cfg:
        out.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    return out


class Baseline:
    """Field pace over time, sampled in fixed windows and interpolated between.

    Sparse windows (a red flag, the first minutes of a session) are dropped
    rather than trusted, and lookups outside the sampled range clamp to the
    nearest end.
    """

    def __init__(self, samples, bucket_s: float, min_laps: int):
        buckets = defaultdict(list)
        for ts, _pilot, _kart, lap_s in samples:
            buckets[int(ts // bucket_s)].append(lap_s)

        self._ts, self._val = [], []
        for b in sorted(buckets):
            laps = buckets[b]
            if len(laps) >= min_laps:
                self._ts.append((b + 0.5) * bucket_s)
                self._val.append(statistics.median(laps))

        all_laps = [s[3] for s in samples]
        self._global = statistics.median(all_laps) if all_laps else 0.0

    def at(self, ts: float) -> float:
        if not self._ts:
            return self._global
        i = bisect.bisect_left(self._ts, ts)
        if i == 0:
            return self._val[0]
        if i >= len(self._ts):
            return self._val[-1]
        t0, t1 = self._ts[i - 1], self._ts[i]
        v0, v1 = self._val[i - 1], self._val[i]
        if t1 == t0:
            return v0
        return v0 + (v1 - v0) * (ts - t0) / (t1 - t0)


def fit_effects(obs: list, lambda_pilot: float, lambda_kart: float,
                iters: int) -> tuple:
    """Alternating least squares on ``(pilot, kart, value, weight)`` rows.

    Each sweep solves one factor holding the other fixed; the ridge term in the
    denominator pulls thinly-observed effects toward zero.  After every sweep
    the kart effects are re-centred on zero and the level handed to the pilots,
    which keeps the decomposition from drifting between runs.
    """
    pilots: dict = {}
    karts: dict = {}

    for _ in range(iters):
        num, den = defaultdict(float), defaultdict(float)
        for p, k, v, w in obs:
            num[p] += w * (v - karts.get(k, 0.0))
            den[p] += w
        for p in num:
            pilots[p] = num[p] / (den[p] + lambda_pilot)

        num, den = defaultdict(float), defaultdict(float)
        for p, k, v, w in obs:
            num[k] += w * (v - pilots.get(p, 0.0))
            den[k] += w
        for k in num:
            karts[k] = num[k] / (den[k] + lambda_kart)

        if karts:
            level = sum(karts.values()) / len(karts)
            for k in karts:
                karts[k] -= level
            for p in pilots:
                pilots[p] += level

    return pilots, karts


def _pilot_keys(samples: list, min_laps: int) -> dict:
    """Decide who counts as a driver in their own right, and who is just a team.

    A driver who has only sat in one kart cannot be told apart from that kart,
    and early in a race that is every driver: each stint is one new name in one
    new kart, so the field breaks into unconnected pairs and nothing can be
    rated.  Their team, though, has been in many karts.  So a driver is folded
    into their team until they have laps of their own — which keeps the fleet
    comparable from the first hour, while a driver with a real sample still
    gets their own pace subtracted rather than the team average.

    Keys arrive as ``"TEAM|Driver"`` from the feed, or plain ``"TEAM"`` when the
    event does not name drivers.
    """
    laps = defaultdict(int)
    for _ts, pilot, _kart, _lap_s in samples:
        laps[pilot] += 1
    return {p: (p if laps[p] >= min_laps or "|" not in p else p.split("|", 1)[0])
            for p in laps}


class _Components:
    """Union-find over the bipartite pilot↔kart graph.

    Two karts are only comparable if a chain of shared drivers connects them.
    Before the first kart swaps nothing is connected, and no amount of lap data
    can say whether a slow lap was the kart or the driver — so the model reports
    that honestly instead of inventing a ranking.
    """

    def __init__(self):
        self._parent = {}

    def find(self, node):
        self._parent.setdefault(node, node)
        while self._parent[node] != node:
            self._parent[node] = self._parent[self._parent[node]]
            node = self._parent[node]
        return node

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


def label_for(delta: float, cfg: dict) -> str:
    for cut, name in zip(cfg["thresholds"], cfg["labels"]):
        if delta < cut:
            return name
    return cfg["labels"][-1]


def rate(samples: list, cfg: dict = None) -> dict:
    """Score every kart from lap samples ``(ts, pilot, kart, lap_s)``.

    ``ts`` is epoch seconds, ``pilot`` identifies who was driving (team, or
    team+driver when the feed names drivers) and ``kart`` is the physical kart.
    Returns per-kart effects in seconds, a delta against the best kart in the
    fleet (so the quickest kart reads 0.00, like the pit board everyone is used
    to) and a label.

    A kart is only given a label once it has enough clean laps *and* shares
    drivers with the rest of the field; until then it is ``Unknown`` with a
    reason, which is the state most karts are in for the first hour of a race.
    """
    cfg = cfg_with_defaults(cfg)
    samples = [s for s in samples if s[3] and s[3] > 0]
    result = {"karts": {}, "pilots": {}, "n_laps": len(samples),
              "linked_karts": 0}
    if not samples:
        return result

    samples.sort(key=lambda s: s[0])
    base = Baseline(samples, cfg["bucket_minutes"] * 60.0, cfg["min_bucket_laps"])

    # Every kart the feed has seen, so one that loses all its laps to the trim
    # is still listed rather than silently dropped.
    raw_laps = defaultdict(int)
    for _ts, _pilot, kart, _lap_s in samples:
        raw_laps[kart] += 1

    # Residual per lap, then one robust observation per (pilot, kart) segment.
    pilot_of = _pilot_keys(samples, cfg["min_pilot_laps"])
    seg_res = defaultdict(list)
    for ts, pilot, kart, lap_s in samples:
        r = lap_s - base.at(ts)
        if -cfg["trim_lo"] <= r <= cfg["trim_hi"]:
            seg_res[(pilot_of[pilot], kart)].append(r)

    obs = []
    kart_laps = defaultdict(int)
    kart_pilots = defaultdict(set)
    comps = _Components()
    for (pilot, kart), res in seg_res.items():
        obs.append((pilot, kart, statistics.median(res), float(len(res))))
        kart_laps[kart] += len(res)
        kart_pilots[kart].add(pilot)
        comps.union(("P", pilot), ("K", kart))

    if obs:
        pilots, karts = fit_effects(obs, cfg["lambda_pilot"], cfg["lambda_kart"],
                                    cfg["iters"])

        # Each component has its own arbitrary level: centre them separately so
        # one isolated group cannot drag the rest of the fleet up or down.
        by_comp = defaultdict(list)
        for kart in karts:
            by_comp[comps.find(("K", kart))].append(kart)
        for root, members in by_comp.items():
            level = sum(karts[k] for k in members) / len(members)
            for k in members:
                karts[k] -= level
            for p in pilots:
                if comps.find(("P", p)) == root:
                    pilots[p] += level

        # The main component is the bulk of the field; only inside it are kart
        # effects on a common scale and therefore comparable.  A component of
        # one kart, or one driver, compares a kart against nothing — the first
        # minutes of every race look like that, before anyone has swapped.
        main = max(by_comp, key=lambda r: sum(kart_laps[k] for k in by_comp[r]))
        main_pilots = {p for p in pilots if comps.find(("P", p)) == main}
        linked = (set(by_comp[main])
                  if len(by_comp[main]) >= 2 and len(main_pilots) >= 2
                  else set())
        result["linked_karts"] = len(linked)
        result["pilots"] = {p: round(v, 3) for p, v in pilots.items()}
    else:
        pilots, karts, linked = {}, {}, set()

    rated = [k for k in linked if kart_laps[k] >= cfg["min_laps"]]
    best = min((karts[k] for k in rated), default=0.0)

    for kart, n_raw in raw_laps.items():
        effect = karts.get(kart, 0.0)
        n = kart_laps.get(kart, 0)
        is_linked = kart in linked
        is_rated = is_linked and n >= cfg["min_laps"]
        delta = effect - best
        if is_rated:
            reason = ""
        elif not is_linked:
            reason = "not yet shared with the field"
        else:
            reason = f"only {n} clean laps"
        result["karts"][kart] = {
            "effect": round(effect, 3),
            "delta": round(delta, 2) if is_rated else None,
            "laps": n,
            "raw_laps": n_raw,
            "pilots": len(kart_pilots.get(kart, ())),
            "rated": is_rated,
            "linked": is_linked,
            # One driver's laps cannot tell that driver apart from the kart.
            "weak": len(kart_pilots.get(kart, ())) < cfg["min_pilots"],
            "label": label_for(delta, cfg) if is_rated else cfg["unknown_label"],
            "reason": reason,
        }

    return result
