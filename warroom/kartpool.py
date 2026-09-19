#!/usr/bin/env python3
"""Automatic kart counting.

The Apex Timing feed identifies *teams* — a race number and a name that stay
put all race.  It says nothing about which physical kart a team is sitting in,
because the organisers hand karts out in the pit lane and no transponder moves
with them.  In a race where every stop means a new kart, keeping that book by
hand is a full-time job for one person and it is wrong within the hour.

So the war room keeps it instead:

* Karts wait in pit **lanes**.  A team that stops hands its kart to the back of
  a lane queue and takes the kart at the front of that same queue — first in,
  first out, which is how the marshals actually work.
* Stops are detected from the feed (the pit counter ticks, or a lap is a whole
  pit-lane slower than usual), so nobody has to press a button when a rival
  stops.
* The single human input is **which lane** a team was sent to, because that is
  the one thing the feed cannot see.  Tap it and the rest — which kart went
  out, who holds what now, and every lap that kart has run since — follows.
  With one lane configured, even that disappears and counting is fully
  automatic.

Everything mutating goes through :meth:`KartPool._mutate`, which snapshots the
state first, so the pit crew can undo a mis-tap under pressure.
"""

import json
import re
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

import rating

# Lanes are told apart by where they are, not by colour.  Colour now means how
# good a kart is — red bad, green good — so colouring a lane red made it look
# like the lane full of bad karts.  A lane is a place: left, right, or a number.
LANE_NAMES = ["Left", "Right"]
LANE_COLOR = "#64748b"             # one neutral slate; lanes are told apart by name
MAX_LANES = 6

def lane_name(i: int, total: int) -> str:
    """Two lanes are the left and the right one; more than two get numbers."""
    if total == 2 and 1 <= i <= 2:
        return LANE_NAMES[i - 1]
    return f"Lane {i}"


DEFAULTS = {
    "enabled": True,
    "lanes": 2,
    "swap_every_stop": True,      # False for races where a team keeps its kart
    "auto_pit": True,             # drive my team's box clock from the feed
    "detect_by_pit_column": True,
    "detect_by_lap_spike": True,
    "pit_lap_spike_s": 20.0,      # a lap this much over the team's own pace
    "skip_laps_after_stop": 1,    # out-lap is not the kart's fault
    "max_undo": 60,
    "rating_refresh_s": 8,
    # On for a practice session — us alone on track, one kart, no swaps.  It
    # changes how the laps are read, not what is recorded; see rating.PRACTICE.
    "practice": False,
    # Per-track rating tuning; see rating.DEFAULTS for the keys.
    "rating": {},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS kart_lane (
    lane  INTEGER PRIMARY KEY,
    name  TEXT,
    color TEXT,
    queue TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS kart_assign (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    team_no  TEXT, team TEXT, kart TEXT,
    start_ts TEXT, end_ts TEXT
);
CREATE TABLE IF NOT EXISTS kart_out (
    kart   TEXT PRIMARY KEY,
    ts     TEXT,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS kart_lap (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL, team_no TEXT, pilot TEXT, kart TEXT, lap_s REAL,
    -- Gap to the kart ahead when the lap was completed.  A lap run in someone
    -- else's slipstream is quicker than the kart deserves, so it is worth less
    -- as evidence.  NULL when the gap could not be read.
    ahead_s REAL,
    -- The three sector times for this lap, in seconds.  A lap time says a
    -- driver lost four tenths; the sectors say where, which is the only part
    -- of a debrief a driver can act on.  NULL where the feed did not send one.
    s1 REAL, s2 REAL, s3 REAL,
    -- Gap to the kart chasing this one.  Close behind in a rental kart is a
    -- shove, not a slipstream: it makes the lap quicker and says nothing about
    -- the kart, so it is recorded the same way the tow is.
    behind_s REAL
);
CREATE TABLE IF NOT EXISTS kart_stop (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT, team_no TEXT, team TEXT,
    kart_in  TEXT, kart_out TEXT,
    lane     INTEGER, state TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS kart_undo (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    TEXT, what TEXT, state TEXT
);
-- What somebody saw and the feed cannot: a bent axle, a black-and-orange, a
-- marshal's word, a kart swapped for a reason nobody timed.  The only entries
-- in the log that cannot be rebuilt from the stops and assignments, so the
-- only ones written down.
CREATE TABLE IF NOT EXISTS crew_note (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS kart_lap_kart ON kart_lap(kart);
"""

PENDING, RESOLVED, NO_CHANGE, AWAIT_KART = "pending", "resolved", "nochange", "await_kart"


def _lap_or_none(v):
    """A gap as seconds, or None when it is blank, a dash, or whole laps."""
    if v in (None, ""):
        return None
    t = str(v).strip().lstrip("+")
    if not t or t in ("-", "--") or not re.match(r'^[\d:.]+$', t):
        return None
    try:
        if ":" in t:
            m, sec = t.rsplit(":", 1)
            return int(m) * 60 + float(sec)
        return float(t)
    except ValueError:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hhmmss(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M:%S")
    except Exception:
        return ""


def _int(v, default=None):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


class KartPool:
    """Live kart book: who holds which kart, what is queued in each lane.

    Callers touch it from the feed thread (:meth:`observe`) and from request
    handlers (everything else), so every public method takes the lock.
    """

    def __init__(self, db_path: str, cfg: dict = None, on_my_stop=None,
                 on_my_release=None):
        self.db_path = db_path
        self.cfg = dict(DEFAULTS)
        self.configure(cfg or {})
        self._lock = threading.RLock()
        self._on_my_stop = on_my_stop
        self._on_my_release = on_my_release

        # Per-team feed bookkeeping, rebuilt on restart from the feed itself.
        self._last_pits = {}
        self._last_laps = {}
        self._last_lap_s = {}
        # {team_no: opened_at}.  Membership answers "is this team's kart
        # unknown", which stays true until the stop is resolved; the timestamp
        # answers "is this team in the box right now", which decays.
        self._pending_teams = {}
        # team_no -> sectors of the lap they are on
        self._sectors = {}
        # team_no -> laps thrown away for want of an answer about a stop
        self._dropped = defaultdict(int)
        self._skip = defaultdict(int)
        self._in_box_since = {}
        self._category = {}
        self._pace = defaultdict(lambda: deque(maxlen=12))
        self._my_team = ""
        self._log = deque(maxlen=250)
        self._rating = {"karts": {}, "pilots": {}, "n_laps": 0, "linked_karts": 0}
        self._rating_at = 0.0
        self._rating_dirty = True

        self._init_db()
        with self._con() as con:
            # Opened at 0: a stop that survived a restart is still unresolved,
            # but nobody has been standing in the box across the restart.
            self._pending_teams = {
                str(r["team_no"]): 0.0 for r in con.execute(
                    "SELECT team_no FROM kart_stop WHERE state IN (?,?)",
                    (PENDING, AWAIT_KART))}
            # The log itself is in memory and dies with the process.  The
            # crew's own notes are the part of it nobody can write again, so
            # they come back.
            for r in con.execute("SELECT ts, text FROM crew_note "
                                 "ORDER BY id DESC LIMIT 60"):
                self._log.append({"ts": _hhmmss(r["ts"]), "text": r["text"],
                                  "team": "", "tag": "crew"})

    # ── storage ───────────────────────────────────────────────────────────────
    def _con(self):
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self):
        with self._con() as con:
            con.executescript(SCHEMA)
            # Databases written before the gap column exists: the laps already
            # in them were recorded without it, and read back as clean.
            cols = {r["name"] for r in con.execute("PRAGMA table_info(kart_lap)")}
            for col in ("ahead_s", "s1", "s2", "s3", "behind_s"):
                if col not in cols:
                    con.execute(f"ALTER TABLE kart_lap ADD COLUMN {col} REAL")
        self._sync_lanes()

    def configure(self, cfg: dict):
        """Apply settings.  Safe to call while racing: lanes are added, and a
        lane is only dropped once it is empty."""
        was_practice = self.cfg.get("practice")
        self.cfg.update({k: v for k, v in (cfg or {}).items() if k in DEFAULTS})
        self.cfg["lanes"] = max(1, min(MAX_LANES, _int(self.cfg["lanes"], 2) or 2))
        if self.cfg.get("practice") != was_practice:
            # The same laps mean something different now, so the cached
            # scores are stale even though no new lap has arrived — which is
            # exactly the case the dirty flag exists to catch.
            self._rating_dirty = True
        if getattr(self, "_lock", None):
            self._sync_lanes()

    def _sync_lanes(self):
        """Create missing lanes; never drop a lane that still holds karts."""
        with self._con() as con:
            have = {r["lane"]: r for r in con.execute("SELECT * FROM kart_lane")}
            for i in range(1, self.cfg["lanes"] + 1):
                if i not in have:
                    con.execute(
                        "INSERT INTO kart_lane(lane,name,color,queue) VALUES(?,?,?,'[]')",
                        (i, lane_name(i, self.cfg["lanes"]),
                         LANE_COLOR))
            for lane, row in have.items():
                if lane > self.cfg["lanes"] and not json.loads(row["queue"] or "[]"):
                    con.execute("DELETE FROM kart_lane WHERE lane=?", (lane,))

    def _lanes(self, con) -> dict:
        return {r["lane"]: {"lane": r["lane"], "name": r["name"], "color": r["color"],
                            "queue": json.loads(r["queue"] or "[]")}
                for r in con.execute("SELECT * FROM kart_lane ORDER BY lane")}

    def _save_queue(self, con, lane: int, queue: list):
        con.execute("UPDATE kart_lane SET queue=? WHERE lane=?",
                    (json.dumps(queue), lane))

    # ── undo ──────────────────────────────────────────────────────────────────
    def _capture(self, con) -> str:
        return json.dumps({
            "lanes": {str(k): v["queue"] for k, v in self._lanes(con).items()},
            "assign": [dict(r) for r in con.execute(
                "SELECT id,team_no,team,kart,start_ts,end_ts FROM kart_assign")],
            "stops": [dict(r) for r in con.execute(
                "SELECT id,ts,team_no,team,kart_in,kart_out,lane,state,source "
                "FROM kart_stop")],
        })

    def _mutate(self, what: str, fn):
        """Run ``fn(con)`` with the pre-state saved so it can be undone."""
        with self._lock, self._con() as con:
            con.execute("INSERT INTO kart_undo(ts,what,state) VALUES(?,?,?)",
                        (_now_iso(), what, self._capture(con)))
            con.execute(
                "DELETE FROM kart_undo WHERE id NOT IN "
                "(SELECT id FROM kart_undo ORDER BY id DESC LIMIT ?)",
                (self.cfg["max_undo"],))
            out = fn(con)
        self._rating_dirty = True
        return out

    def undo(self) -> bool:
        with self._lock, self._con() as con:
            row = con.execute(
                "SELECT * FROM kart_undo ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return False
            state = json.loads(row["state"])
            for lane, queue in state["lanes"].items():
                self._save_queue(con, int(lane), queue)
            con.execute("DELETE FROM kart_assign")
            for a in state["assign"]:
                con.execute("INSERT INTO kart_assign(id,team_no,team,kart,start_ts,end_ts)"
                            " VALUES(?,?,?,?,?,?)",
                            (a["id"], a["team_no"], a["team"], a["kart"],
                             a["start_ts"], a["end_ts"]))
            con.execute("DELETE FROM kart_stop")
            for s in state["stops"]:
                con.execute("INSERT INTO kart_stop(id,ts,team_no,team,kart_in,kart_out,"
                            "lane,state,source) VALUES(?,?,?,?,?,?,?,?,?)",
                            (s["id"], s["ts"], s["team_no"], s["team"], s["kart_in"],
                             s["kart_out"], s["lane"], s["state"], s["source"]))
            con.execute("DELETE FROM kart_undo WHERE id=?", (row["id"],))
            self._pending_teams = {
                str(s["team_no"]): 0.0 for s in state["stops"]
                if s["state"] in (PENDING, AWAIT_KART)}
            self._note(f"undo — {row['what']}")
        self._rating_dirty = True
        return True

    def _note(self, text: str, team: str = "", tag: str = ""):
        self._log.appendleft({"ts": _hhmmss(_now_iso()), "text": text,
                              "team": team, "tag": tag})

    def note(self, text: str) -> bool:
        """Write down something only a person saw.  Kept across a restart."""
        text = " ".join(str(text or "").split())[:200]
        if not text:
            return False
        with self._lock, self._con() as con:
            con.execute("INSERT INTO crew_note(ts,text) VALUES(?,?)",
                        (_now_iso(), text))
        self._note(text, tag="crew")
        return True

    # ── assignment ────────────────────────────────────────────────────────────
    def _held_by(self, con, team_no: str):
        row = con.execute(
            "SELECT kart FROM kart_assign WHERE team_no=? AND end_ts IS NULL "
            "ORDER BY id DESC LIMIT 1", (str(team_no),)).fetchone()
        return row["kart"] if row else None

    def _assign(self, con, team_no: str, team: str, kart, ts: str = None):
        ts = ts or _now_iso()
        con.execute("UPDATE kart_assign SET end_ts=? WHERE team_no=? AND end_ts IS NULL",
                    (ts, str(team_no)))
        if kart:
            con.execute("INSERT INTO kart_assign(team_no,team,kart,start_ts) "
                        "VALUES(?,?,?,?)", (str(team_no), team, str(kart), ts))

    def set_kart(self, team_no: str, team: str, kart) -> None:
        """Manual override — the truth when someone reads the number off the kart."""
        def go(con):
            self._assign(con, team_no, team, str(kart).strip() if kart else None)
            self._note(f"kart {kart or '—'} set by hand", team or str(team_no))
        self._mutate(f"set kart for {team or team_no}", go)

    # ── lanes ─────────────────────────────────────────────────────────────────
    def _next_placeholder(self, lanes: dict) -> str:
        """A short label for a kart parked in a lane before anyone has read
        its number off it -- holds the queue position, renamed in place once
        it's known (see lane_rename)."""
        seen = {int(k[1:]) for info in lanes.values() for k in info["queue"]
                if k.startswith("?") and k[1:].isdigit()}
        n = 1
        while n in seen:
            n += 1
        return f"?{n}"

    def lane_add(self, lane: int, kart: str = ""):
        def go(con):
            lanes = self._lanes(con)
            if lane not in lanes:
                return
            k = str(kart).strip() or self._next_placeholder(lanes)
            if con.execute("SELECT 1 FROM kart_out WHERE kart=?",
                           (k,)).fetchone():
                self._note(f"kart {k} is out of service — not queued",
                           tag="warn")
                return
            queue = [x for x in lanes[lane]["queue"] if x != k] + [k]
            # A kart parked in a lane is not with a team any more.
            con.execute("UPDATE kart_assign SET end_ts=? WHERE kart=? AND end_ts IS NULL",
                        (_now_iso(), k))
            for other, info in lanes.items():
                if other != lane and k in info["queue"]:
                    self._save_queue(con, other,
                                     [x for x in info["queue"] if x != k])
            self._save_queue(con, lane, queue)
            self._note(f"kart {k} queued in lane {lane}"
                       + (" — number unknown yet" if k.startswith("?") else ""),
                       tag="lane")
        self._mutate(f"add kart {kart or '?'} to lane {lane}", go)

    def lane_rename(self, lane: int, old: str, new: str):
        """Fill in a placeholder's real number once it has been read off the
        kart, without losing its place in the queue."""
        new = str(new).strip()
        if not new:
            return
        def go(con):
            lanes = self._lanes(con)
            if lane not in lanes or old not in lanes[lane]["queue"]:
                return
            for other, info in lanes.items():
                if other != lane and new in info["queue"]:
                    self._save_queue(con, other,
                                     [x for x in info["queue"] if x != new])
            queue = [new if x == old else x for x in lanes[lane]["queue"]]
            self._save_queue(con, lane, queue)
            self._note(f"kart {old} identified as {new}", tag="lane")
        self._mutate(f"rename {old} to {new} in lane {lane}", go)

    def lane_remove(self, lane: int, kart: str):
        def go(con):
            lanes = self._lanes(con)
            if lane in lanes:
                self._save_queue(con, lane,
                                 [k for k in lanes[lane]["queue"] if k != str(kart)])
                self._note(f"kart {kart} taken out of lane {lane}", tag="lane")
        self._mutate(f"remove kart {kart} from lane {lane}", go)

    # ── karts out of service ──────────────────────────────────────────────────
    def retire_kart(self, kart: str, reason: str = ""):
        """Take a kart out of service — broken, stored, withdrawn by the pit.

        It leaves both lanes so it can never be offered as the next kart, but
        its laps stay: they are still evidence about the rest of the fleet, and
        deleting them would quietly change every other kart's score.
        """
        kart = str(kart).strip()
        if not kart:
            return

        def go(con):
            con.execute("INSERT OR REPLACE INTO kart_out(kart,ts,reason) "
                        "VALUES(?,?,?)", (kart, _now_iso(), reason.strip()))
            for lane, info in self._lanes(con).items():
                if kart in info["queue"]:
                    self._save_queue(con, lane,
                                     [k for k in info["queue"] if k != kart])
            self._note(f"kart {kart} out of service"
                       + (f" — {reason.strip()}" if reason.strip() else ""),
                       tag="warn")
        self._mutate(f"retire kart {kart}", go)

    def unretire_kart(self, kart: str):
        """Back in service. It still has to be put into a lane by hand."""
        kart = str(kart).strip()

        def go(con):
            con.execute("DELETE FROM kart_out WHERE kart=?", (kart,))
            self._note(f"kart {kart} back in service", tag="")
        self._mutate(f"return kart {kart}", go)

    def retired(self) -> dict:
        with self._lock, self._con() as con:
            return {r["kart"]: {"ts": r["ts"], "reason": r["reason"] or ""}
                    for r in con.execute("SELECT * FROM kart_out")}

    # ── stops ─────────────────────────────────────────────────────────────────
    def _open_stop(self, con, team_no: str, team: str, source: str,
                   ts: str = None) -> int:
        kart_in = self._held_by(con, team_no)
        if not self.cfg["swap_every_stop"]:
            cur = con.execute(
                "INSERT INTO kart_stop(ts,team_no,team,kart_in,kart_out,lane,state,source)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (ts or _now_iso(), str(team_no), team, kart_in, kart_in, None,
                 NO_CHANGE, source))
            return cur.lastrowid
        cur = con.execute(
            "INSERT INTO kart_stop(ts,team_no,team,kart_in,state,source) "
            "VALUES(?,?,?,?,?,?)",
            (ts or _now_iso(), str(team_no), team, kart_in, PENDING, source))
        stop_id = cur.lastrowid
        # Until the lane is known the team's laps belong to no kart at all.
        self._pending_teams[str(team_no)] = time.time()
        # With one lane there is no lane to ask about, so the only thing that
        # can still go wrong is the order: if another kart is in the pit lane
        # at the same moment, which of the two takes the kart at the front is
        # something only the person standing there can see.
        if self.cfg["lanes"] == 1 and not self._others_in_box(str(team_no)):
            self._do_resolve(con, stop_id, 1)
        return stop_id

    def _others_in_box(self, team_no: str, window: float = 300.0) -> list:
        """Other karts in the pit lane, or stopped and not yet accounted for.

        Either way we cannot know whose hand reached the front kart first.
        """
        now = time.time()
        others = {t for t, since in self._in_box_since.items()
                  if t != team_no and now - since < window}
        # Only stops recent enough that someone could still be standing there.
        # Without this the set never drains — one unresolved stop would block
        # automatic counting for the rest of the race, for every team.
        others |= {t for t, since in self._pending_teams.items()
                   if t != team_no and now - since < window}
        return sorted(others)

    def manual_stop(self, team_no: str, team: str = "") -> int:
        """A stop the feed never saw — the reason the pit phone has a button."""
        return self._mutate(f"stop for {team or team_no}",
                            lambda con: self._open_stop(con, team_no, team, "manual"))

    def _do_resolve(self, con, stop_id: int, lane: int):
        stop = con.execute("SELECT * FROM kart_stop WHERE id=?", (stop_id,)).fetchone()
        if not stop or stop["state"] not in (PENDING, AWAIT_KART):
            return
        lanes = self._lanes(con)
        if lane not in lanes:
            return
        queue = lanes[lane]["queue"]
        kart_out = queue.pop(0) if queue else None
        # Pop first, then hand the incoming kart to the back, so a team can
        # never be given the kart it just walked out of.
        if stop["kart_in"] and stop["kart_in"] != kart_out:
            queue.append(stop["kart_in"])
        self._save_queue(con, lane, queue)

        state = RESOLVED if kart_out else AWAIT_KART
        con.execute("UPDATE kart_stop SET lane=?, kart_out=?, state=? WHERE id=?",
                    (lane, kart_out, state, stop_id))
        if kart_out:
            self._assign(con, stop["team_no"], stop["team"], kart_out, stop["ts"])
            self._pending_teams.pop(str(stop["team_no"]), None)
            self._dropped.pop(str(stop["team_no"]), None)
            self._skip[str(stop["team_no"])] = self.cfg["skip_laps_after_stop"]
            self._note(f"kart {stop['kart_in'] or '?'} → {kart_out} via lane {lane}",
                       stop["team"], tag="swap")
        else:
            self._assign(con, stop["team_no"], stop["team"], None, stop["ts"])
            self._note(f"lane {lane} was empty — tell me the kart they got",
                       stop["team"], tag="ask")

    def resolve(self, stop_id: int, lane: int = None, no_change: bool = False,
                kart_out: str = None):
        """Answer the one question the feed cannot: which lane did they use."""
        def go(con):
            if no_change:
                stop = con.execute("SELECT * FROM kart_stop WHERE id=?",
                                   (stop_id,)).fetchone()
                if stop:
                    con.execute("UPDATE kart_stop SET state=?, kart_out=? WHERE id=?",
                                (NO_CHANGE, stop["kart_in"], stop_id))
                    self._pending_teams.pop(str(stop["team_no"]), None)
                    self._dropped.pop(str(stop["team_no"]), None)
                    self._skip[str(stop["team_no"])] = self.cfg["skip_laps_after_stop"]
                    self._note("driver change only, same kart", stop["team"],
                               tag="swap")
                return
            if kart_out:
                self._take_named_kart(con, stop_id, str(kart_out).strip())
                return
            self._do_resolve(con, stop_id, int(lane))
        self._mutate(f"resolve stop {stop_id}", go)

    def _take_named_kart(self, con, stop_id: int, kart_out: str):
        """The number read off the kart wins over anything we inferred.

        It also has to balance the books: the kart they drove away in stops
        waiting in a lane, and the one they handed over starts waiting.
        """
        stop = con.execute("SELECT * FROM kart_stop WHERE id=?", (stop_id,)).fetchone()
        if not stop:
            return
        already_queued = stop["state"] == AWAIT_KART   # _do_resolve queued it

        for lane, info in self._lanes(con).items():
            if kart_out in info["queue"]:
                self._save_queue(con, lane, [k for k in info["queue"] if k != kart_out])

        kart_in = stop["kart_in"]
        if kart_in and kart_in != kart_out and not already_queued:
            lane = stop["lane"] or (1 if self.cfg["lanes"] == 1 else None)
            lanes = self._lanes(con)
            if lane in lanes:
                self._save_queue(con, lane, lanes[lane]["queue"] + [kart_in])
            else:
                # Nobody said which lane, so the kart is off the board until
                # someone puts it back — the log is how they find out.
                self._note(f"kart {kart_in} handed over — lane unknown, "
                           f"add it to a lane when you see it",
                           stop["team"], tag="ask")

        con.execute("UPDATE kart_stop SET kart_out=?, state=? WHERE id=?",
                    (kart_out, RESOLVED, stop_id))
        self._assign(con, stop["team_no"], stop["team"], kart_out, stop["ts"])
        self._pending_teams.pop(str(stop["team_no"]), None)
        self._dropped.pop(str(stop["team_no"]), None)
        self._skip[str(stop["team_no"])] = self.cfg["skip_laps_after_stop"]
        self._note(f"kart {kart_in or '?'} → {kart_out} (read off the kart)",
                   stop["team"], tag="swap")

    def pending(self) -> list:
        with self._lock, self._con() as con:
            rows = con.execute(
                "SELECT * FROM kart_stop WHERE state IN (?,?) ORDER BY id",
                (PENDING, AWAIT_KART)).fetchall()
            now = time.time()
            mine = (self._my_team or "").strip().lower()
            out = []
            for r in rows:
                since = self._in_box_since.get(str(r["team_no"]))
                if r["state"] == AWAIT_KART:
                    asks = "kart"          # the lane was empty on our side
                elif self.cfg["lanes"] > 1:
                    asks = "lane"          # the one thing the feed cannot see
                else:
                    asks = "order"         # two karts in the lane at once
                out.append({
                    "id": r["id"], "team": r["team"], "team_no": r["team_no"],
                    "category": self._category.get(str(r["team_no"]), ""),
                    "kart_in": r["kart_in"], "state": r["state"], "asks": asks,
                    "at": _hhmmss(r["ts"]),
                    "in_box_s": int(now - since) if since else None,
                    "with_them": self._others_in_box(str(r["team_no"])),
                    # What the silence is costing: every lap since this stop
                    # is a lap the kart ratings never see.
                    "laps_lost": self._dropped.get(str(r["team_no"]), 0),
                    # Ours is the one stop where somebody can walk over and
                    # read the number off the kart, and with a queue behind us
                    # that is the only answer that is reliable: the lane hands
                    # out its front kart, so tapping it while an earlier stop
                    # on the same lane is unanswered gives us their kart.
                    "mine": bool(mine) and (r["team"] or "").strip().lower() == mine,
                })
            # Strictly the order they stopped in, ours included.  A lane
            # hands out its front kart, so the queue is only rebuilt
            # correctly if the answers arrive in the same order the karts
            # actually left — sorting ours to the top made every one of our
            # stops an out-of-order answer, and took the kart belonging to
            # whoever stopped before us.  Ours is marked, not moved: the
            # phone leads it with the kart number, which is the one answer
            # that does not disturb the sequence.
            return out

    # ── feed ingestion ────────────────────────────────────────────────────────
    def observe(self, rows: list, my_team: str = "", now: float = None):
        """Fold one timing snapshot into the book.

        Called on every feed update, so it has to be cheap and, above all,
        idempotent: the same snapshot arriving twice must not invent a stop.
        """
        if not self.cfg["enabled"] or not rows:
            return
        # Kept so pending() can put our own question first.  In a full field
        # every rival's stop asks one too, and by half distance there are two
        # hundred of them — ours has to be the one on screen when our kart is
        # the one standing in the box.
        self._my_team = my_team or self._my_team
        now = now or time.time()
        ahead = self.gaps_ahead(rows)
        behind = self.gaps_behind(rows)
        new_stops, released = [], []
        entered_box, left_box = [], []

        with self._lock, self._con() as con:
            # Who is in the pit lane has to be known for the whole field before
            # any stop is judged: whether a stop is unambiguous depends on the
            # others, and they may be further down the same snapshot.
            for row in rows:
                team_no = str(row.get("kart") or row.get("team_no") or "").strip()
                if not team_no:
                    continue
                if row.get("category"):
                    self._category[team_no] = row["category"]
                was_in = team_no in self._in_box_since
                if row.get("in_pit"):
                    if not was_in:
                        entered_box.append((team_no, (row.get("team") or "").strip()))
                    self._in_box_since.setdefault(team_no, now)
                else:
                    if was_in:
                        left_box.append((team_no, (row.get("team") or "").strip()))
                    self._in_box_since.pop(team_no, None)

            for row in rows:
                team_no = str(row.get("kart") or row.get("team_no") or "").strip()
                team = (row.get("team") or "").strip()
                if not team_no:
                    continue

                pits = _int(row.get("pits"))
                laps = _int(row.get("total_laps"))
                lap_s = row.get("last_lap_s")
                # The feed repeats the same row many times a second, and a slow
                # in-lap stays on screen for the whole stop.  Everything below
                # that reasons about laps has to look at new laps only.
                fresh_lap = bool(lap_s) and self._new_lap(team_no, laps, lap_s)

                stopped = False
                if self.cfg["detect_by_pit_column"] and pits is not None:
                    prev = self._last_pits.get(team_no)
                    # First sight only seeds the counter — a reconnect mid-race
                    # must not replay every stop the team has already made.
                    if prev is not None and pits > prev:
                        stopped = True
                    self._last_pits[team_no] = pits

                if (not stopped and self.cfg["detect_by_lap_spike"]
                        and fresh_lap and self._is_pit_lap(team_no, lap_s)):
                    stopped = True

                if stopped:
                    self._in_box_since.setdefault(team_no, now)
                    self._open_stop(con, team_no, team, "feed")
                    self._note("stopped — which lane?" if self.cfg["lanes"] > 1
                               else "stopped", team or team_no, tag="stop")
                    new_stops.append((team_no, team))
                elif laps is not None and self._last_laps.get(team_no) is not None \
                        and laps > self._last_laps[team_no]:
                    released.append((team_no, team))

                # The lap that ends in the pit lane says nothing about the kart.
                if fresh_lap and not stopped:
                    self._record_lap(con, team_no, team, row, lap_s, now,
                                     ahead.get(str(row.get("kart", ""))),
                                     behind.get(str(row.get("kart", ""))))
                # Bank the sectors after the lap is written, never before: what
                # is on the board now belongs to the lap being run, so banking
                # first would file the new lap's splits under the old one.  A
                # final sector arriving in the same frame as the lap time is
                # lost this way, which is the safe direction to be wrong in.
                self._note_sectors(team_no, row)
                if fresh_lap and lap_s > 0:
                    self._pace[team_no].append(lap_s)
                if lap_s:
                    self._last_lap_s[team_no] = lap_s
                if laps is not None:
                    self._last_laps[team_no] = laps

        # Our own box clock runs off the earliest signal there is: the feed
        # showing our kart in the lane, which lands before the pit counter
        # ticks.  Both paths are idempotent, so whichever arrives first wins
        # and the other is a no-op — an event without the in-pit flag still
        # boxes us on the counter.
        if my_team and self.cfg["auto_pit"]:
            for events, handler in ((entered_box + new_stops, self._on_my_stop),
                                    (left_box + released, self._on_my_release)):
                fired = set()
                for team_no, team in events:
                    if team == my_team and handler and team_no not in fired:
                        fired.add(team_no)
                        handler(team_no)

    def _new_lap(self, team_no: str, laps, lap_s: float) -> bool:
        """Has this team completed a lap we have not already counted?

        First sight only seeds: the lap on screen when we connect was run
        before we were watching, possibly in a different kart.
        """
        if laps is not None:
            prev = self._last_laps.get(team_no)
            return prev is not None and laps > prev
        prev_s = self._last_lap_s.get(team_no)
        return prev_s is not None and abs(lap_s - prev_s) > 1e-6

    def _is_pit_lap(self, team_no: str, lap_s: float) -> bool:
        """A lap a whole pit-lane slower than this team's own recent pace."""
        pace = self._pace[team_no]
        if len(pace) < 5:
            return False
        ref = sorted(pace)[len(pace) // 2]
        return lap_s > ref + self.cfg["pit_lap_spike_s"]

    @staticmethod
    def road_order(rows: list) -> list:
        """The field in race order as ``(kart, gap to the leader)``.

        Apex sends the gap to the leader, so every interval between neighbours
        comes out of this one list.  A kart whose gap cannot be read — blank, a
        dash, or a number of whole laps — is left out rather than guessed at.
        """
        placed = []
        for r in rows:
            try:
                pos = int(str(r.get("pos", "") or 0).strip() or 0)
            except ValueError:
                continue
            if pos:
                placed.append((pos, str(r.get("kart", "")), r.get("gap")))
        placed.sort()
        out = []
        for i, (_pos, kart, raw) in enumerate(placed):
            # The leader's gap cell is blank on every Apex board: they are
            # ahead of the field by nought, not by an unknown amount.  Reading
            # that as missing drops them out of the order and hands the leader's
            # place to the kart behind.
            g = 0.0 if i == 0 else _lap_or_none(raw)
            if g is not None:
                out.append((kart, g))
        return out

    @staticmethod
    def gaps_ahead(rows: list) -> dict:
        """Gap from each kart to the one in front.  The leader gets None."""
        order = KartPool.road_order(rows)
        return {kart: (None if i == 0 else round(g - order[i - 1][1], 3))
                for i, (kart, g) in enumerate(order)}

    @staticmethod
    def gaps_behind(rows: list) -> dict:
        """Gap from each kart to the one chasing it.  The last kart gets None.

        The same interval read the other way round: whoever is behind you is
        close enough to be pushing, and in rental karting that is a shove, not
        a slipstream — it makes a lap quicker without saying anything about the
        kart.
        """
        order = KartPool.road_order(rows)
        return {kart: (None if i == len(order) - 1
                       else round(order[i + 1][1] - g, 3))
                for i, (kart, g) in enumerate(order)}

    def _record_lap(self, con, team_no: str, team: str, row: dict,
                    lap_s: float, now: float, ahead_s: float = None,
                    behind_s: float = None):
        """Store one lap against the kart we believe the team is holding."""
        # The sector buffer is emptied whether or not the lap is kept, because
        # those sectors belong to the lap that has just ended either way.
        sectors = self._sectors.pop(team_no, {})
        if team_no in self._pending_teams:
            # In the box with the kart unknown: attributing this lap to the
            # kart they walked out of would be a guess, so it is dropped.  The
            # count is kept, because an answer nobody gives costs a lap a
            # minute for the rest of the race and that has to be visible.
            self._dropped[team_no] += 1
            if self._dropped[team_no] in (5, 20, 60):
                self._note(f"{self._dropped[team_no]} laps not counted while "
                           f"this stop is unanswered", team or team_no, tag="ask")
            return
        if self._skip[team_no] > 0:
            self._skip[team_no] -= 1     # out-lap is not the kart's fault
            return
        # A kart we manage (ours, or a rival stop we happened to answer) keeps
        # its real identity, so its laps still count towards rating other
        # karts.  One we have never tracked at all -- every rival we never
        # walk over and read a number off -- gets a placeholder scoped to the
        # team alone, so its laps stay in kart_lap for "this team's history"
        # without ever pretending to know which physical kart it was in: a
        # made-up id with exactly one team can never link to the real fleet.
        kart = self._held_by(con, team_no) or f"team:{team_no}"
        driver = (row.get("driver") or "").strip()
        pilot = f"{team or team_no}|{driver}" if driver else (team or team_no)
        con.execute("INSERT INTO kart_lap(ts,team_no,pilot,kart,lap_s,ahead_s,"
                    "s1,s2,s3,behind_s) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (now, team_no, pilot, kart, float(lap_s), ahead_s,
                     sectors.get("s1"), sectors.get("s2"), sectors.get("s3"),
                     behind_s))
        self._rating_dirty = True

    def _note_sectors(self, team_no: str, row: dict):
        """Remember the sectors of the lap a team is on, until it ends.

        Apex fills the sector columns as a kart passes each split, so what is
        on the board belongs to the lap in progress, not the one on the
        timesheet.  They are held here and written when that lap completes.
        A sector the feed never sent stays missing rather than being guessed.
        """
        buf = self._sectors.setdefault(team_no, {})
        for key in ("s1", "s2", "s3"):
            v = _lap_or_none(row.get(key))
            if v is not None:
                buf[key] = v

    # ── rating ────────────────────────────────────────────────────────────────
    @property
    def tow_gap_s(self) -> float:
        """How close behind counts as a tow — the model's setting, not a copy.

        Two homes for one number is two answers: whatever the rating is built
        on is what the screen has to mark up.
        """
        return rating.cfg_with_defaults(self.cfg.get("rating"))["tow_gap_s"]

    @property
    def push_gap_s(self) -> float:
        """How close behind counts as a shove — the model's setting, not a copy."""
        return rating.cfg_with_defaults(self.cfg.get("rating"))["push_gap_s"]

    def rating_cfg(self) -> dict:
        """The rating settings, with practice's overrides on top when we are
        in one.  Anything set per track still wins for the keys practice does
        not touch."""
        cfg = dict(self.cfg.get("rating") or {})
        if self.cfg.get("practice"):
            cfg.update(rating.PRACTICE)
        return cfg

    def ratings(self, force: bool = False) -> dict:
        """Kart scores, recomputed at most every few seconds."""
        if not (force or self._rating_dirty) or \
                (not force and time.time() - self._rating_at < self.cfg["rating_refresh_s"]):
            return self._rating
        with self._lock, self._con() as con:
            samples = [(r["ts"], r["pilot"], r["kart"], r["lap_s"], r["ahead_s"],
                        r["behind_s"]) for r in con.execute(
                           "SELECT ts,pilot,kart,lap_s,ahead_s,behind_s "
                           "FROM kart_lap")]
        try:
            self._rating = rating.rate(samples, self.rating_cfg())
        except Exception as e:
            # Kart scores are an opinion; the timing screen is not.  A rater
            # that trips over one odd row must not take the whole wall down in
            # the middle of a 25-hour race — keep the last scores and say so.
            self._note(f"kart rating failed, keeping the last scores: {e}",
                       tag="warn")
            self._rating.setdefault("karts", {})
            self._rating.setdefault("pilots", {})
            self._rating["error"] = str(e)
        else:
            self._rating.pop("error", None)
        self._rating_at = time.time()
        self._rating_dirty = False
        return self._rating

    def recent_best(self, window_minutes: float = 20.0) -> float:
        """The quickest lap anyone has turned lately.

        The reference a call is made against has to move with the track: a best
        set in the second hour is unreachable by four in the morning, and a
        rule anchored to it would simply stop firing.
        """
        cutoff = time.time() - window_minutes * 60.0
        with self._lock, self._con() as con:
            row = con.execute("SELECT MIN(lap_s) AS b FROM kart_lap WHERE ts>=?",
                              (cutoff,)).fetchone()
        return row["b"] if row and row["b"] else None

    def pilot_levels(self) -> dict:
        """How far off the field each team normally runs, by team name.

        Keyed on the team rather than team+driver: the war room is watching
        other teams on the board, and that is the name it can see.
        """
        out = {}
        for pilot, effect in (self.ratings().get("pilots") or {}).items():
            team = rating.team_of(pilot).strip()
            if team:
                out[team] = min(out[team], effect) if team in out else effect
        return out

    # ── views ─────────────────────────────────────────────────────────────────
    def kart_of(self) -> dict:
        with self._lock, self._con() as con:
            return {r["team_no"]: r["kart"] for r in con.execute(
                "SELECT team_no,kart FROM kart_assign WHERE end_ts IS NULL")}

    def snapshot(self) -> dict:
        rated = self.ratings()["karts"]
        gone = self.retired()
        with self._lock, self._con() as con:
            lanes = self._lanes(con)
            holders = {r["kart"]: r["team"] or r["team_no"] for r in con.execute(
                "SELECT kart,team,team_no FROM kart_assign WHERE end_ts IS NULL")}
            stops_seen = con.execute("SELECT COUNT(*) FROM kart_stop").fetchone()[0]
            best = {r["kart"]: r["b"] for r in con.execute(
                "SELECT kart, MIN(lap_s) AS b FROM kart_lap GROUP BY kart")}
        fleet_best = min(best.values(), default=None)

        def card(num: str) -> dict:
            info = rated.get(num, {})
            return {
                "num": num,
                "label": info.get("label", "Unknown"),
                "delta": info.get("delta"),
                "laps": info.get("laps", 0),
                "pilots": info.get("pilots", 0),
                "weak": info.get("weak", True),
                "thin": info.get("thin", False),
                "reason": info.get("reason", "no laps yet"),
                "best_s": best.get(num),
                # The kart's best lap against the fleet's, for the first hour
                # when no kart has a grade yet.  Half of it is whoever was
                # driving, so it is a hint and never a score — measured, it
                # ranks the fleet at about 0.55 against the truth in a field
                # as mixed as PRO and AM, where the model ranks it at zero.
                "best_delta_s": (round(best[num] - fleet_best, 3)
                                 if fleet_best and num in best else None),
                "holder": holders.get(num, ""),
                "fade_s": info.get("fade_s"),
                "fade_runs": info.get("fade_runs", 0),
                # Laps run in clear air vs in someone's tow.  A kart only ever
                # seen in traffic has not really been read.
                "clean_laps": info.get("clean_laps", 0),
                "tow_laps": info.get("tow_laps", 0),
                "push_laps": info.get("push_laps", 0),
                "retired": num in gone,
                "retired_reason": (gone.get(num) or {}).get("reason", ""),
            }

        in_lane = set()
        lane_out = []
        for lane in sorted(lanes):
            queue = lanes[lane]["queue"]
            in_lane.update(queue)
            lane_out.append({**lanes[lane], "karts": [card(k) for k in queue]})

        # A kart out of service has left the lanes and holds nothing, so
        # without this it would vanish from the fleet — no way to see it is
        # out, and no way to put it back.
        known = set(rated) | set(holders) | in_lane | set(gone)
        fleet = sorted(
            (card(k) for k in known),
            key=lambda c: (c["delta"] is None, c["delta"] if c["delta"] is not None else 0,
                           _int(c["num"], 0) or 0))

        return {
            "enabled": self.cfg["enabled"],
            "lanes": lane_out,
            "pending": self.pending(),
            "fleet": fleet,
            "kart_of": self.kart_of(),
            "log": list(self._log)[:40],
            "stops_seen": stops_seen,
            "unrated": sum(1 for c in fleet if c["delta"] is None),
            # How much of the fleet we can actually say anything about.  A kart
            # needs a second driver before it can be told apart from whoever
            # drove it, so a race started cold is blind for about the first
            # hour — this is what says so before the lights go out.
            "ready": {
                "graded": sum(1 for c in fleet if c["delta"] is not None),
                "solid": sum(1 for c in fleet
                             if c["delta"] is not None and not c["thin"]),
                "total": len(fleet),
            },
            "retired": sorted(gone),
            "swap_every_stop": self.cfg["swap_every_stop"],
        }

    def reset(self):
        with self._lock, self._con() as con:
            for table in ("kart_lap", "kart_assign", "kart_stop", "kart_undo",
                          "kart_out"):
                con.execute(f"DELETE FROM {table}")
            for lane in self._lanes(con):
                self._save_queue(con, lane, [])
        self._last_pits.clear()
        self._last_laps.clear()
        self._last_lap_s.clear()
        self._pending_teams.clear()
        self._skip.clear()
        self._in_box_since.clear()
        self._category.clear()
        self._pace.clear()
        self._log.clear()
        # Marking it dirty is not enough: ratings() still serves the cached
        # scores for the refresh window, so a wipe left the old grades on
        # screen for several seconds. Throw them away outright.
        self._rating = {"karts": {}, "pilots": {}, "n_laps": 0, "linked_karts": 0}
        self._rating_at = 0.0
        self._rating_dirty = True
