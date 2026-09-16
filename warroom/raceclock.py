#!/usr/bin/env python3
"""The race clock, taken from the timing tower rather than from us.

Our own clock starts when somebody remembers to press START, drifts through a
24-hour race and knows nothing about a red flag that stops the countdown.  The
Apex feed carries the clock the organisers actually run, in its ``dyn`` header
fields — normally as ``elapsed / total``, sometimes as a single number counting
one way or the other.

A single number is ambiguous, so the clock works out which it is by watching it
move: counting down is time remaining, counting up is time elapsed.  Between
feed updates it extrapolates from the last reading, which keeps the display
ticking smoothly at one second even though the feed arrives in bursts.
"""

import re
import time

# H:MM:SS or MM:SS, rejecting lap times like 1:02.478 and any longer run of
# digits and colons around the match.
_CLOCK = re.compile(r'(?<![\d:.])(\d{1,3}):([0-5]\d)(?::([0-5]\d))?(?![\d:.])')

# Both Apex installs we have sampled — KIP Palmela and RKC — send the clock as
# a bare millisecond counter rather than as h:mm:ss, so the colon pattern above
# never sees it.  A bare number on its own could be anything, though, so it only
# becomes a clock once it has been watched moving at about a thousand a second.
_MS_PER_S = 1000.0
_MS_SLACK = 0.35            # how far off that rate the average may sit
_MS_MAX_DIGITS = 12
# The rate has to be measured over a decent stretch.  KIP's counter steps down
# by a flat 30,000 every time the tower republishes it, but those frames reach
# us whenever the poll happens to land — measured live, the same clock gave
# 1368, 641 and 2727 a second over consecutive readings and 1128 against a
# fixed anchor eighty seconds back.  Judging on consecutive pairs reads the
# polling jitter, not the clock.
_MS_MIN_BASELINE = 20.0
# How long an anchor is kept while it has proved nothing.  Long enough for the
# average to settle, short enough that a field which only becomes a clock later
# is not judged against a reading from another session.
_MS_MAX_BASELINE = 300.0

# Words the timing feeds put next to a countdown, in the languages Apex ships.
_REMAINING_WORDS = ("remain", "restant", "restante", "left", "to go", "rest")
_ELAPSED_WORDS = ("elapsed", "écoulé", "ecoule", "decorrido", "run")


def parse_clocks(text: str) -> list:
    """Every wall-clock duration in ``text``, in seconds."""
    out = []
    for h, m, s in _CLOCK.findall(text or ""):
        if s:
            out.append(int(h) * 3600 + int(m) * 60 + int(s))
        else:
            out.append(int(h) * 60 + int(m))
    return out


class ApexClock:
    """Race time as the tower shows it, with the gaps filled in."""

    def __init__(self, stale_after: float = 90.0):
        self.total = None
        self.elapsed = None
        self.remaining = None
        self.at = 0.0
        self.stale_after = stale_after
        self._prev_single = None
        self._ms_prev = None        # (value, when) of the last bare number
        self._ms_confirmed = False  # this feed has proved it counts in millis

    # ── ingest ────────────────────────────────────────────────────────────────
    def update(self, text: str, now: float = None) -> bool:
        """Fold one header string into the clock.  True if it carried time."""
        now = now or time.time()
        values = parse_clocks(text)
        if not values:
            secs = self._millis_as_seconds(text, now)
            if secs is None:
                return False
            values = [secs]
        low = (text or "").lower()

        if len(values) >= 2:
            # "10:15:40 / 24:00:00" — the larger of the pair is the race length.
            a, b = values[0], values[1]
            total, other = (b, a) if b >= a else (a, b)
            self.total = total
            if any(w in low for w in _REMAINING_WORDS):
                self._set(remaining=other, now=now)
            else:
                self._set(elapsed=other, now=now)
            return True

        value = values[0]
        if any(w in low for w in _REMAINING_WORDS):
            self._set(remaining=value, now=now)
        elif any(w in low for w in _ELAPSED_WORDS):
            self._set(elapsed=value, now=now)
        else:
            # Unlabelled: let it move, and believe what the movement says.
            prev = self._prev_single
            if prev is None or prev == value:
                # Nothing to infer from yet — assume it counts up, and let the
                # next reading correct us if it is a countdown.
                if self.remaining is not None:
                    self._set(remaining=value, now=now)
                else:
                    self._set(elapsed=value, now=now)
            elif value < prev:
                self._set(remaining=value, now=now)
            else:
                self._set(elapsed=value, now=now)
        self._prev_single = value
        return True

    def _millis_as_seconds(self, text, now: float):
        """A bare millisecond counter, in seconds — or None if it is not one.

        The first sighting proves nothing: a lap count, a temperature and a
        distance are all bare numbers too.  What a clock does and they do not
        is move at a thousand a second averaged over twenty seconds or more,
        so that is what is waited for — the first reading is kept as an anchor
        and nothing is decided until enough time has passed for the feed's own
        irregular delivery to average out.  Once a feed has shown it, it keeps
        the benefit of the doubt: otherwise the clock would be dropped exactly
        when it stops under a red flag, which is the moment it is worth most.
        """
        t = (text or "").strip()
        if not t.isdigit() or len(t) > _MS_MAX_DIGITS:
            self._ms_prev = None
            return None
        value = int(t)
        if self._ms_confirmed:
            self._ms_prev = (value, now)
            return value / _MS_PER_S
        anchor = self._ms_prev
        if anchor is None:
            self._ms_prev = (value, now)
            return None
        span = now - anchor[1]
        if span < _MS_MIN_BASELINE:
            return None                  # too soon to tell a clock from a count
        rate = abs(value - anchor[0]) / span
        if abs(rate - _MS_PER_S) > _MS_PER_S * _MS_SLACK:
            # Not a clock at this pace — but the anchor stays, because the
            # average is what settles.  Measured live on KIP, the first span
            # read 1368 a second and the third 1128 against the same anchor;
            # re-anchoring on each miss throws away the very baseline that
            # makes it converge, and nothing is ever confirmed.
            if span > _MS_MAX_BASELINE:
                self._ms_prev = (value, now)
            return None
        self._ms_confirmed = True
        # The reading that proved the rate also showed the direction, so hand
        # it on: without it the first good value is assumed to count up and the
        # wall reports fifteen minutes elapsed in a twenty-five hour race until
        # the next frame corrects it.
        if self._prev_single is None:
            self._prev_single = anchor[0] / _MS_PER_S
        self._ms_prev = (value, now)
        return value / _MS_PER_S

    def _set(self, elapsed=None, remaining=None, now=0.0):
        self.at = now
        if elapsed is not None:
            self.elapsed = float(elapsed)
            self.remaining = (self.total - self.elapsed) if self.total else None
        if remaining is not None:
            self.remaining = float(remaining)
            self.elapsed = (self.total - self.remaining) if self.total else None

    def set_total(self, seconds: float):
        """Race length from config, used when the feed only sends one number."""
        if seconds and not self.total:
            self.total = float(seconds)
            if self.remaining is not None and self.elapsed is None:
                self.elapsed = self.total - self.remaining
            elif self.elapsed is not None and self.remaining is None:
                self.remaining = self.total - self.elapsed

    # ── read ──────────────────────────────────────────────────────────────────
    def ok(self, now: float = None) -> bool:
        """False once the feed has gone quiet — better our clock than a frozen one."""
        if not self.at or (self.elapsed is None and self.remaining is None):
            return False
        return (now or time.time()) - self.at <= self.stale_after

    def state(self, now: float = None) -> dict:
        now = now or time.time()
        drift = max(0.0, now - self.at)
        elapsed = self.elapsed + drift if self.elapsed is not None else None
        remaining = max(0.0, self.remaining - drift) if self.remaining is not None else None
        return {"elapsed": elapsed, "remaining": remaining, "total": self.total,
                "ok": self.ok(now), "age": round(drift, 1)}

    def reset(self):
        self.__init__(self.stale_after)
