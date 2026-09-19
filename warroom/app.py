#!/usr/bin/env python3
"""TPC War Room — Karting Endurance Strategy Tool"""

from flask import Flask, render_template, jsonify, request
import threading, time, json, sqlite3, urllib.request, urllib.error, urllib.parse
import html.parser, re, os, math, statistics, unicodedata
from datetime import datetime, timedelta
from typing import Optional
from collections import defaultdict

import kartpool
import entries
from raceclock import ApexClock

try:
    import websocket as _ws_mod
    _HAS_WS = True
    # A port that is firewalled rather than refused never answers, so with no
    # timeout the connect attempt hangs for however long the OS waits on a SYN
    # nobody answers -- worker() then sits in it instead of falling back to
    # AJAX. Every WS attempt this process makes is to an Apex feed, so one
    # process-wide bound is enough.
    _ws_mod.setdefaulttimeout(10)
except ImportError:
    _HAS_WS = False

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
# WARROOM_CONFIG mirrors WARROOM_DB: it lets a second instance, or a test, run
# against its own settings instead of whatever the last run happened to save.
_CFG_PATH = os.environ.get("WARROOM_CONFIG") or os.path.join(
    os.path.dirname(__file__), "config.json")
os.makedirs(os.path.dirname(os.path.abspath(_CFG_PATH)), exist_ok=True)

def load_cfg() -> dict:
    base = {
        # The organisers' entry list spells us this way.  Apex may not — the
        # matching handles a clipped or reordered name — but starting from
        # the real name is one less thing to type in on the Friday.
        "team_name": "TPC CIAO CUORE",
        "apex_url": "",
        "refresh_interval": 5,
        # Defaults are the 24 Horas de Portugal 2026 regulation (KIP Palmela,
        # 19-20 September).  Section numbers below refer to that document.
        "race": {
            "category": "AM",             # PRO or AM — §2.5, sets the two below
            "duration_minutes": 1500,     # §3.2  the race is 25 hours, not 24
            "mandatory_pits": 34,         # §3.8  PRO 28, AM 34
            "stint_max_minutes": 60,      # §3.10 PRO 80, AM 60
            "stint_min_minutes": 10,      # §3.10 a turn under this is penalised
            "pit_duration_seconds": 180,  # §3.9  3 minutes, timed electronically
            # Their clock starts at the pit entry beam; ours starts when the
            # feed notices or someone presses. Being a few seconds late to
            # start means our countdown finishes early, and leaving then is a
            # 20s penalty, so hold the kart this much longer than the minimum.
            "pit_safety_seconds": 5,
            "no_pit_last_minutes": 30,    # §3.8  pit lane shuts at 24:30
            # Flag any team lapping this close to the current reference pace.
            # Measured against the recent best, never the race best — over 25
            # hours the race best stops being reachable and the rule goes quiet
            # exactly when it would be most useful.
            "watch_within_s": 1.0,
            "watch_window_minutes": 20,
            # What a stop really costs against staying out: the 3 minutes in
            # the box plus the pit lane itself.  Measure it in practice and set
            # it — the minimum alone understates the loss.
            "pit_loss_seconds": 200,
            "driver_min_minutes": 120,    # §3.14 every driver, over the event
            "pace_drop_warn": 0.30,
            "pace_drop_box": 0.50,
            # Fatigue: how far a driver's laps have to drop against their own
            # opening ones before it is worth saying, and how long they have to
            # have been in the kart before a drop means tiredness rather than
            # a scruffy first few laps.
            "fade_warn_s": 0.40,
            "fade_after_minutes": 25,
        },
        "karts": dict(kartpool.DEFAULTS),
    }
    race = karts = {}
    if os.path.exists(_CFG_PATH):
        with open(_CFG_PATH) as f:
            saved = json.load(f)
        # The two nested sections are merged, not replaced: a saved file that
        # is missing a key should fall back to the default for it, not lose it.
        race, karts = saved.pop("race", {}), saved.pop("karts", {})
        base.update(saved)
        base["race"]["category"] = race.get("category", base["race"]["category"])
    # Category first, saved values second.  The category fills in the stops and
    # the stint ceiling; a number typed into the settings box then overrides it.
    # The other order silently undid every such edit on the next restart.
    apply_category(base["race"])
    base["race"].update(race)
    base["karts"].update(karts)
    return base

# §3.8 and §3.10: the only two numbers that differ between the categories.
CATEGORY_RULES = {
    "PRO": {"mandatory_pits": 28, "stint_max_minutes": 80},
    "AM":  {"mandatory_pits": 34, "stint_max_minutes": 60},
}

def apply_category(race: dict):
    """Fill in the category's stops and stint ceiling.

    Called before the saved config is merged, so this is the default a saved
    number overrides — naming a category is enough to get that category's
    numbers, and typing one in by hand still wins.
    """
    rules = CATEGORY_RULES.get(str(race.get("category", "")).upper())
    if rules:
        race.update(rules)

CFG = load_cfg()

def save_cfg():
    with open(_CFG_PATH, "w") as f:
        json.dump(CFG, f, indent=2)

# ── Database ───────────────────────────────────────────────────────────────────
# WARROOM_DB lets a second instance (or a test) run against its own race,
# which is how the mock race and the real one stay out of each other's way.
DB = os.environ.get("WARROOM_DB") or os.path.join(
    os.path.dirname(__file__), "data", "race.db")
# A mounted disk starts empty and a hand-set path may point anywhere, and
# sqlite will not make the folder for us — it just refuses to open the file,
# which reaches the wall as "unable to open database file" and no war room at
# all.  Cheaper to create it than to debug that on the Friday.
os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)

def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with sqlite3.connect(DB) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS drivers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                total_seconds REAL DEFAULT 0,
                sort_order INTEGER DEFAULT 99
            );
            CREATE TABLE IF NOT EXISTS stints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                driver_id INTEGER,
                start_ts TEXT,
                end_ts TEXT,
                duration_seconds REAL,
                FOREIGN KEY(driver_id) REFERENCES drivers(id)
            );
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );
            INSERT OR IGNORE INTO kv VALUES ('status',       'idle');
            INSERT OR IGNORE INTO kv VALUES ('race_start',   '');
            INSERT OR IGNORE INTO kv VALUES ('stint_start',  '');
            INSERT OR IGNORE INTO kv VALUES ('pit_start',    '');
            INSERT OR IGNORE INTO kv VALUES ('driver_id',    '');
            INSERT OR IGNORE INTO kv VALUES ('pit_plan',     '');
                """)
        # How long the box stop after this stint actually took. Without it
        # pit_loss_seconds can only ever be a guess.
        _add_column(con, "stints", "box_seconds", "REAL")
        # init_db() runs on every import (gunicorn needs that too), which
        # every test also triggers -- so seeding here unconditionally would
        # plant 7 real drivers ahead of whatever id a test expects to create
        # first.  Opt-in only: WARROOM_SEED_TURNO=1 python3 app.py, once, on
        # a fresh checkout.
        if os.environ.get("WARROOM_SEED_TURNO"):
            _seed_turno_plan(con)

# The turno sheet handed out before the race: 7 drivers and their planned
# start time for each of the 35 stints (turn 1 is the starting driver, before
# any stop; turns 2-35 are the 34 mandatory stops in order).  Seeded once, on
# an empty database, so a fresh checkout on race day starts with the plan
# already in instead of needing it typed in by hand under pressure.
TURNO_DRIVER_ORDER = ["BALIKÓ", "CASINHA", "CONTENTE", "DINIS", "CAXI", "BERNA", "LOBO"]
TURNO_TURNS = [
    (1, "DINIS", "13:00"), (2, "BALIKÓ", "13:45"), (3, "BERNA", "14:26"),
    (4, "CAXI", "15:09"), (5, "BALIKÓ", "15:52"), (6, "BERNA", "16:35"),
    (7, "CAXI", "17:18"), (8, "BALIKÓ", "18:01"), (9, "BERNA", "18:44"),
    (10, "CAXI", "19:27"), (11, "BALIKÓ", "20:10"), (12, "CASINHA", "20:53"),
    (13, "CAXI", "21:36"), (14, "DINIS", "22:19"), (15, "LOBO", "23:02"),
    (16, "CONTENTE", "23:45"), (17, "BERNA", "00:28"), (18, "DINIS", "01:11"),
    (19, "LOBO", "01:54"), (20, "CONTENTE", "02:37"), (21, "BERNA", "03:20"),
    (22, "CASINHA", "04:03"), (23, "BALIKÓ", "04:46"), (24, "CAXI", "05:29"),
    (25, "DINIS", "06:12"), (26, "LOBO", "06:55"), (27, "CONTENTE", "07:38"),
    (28, "CASINHA", "08:21"), (29, "LOBO", "09:04"), (30, "CONTENTE", "09:47"),
    (31, "CASINHA", "10:30"), (32, "LOBO", "11:13"), (33, "CONTENTE", "11:56"),
    (34, "CASINHA", "12:39"), (35, "DINIS", "13:22"),
]

def _seed_turno_plan(con):
    """Load the turno sheet into an empty database. A no-op once any driver
    already exists, so this never overwrites a plan the crew has edited."""
    if con.execute("SELECT 1 FROM drivers LIMIT 1").fetchone():
        return
    for i, name in enumerate(TURNO_DRIVER_ORDER):
        con.execute("INSERT INTO drivers(name, sort_order) VALUES (?,?)", (name, i))
    ids = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM drivers")}
    plan = [{"driver_id": None, "note": ""} for _ in range(len(TURNO_TURNS) - 1)]
    for turn, name, time in TURNO_TURNS:
        if turn == 1:
            con.execute("INSERT OR REPLACE INTO kv VALUES ('driver_id', ?)",
                        (str(ids[name]),))
            continue
        plan[turn - 2] = {"driver_id": ids[name], "note": "", "time": time}
    con.execute("INSERT OR REPLACE INTO kv VALUES ('pit_plan', ?)",
                (json.dumps(plan),))

def _add_column(con, table: str, col: str, decl: str):
    """Add a column to an existing database, once. SQLite has no IF NOT EXISTS
    for columns, so the existing ones are read first."""
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    if col not in have:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def get_db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con

def kv_get(key: str) -> str:
    with get_db() as con:
        row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""

def kv_set(key: str, val: str):
    with get_db() as con:
        con.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, str(val)))

# ── Timing parser ──────────────────────────────────────────────────────────────
_CELL_MAP = {
    "rk": "pos", "pos": "pos",
    # Apex "no" is the team's race number: it stays put all race, while the
    # physical kart under the team changes at every stop (see kartpool.py).
    "no": "kart", "kart": "kart",
    "dr": "driver", "driver": "driver",
    "name": "team", "team": "team",
    "cat": "category", "class": "category", "grp": "category",
    "llp": "last_lap", "blp": "best_lap", "tlp": "total_laps",
    # Sectors.  Where a lap was lost is the only thing a lap time cannot say,
    # and it is what a driver debrief is actually about.
    "s1": "s1", "s2": "s2", "s3": "s3",
    "pit": "pits", "gap": "gap", "int": "interval",
    # Apex timing state classes (only appear on lap-time cells)
    "tb": "last_lap", "ti": "last_lap", "tn": "last_lap", "ib": "best_lap",
}

# The last-lap cell's own class is Apex's verdict on that lap, sent for every
# lap of every session in every fixture we have: purple against the field,
# green against the kart's own best, or plain.  It was being read only to
# guess the column when the header did not say — the colour itself was
# thrown away, which is the one thing a real timing screen never does.
LAP_MARK = {"tb": "sb", "ti": "pb"}

_global_col_types: dict = {}   # "c6" -> "last_lap" (built from grid header row)
# Every column the header declared, including the ones we do not read.  A
# column the header named is never guessed at from a CSS class, which is how
# a sector time came to be read as a lap time.
_global_head_cols: set = set()
_row_kart_map: dict     = {}   # "r14915" -> "17"    (built from parsed data rows)

class ApexParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self._cur, self._col, self._mark = [], None, None, None
        self.meta: dict = {}        # data-id -> {"text":..., "cls":...}
        self._meta_id: Optional[str] = None
        self._is_head  = False
        self._row_did: Optional[str] = None
        self.col_types: dict    = {}   # c6 -> "last_lap" (from head row data-type)
        self.head_cols: set     = set()  # every column the header declared
        self.saw_head = False          # this frame carried its own header
        self.row_kart_map: dict = {}   # r14915 -> "17"

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        did = a.get("data-id")
        if did:
            self._meta_id = did
            cls = a.get("class", "").strip()
            if cls:
                self.meta.setdefault(did, {})["cls"] = cls
        if tag == "tr":
            tr_cls = a.get("class", "").split()
            self._is_head = "head" in tr_cls
            if self._is_head:
                self.saw_head = True
            self._row_did = did
            self._cur = None if self._is_head else {"row_cls": " ".join(tr_cls)}
            self._meta_id = None
        elif tag in ("td", "th"):
            self._col = None
            self._mark = None
            dt = a.get("data-type", "")
            if self._is_head and did:
                # Every declared column is recorded, mapped or not: "this is S1"
                # is as much an answer as "this is the last lap".
                self.head_cols.add(did)
                self.saw_head = True
                if dt in _CELL_MAP:
                    self.col_types[did] = _CELL_MAP[dt]
            elif dt in _CELL_MAP and self._cur is not None:
                self._col = _CELL_MAP[dt]
            elif self._cur is not None:
                # The cell's data-id ends in the header's column id, so "r93c8"
                # is whatever column c8 was declared to be.  Prefer the header
                # in this very frame — the module-level map is only updated
                # once the whole frame is parsed, so on the first frame (the
                # one that carries the header) it is still empty.
                types = self.col_types if self.saw_head else _global_col_types
                known = self.head_cols if self.saw_head else _global_head_cols
                col_m = re.search(r'(c\d+)$', did or "")
                col = col_m.group(1) if col_m else None
                if col and col in known:
                    # The header's word is final, even when the answer is "not
                    # a column we read".  Guessing from the CSS class here is
                    # how Palmela's S1 landed in the lap time: the sector cell
                    # carries the same "tn" class as the lap cell and comes
                    # first, so it won.
                    self._col = types.get(col)
                else:
                    for cls in a.get("class", "").split():
                        if cls in _CELL_MAP:
                            self._col = _CELL_MAP[cls]
                            break
                if self._col == "last_lap":
                    for cls in a.get("class", "").split():
                        if cls in LAP_MARK:
                            self._mark = LAP_MARK[cls]
                            break

    def handle_data(self, data):
        v = data.strip()
        if not v:
            return
        if self._meta_id:
            self.meta.setdefault(self._meta_id, {})["text"] = v
        if self._col and self._cur is not None:
            self._cur.setdefault(self._col, v)
            if self._col == "last_lap":
                self._cur.setdefault("last_lap_mark", self._mark or "")
            self._col = None

    def handle_endtag(self, tag):
        if tag in ("td", "th", "div", "span"):
            self._meta_id = None
        if tag == "tr":
            if self._cur and len(self._cur) >= 3:
                self.rows.append(self._cur)
                if self._row_did and self._cur.get("kart"):
                    self.row_kart_map[self._row_did] = self._cur["kart"]
            self._cur = None

def parse_laptime(s: str) -> Optional[float]:
    if not s or s.strip() in ("", "-", "--", "–"):
        return None
    s = s.strip()
    try:
        if ":" in s:
            m, rest = s.split(":", 1)
            return int(m) * 60 + float(rest)
        return float(s)
    except Exception:
        return None

def fmt_laptime(secs: Optional[float]) -> str:
    if secs is None:
        return "-"
    m, s = divmod(secs, 60)
    return f"{int(m)}:{s:06.3f}"

def fmt_duration(secs: float) -> str:
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

def fmt_mmss(secs: float) -> str:
    secs = max(0, int(secs))
    m, s = divmod(secs, 60)
    return f"{m}:{s:02d}"

def log(event: str, detail: str = ""):
    ts = datetime.utcnow().strftime("%H:%M:%S")
    line = f"[{ts}] {event}"
    if detail:
        line += f"  {detail}"
    print(line, flush=True)

# ── Shared live state ──────────────────────────────────────────────────────────
_lock = threading.Lock()
_teams: list = []
_lap_hist: dict = {}   # team_key -> [float, ...]
_apex_ok = False
_apex_session: dict = {"name": "", "light": "", "dyn1": "", "dyn2": "",
                       "weather": [], "control": []}
_ws_msg_count = 0

def _process_meta(meta: dict):
    """Update session state from parsed data-id elements.
    Handles both ApexParser dict format {text,cls} and pipe parser plain strings."""
    global _apex_session

    def _val(v):
        # Weather and the control log arrive already structured; everything
        # else is a cell that may be {"text":..,"cls":..} or a bare string.
        if isinstance(v, (list, tuple)):
            return list(v)
        return (v.get("text", "") or v.get("cls", "")) if isinstance(v, dict) else str(v)

    updated = {}
    for key, field in [
        ("title1", "name"), ("title2", "name"), ("name", "name"),
        ("light", "light"),
        ("dyn1", "dyn1"), ("dyn2", "dyn2"),
        ("track", "track"),
        ("weather", "weather"),
        ("control", "control"),
    ]:
        if key in meta and field not in updated:
            v = _val(meta[key])
            if v:
                updated[field] = v

    if updated:
        with _lock:
            _apex_session.update(updated)
        log("APEX SESSION", "  ".join(f"{k}={v}" for k, v in updated.items() if v))

    # The organisers' clock lives in the dyn header fields. Prefer it to ours:
    # it knows about red flags and it does not depend on anyone pressing START.
    for field in ("dyn1", "dyn2"):
        if field in meta and _apex_clock.update(_val(meta[field])):
            _apex_clock.set_total(CFG["race"]["duration_minutes"] * 60)

# ── Kart pool ─────────────────────────────────────────────────────────────────
_apex_clock = ApexClock()

def _feed_boxed_me(_team_no: str):
    """The feed saw our kart enter the pit lane — start the box clock for real."""
    if kv_get("status") == "racing":
        _do_box(0.0, source="feed")

def _feed_released_me(_team_no: str):
    """Our kart is running again — close the stop without anyone pressing a key."""
    if kv_get("status") == "pitting":
        _do_pit_done(kv_get("driver_id"), source="feed")

POOL = kartpool.KartPool(DB, CFG.get("karts", {}),
                         on_my_stop=_feed_boxed_me,
                         on_my_release=_feed_released_me)

# ── Apex Timing data ingestion ─────────────────────────────────────────────────
_PIT_MARK = re.compile(r'\bpit\b|\bbox\b|\bin_?pit\b', re.I)

def _enrich(t: dict) -> dict:
    # Events without a separate team column name the competitor in the driver
    # cell; the rest of the app keys off "team", so make sure it is filled.
    if not t.get("team") and t.get("driver"):
        t["team"] = t["driver"]
    # Apex marks a kart in the pit lane on the row itself and, on some events,
    # by writing PIT over the last-lap cell.
    t["in_pit"] = bool(_PIT_MARK.search(t.get("row_cls", "") or "")) or \
        _PIT_MARK.fullmatch((t.get("last_lap") or "").strip()) is not None
    key = t.get("kart") or t.get("team") or str(t.get("pos", ""))
    ll = parse_laptime(t.get("last_lap", ""))
    hist = _lap_hist.setdefault(key, [])
    if ll and (not hist or hist[-1] != ll):
        hist.append(ll)
        del hist[:-30]
    t["last_lap_s"]  = ll
    t["best_lap_s"]  = parse_laptime(t.get("best_lap", ""))
    t["avg5_s"]  = sum(hist[-5:])  / len(hist[-5:])  if hist else None
    t["avg10_s"] = sum(hist[-10:]) / len(hist[-10:]) if hist else None
    t["avg5"]    = fmt_laptime(t["avg5_s"])
    t["avg10"]   = fmt_laptime(t["avg10_s"])
    t["sectors"] = mark_sectors(str(key), t, _sector_best["session"],
                                _sector_best["by_kart"])
    return t

# Sector benchmarks, the way every timing screen in the paddock reads them:
# purple is the fastest anyone has gone through that sector all session, green
# is this kart's own best, yellow is slower than its own best.
_sector_best: dict = {"session": {}, "by_kart": {}}
SECTORS = ("s1", "s2", "s3")


def _reset_sector_best():
    _sector_best["session"] = {}
    _sector_best["by_kart"] = {}


def note_sectors(kart: str, row: dict, session: dict, by_kart: dict):
    """Fold one kart's sector times into the session and its own bests.

    Every row in a grid is folded in before any row is coloured.  Rows arrive
    in whatever order Apex sends them, so a kart coloured before a quicker one
    had been read would keep a purple that was no longer its own — which is
    how the slowest kart at Palmela came to be shown in purple.
    """
    mine = by_kart.setdefault(kart, {})
    for key in SECTORS:
        t = parse_laptime((row.get(key) or "").strip())
        if t is None or t <= 0:
            continue
        if session.get(key) is None or t < session[key]:
            session[key] = t
        if mine.get(key) is None or t < mine[key]:
            mine[key] = t


def mark_sectors(kart: str, row: dict, session: dict, by_kart: dict) -> dict:
    """Colour one kart's sectors against the session and against itself.

    Returns ``{"s1": {"t": "24.176", "mark": "sb"}, ...}`` where the mark is
    ``sb`` (purple, fastest in the session), ``pb`` (green, this kart's own
    best) or ``slow`` (yellow).  Read off the bests as they stand, so when
    someone takes a purple the previous holder goes green on the next frame.

    A sector the feed has not sent is left out rather than shown as nought.
    """
    note_sectors(kart, row, session, by_kart)
    mine = by_kart.get(kart, {})
    out = {}
    for key in SECTORS:
        raw = (row.get(key) or "").strip()
        t = parse_laptime(raw)
        if t is None or t <= 0:
            continue
        out[key] = {"t": raw,
                    "mark": ("sb" if t <= session.get(key, t)
                             else "pb" if t <= mine.get(key, t) else "slow")}
    return out


def _process_rows(rows: list) -> bool:
    """Replace _teams with a newly-parsed full grid. Returns True if any rows."""
    global _teams, _apex_ok
    if not rows:
        return False
    global _feed_team_name
    matched = match_team(CFG.get("team_name", ""),
                         [r.get("team", "") for r in rows])
    if matched and matched != _feed_team_name:
        log("APEX TEAM", f"{CFG.get('team_name','')!r} is {matched!r} in the feed")
    _feed_team_name = matched or ""
    with _lock:
        # Two passes: every sector in this grid is read before any of them is
        # coloured, so the order Apex happens to send the rows in cannot decide
        # who holds the purple.
        for r in rows:
            note_sectors(str(r.get("kart") or r.get("team") or r.get("pos", "")),
                         r, _sector_best["session"], _sector_best["by_kart"])
        built = [_enrich(dict(r)) for r in rows]
        if built:
            _teams = built
            _apex_ok = True
    # Outside the lock: the pool's callbacks take it themselves.
    _feed_pool(built)
    return True

def _feed_pool(rows: list):
    if not rows:
        return
    try:
        POOL.observe(rows, my_team=my_team_name())
    except Exception as e:
        log("KARTPOOL ERR", str(e))

def _apply_cell_updates(cell_updates: dict) -> bool:
    """Apply incremental C-command cell updates to existing _teams in-place."""
    if not cell_updates:
        return False
    with _lock:
        for t in _teams:
            kart = t.get("kart")
            if kart in cell_updates:
                t.update(cell_updates[kart])
                _enrich(t)
        snapshot = list(_teams)
    _feed_pool(snapshot)
    return True

_ws_url_cache: Optional[str] = None   # "" means checked and not found
_ws_url_checked_at: float = 0.0
_apex_endpoints: dict = {}            # {"ws": ..., "ajax": ..., "port": ...}

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120"

def _http_get(url: str, referer: str = "", timeout: int = 5) -> str:
    headers = {"User-Agent": _UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")

DISCOVERY_TTL_S = 300      # how long a working answer is trusted
DISCOVERY_RETRY_S = 15     # how soon a failed one is tried again

def _find_apex_endpoints(page_url: str) -> dict:
    """Read the event page + its config.js and work out where the data lives.

    Apex serves the page from live.apex-timing.com but the timing feed from
    whatever `configHost` names — live-data.apex-timing.com, in practice.  The
    live timing JS derives both transports from `configPort`:

        wss://<configHost>:<configPort + 3>/          the WebSocket
        <configRequestUrl>live_ajax.php?port=<+4>     the polling fallback

    Result is cached for 5 minutes.  Returns {} when nothing could be found.
    """
    global _ws_url_cache, _ws_url_checked_at, _apex_endpoints
    now_t = time.time()
    # A discovery that worked is good for five minutes.  One that failed is
    # good for fifteen seconds: over twenty-five hours the network will drop a
    # handshake sooner or later, and caching that answer alongside a real one
    # blacked the feed out for five minutes every time it happened.
    ttl = DISCOVERY_TTL_S if _apex_endpoints else DISCOVERY_RETRY_S
    if _ws_url_checked_at and now_t - _ws_url_checked_at < ttl:
        return _apex_endpoints
    _ws_url_checked_at = now_t
    _apex_endpoints, _ws_url_cache = {}, ""

    if not page_url:
        return _apex_endpoints

    try:
        base = page_url.split('#')[0].rstrip('/')
        src = _http_get(base)

        # 1. Apex Timing pattern: the event's own config.js holds host and port.
        for script_src in re.findall(r"""<script[^>]+src=['"]([^'"]+)['"]""", src):
            if "config.js" not in script_src:
                continue
            config_url = urllib.parse.urljoin(base + "/", script_src)
            try:
                cfg_src = _http_get(config_url, referer=base)
            except Exception:
                continue
            m = re.search(r'configPort\s*=\s*(\d+)', cfg_src)
            if not m:
                continue
            port = int(m.group(1))
            h = re.search(r"""configHost\s*=\s*['"]([^'"]+)['"]""", cfg_src)
            host = h.group(1) if h else urllib.parse.urlparse(base).netloc
            a = re.search(r"""configRequestUrl\s*=\s*['"](https?://[^'"]+)['"]""", cfg_src)
            ajax = a.group(1) if a else f"https://{host}/live-timing/commonv2/functions/"
            _apex_endpoints = {
                "ws": f"wss://{host}:{port + 3}/",
                "ajax": ajax.rstrip('/') + "/live_ajax.php",
                "port": port,
                "referer": base + "/",
            }
            _ws_url_cache = _apex_endpoints["ws"]
            print(f"[apex] host={host} port={port} ws={_ws_url_cache}", flush=True)
            return _apex_endpoints

        # 2. Fallback: an explicit WebSocket URL written into the page JS.
        m = re.search(r"""new\s+WebSocket\s*\(\s*['"]([^'"]+)['"]""", src)
        if m:
            _ws_url_cache = m.group(1)
            _apex_endpoints = {"ws": _ws_url_cache, "referer": base + "/"}
            print(f"[apex] WS URL explicit: {_ws_url_cache}", flush=True)
            return _apex_endpoints

        # 3. Fallback: a bare port variable inline in the page.
        m = re.search(r"""(?:configPort|wsPort|ws_port)\s*=\s*(\d{3,5})""", src, re.I)
        if m:
            port = int(m.group(1))
            host = urllib.parse.urlparse(base).netloc
            _ws_url_cache = f"wss://{host}:{port + 3}/"
            _apex_endpoints = {"ws": _ws_url_cache, "port": port, "referer": base + "/"}
            print(f"[apex] WS URL from inline port: {_ws_url_cache}", flush=True)
            return _apex_endpoints

        print("[apex] No feed endpoints found on the event page", flush=True)
    except Exception as e:
        print(f"[apex] Discovery error: {e}", flush=True)
    return _apex_endpoints

def _find_ws_url(page_url: str) -> Optional[str]:
    """The WebSocket URL alone, for callers that only speak WS."""
    return _find_apex_endpoints(page_url).get("ws") or None

# live_ajax.php is a resumable cursor: it hands back the init flag and index to
# send on the next call, so each poll returns only what changed since the last.
_ajax_state: dict = {"init": "1", "index": "0", "counter": 0, "errors": 0}

def _fetch_http(page_url: str) -> str:
    """Poll Apex's AJAX fallback and return one pipe-protocol payload.

    The response is `init@index@payload`, where payload is byte-for-byte what
    the WebSocket would have pushed — so the caller parses it the same way.
    """
    ep = _find_apex_endpoints(page_url)
    if not ep.get("ajax") or not ep.get("port"):
        return ""
    _ajax_state["counter"] += 1
    q = urllib.parse.urlencode({
        "version": "1.0.0",
        "init": _ajax_state["init"],
        "index": _ajax_state["index"],
        "port": ep["port"] + 4,
        "counter": _ajax_state["counter"],
        "duration": 0,
        "id": 0,
        "ignored": "",
    })
    try:
        text = _http_get(f"{ep['ajax']}?{q}", referer=ep.get("referer", page_url), timeout=8)
    except Exception as e:
        # An empty return means "nothing new" to every caller, so a poll that
        # never got through has to be counted separately — otherwise a track
        # we could not reach is indistinguishable from a track sitting idle.
        _ajax_state["errors"] = _ajax_state.get("errors", 0) + 1
        print(f"[apex] AJAX poll failed: {e}", flush=True)
        return ""
    parts = text.split("@", 2)
    if len(parts) < 3:
        return ""
    _ajax_state["init"], _ajax_state["index"] = parts[0], parts[1]
    if parts[2] == "REFRESH_BROWSER":
        # Apex wants a clean slate; drop the cursor so the next poll re-inits.
        _ajax_state.update({"init": "1", "index": "0"})
        return ""
    return parts[2]

def _apex_request(page_url: str, request: str, timeout: int = 20) -> str:
    """POST one request to Apex's request.php and return the body.

    The same host and port discovery the live feed uses; this is the channel
    the site's own "previous lives" menu talks to.
    """
    ep = _find_apex_endpoints(page_url)
    if not ep.get("ajax") or not ep.get("port"):
        return ""
    url = ep["ajax"].rsplit("/", 1)[0] + "/request.php"
    data = urllib.parse.urlencode({"port": ep["port"], "request": request})
    req = urllib.request.Request(
        url, data=data.encode(),
        headers={"User-Agent": _UA, "Referer": ep.get("referer", page_url),
                 "Content-Type": "application/x-www-form-urlencoded"})
    # A one-shot request, unlike the live poll: nothing comes round again in
    # five seconds to cover for it, so a dropped connection is worth retrying.
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="ignore").strip()
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    print(f"[apex] request {request!r} failed: {last}", flush=True)
    return ""


def apex_sessions(page_url: str) -> list:
    """Every session this track has archived, newest first.

    Apex answers ``id#name`` a line at a time.  "error" and an empty body both
    mean no history, which is what a track that has just come online looks
    like — not a failure worth shouting about.
    """
    body = _apex_request(page_url, "S#")
    if not body or body == "error":
        return []
    out = []
    for line in body.split("\n"):
        sid, _, name = line.strip().partition("#")
        if sid:
            out.append({"id": sid, "name": name or sid})
    return out


def apex_session_result(page_url: str, sid: str) -> dict:
    """The final classification of an archived session.

    This is the whole of what Apex keeps: where everyone finished, their best
    lap and their lap count — not the individual laps.  A kart's rating needs
    many laps shared between drivers, so a classification cannot feed it; it
    is a result to read, and it is exactly what settles "what did we do in
    qualifying".
    """
    body = _apex_request(page_url, f"S#{sid}")
    if not body or body == "error":
        return {"rows": [], "meta": {}}
    parts = body.split("@", 2)
    payload = parts[2] if len(parts) > 2 else body
    rows, _cells, meta = _parse_apex_pipe(payload)
    return {"rows": rows, "meta": {k: v for k, v in meta.items() if v},
            "id": sid}


def _reset_ajax_state():
    _ajax_state.update({"init": "1", "index": "0", "counter": 0, "errors": 0})

# An incremental cell arrives as its own command, not under "C": KIP sends
# "r56c9|tn|1:12.502" — row 56, column 9, the CSS class, then the text.
_PIPE_CELL = re.compile(r'^(r\w+?)(c\d+)$')

def _parse_apex_pipe(msg: str) -> tuple:
    """Parse Apex Timing pipe-delimited WebSocket protocol.
    Returns (rows, cell_updates, meta).
    rows = full row dicts for _process_rows,
    cell_updates = {kart: {field: value}} for _apply_cell_updates,
    meta = session info for _process_meta."""
    global _global_col_types, _global_head_cols, _row_kart_map
    rows: list = []
    cell_updates: dict = {}
    meta: dict = {}
    for line in msg.replace('\r', '').split('\n'):
        line = line.strip()
        if not line:
            continue
        parts = line.split('|', 2)   # max 3 fields; value may contain pipes
        cmd = parts[0]
        mod = parts[1].strip() if len(parts) > 1 else ''
        val = parts[2]         if len(parts) > 2 else ''

        if cmd == 'grid' and val:
            hp = ApexParser()
            hp.feed(val)
            if hp.col_types:
                # Replace, never merge: a layout with fewer columns than the
                # last one must not inherit the leftovers.
                _global_col_types.clear()
                _global_col_types.update(hp.col_types)
                _global_head_cols.clear()
                _global_head_cols.update(hp.head_cols)
            if hp.row_kart_map:
                _row_kart_map.update(hp.row_kart_map)
            rows.extend(hp.rows)

        elif cmd in ('R', 'row') and val:
            hp = ApexParser()
            hp.feed(val)
            if hp.row_kart_map:
                _row_kart_map.update(hp.row_kart_map)
            rows.extend(hp.rows)

        elif _PIPE_CELL.match(cmd):
            # The live form.  Apex only sends the grid once; everything after
            # it — every lap time, lap count, position and gap — comes through
            # here.  Dropping these left the board frozen on the opening grid
            # for the whole session, refreshing only when a dropped poll forced
            # a full resend.
            m = _PIPE_CELL.match(cmd)
            row_id, col_id = m.group(1), m.group(2)
            kart = _row_kart_map.get(row_id)
            # The header is the authority on what a column holds; the CSS class
            # is only a fallback, for the same reason a sector time once got
            # read as a lap time.
            field = _global_col_types.get(col_id) or _CELL_MAP.get(mod)
            if kart and field:
                text = re.sub(r'<[^>]+>', '', val).strip()
                if text:
                    cell_updates.setdefault(kart, {})[field] = text
                    if field == "last_lap":
                        # Always written, even blank: a normal lap has to
                        # clear a purple the kart posted an hour ago, not
                        # leave it glowing on a merge into the existing row.
                        cell_updates[kart]["last_lap_mark"] = LAP_MARK.get(mod, "")

        elif re.match(r'^r\w+$', cmd) and mod in ('*out', '*in'):
            # Some endurance events (lemans-karting2, 2026-09-18) push a pit
            # lane entry/exit as a bare row command instead of resending the
            # row's CSS class, which we otherwise only ever see once, in the
            # opening grid.  Without this a kart's in_pit status freezes at
            # whatever the grid said and never updates again for the rest of
            # the session.  Routing it through row_cls means _enrich's
            # existing _PIT_MARK check picks it up for free.
            kart = _row_kart_map.get(cmd)
            if kart:
                cell_updates.setdefault(kart, {})["row_cls"] = (
                    "pit" if mod == '*out' else "")

        elif cmd == 'C' and mod:
            # Incremental cell update: mod="r14915c6", val="<td ...>0:52.3</td>"
            m = re.match(r'(r\w+?)(c\d+)$', mod)
            if m:
                row_id, col_id = m.group(1), m.group(2)
                kart  = _row_kart_map.get(row_id)
                field = _global_col_types.get(col_id)
                if kart and field:
                    text = re.sub(r'<[^>]+>', '', val).strip()
                    if text:
                        cell_updates.setdefault(kart, {})[field] = text

        elif cmd in ('title1', 'title2') and val.strip():
            meta['name'] = val.strip()

        elif cmd == 'dyn1':
            meta['dyn1'] = val.strip()

        elif cmd == 'dyn2':
            meta['dyn2'] = val.strip()

        elif cmd == 'light':
            # mod = lr (red) | lg (green) | ly (yellow) | lsc (safety car)
            meta['light'] = mod

        elif cmd == 'track' and val.strip():
            meta['track'] = val.strip()

        elif cmd == 'com' and val.strip():
            # Race control's own log, which Apex has been sending all along:
            # "<p><b>21:05</b><span data-flag="green"></span>Start</p>".
            meta['control'] = parse_control_log(val)

        elif cmd in ('wth1', 'wth2', 'wth3') and val.strip():
            meta.setdefault('weather', []).append(re.sub(r'<[^>]+>', '', val).strip())

    return rows, cell_updates, meta


_CONTROL_ENTRY = re.compile(r'<p\b[^>]*>(.*?)</p>', re.I | re.S)
_CONTROL_TIME  = re.compile(r'<b[^>]*>(.*?)</b>', re.I | re.S)
_CONTROL_FLAG  = re.compile(r'data-flag="([^"]*)"', re.I)

# Not followed by a colon: a timestamp that escaped the strip above opens
# exactly like a competitor number, and "14:50" is not kart 14.
_CONTROL_KART = re.compile(r'^\s*(\d{1,3})(?![:\d])')

def parse_control_log(html_s: str) -> list:
    """Race control's messages, newest first.

    Safety car and red flag periods are the cheapest stops of the race — a stop
    taken under a neutralisation costs a fraction of one taken under green — so
    this is worth reading rather than discarding.  Who a message is about is
    control_for_kart's job, which needs the field to tell a kart from an hour.
    """
    out = []
    for block in _CONTROL_ENTRY.findall(html_s) or ([html_s] if html_s.strip() else []):
        t = _CONTROL_TIME.search(block)
        f = _CONTROL_FLAG.search(block)
        text = re.sub(r'<[^>]+>', ' ', _CONTROL_TIME.sub('', block))
        text = re.sub(r'\s+', ' ', text).strip()
        if not (text or f):
            continue
        out.append({"at": (t.group(1).strip() if t else ""),
                    "flag": (f.group(1).strip().lower() if f else ""),
                    "text": text})
    return out


def control_for_kart(control: list, karts: set) -> list:
    """Tag each control message with the kart it is about, where there is one.

    A message aimed at one competitor opens with their number, whatever
    language the event runs in — RKC sends "15 Avertissement - Passage au stand
    en 00:56", which is kart 15 warned for a 56 second stop, the same rule
    §15.1 charges us 20s a block for.  A leading number on its own proves
    nothing, though: "15 minutes remaining" starts the same way and a stray
    timestamp would too.  So it only counts when that number is a kart the
    feed is actually showing.
    """
    out = []
    for e in control:
        m = _CONTROL_KART.match(e.get("text", ""))
        kart = m.group(1) if m and m.group(1) in karts else ""
        out.append({**e, "kart": kart})
    return out

def _ws_run(ws_url: str):
    """Connect to Apex Timing WebSocket, push rows on every message."""
    global _apex_ok, _ws_msg_count

    def on_msg(ws, msg):
        """A frame off the socket goes through the same door as a polled one.

        It used to have its own copy of the dispatch, plus a branch for JSON
        and one for bare HTML.  Across every frame of every recording — ten
        sessions, five tracks — neither has ever fired: Apex speaks the pipe
        protocol on both transports.  So the socket now hands the payload to
        _consume_pipe, which is the same code the AJAX fallback has been
        running all along, and the one the tests cover.
        """
        global _ws_msg_count
        s = (msg or "").strip()
        if not s:
            return
        _ws_msg_count += 1
        # The first frame in full: it is the only look we get at what the
        # socket actually speaks if it ever turns out not to be this.
        if _ws_msg_count == 1:
            log("APEX WS MSG#1", f"{len(s)} bytes\n{s[:3000]}")
        elif _ws_msg_count % 20 == 0:
            log("APEX WS", f"msg #{_ws_msg_count} | teams={len(_teams)} | "
                           f"session={_apex_session.get('name','?')}")
        got = _consume_pipe(s)
        # The opening frames are where a socket that speaks something else
        # would give itself away, so say whether they read as anything.
        if _ws_msg_count <= 3:
            log("APEX PARSED" if got else "APEX WS UNREAD",
                f"{len(_teams)} teams on the board")

    def on_open(ws):
        global _apex_ok, _ws_msg_count
        _ws_msg_count = 0
        _apex_ok = True
        log("APEX WS CONNECTED", ws_url)

    def on_close(ws, code, msg):
        global _apex_ok
        _apex_ok = False
        log("APEX WS CLOSED", f"code={code}")

    def on_error(ws, _err):
        global _apex_ok
        _apex_ok = False
        log("APEX WS ERROR", str(_err))

    ws = _ws_mod.WebSocketApp(ws_url,
        on_message=on_msg, on_error=on_error,
        on_close=on_close, on_open=on_open)
    ws.run_forever(ping_interval=30, ping_timeout=10)

# ── Pit plan ───────────────────────────────────────────────────────────────────
def get_pit_plan() -> list:
    raw = kv_get("pit_plan")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    n = CFG["race"]["mandatory_pits"]
    plan = [{"driver_id": None, "note": ""} for _ in range(n)]
    kv_set("pit_plan", json.dumps(plan))
    return plan

def set_plan_stop(stop_idx: int, driver_id, time: str = None):
    plan = get_pit_plan()
    n = CFG["race"]["mandatory_pits"]
    while len(plan) < n:
        plan.append({"driver_id": None, "note": ""})
    if 0 <= stop_idx < n:
        plan[stop_idx]["driver_id"] = driver_id
        # A pre-agreed clock time (e.g. from the paper turno sheet) beats the
        # even split once someone has actually planned the stop by hand.
        if time is not None:
            plan[stop_idx]["time"] = time
    kv_set("pit_plan", json.dumps(plan[:n]))

# ── Background worker ──────────────────────────────────────────────────────────
_ws_blocked = False   # set once the WS host proves unreachable from here

def _consume_pipe(payload: str) -> bool:
    """Feed one pipe-protocol payload through the same path as a WS frame."""
    global _apex_ok
    if not payload:
        return False
    try:
        rows, cell_updates, meta = _parse_apex_pipe(payload)
    except Exception as e:
        log("APEX PIPE ERR", str(e))
        return False
    if meta:
        _process_meta(meta)
    if cell_updates:
        _apply_cell_updates(cell_updates)
        _apex_ok = True
    if _process_rows(rows):
        _apex_ok = True
    return bool(rows or cell_updates or meta)

def worker():
    global _apex_ok, _apex_session, _ws_blocked, _ws_msg_count
    while True:
        url = CFG.get("apex_url", "")

        if not url:
            time.sleep(5)
            continue

        ws_url = _find_ws_url(url) if _HAS_WS else None
        if ws_url and not _ws_blocked:
            _ws_msg_count = 0
            t = threading.Thread(target=_ws_run, args=(ws_url,), daemon=True)
            t.start()
            # The thread ends when run_forever returns, which is the only thing
            # we are waiting for: a close, an error, or a socket that never
            # opened.  Waiting on a close callback instead would sit here the
            # full ten minutes whenever run_forever bailed without firing one.
            t.join(timeout=600)
            # The timing ports are on a different host to the event page and are
            # not always reachable (corporate egress, a firewall between us and
            # live-data).  One silent attempt is enough to know — after that,
            # poll the AJAX fallback on 443 instead of retrying forever.
            if _ws_msg_count == 0:
                _ws_blocked = True
                log("APEX WS", "no frames — falling back to AJAX polling")
            else:
                time.sleep(3)  # brief pause before reconnect to avoid tight loop
                continue

        # AJAX polling fallback: same pipe payload, over plain HTTPS.
        if _consume_pipe(_fetch_http(url)):
            log("HTTP DATA", "frame parsed")
        else:
            with _lock:
                _apex_ok = False
        time.sleep(CFG.get("refresh_interval", 5))

# ── Penalties and virtual position ─────────────────────────────────────────────
def penalty_for_shortfall(short_s: float) -> int:
    """Seconds of penalty for being this many seconds short.

    §15.1 (box under 3 minutes), §15.4 (over the stint limit) and §15.5 (under
    a driver's required time) all use the same ladder: "até 10 segundos em
    falta penaliza 20 segundos; 11 a 20 segundos em falta penaliza 40 segundos
    e assim sucessivamente" — 20 seconds for every started block of ten.
    """
    if short_s <= 0:
        return 0
    return 20 * math.ceil(short_s / 10.0)


_LAP_GAP = re.compile(r'^\s*(\d+)\s*(?:(?:lap|volta|tour)s?|t)\b', re.I)

def gap_seconds(gap: str, lap_s: Optional[float]) -> Optional[float]:
    """Apex writes a gap either as seconds behind, or as whole laps.

    Laps only become a number of seconds once we know how long a lap takes, so
    without a lap time a lapped gap is unknown rather than zero.
    """
    if not gap:
        return None
    g = str(gap).strip().lstrip('+')
    if not g or g in ('-', '--'):
        return None
    m = _LAP_GAP.match(g)
    if m:
        return int(m.group(1)) * lap_s if lap_s else None
    try:
        if ':' in g:                     # m:ss.sss
            mins, secs = g.rsplit(':', 1)
            return int(mins) * 60 + float(secs)
        return float(g)
    except ValueError:
        return None


def virtual_positions(teams: list, mandatory_pits: int, pit_loss_s: float,
                      lap_s: Optional[float]) -> dict:
    """Where the order really stands once everyone's remaining stops are taken.

    On the road a team that has skipped its stops leads; it does not really.
    Each team still owes (mandatory - done) stops, and each stop costs about
    pit_loss_s, so the honest comparison adds that debt to the gap.

    Returns {kart: {"virtual_pos", "stops_owed", "debt_s"}}.
    Teams whose gap cannot be read keep their track position rather than being
    guessed at.
    """
    rows = []
    for t in teams:
        kart = str(t.get("kart", ""))
        if not kart:
            continue
        try:
            done = int(str(t.get("pits", "") or 0).strip() or 0)
        except ValueError:
            done = 0
        owed = max(0, mandatory_pits - done)
        gap = gap_seconds(t.get("gap", ""), lap_s)
        try:
            pos = int(str(t.get("pos", "") or 0).strip() or 0)
        except ValueError:
            pos = 0
        rows.append({"kart": kart, "pos": pos, "owed": owed,
                     "gap": 0.0 if pos == 1 else gap})

    known = [r for r in rows if r["gap"] is not None]
    if not known:
        return {}
    # Debt is the time a team still has to spend in the pits, whoever they are
    # — an absolute number, not a comparison, so it reads the same for the
    # leader as for the last car.  The order then falls out of where each team
    # is on the road plus what it still owes.
    for r in known:
        r["debt"] = r["owed"] * pit_loss_s
        r["virtual"] = r["gap"] + r["debt"]

    out = {}
    for i, r in enumerate(sorted(known, key=lambda r: r["virtual"]), start=1):
        out[r["kart"]] = {
            "virtual_pos": i,
            "stops_owed":  r["owed"],
            "debt_s":      round(r["debt"], 1),
        }
    return out

def next_karts_out(lanes: list) -> list:
    """The karts a team would be handed if it boxed right now.

    §3.13: the kart is "um de dois, definido por sorteio aleatório" — one of
    two, drawn at random.  The draw picks the *lane*; the kart is then whatever
    has been waiting longest in it.  So the two candidates are simply the front
    of each queue, and they are knowable before boxing even though which of the
    two you get is not.
    """
    out = []
    for lane in lanes:
        karts = lane.get("karts") or []
        if karts:
            out.append({**karts[0], "lane": lane.get("lane"),
                        "lane_name": lane.get("name", ""),
                        "lane_color": lane.get("color", "")})
    return out


def box_now_verdict(candidates: list, current_delta: Optional[float]) -> dict:
    """Is boxing now a trade up or down, given we cannot choose the lane?

    Both candidates matter because either could be the one drawn, so the honest
    summary is the worst case as well as the best.
    """
    rated = [c for c in candidates if c.get("delta") is not None]
    if not rated:
        return {"verdict": "unknown",
                "detail": "no rating on the karts waiting yet"}
    deltas = [c["delta"] for c in rated]
    best, worst = min(deltas), max(deltas)
    if current_delta is None:
        return {"verdict": "unknown",
                "detail": f"waiting: {fmt_delta(best)} to {fmt_delta(worst)}"}
    # Lower delta is a quicker kart, so an improvement is a fall in delta.
    if worst < current_delta:
        v = "better"            # even the unlucky draw is an upgrade
    elif best > current_delta:
        v = "worse"             # even the lucky draw is a downgrade
    else:
        v = "mixed"
    return {"verdict": v,
            "detail": f"ours {fmt_delta(current_delta)} · "
                      f"waiting {fmt_delta(best)} to {fmt_delta(worst)}"}


def fmt_delta(d: Optional[float]) -> str:
    if d is None:
        return "?"
    return f"{d:+.2f}s"

def box_time_summary(history: list, configured_loss_s: float,
                     minimum_s: float) -> dict:
    """What our stops have actually cost, against what we assumed.

    pit_loss_seconds starts life as a guess.  Every stop we serve measures the
    box half of it exactly, so after a handful the guess can be replaced with
    a number.  The remainder — the in and out lap, which no box clock sees —
    stays unmeasured, so the suggestion is explicitly a floor, not the answer.
    """
    times = sorted(h["box_s"] for h in history if h.get("box_s"))
    if not times:
        return {"n": 0, "note": "No stop served yet — pit loss is still the "
                                "configured estimate."}
    med = statistics.median(times)
    over = med - minimum_s
    # Every stop is charged the minimum whatever we do; what is ours to lose
    # is the time on top of it, and over thirty-four stops it compounds into
    # something worth a driver change.  Only overruns count — a stop under
    # the minimum is a penalty, not time in hand, and §15.1 charges for it.
    extra = sum(max(0.0, t - minimum_s) for t in times)
    return {
        "n": len(times),
        "median_s": round(med, 1),
        "best_s": round(times[0], 1),
        "extra_s": round(extra, 1),
        "note": (f"{len(times)} stops, median {fmt_mmss(med)} in the box "
                 f"({over:+.0f}s on the {fmt_mmss(minimum_s)} minimum), "
                 f"best {fmt_mmss(times[0])}, {fmt_mmss(extra)} given away "
                 f"in total. "
                 f"Pit loss is set to {configured_loss_s:.0f}s; the box alone "
                 f"is {med:.0f}s, so the in and out lap make up the rest."),
    }


def kart_watch(teams: list, window_best_s: Optional[float],
               within_s: float, pilot_level: dict = None) -> dict:
    """Karts in the field that are going better than their driver should.

    The point is not our own kart, it is everyone else's.  If the last-placed
    team is lapping within a second of the current reference at three in the
    morning, that is not a sudden talent — it is a very good kart, and it will
    come round to a lane eventually.

    The reference is the quickest lap of the last little while, not the best of
    the race: in twenty-five hours through a night the race best stops meaning
    anything by dawn, and a rule measured against it would never fire again.

    pilot_level, when the rater has one, is how far off that team normally
    runs.  Beating their own normal level is the real signal — a quick team
    lapping quickly says nothing.
    """
    if not window_best_s:
        return {"reference_s": None, "karts": []}
    pilot_level = pilot_level or {}
    out = []
    for t in teams:
        last = t.get("last_lap_s") or t.get("avg5_s")
        kart = str(t.get("kart", ""))
        if not last or not kart:
            continue
        off = last - window_best_s
        if off > within_s:
            continue
        usual = pilot_level.get((t.get("team") or "").strip())
        out.append({
            "kart": kart,
            "team": t.get("team", ""),
            "off_s": round(off, 2),
            # How much better than that team's own normal level this is.
            "beat_own_s": round(usual - off, 2) if usual is not None else None,
            "usual_s": round(usual, 2) if usual is not None else None,
        })
    # The most surprising first: beating your own level matters more than being
    # near the front, because the front is where the quick teams live anyway.
    out.sort(key=lambda r: (-(r["beat_own_s"] or 0), r["off_s"]))
    return {"reference_s": round(window_best_s, 3),
            "reference": fmt_laptime(window_best_s),
            "within_s": within_s, "karts": out[:8]}


def class_positions(teams: list, lap_s: Optional[float]) -> dict:
    """Position and gaps within a team's own category.

    We race AM (§2.5).  The leader on the timing screen is very likely a PRO
    team we are not classified against, so the overall gap is the wrong number
    to make a call on — what matters is the AM team directly ahead.

    Returns {kart: {"class", "class_pos", "ahead_s"}}.  Teams whose gap cannot be read are left out
    rather than guessed at; an event with no category column is simply one
    class, which is the truth for it.
    """
    rows = []
    for t in teams:
        kart = str(t.get("kart", ""))
        if not kart:
            continue
        try:
            pos = int(str(t.get("pos", "") or 0).strip() or 0)
        except ValueError:
            pos = 0
        gap = 0.0 if pos == 1 else gap_seconds(t.get("gap", ""), lap_s)
        if gap is None:
            continue
        rows.append({"kart": kart, "pos": pos, "gap": gap,
                     "cat": (t.get("category") or "").strip().upper()})

    out = {}
    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["cat"]].append(r)
    for cat, group in by_cat.items():
        group.sort(key=lambda r: (r["pos"] or 10 ** 6, r["gap"]))
        for i, r in enumerate(group):
            ahead = group[i - 1] if i else None
            out[r["kart"]] = {
                "class":     cat,
                "class_pos": i + 1,
                "ahead_s":   round(r["gap"] - ahead["gap"], 1) if ahead else None,
            }
    return out


def check_pit_plan(plan: list, race: dict, drivers: list) -> list:
    """Everything wrong with a plan, worst first, before the race finds out.

    A plan is only legal if every stint fits under the ceiling (§3.10), every
    driver clears their 120 minutes (§3.14), and all the stops fit before the
    pit lane shuts at 24:30 (§3.8).  All three are arithmetic, and all three
    are cheaper to discover on Friday than at 4am.
    """
    stops   = int(race["mandatory_pits"])
    total_s = race["duration_minutes"] * 60
    close_s = total_s - race["no_pit_last_minutes"] * 60
    box_s   = race["pit_duration_seconds"]
    max_s   = race["stint_max_minutes"] * 60
    min_s   = race.get("stint_min_minutes", 0) * 60
    owed_s  = race.get("driver_min_minutes", 0) * 60

    # Time in the box does not count as stint time (§3.11), so the driving is
    # what is left once every stop has been served.
    driving_s = max(0.0, total_s - stops * box_s)
    stints    = stops + 1
    avg_s     = driving_s / stints if stints else 0.0

    problems = []
    if avg_s > max_s:
        problems.append({
            "level": "blocker",
            "text": f"{stints} stints over {fmt_duration(driving_s)} of driving "
                    f"averages {fmt_duration(avg_s)}, past the "
                    f"{race['stint_max_minutes']} min limit. More stops than the "
                    f"{stops} mandatory ones are needed."})
    if min_s and avg_s < min_s:
        problems.append({
            "level": "warn",
            "text": f"Average stint {fmt_duration(avg_s)} is under the "
                    f"{race.get('stint_min_minutes')} min minimum."})

    # Can the mandatory stops physically fit before the lane shuts?
    if stops * box_s > close_s:
        problems.append({
            "level": "blocker",
            "text": f"{stops} stops of {fmt_duration(box_s)} cannot fit before "
                    f"the pit lane shuts at {fmt_duration(close_s)}."})

    assigned = [p.get("driver_id") for p in plan[:stops]]
    unassigned = sum(1 for d in assigned if not d)
    if unassigned:
        problems.append({
            "level": "warn",
            "text": f"{unassigned} of {stops} stops have no driver yet."})

    # Each stop hands the kart to the driver named for it, so a driver's time
    # is the stints that follow their stops, plus the first stint for whoever
    # starts the race.
    if owed_s and drivers:
        share = defaultdict(float)
        for d in assigned:
            if d:
                share[str(d)] += avg_s
        for drv in drivers:
            got = share.get(str(drv["id"]), 0.0)
            if got + 1e-6 < owed_s:
                problems.append({
                    "level": "blocker",
                    "text": f"{drv['name']} is planned for {fmt_duration(got)}, "
                            f"short of the {race.get('driver_min_minutes')} min "
                            f"every driver owes."})

    order = {"blocker": 0, "warn": 1}
    problems.sort(key=lambda p: order.get(p["level"], 9))
    return problems

def sector_review(mine: list, team_rows: list, least: int = 5) -> list:
    """Where a driver's lap goes, sector by sector, against the team's best.

    A lap time says someone is four tenths off; only the sectors say which
    corners, and that is the only part of a debrief a driver can do anything
    with.  Medians on both sides, so one blocked lap does not invent a
    weakness, and a sector nobody has ``least`` laps in is left out rather
    than compared on two samples.
    """
    def median_of(rows, key):
        vals = [r[key] for r in rows if r.get(key)]
        return statistics.median(vals) if len(vals) >= least else None

    by_driver = defaultdict(list)
    for r in team_rows:
        by_driver[(r["pilot"] or "").rsplit("|", 1)[-1].strip()].append(r)

    out = []
    for key, label in (("s1", "Sector 1"), ("s2", "Sector 2"), ("s3", "Sector 3")):
        got = median_of(mine, key)
        if got is None:
            continue
        others = [median_of(rows, key) for rows in by_driver.values()]
        best = min((v for v in others if v is not None), default=None)
        out.append({"sector": label, "median": round(got, 3),
                    "best": round(best, 3) if best is not None else None,
                    "loss": round(got - best, 3) if best is not None else None,
                    "laps": sum(1 for r in mine if r.get(key))})
    return out


def _int_or_last(v) -> int:
    """A position as a number; anything unreadable sorts to the back."""
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 10 ** 6


def norm_team(name: str) -> tuple:
    """A team name reduced to comparable words.

    Case, accents and punctuation all differ between the entry list and the
    timing screen — "MICROÁGUA", "Microagua" and "MICRO-AGUA" are one team —
    so they are stripped and what is left is compared word by word.
    """
    flat = unicodedata.normalize("NFKD", str(name or ""))
    flat = "".join(c for c in flat if not unicodedata.combining(c))
    return tuple(re.sub(r"[^A-Z0-9 ]+", " ", flat.upper()).split())


def match_team(want: str, names) -> Optional[str]:
    """The one name in ``names`` that means ``want``, or None.

    Apex does not have to spell a team the way the entry list does: we are
    "TPC CIAO CUORE" on the entry list and may be plain "TPC" on the timing
    screen.  Exact equality would leave the pit wall with no team at all for
    twenty-five hours, so a name that is the start of another, or a subset of
    its words, counts.  So does one cut off mid-word: the team column is a
    fixed width and a long name arrives clipped, which is how "TPC CIAO CUORE"
    reaches us as "TPC CIAO CUO" and matched nothing at all.

    It never guesses between two.  This entry list has three STF teams, two
    TRACK LIMITS and a JURASSIC KART alongside a JURASSIC KART RAPTOR, and
    showing the wrong team's pace on the wall is worse than showing none — so
    a tie returns None and the wall says it cannot find us.
    """
    w = norm_team(want)
    if not w:
        return None

    def score(cand):
        c = norm_team(cand)
        if not c:
            return 0
        # Compared with the gaps closed up too, so a hyphen cannot split a word
        # an accent leaves whole: "MICRO-AGUA" is "MICROÁGUA".
        if c == w or "".join(c) == "".join(w):
            return 4
        short, long = (w, c) if len(w) <= len(c) else (c, w)
        if long[:len(short)] == short:
            return 3                      # "TPC" opening "TPC CIAO CUORE"
        # Clipped mid-word.  Weaker than a whole-word prefix, because a cut can
        # land anywhere and two teams can share the surviving half — which is
        # what the tie rule below is for: "TRACK LIMITS" is both of ours.
        # Which side is shorter is decided on characters here, not words: a cut
        # that leaves the word count alone ("TPC CIAO CUORE" -> "TPC CIAO CUO")
        # would otherwise be compared the wrong way round and match nothing,
        # and this is asked in both directions — the config name against the
        # feed's, and the feed's against the entry list.
        a, b = "".join(w), "".join(c)
        sj, lj = (a, b) if len(a) <= len(b) else (b, a)
        if lj.startswith(sj):
            return 2
        if set(short) <= set(long):
            return 1                      # the same words, in another order
        return 0

    scored = [(score(n), n) for n in names]
    best = max((s for s, _n in scored), default=0)
    if not best:
        return None
    hits = [n for s, n in scored if s == best]
    return hits[0] if len(hits) == 1 else None


def category_of(team: str) -> str:
    """PRO or AM from the entry list, or "" when it is not on it.

    Only ever a fallback: the feed's own class column is the authority, and
    the entry list is one team short.
    """
    found = match_team(team, [n for n, _c in entries.ENTRIES])
    return dict(entries.ENTRIES).get(found, "") if found else ""


# Whatever Apex is calling us, resolved from the last full grid.  Our laps are
# filed under the feed's spelling, so anything that looks them up has to ask
# for that and not for what we typed into the settings box.
_feed_team_name: str = ""


def my_team_name() -> str:
    """The name our own rows carry: the feed's if we have matched it, else ours."""
    return (_feed_team_name or CFG.get("team_name", "") or "").strip()


# ── Driver fatigue ─────────────────────────────────────────────────────────────
def rest_seconds(stints: list, now: datetime) -> dict:
    """How long each driver has been out of the kart, by driver id.

    At 4am this is the number that decides who goes next.  A driver who got
    out twenty minutes ago is not rested, however much time they still owe.
    Only finished stints count: whoever is out on track is not resting.
    """
    last = {}
    for st in stints:
        did, end = str(st.get("driver_id") or ""), st.get("end_ts")
        if not did or not end:
            continue
        try:
            ts = datetime.fromisoformat(end)
        except (TypeError, ValueError):
            continue
        if did not in last or ts > last[did]:
            last[did] = ts
    return {d: max(0.0, (now - ts).total_seconds()) for d, ts in last.items()}


def stint_fade(lap_times: list, edge: int = 5) -> Optional[float]:
    """How much slower the last few laps are than the first few, in seconds.

    ``lap_times`` is one stint's laps in the order they were run.  Positive
    means the driver is dropping off — the thing you cannot see from the tower
    and the driver will not admit on the radio.  Medians, because one lap stuck
    behind a backmarker is not fatigue.

    None until there are laps enough at both ends to compare, which is the
    honest answer for the first ten minutes of every stint.
    """
    laps = [t for t in lap_times if t and t > 0]
    if len(laps) < edge * 2:
        return None
    return round(statistics.median(laps[-edge:])
                 - statistics.median(laps[:edge]), 3)


def fatigue_note(fade_s: Optional[float], stint_s: float,
                 cfg: dict = None) -> Optional[dict]:
    """A word about the driver on track, or nothing at all.

    It never says who to put in — that is the team's rota and the team's call.
    It says what the laps are doing, so the call is made knowing.
    """
    cfg = cfg or {}
    slow = cfg.get("fade_warn_s", 0.4)
    long_s = cfg.get("fade_after_minutes", 25) * 60
    if fade_s is None or stint_s < long_s:
        return None
    if fade_s >= slow * 2:
        return {"level": "warn",
                "text": f"Dropping off: {fade_s:+.2f}s on their opening laps "
                        f"after {fmt_duration(stint_s)} in the kart."}
    if fade_s >= slow:
        return {"level": "note",
                "text": f"Slipping {fade_s:+.2f}s against their opening laps."}
    if fade_s <= -slow:
        return {"level": "good",
                "text": f"Still building: {fade_s:+.2f}s on their opening laps."}
    return {"level": "good", "text": "Holding their pace."}


def pit_by_seconds(stint_s: float, race_elapsed_s: float, pits_done: int,
                   race: dict, lap_s: Optional[float] = None) -> dict:
    """How long the next stop can still wait, and which rule is holding it.

    Two deadlines run at once and the earlier one is the only one that matters:

    * the stint ceiling — §3.10 caps a stint and §15.4 charges 20s for every
      started 10s over it, so this one is a penalty the moment it passes;
    * the schedule — every mandatory stop still owed needs its three minutes
      in the box before the lane shuts at 24:30 (§3.8, §3.9), so waiting past
      this point means a stop that can never be served.  Stops cannot be taken
      back to back: between two of them the kart has to leave the lane, get
      round, and come in again, so N stops need N boxes and N-1 laps.  Budget
      only the boxes and the deadline comes out later than it really is — with
      five owed, by four minutes, which is a stop that never happens.

    Returned in seconds with the rule named, because "box by 21:40" and why
    are one thought on a pit wall and two clicks anywhere else.
    """
    total_s  = race["duration_minutes"] * 60
    max_s    = race["stint_max_minutes"] * 60
    no_pit_s = race["no_pit_last_minutes"] * 60
    box_s    = race["pit_duration_seconds"]
    stops_left = max(0, int(race["mandatory_pits"]) - int(pits_done))

    # Time left before the pit lane shuts for good.
    room = (total_s - race_elapsed_s) - no_pit_s
    if room <= 0:
        return {"seconds": 0, "reason": "lane shut",
                "detail": f"Pit lane shut for the last {race['no_pit_last_minutes']} min"}

    ceiling = max_s - stint_s
    if not stops_left:
        return {"seconds": max(0.0, ceiling), "reason": "stint limit",
                "detail": f"Every mandatory stop served — only the "
                          f"{race['stint_max_minutes']} min stint ceiling left"}

    # lap_s is unknown only before anyone has completed a lap, when the lane
    # shuts a day away and this term cannot decide anything.
    between = (stops_left - 1) * (lap_s or 0.0)
    schedule = room - stops_left * box_s - between
    if schedule < 0:
        # Past saving, and that is a different thing to say than "box now".
        # Boxing now is what you do either way; what the wall needs to know is
        # that one of these stops is not going to happen, so the argument
        # becomes which penalty to take rather than when to leave.
        missed = int(-schedule // (box_s + (lap_s or 0.0))) + 1
        return {"seconds": 0, "reason": "too late",
                "detail": f"{fmt_duration(-schedule)} short for the {stops_left} "
                          f"stops still owed — about {missed} of them cannot be "
                          f"served before the lane shuts"}
    if schedule <= ceiling:
        return {"seconds": max(0.0, schedule), "reason": "schedule",
                "detail": f"{stops_left} stop(s) still owed, "
                          f"{fmt_duration(stops_left * box_s + between)} of box "
                          f"and out-and-back to fit before the lane shuts"}
    return {"seconds": max(0.0, ceiling), "reason": "stint limit",
            "detail": f"{race['stint_max_minutes']} min ceiling — over it is "
                      f"20s per started 10s (§15.4)"}


# ── Strategy engine ────────────────────────────────────────────────────────────
def compute_strategy(stint_s: float, race_elapsed_s: float, pits_done: int,
                     my_avg5: Optional[float], prev_avg5: Optional[float],
                     light: str = "", box_now: dict = None) -> dict:
    """What to do about the pit lane, right now.

    Ordered by what actually overrules what.  A hard limit beats an
    opportunity; an opportunity beats a schedule; a schedule beats habit.
    Every branch says why, because a call nobody understands gets ignored.
    """
    R = CFG["race"]
    total_s   = R["duration_minutes"] * 60
    max_s     = R["stint_max_minutes"] * 60
    min_s     = R.get("stint_min_minutes", 0) * 60
    no_pit_s  = R["no_pit_last_minutes"] * 60
    remaining = total_s - race_elapsed_s
    stops_left = max(0, R["mandatory_pits"] - pits_done)

    def out(label, cls, detail, why=""):
        return {"label": label, "cls": cls, "detail": detail, "why": why}

    # 1. The lane is shut. Nothing else matters.
    if remaining <= no_pit_s:
        if stops_left:
            return out("STOPS MISSED", "box",
                       f"{stops_left} stop(s) never taken",
                       f"Pit lane shut with {stops_left} outstanding — "
                       f"5 laps each at the flag (§3.13.1)")
        return out("HOLD", "hold",
                   f"Pit lane shut for the last {R['no_pit_last_minutes']} min",
                   "All mandatory stops served")

    # 2. A stint we cannot legally extend.
    if stint_s >= max_s - 60:
        return out("BOX NOW", "box", "STINT LIMIT — BOX IMMEDIATELY",
                   f"Over {R['stint_max_minutes']} min is 20s per 10s (§15.4)")

    # 3. Too few stops left for the time left: the schedule is now the limit.
    #    Each remaining stop still needs a stint under the ceiling to sit in.
    if stops_left:
        room = remaining - no_pit_s
        need = stops_left * R["pit_duration_seconds"]
        if room - need < stops_left * 60:       # under a minute of slack a stop
            return out("BOX NOW", "box",
                       f"{stops_left} stops left, {fmt_duration(room)} to take them",
                       "Running out of pit window — stops stack up from here")

    # 4. The track is neutralised. This is the cheapest stop of the race and it
    #    will not last, so it outranks anything that is merely on schedule.
    if light in ("ly", "lsc", "lr") and stint_s >= min_s and stops_left:
        flag = {"ly": "yellow", "lsc": "safety kart", "lr": "red flag"}[light]
        return out("BOX NOW", "box", f"Track under {flag} — stop is cheap",
                   "Everyone is slow, so the stop costs a fraction of green")

    # 5. Pace gone. A kart that has fallen off is losing more than a stop costs.
    if my_avg5 and prev_avg5 and prev_avg5 > 0:
        drop = my_avg5 - prev_avg5
        if drop > R.get("pace_drop_box", 0.50) and stint_s >= min_s:
            return out("BOX NOW", "box", f"Pace collapsed +{drop:.2f}s",
                       "Losing more every lap than the stop would cost")
        if drop > R.get("pace_drop_warn", 0.30):
            return out("PREPARE", "prepare", f"Pace dropping +{drop:.2f}s",
                       "Watch the next two laps before committing")

    # 6. The kart lottery. Only worth acting on when a stop is due anyway.
    bn = box_now or {}
    near_due = stint_s >= max_s - 12 * 60
    if stops_left and near_due and bn.get("verdict") == "better":
        return out("BOX NOW", "box", "Both karts waiting are better than ours",
                   bn.get("detail", ""))
    if stops_left and near_due and bn.get("verdict") == "worse" \
            and stint_s < max_s - 4 * 60:
        return out("WAIT", "hold", "Both karts waiting are worse — hold if you can",
                   bn.get("detail", ""))

    # 7. The ordinary approach to the limit.
    if stint_s >= max_s - 4 * 60:
        return out("PREPARE", "prepare",
                   f"Box in 1-3 laps · stint {fmt_duration(stint_s)}",
                   f"{fmt_duration(max_s - stint_s)} left of the stint")
    if stint_s >= max_s - 10 * 60:
        return out("PREPARE", "prepare", "Approaching limit — stay alert",
                   f"{fmt_duration(max_s - stint_s)} left of the stint")

    # 8. Behind the schedule. The stops must fit before the lane shuts (§3.8),
    #    so they are paced against that window, not the whole race.
    pit_window_s = max(1.0, total_s - no_pit_s)
    expected = (min(race_elapsed_s, pit_window_s) / pit_window_s) * R["mandatory_pits"]
    if pits_done < expected - 1.5:
        return out("PREPARE", "prepare",
                   f"Behind pit plan ({pits_done}/{R['mandatory_pits']})",
                   f"Should be near {expected:.0f} by now")

    return out("HOLD", "hold", "On plan — hold position",
               f"{stops_left} stops left, {fmt_duration(remaining)} to run")

# ── Snapshot ───────────────────────────────────────────────────────────────────
_prev_avg5: Optional[float] = None

def make_snapshot() -> dict:
    global _prev_avg5

    with _lock:
        teams_raw    = list(_teams)
        apex_ok      = _apex_ok
        apex_session = dict(_apex_session)

    now = datetime.utcnow()
    status = kv_get("status")

    # Race clock — the tower's if it is live, ours if the feed has gone quiet.
    race_start = kv_get("race_start")
    race_elapsed = 0.0
    if race_start and status in ("racing", "pitting"):
        race_elapsed = (now - datetime.fromisoformat(race_start)).total_seconds()
    race_remaining = max(0.0, CFG["race"]["duration_minutes"] * 60 - race_elapsed)

    clock = _apex_clock.state()
    clock_source = "local"
    # In practice the tower's clock is counting down a half-hour track
    # session, not our race, and the feed sends one bare number with no
    # length attached — so it is read against the configured 25 hours and
    # comes out as 24h36m elapsed.  Every deadline downstream then believes
    # the pit lane has shut: the wall showed "STOPS MISSED, 34 never taken"
    # against a practice session with a kart circulating happily.
    if clock["ok"] and not CFG["karts"].get("practice"):
        clock_source = "apex"
        if clock["elapsed"] is not None:
            race_elapsed = clock["elapsed"]
        if clock["remaining"] is not None:
            race_remaining = clock["remaining"]
        elif clock["total"]:
            race_remaining = max(0.0, clock["total"] - race_elapsed)

    # Stint elapsed
    stint_start   = kv_get("stint_start")
    stint_running = bool(stint_start) and status == "racing"
    stint_s = 0.0
    if stint_running:
        stint_s = (now - datetime.fromisoformat(stint_start)).total_seconds()

    # Pit timer
    pit_start = kv_get("pit_start")
    pit_remaining = float(CFG["race"]["pit_duration_seconds"])
    pit_elapsed   = 0.0
    pit_min_met   = False
    if pit_start and status == "pitting":
        pit_elapsed   = (now - datetime.fromisoformat(pit_start)).total_seconds()
        # Count against the minimum plus our safety margin, never against the
        # bare minimum: the clock we are running is not the one that scores us.
        safe_target   = (CFG["race"]["pit_duration_seconds"]
                         + CFG["race"].get("pit_safety_seconds", 0))
        pit_remaining = max(0.0, safe_target - pit_elapsed)
        pit_min_met   = pit_elapsed >= safe_target
    # §3.9 is timed electronically and §15.1 charges 20s per started 10s short,
    # so the number worth showing is what leaving right now would cost.
    # The penalty is charged against the regulation minimum, not our margin.
    pit_penalty_now = penalty_for_shortfall(
        CFG["race"]["pit_duration_seconds"] - pit_elapsed)

    # Drivers
    driver_id = kv_get("driver_id")
    current_driver = None
    drivers = []
    pit_history = []
    pits_done = 0
    with get_db() as con:
        for row in con.execute("SELECT * FROM drivers ORDER BY sort_order, id"):
            d = dict(row)
            d["total_fmt"] = fmt_duration(d["total_seconds"])
            # §3.14: a driver short of the minimum costs the team 20s per 10s
            # missing, and it is only fixable while there is still race left.
            owed = CFG["race"].get("driver_min_minutes", 0) * 60 - d["total_seconds"]
            d["owed_seconds"] = max(0.0, owed)
            d["owed_fmt"]     = fmt_duration(max(0.0, owed))
            d["active"]       = str(row["id"]) == str(driver_id)
            if d["active"]:
                current_driver = d
            drivers.append(d)

        stints_done = con.execute("SELECT COUNT(*) FROM stints").fetchone()[0]
        finished = [dict(r) for r in con.execute(
            "SELECT driver_id, end_ts FROM stints WHERE end_ts IS NOT NULL")]

    # How long each driver has been out of the kart.  Whoever is on track is
    # not resting, however long ago their previous stint ended.
    rested = rest_seconds(finished, now)
    for d in drivers:
        r = None if d["active"] else rested.get(str(d["id"]))
        d["rested_s"] = r
        d["rested_fmt"] = fmt_duration(r) if r is not None else ""

    # Once, not once per driver: this reads every stint there is, so running it
    # inside the loop above filed the whole race under each driver in turn and
    # restarted the numbering each time.
    for i, row in enumerate(con.execute("""
        SELECT s.id, s.driver_id, s.start_ts, s.end_ts, s.duration_seconds,
               d.name as driver_name, s.box_seconds
        FROM stints s
        LEFT JOIN drivers d ON d.id = s.driver_id
        ORDER BY s.id
    """), start=1):
        pit_history.append({
            "n":        i,
            "driver":   row["driver_name"] or "?",
            "duration": fmt_duration(row["duration_seconds"] or 0),
            "start":    row["start_ts"][:19].replace("T", " ") if row["start_ts"] else "-",
            "end":      row["end_ts"][:19].replace("T", " ")   if row["end_ts"]   else "-",
            "dur_s":    row["duration_seconds"] or 0,
            "box":      fmt_mmss(row["box_seconds"]) if row["box_seconds"] else "",
            "box_s":    row["box_seconds"],
        })

    # My team
    my_name = CFG.get("team_name", "")
    # Apex spells teams its own way, so find ours by meaning rather than by
    # exact text — but on one unambiguous answer only.  Everything below keys
    # off the name the feed actually used.
    feed_name = match_team(my_name, [t.get("team", "") for t in teams_raw])
    my_team = next((t for t in teams_raw if t.get("team", "") == feed_name),
                   None) if feed_name else None
    my_avg5 = my_team["avg5_s"] if my_team else None

    # The timekeepers' pit count is the one that settles a protest, so use it
    # when the feed carries it and fall back to our own stint log when it does not.
    # The feed's counter wins: it is the organiser's count, and §3.8 is judged
    # on their records, not ours.  But the two disagreeing is worth saying out
    # loud — it means either we have missed logging a stop, in which case our
    # box times and driver totals are wrong too, or the feed is counting
    # something we are not, in which case the number steering the whole endgame
    # is not the one we think.  Silence on this was the expensive option.
    pits_done = stints_done
    stop_count_split = None
    if my_team:
        try:
            pits_done = int(str(my_team.get("pits", "")).strip())
        except (TypeError, ValueError):
            pass
        else:
            if pits_done != stints_done:
                stop_count_split = {"feed": pits_done, "ours": stints_done}

    # Is the driver on track dropping off?  Their own laps from this stint,
    # in the order they were run — nobody else's pace comes into it.
    fade_s = None
    lap_name = my_team_name()
    if stint_running and lap_name:
        with POOL._con() as con:
            stint_laps = [r["lap_s"] for r in con.execute(
                "SELECT lap_s FROM kart_lap WHERE ts >= ? AND "
                "(pilot = ? OR pilot LIKE ?) ORDER BY id",
                (datetime.fromisoformat(stint_start).timestamp(),
                 lap_name, lap_name + "|%"))]
        fade_s = stint_fade(stint_laps)
    fatigue = fatigue_note(fade_s, stint_s, CFG["race"])

    strat = None      # filled in below, once the kart pool has been read
    _prev_avg5 = my_avg5

    # Track-wide average of all avg5 values
    all_avgs    = [t["avg5_s"] for t in teams_raw if t.get("avg5_s")]
    track_avg_s = sum(all_avgs) / len(all_avgs) if all_avgs else None
    track_avg   = fmt_laptime(track_avg_s) if track_avg_s else "-"

    # Serialize teams (drop raw floats the frontend doesn't need)
    pool = POOL.snapshot()
    kart_of = pool["kart_of"]
    kart_card = {c["num"]: c for c in pool["fleet"]}
    # What we would be handed if we boxed this lap, and whether that is a trade
    # up — the whole point of rating the karts at all.
    candidates = next_karts_out(pool["lanes"])
    my_held = kart_of.get(str(my_team.get("kart", ""))) if my_team else None
    my_card = kart_card.get(my_held) if my_held else None
    box_now = box_now_verdict(candidates, my_card["delta"] if my_card else None)
    # We are classified against our own category, not the overall leader.
    klass = class_positions(teams_raw, track_avg_s)
    # The reference is the quickest lap of the recent window, so the rule keeps
    # up with the track instead of chasing a best set hours ago.
    R2 = CFG["race"]
    window_best = POOL.recent_best(R2.get("watch_window_minutes", 20))
    watch = kart_watch(teams_raw, window_best, R2.get("watch_within_s", 1.0),
                       POOL.pilot_levels())
    # The call depends on the flag and on what is waiting in the lanes, so it
    # is made after both are known.
    strat = compute_strategy(stint_s, race_elapsed, pits_done, my_avg5, _prev_avg5,
                             light=apex_session.get("light", ""), box_now=box_now)
    R = CFG["race"]
    virt = virtual_positions(teams_raw, R["mandatory_pits"],
                             R.get("pit_loss_seconds") or R["pit_duration_seconds"],
                             track_avg_s)
    teams_out = []
    # Interval to the kart directly ahead on the road — the column an F1 wall
    # reads next to the gap, because the gap says where you are in the race and
    # the interval says whether you can do anything about it.
    road_int = kartpool.KartPool.gaps_ahead(teams_raw)
    # Whoever holds the outright fastest lap of the session — the one F1 marks
    # with a purple tag that stays put until somebody beats it, unlike the
    # last-lap colour above which fades the moment that kart's next lap is not.
    best_times = [t["best_lap_s"] for t in teams_raw if t.get("best_lap_s")]
    fastest_s = min(best_times) if best_times else None
    # Race order, always.  Apex sends its rows in whatever order suits it, and
    # a timing screen that is not in position order cannot be read at a glance.
    teams_raw.sort(key=lambda t: _int_or_last(t.get("pos")))
    for t in teams_raw:
        held = kart_of.get(str(t.get("kart", "")))
        card = kart_card.get(held) if held else None
        teams_out.append({
            "pos":        t.get("pos", ""),
            "kart":       t.get("kart", ""),
            "team":       t.get("team", ""),
            "driver":     t.get("driver", ""),
            "category":   t.get("category", "") or category_of(t.get("team", "")),
            "in_pit":     t.get("in_pit", False),
            "my_kart":    held or "",
            "kart_label": card["label"] if card else "",
            "kart_delta": card["delta"] if card else None,
            "sectors":    t.get("sectors") or {},
            "int_s":      road_int.get(str(t.get("kart", ""))),
            "last_lap":   t.get("last_lap", "-"),
            "last_lap_mark": t.get("last_lap_mark", ""),
            "avg5":       t.get("avg5", "-"),
            "avg10":      t.get("avg10", "-"),
            "best_lap":   t.get("best_lap", "-"),
            "fastest_lap": fastest_s is not None and t.get("best_lap_s") == fastest_s,
            "total_laps": t.get("total_laps", "-"),
            "pits":       t.get("pits", "-"),
            "gap":        t.get("gap", "-"),
            "is_my_team": bool(feed_name) and t.get("team", "") == feed_name,
            **(virt.get(str(t.get("kart", ""))) or
               {"virtual_pos": None, "stops_owed": None, "debt_s": None}),
            **(klass.get(str(t.get("kart", ""))) or
               {"class": "", "class_pos": None, "ahead_s": None}),
        })

    stint_pct = min(100, (stint_s / (CFG["race"]["stint_max_minutes"] * 60)) * 100) if stint_s else 0
    pit_by = pit_by_seconds(stint_s, race_elapsed, pits_done, CFG["race"],
                            lap_s=my_avg5 or track_avg_s)
    # Pit plan
    raw_plan = get_pit_plan()
    n_planned = CFG["race"]["mandatory_pits"]
    avg_stint_s = (CFG["race"]["duration_minutes"] * 60) / max(1, n_planned)
    drv_by_id = {d["id"]: d["name"] for d in drivers}
    pit_plan_out = []
    for i in range(n_planned):
        stop = raw_plan[i] if i < len(raw_plan) else {"driver_id": None}
        did = stop.get("driver_id")
        actual = pit_history[i] if i < len(pit_history) else None
        pit_plan_out.append({
            "n": i + 1,
            "driver_id": did,
            "driver": drv_by_id.get(int(did), "") if did is not None else "",
            "planned_s": int((i + 1) * avg_stint_s),
            # A hand-planned clock time (turno sheet) overrides the even split.
            "planned_fmt": stop.get("time") or fmt_duration((i + 1) * avg_stint_s),
            "done": i < len(pit_history),
            "actual_end": actual["end"] if actual else "",
        })

    return {
        "ts":               now.isoformat() + "Z",  # explicit UTC so JS Date() parses correctly
        "status":           status,
        "apex_ok":          apex_ok,
        "apex_session":     {**apex_session, "control": control_for_kart(
                                apex_session.get("control") or [],
                                {str(t.get("kart", "")) for t in teams_raw})},
        "race_elapsed":     race_elapsed,
        "race_remaining":   race_remaining,
        "race_remaining_fmt": fmt_duration(race_remaining),
        "stint_s":          stint_s,
        "stint_running":    stint_running,
        "stint_fmt":        fmt_duration(stint_s),
        "stint_pct":        round(stint_pct, 1),
        # The one pit number a wall reads at a glance: how long this stop can
        # still wait, and which rule is the one holding it.
        "pit_by":           pit_by,
        "pit_remaining":    pit_remaining,
        "pit_remaining_fmt": fmt_mmss(pit_remaining),
        "pit_min_met":       pit_min_met,
        "pit_penalty_now":   pit_penalty_now,
        "strategy":         strat,
        "current_driver":   current_driver,
        "drivers":          drivers,
        # What this stint's laps are doing, so the rota is decided knowing.
        "fatigue":          fatigue,
        "pits_done":        pits_done,
        "stints_done":      stints_done,
        "stop_count_split": stop_count_split,
        "mandatory_pits":   CFG["race"]["mandatory_pits"],
        "stint_max_minutes": CFG["race"]["stint_max_minutes"],
        "next_karts":       candidates,
        "kart_watch":       watch,
        "plan_problems":    check_pit_plan(get_pit_plan(), CFG["race"], drivers),
        "box_now":          box_now,
        "track_avg":        track_avg,
        "pit_history":      pit_history,
        "box_times":        box_time_summary(
                                pit_history,
                                CFG["race"].get("pit_loss_seconds")
                                or CFG["race"]["pit_duration_seconds"],
                                CFG["race"]["pit_duration_seconds"]),
        "team_name":        my_name,
        # What Apex is actually calling us.  Blank means we could not be found
        # in the feed — or that more than one row could have been us, which is
        # a thing to fix in the settings, not to guess at.
        "feed_team_name":   feed_name or "",
        "apex_url":         CFG.get("apex_url", ""),
        "teams":            teams_out,
        "pit_plan":         pit_plan_out,
        "clock_source":     clock_source,
        "kartpool":         pool,
        "auto_pit":         CFG["karts"].get("auto_pit", True),
        # On the wall so nobody drives Saturday with Friday's switch still set.
        "practice":         CFG["karts"].get("practice", False),
        "exclude_teams":    (CFG["karts"].get("rating") or {}).get("exclude_teams", []),
        "my_team":          {
            "pos":   my_team.get("pos", "?"),
            "kart":  my_team.get("kart", "?"),
            "avg5":  my_team.get("avg5", "-"),
            "laps":  my_team.get("total_laps", "-"),
            "phys_kart":  kart_of.get(str(my_team.get("kart", "")), ""),
            "kart_label": (kart_card.get(kart_of.get(str(my_team.get("kart", "")), ""))
                           or {}).get("label", ""),
            "kart_delta": (kart_card.get(kart_of.get(str(my_team.get("kart", "")), ""))
                           or {}).get("delta"),
        } if my_team else None,
    }

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/")
def index():
    return render_template("index.html")

@app.get("/api/state")
def api_state():
    return jsonify(make_snapshot())

# ── Race control ───────────────────────────────────────────────────────────────
@app.post("/api/race/start")
def race_start():
    now = datetime.utcnow().isoformat()
    kv_set("race_start", now)
    kv_set("stint_start", now)   # stint clock always starts with the race
    kv_set("status", "racing")
    log("RACE START")
    return jsonify(ok=True)

@app.post("/api/race/stop")
def race_stop():
    kv_set("status", "idle")
    log("RACE STOP")
    return jsonify(ok=True)

@app.post("/api/race/reset")
def race_reset():
    for k, v in [("status","idle"),("race_start",""),("stint_start",""),
                  ("pit_start",""),("driver_id","")]:
        kv_set(k, v)
    with get_db() as con:
        con.execute("DELETE FROM stints")
        con.execute("UPDATE drivers SET total_seconds=0")
    # The kart pool is deliberately left alone.  It holds what we learned about
    # the physical karts and who is sitting in which one, and §3.2.1 starts the
    # race on exactly the karts the teams finished qualifying in — so clearing
    # the race clock between qualifying and the start must not throw away the
    # pace measured in qualifying.  /api/karts/reset wipes the fleet on purpose.
    _apex_clock.reset()
    log("RACE RESET", "kart pool kept")
    return jsonify(ok=True)

# ── Driver management ──────────────────────────────────────────────────────────
@app.post("/api/driver/set")
def driver_set():
    did = str(request.json.get("driver_id", ""))
    kv_set("driver_id", did)
    with get_db() as con:
        row = con.execute("SELECT name FROM drivers WHERE id=?", (did,)).fetchone()
    name = row["name"] if row else did
    log("DRIVER SET", name)
    return jsonify(ok=True)

@app.post("/api/driver/add")
def driver_add():
    name = (request.json.get("name") or "").strip()
    if not name:
        return jsonify(ok=False, error="Name required"), 400
    with get_db() as con:
        con.execute("INSERT INTO drivers(name) VALUES(?)", (name,))
    log("DRIVER ADD", name)
    return jsonify(ok=True)

@app.post("/api/driver/rename")
def driver_rename():
    did  = request.json.get("driver_id")
    name = (request.json.get("name") or "").strip()
    if not name:
        return jsonify(ok=False, error="Name required"), 400
    with get_db() as con:
        con.execute("UPDATE drivers SET name=? WHERE id=?", (name, did))
    return jsonify(ok=True)

@app.post("/api/driver/delete")
def driver_delete():
    did = request.json.get("driver_id")
    with get_db() as con:
        con.execute("DELETE FROM drivers WHERE id=?", (did,))
    # Clear current driver if it was this one
    if kv_get("driver_id") == str(did):
        kv_set("driver_id", "")
    return jsonify(ok=True)

# ── Pit management ─────────────────────────────────────────────────────────────
@app.post("/api/pit/box")
def pit_box():
    """Kart entered pit lane — end current stint, start minimum-time timer.
    Accepts optional offset_seconds so the box time can be entered retroactively."""
    data = request.json or {}
    _do_box(float(data.get("offset_seconds", 0)))
    return jsonify(ok=True)

def _do_box(offset_s: float = 0.0, source: str = "button"):
    """Close the running stint and start the minimum-pit-time countdown."""
    now = datetime.utcnow()
    box_time    = now - timedelta(seconds=offset_s)   # when the kart actually entered

    did         = kv_get("driver_id")
    stint_start = kv_get("stint_start")
    dur = 0.0

    if did and stint_start and kv_get("status") == "racing":
        try:
            dur = max(0.0, (box_time - datetime.fromisoformat(stint_start)).total_seconds())
            with get_db() as con:
                con.execute(
                    "INSERT INTO stints(driver_id,start_ts,end_ts,duration_seconds) VALUES(?,?,?,?)",
                    (did, stint_start, box_time.isoformat(), dur)
                )
                con.execute(
                    "UPDATE drivers SET total_seconds=total_seconds+? WHERE id=?",
                    (dur, did)
                )
        except Exception:
            pass

    kv_set("pit_start", box_time.isoformat())
    kv_set("status", "pitting")
    driver_name = ""
    if did:
        with get_db() as con:
            row = con.execute("SELECT name FROM drivers WHERE id=?", (did,)).fetchone()
            driver_name = row["name"] if row else did
    offset_note = f"  (retroactive -{int(offset_s)}s)" if offset_s else ""
    log("BOX NOW", f"driver={driver_name}  "
                   f"stint={fmt_duration(dur if did and stint_start else 0)}"
                   f"{offset_note}  [{source}]")

@app.post("/api/pit/done")
def pit_done():
    """New driver seated — start fresh stint."""
    _do_pit_done(request.json.get("driver_id") or kv_get("driver_id"))
    return jsonify(ok=True)

def _do_pit_done(new_did, source: str = "button"):
    new_did = str(new_did or "")
    pit_s = kv_get("pit_start")
    pit_elapsed = 0.0
    if pit_s:
        pit_elapsed = (datetime.utcnow() - datetime.fromisoformat(pit_s)).total_seconds()
    kv_set("driver_id",   new_did)
    kv_set("stint_start", datetime.utcnow().isoformat())
    kv_set("pit_start",   "")
    kv_set("status",      "racing")
    with get_db() as con:
        row = con.execute("SELECT name FROM drivers WHERE id=?", (new_did,)).fetchone()
        new_name = row["name"] if row else new_did
        # Against the stint that just ended — that is the stop it belongs to.
        if pit_elapsed > 0:
            con.execute("UPDATE stints SET box_seconds=? WHERE id="
                        "(SELECT MAX(id) FROM stints)", (pit_elapsed,))
    log("PIT DONE", f"driver={new_name}  pit_time={fmt_mmss(pit_elapsed)}  [{source}]")

# ── Pit plan route ─────────────────────────────────────────────────────────────
@app.post("/api/plan/set")
def api_plan_set():
    data = request.json or {}
    stop = int(data.get("stop", 1)) - 1  # 1-indexed from client
    driver_id = data.get("driver_id")    # None to unassign
    set_plan_stop(stop, driver_id, data.get("time"))
    return jsonify(ok=True)

@app.post("/api/plan/reset")
def api_plan_reset():
    kv_set("pit_plan", "")
    return jsonify(ok=True)

# ── Karts ─────────────────────────────────────────────────────────────────────
@app.get("/pit")
def pit_phone():
    """Phone-sized lane view for whoever is standing in the pit lane."""
    return render_template("pit.html")

@app.get("/api/laps/<team_no>")
def api_laps(team_no):
    """Every lap we have recorded for one team, newest first.

    Only laps taken in a kart we could name are stored, so a team whose stops
    went unanswered will have gaps — which is itself worth seeing.
    """
    with POOL._con() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT ts, pilot, kart, lap_s FROM kart_lap WHERE team_no=? "
            "ORDER BY id DESC LIMIT 400", (str(team_no),))]
    for r in rows:
        r["lap"] = fmt_laptime(r["lap_s"])
        # Stored as "TEAM|Driver" so the rater can tell pilots apart; only the
        # driver is worth showing next to a lap time.
        r["pilot"] = (r["pilot"] or "").rsplit("|", 1)[-1]
    best = min((r["lap_s"] for r in rows), default=None)
    return jsonify(team_no=str(team_no), laps=rows, count=len(rows),
                   best=fmt_laptime(best) if best else "-", best_s=best)

@app.get("/api/kart/<num>/laps")
def api_kart_laps(num):
    """Every lap turned in one kart, whoever was driving it.

    This is the evidence behind the kart's rating: which teams have had it,
    how it went for each of them, and whether the score rests on one driver's
    opinion or several.
    """
    num = str(num)
    with POOL._con() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT ts, pilot, team_no, lap_s, ahead_s, behind_s FROM kart_lap "
            "WHERE kart=? ORDER BY id DESC LIMIT 600", (num,))]
    by_pilot = defaultdict(list)
    tow_gap = POOL.tow_gap_s
    push_gap = POOL.push_gap_s
    for r in rows:
        r["pilot"] = (r["pilot"] or "").rsplit("|", 1)[-1]
        r["lap"] = fmt_laptime(r["lap_s"])
        # A lap run within a length of the kart ahead was towed, so it says
        # more about the slipstream than about the kart.
        r["tow"] = r["ahead_s"] is not None and r["ahead_s"] <= tow_gap
        # A kart right behind is shoving, not slipstreaming.  Shown, not scored.
        r["push"] = r["behind_s"] is not None and r["behind_s"] <= push_gap
        by_pilot[r["pilot"]].append(r["lap_s"])

    times = [r["lap_s"] for r in rows]
    clean = [r["lap_s"] for r in rows if not r["tow"]]
    card = next((c for c in POOL.snapshot()["fleet"] if c["num"] == num), None)
    return jsonify(
        kart=num, laps=rows, count=len(rows),
        best=fmt_laptime(min(times)) if times else "-",
        avg=fmt_laptime(sum(times) / len(times)) if times else "-",
        label=(card or {}).get("label", "Unknown"),
        delta=(card or {}).get("delta"),
        reason=(card or {}).get("reason", ""),
        holder=(card or {}).get("holder", ""),
        # The clean best is the one worth quoting: a tow flatters a lap by the
        # best part of a second and says nothing about the kart.
        tow_laps=len(rows) - len(clean), clean_laps=len(clean),
        push_laps=sum(1 for r in rows if r["push"]),
        clean_best=fmt_laptime(min(clean)) if clean else "-",
        clean_avg=fmt_laptime(sum(clean) / len(clean)) if clean else "-",
        drivers=sorted(
            ({"pilot": p, "laps": len(v), "best": fmt_laptime(min(v)),
              "avg": fmt_laptime(sum(v) / len(v))} for p, v in by_pilot.items()),
            key=lambda d: -d["laps"]))

@app.get("/api/driver/<int:did>/laps")
def api_driver_laps(did):
    """One driver's whole record: laps, stints, and which karts they had.

    Their pace reads differently depending on what they were sitting in, so
    the karts are broken out — a driver who looks slow may have drawn badly.
    """
    with get_db() as con:
        row = con.execute("SELECT * FROM drivers WHERE id=?", (did,)).fetchone()
        if not row:
            return jsonify(error="no such driver"), 404
        drv = dict(row)
        stints = [dict(r) for r in con.execute(
            "SELECT start_ts, end_ts, duration_seconds FROM stints "
            "WHERE driver_id=? ORDER BY id DESC", (did,))]

    # Laps are stored against "TEAM|Driver", so match on the driver half.
    name = (drv["name"] or "").strip()
    with POOL._con() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT ts, pilot, kart, lap_s, s1, s2, s3, ahead_s FROM kart_lap "
            "ORDER BY id DESC LIMIT 4000")]
    team_rows = [r for r in rows
                 if (r["pilot"] or "").split("|", 1)[0].strip().lower()
                 == my_team_name().lower()]
    rows = [r for r in rows
            if (r["pilot"] or "").rsplit("|", 1)[-1].strip().lower() == name.lower()]
    tow_gap = POOL.tow_gap_s
    by_kart = defaultdict(list)
    for r in rows:
        r["lap"] = fmt_laptime(r["lap_s"])
        r["tow"] = r["ahead_s"] is not None and r["ahead_s"] <= tow_gap
        by_kart[r["kart"]].append(r["lap_s"])

    times = [r["lap_s"] for r in rows]
    # A best lap is one lap, and it is the one most likely to have been a tow.
    # What a driver is worth over a stint is the middle of their clean laps,
    # and what they cost is the tail — so both are quoted, and neither is the
    # headline number a single quick lap makes it.
    clean = sorted(r["lap_s"] for r in rows if not r["tow"])
    pace_on = clean or sorted(times)
    owed = max(0.0, CFG["race"].get("driver_min_minutes", 0) * 60
               - drv["total_seconds"])

    # How steady they are, and where that puts them among their own team.  It
    # is measured against the field baseline, so it is not a pace ranking: a
    # slower driver who repeats the same lap is more use on a long stint than
    # a quick one who throws a second away every fourth lap.
    cards = POOL.ratings().get("pilot_cards") or {}
    mine = {k.rsplit("|", 1)[-1].strip(): v for k, v in cards.items()
            if "|" in k and k.rsplit("|", 1)[0].strip() == my_team_name()}
    ranked = sorted((n for n in mine if mine[n]["spread_s"] is not None),
                    key=lambda n: mine[n]["spread_s"])
    card = mine.get(name)
    # Practice is our team alone in one kart, so what is left between drivers
    # is the drivers.  In a race the same number is tangled up with whichever
    # kart they happened to be given, so it is only offered here.
    pace = None
    if CFG["karts"].get("practice") and card and len(mine) > 1:
        order = sorted(mine, key=lambda n: mine[n]["effect"])
        pace = {"delta_s": round(card["effect"] - mine[order[0]]["effect"], 3),
                "rank": order.index(name) + 1, "of": len(order),
                "laps": card["laps"], "quickest": order[0]}
    consistency = None
    if card and card["spread_s"] is not None:
        consistency = {
            "spread_s": card["spread_s"], "laps": card["laps"],
            "rank": ranked.index(name) + 1 if name in ranked else None,
            "of": len(ranked),
            "steadiest_s": mine[ranked[0]]["spread_s"] if ranked else None}
    for st in stints:
        st["dur"] = fmt_duration(st["duration_seconds"] or 0)
    return jsonify(
        id=did, name=name, laps=rows[:600], count=len(rows),
        best=fmt_laptime(min(times)) if times else "-",
        avg=fmt_laptime(sum(times) / len(times)) if times else "-",
        clean_laps=len(clean),
        median=fmt_laptime(statistics.median(pace_on)) if pace_on else "-",
        # quantiles needs two points to interpolate between; with one lap the
        # ninetieth percentile is that lap.
        p90=fmt_laptime(statistics.quantiles(pace_on, n=10)[8]) if len(pace_on) > 1
            else (fmt_laptime(pace_on[0]) if pace_on else "-"),
        total_fmt=fmt_duration(drv["total_seconds"]),
        owed_fmt=fmt_duration(owed), owed_seconds=owed,
        consistency=consistency,
        pace=pace,
        sectors=sector_review(rows, team_rows),
        stints=stints[:40], stint_count=len(stints),
        karts=sorted(
            ({"kart": k, "laps": len(v), "best": fmt_laptime(min(v)),
              "avg": fmt_laptime(sum(v) / len(v))} for k, v in by_kart.items()),
            key=lambda d: -d["laps"]))

@app.get("/api/apex/sessions")
def api_apex_sessions():
    """Every session this track has archived, for reviewing a finished run."""
    return jsonify(sessions=apex_sessions(CFG.get("apex_url", "")))


@app.get("/api/apex/session/<sid>")
def api_apex_session(sid):
    """One archived session's final classification.

    This is all Apex keeps of a finished session: the order, each kart's best
    lap and its lap count.  It is not the individual laps, so it cannot feed
    the kart model — that needs many laps shared between drivers — but it is
    what answers "what did we actually do in qualifying".
    """
    res = apex_session_result(CFG.get("apex_url", ""), str(sid))
    rows = sorted(res["rows"], key=lambda r: _int_or_last(r.get("pos")))
    return jsonify(
        id=res.get("id", sid),
        name=(res["meta"].get("name") or "").strip(),
        control=res["meta"].get("control") or [],
        finished=res["meta"].get("light") == "lf",
        rows=[{"pos": r.get("pos", ""), "kart": r.get("kart", ""),
               "team": r.get("team", "") or r.get("driver", ""),
               "driver": r.get("driver", ""),
               "best_lap": r.get("best_lap", "-"),
               "last_lap": r.get("last_lap", "-"),
               "total_laps": r.get("total_laps", "-"),
               "gap": r.get("gap", "-")} for r in rows])


@app.get("/api/karts")
def api_karts():
    return jsonify(POOL.snapshot())

@app.post("/api/note")
def api_note():
    """One line about something the feed cannot see.

    Free text on purpose: at four in the morning nobody picks a category off
    a dropdown, they type "17 bent steering, do not take it again".
    """
    return jsonify(ok=POOL.note((request.json or {}).get("text", "")))

@app.post("/api/kart/retire")
def api_kart_retire():
    """A kart out of service — broken, stored, withdrawn by the organisers."""
    d = request.json or {}
    kart = str(d.get("kart", "")).strip()
    if not kart:
        return jsonify(ok=False, error="no kart"), 400
    if d.get("back"):
        POOL.unretire_kart(kart)
    else:
        POOL.retire_kart(kart, str(d.get("reason", "")))
    return jsonify(ok=True)

@app.post("/api/karts/reset")
def api_karts_reset():
    """Forget the fleet: every kart's measured pace and who is holding it.

    Separate from the race reset on purpose — this is the one that loses the
    qualifying data, so it should only happen when someone means it.
    """
    POOL.reset()
    log("KART POOL RESET", "ratings and assignments cleared")
    return jsonify(ok=True)

@app.post("/api/kart/lane")
def api_kart_lane():
    """Answer the one question the feed cannot: which lane did they use."""
    d = request.json or {}
    POOL.resolve(int(d["stop_id"]),
                 lane=d.get("lane"),
                 no_change=bool(d.get("no_change")),
                 kart_out=d.get("kart"))
    return jsonify(ok=True)

@app.post("/api/kart/add")
def api_kart_add():
    """Blank kart is deliberate: a placeholder holding the queue position
    until someone reads the number off the physical kart -- see lane_rename."""
    d = request.json or {}
    POOL.lane_add(int(d.get("lane", 1)), str(d.get("kart", "")).strip())
    return jsonify(ok=True)

@app.post("/api/kart/rename")
def api_kart_rename():
    d = request.json or {}
    POOL.lane_rename(int(d.get("lane", 1)), str(d.get("old", "")).strip(),
                     str(d.get("new", "")).strip())
    return jsonify(ok=True)

@app.post("/api/kart/remove")
def api_kart_remove():
    d = request.json or {}
    POOL.lane_remove(int(d.get("lane", 1)), str(d.get("kart", "")).strip())
    return jsonify(ok=True)

@app.post("/api/kart/assign")
def api_kart_assign():
    """The number read off the kart always wins over what we inferred."""
    d = request.json or {}
    team_no = str(d.get("team_no", "")).strip()
    if not team_no:
        return jsonify(ok=False, error="Team number required"), 400
    with _lock:
        team = next((t.get("team", "") for t in _teams
                     if str(t.get("kart", "")) == team_no), "")
    POOL.set_kart(team_no, team, str(d.get("kart", "")).strip())
    return jsonify(ok=True)

@app.post("/api/kart/stop")
def api_kart_stop():
    """A stop the feed never saw."""
    d = request.json or {}
    team_no = str(d.get("team_no", "")).strip()
    with _lock:
        team = next((t.get("team", "") for t in _teams
                     if str(t.get("kart", "")) == team_no), "")
    stop_id = POOL.manual_stop(team_no, team)
    return jsonify(ok=True, stop_id=stop_id)

@app.post("/api/kart/undo")
def api_kart_undo():
    ok = POOL.undo()
    return jsonify(ok=ok)

# ── Settings ───────────────────────────────────────────────────────────────────
@app.post("/api/settings")
def api_settings():
    data = request.json or {}
    if "apex_url" in data:
        new_url = data["apex_url"].strip()
        # Saving settings for any reason always resends the current URL, so
        # only a real change should pay for a fresh WS probe -- otherwise
        # ticking an unrelated checkbox re-blocks a connection that was
        # already working AJAX-only and stalls the feed for no reason.
        if new_url != CFG.get("apex_url", ""):
            CFG["apex_url"] = new_url
            global _ws_url_cache, _ws_url_checked_at, _ws_blocked
            _ws_url_cache, _ws_url_checked_at = None, 0.0  # force re-scan next cycle
            # The new event has its own AJAX cursor and its own socket; carrying
            # the old track's over would ask Apex to resume a stream that is
            # not ours.
            _reset_ajax_state()
            # Another track's purple is not ours.
            _reset_sector_best()
            _ws_blocked = False
    if "team_name" in data:
        CFG["team_name"] = data["team_name"].strip()
    if "duration_minutes" in data:
        CFG["race"]["duration_minutes"] = int(data["duration_minutes"])
    if "mandatory_pits" in data:
        CFG["race"]["mandatory_pits"] = int(data["mandatory_pits"])
    if "stint_max_minutes" in data:
        CFG["race"]["stint_max_minutes"] = int(data["stint_max_minutes"])
    for key, cast in (("lanes", int), ("auto_pit", bool),
                      ("swap_every_stop", bool), ("practice", bool)):
        if key in data:
            CFG["karts"][key] = cast(data[key])
    if "exclude_teams" in data:
        # Comma separated from the settings box, a list on the wire.
        raw = data["exclude_teams"]
        names = raw.split(",") if isinstance(raw, str) else list(raw or [])
        CFG["karts"].setdefault("rating", {})["exclude_teams"] = \
            [n.strip() for n in names if n and n.strip()]
    POOL.configure(CFG["karts"])
    save_cfg()
    return jsonify(ok=True)

# ── Startup (runs for both direct execution and gunicorn) ─────────────────────
init_db()
# apex_dump imports this module to reuse the parser, and does its own polling.
# Two pollers would share one AJAX cursor and steal frames from each other, so
# the tool sets this to keep the background worker out of the way.
if not os.environ.get("WARROOM_NO_WORKER"):
    threading.Thread(target=worker, daemon=True).start()

# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"\n  {CFG['team_name']} WAR ROOM  ->  http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
