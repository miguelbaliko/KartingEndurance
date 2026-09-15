#!/usr/bin/env python3
"""TPC War Room — Karting Endurance Strategy Tool"""

from flask import Flask, render_template, jsonify, request, Response
import threading, time, json, sqlite3, urllib.request, urllib.error, urllib.parse
import html.parser, re, os, queue, math, statistics
from datetime import datetime, timedelta
from typing import Optional
from collections import defaultdict

import kartpool
from raceclock import ApexClock

try:
    import websocket as _ws_mod
    import ssl as _ssl
    _HAS_WS = True
except ImportError:
    _HAS_WS = False

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
# WARROOM_CONFIG mirrors WARROOM_DB: it lets a second instance, or a test, run
# against its own settings instead of whatever the last run happened to save.
_CFG_PATH = os.environ.get("WARROOM_CONFIG") or os.path.join(
    os.path.dirname(__file__), "config.json")

def load_cfg() -> dict:
    base = {
        "team_name": "TPC",
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
            # What a stop really costs against staying out: the 3 minutes in
            # the box plus the pit lane itself.  Measure it in practice and set
            # it — the minimum alone understates the loss.
            "pit_loss_seconds": 200,
            "driver_min_minutes": 120,    # §3.14 every driver, over the event
            "pace_drop_warn": 0.30,
            "pace_drop_box": 0.50,
        },
        "karts": dict(kartpool.DEFAULTS),
    }
    if os.path.exists(_CFG_PATH):
        with open(_CFG_PATH) as f:
            saved = json.load(f)
        base.update(saved)
        if "race" in saved:
            base["race"].update(saved["race"])
        if "karts" in saved:
            base["karts"].update(saved["karts"])
    apply_category(base["race"])
    return base

# §3.8 and §3.10: the only two numbers that differ between the categories.
CATEGORY_RULES = {
    "PRO": {"mandatory_pits": 28, "stint_max_minutes": 80},
    "AM":  {"mandatory_pits": 34, "stint_max_minutes": 60},
}

def apply_category(race: dict):
    """Set the category's stops and stint ceiling, unless they were overridden.

    A saved config that names a category but keeps the other category's numbers
    is the more likely mistake, so the category wins over stale saved values.
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

def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with sqlite3.connect(DB) as con:
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
            INSERT OR IGNORE INTO kv VALUES ('session_mode', 'race');
            INSERT OR IGNORE INTO kv VALUES ('next_driver_id', '');
                """)
        # How long the box stop after this stint actually took. Without it
        # pit_loss_seconds can only ever be a guess.
        _add_column(con, "stints", "box_seconds", "REAL")

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
    "pit": "pits", "gap": "gap", "int": "interval",
    # Apex timing state classes (only appear on lap-time cells)
    "tb": "last_lap", "ti": "last_lap", "tn": "last_lap", "ib": "best_lap",
}

_global_col_types: dict = {}   # "c6" -> "last_lap" (built from grid header row)
_row_kart_map: dict     = {}   # "r14915" -> "17"    (built from parsed data rows)

class ApexParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self._cur, self._col = [], None, None
        self.meta: dict = {}        # data-id -> {"text":..., "cls":...}
        self._meta_id: Optional[str] = None
        self._is_head  = False
        self._row_did: Optional[str] = None
        self.col_types: dict    = {}   # c6 -> "last_lap" (from head row data-type)
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
            dt = a.get("data-type", "")
            if dt in _CELL_MAP:
                # Header row: register column type; data row: map column
                if self._is_head and did:
                    self.col_types[did] = _CELL_MAP[dt]
                    self.saw_head = True
                if self._cur is not None:
                    self._col = _CELL_MAP[dt]
            elif self._cur is not None:
                for cls in a.get("class", "").split():
                    if cls in _CELL_MAP:
                        self._col = _CELL_MAP[cls]
                        break
                # Fallback: the cell's data-id ends in the header's column id,
                # so "r93c8" is whatever column c8 was declared to be.  Prefer
                # the header in this very frame — the module-level map is only
                # updated once the whole frame is parsed, so on the first frame
                # (the one that carries the header) it is still empty.
                if self._col is None and did:
                    col_m = re.search(r'(c\d+)$', did)
                    if col_m:
                        col = col_m.group(1)
                        # A header in this frame is the last word: a column it
                        # does not declare is not a column we read.  Falling
                        # back to the module map here would let a previous
                        # event's layout decide — which is how Palmela's sector
                        # time landed in the lap time, because kartplanet had a
                        # lap time in that same column.
                        self._col = (self.col_types.get(col) if self.saw_head
                                     else _global_col_types.get(col))

    def handle_data(self, data):
        v = data.strip()
        if not v:
            return
        if self._meta_id:
            self.meta.setdefault(self._meta_id, {})["text"] = v
        if self._col and self._cur is not None:
            self._cur.setdefault(self._col, v)
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
_sse_queues: list = []

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
        _do_pit_done(kv_get("next_driver_id") or kv_get("driver_id"), source="feed")

POOL = kartpool.KartPool(DB, CFG.get("karts", {}),
                         on_my_stop=_feed_boxed_me,
                         on_my_release=_feed_released_me)

def broadcast():
    data = "data: " + json.dumps(make_snapshot()) + "\n\n"
    dead = []
    for q in _sse_queues:
        try:
            q.put_nowait(data)
        except queue.Full:
            dead.append(q)
    for q in dead:
        try:
            _sse_queues.remove(q)
        except ValueError:
            pass

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
    return t

def _process_rows(rows: list) -> bool:
    """Replace _teams with a newly-parsed full grid. Returns True if any rows."""
    global _teams, _apex_ok
    if not rows:
        return False
    with _lock:
        built = [_enrich(dict(r)) for r in rows]
        if built:
            _teams = built
            _apex_ok = True
    # Outside the lock: the pool's callbacks broadcast, and broadcasting needs it.
    _feed_pool(built)
    return True

def _feed_pool(rows: list):
    if not rows:
        return
    try:
        POOL.observe(rows, my_team=CFG.get("team_name", ""))
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
    if _ws_url_checked_at and now_t - _ws_url_checked_at < 300:
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
_ajax_state: dict = {"init": "1", "index": "0", "counter": 0}

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

def _reset_ajax_state():
    _ajax_state.update({"init": "1", "index": "0", "counter": 0})

def _parse_apex_pipe(msg: str) -> tuple:
    """Parse Apex Timing pipe-delimited WebSocket protocol.
    Returns (rows, cell_updates, meta).
    rows = full row dicts for _process_rows,
    cell_updates = {kart: {field: value}} for _apply_cell_updates,
    meta = session info for _process_meta."""
    global _global_col_types, _row_kart_map
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
            if hp.row_kart_map:
                _row_kart_map.update(hp.row_kart_map)
            rows.extend(hp.rows)

        elif cmd in ('R', 'row') and val:
            hp = ApexParser()
            hp.feed(val)
            if hp.row_kart_map:
                _row_kart_map.update(hp.row_kart_map)
            rows.extend(hp.rows)

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

def parse_control_log(html_s: str) -> list:
    """Race control's messages, newest first.

    Safety car and red flag periods are the cheapest stops of the race — a stop
    taken under a neutralisation costs a fraction of one taken under green — so
    this is worth reading rather than discarding.
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

def _ws_run(ws_url: str, done_evt: threading.Event):
    """Connect to Apex Timing WebSocket, push rows on every message."""
    global _apex_ok, _ws_msg_count

    def on_msg(ws, msg):
        global _ws_msg_count, _apex_ok
        if not msg:
            return
        _ws_msg_count += 1
        try:
            s = msg.strip()
            if not s:
                return

            # Log first message in full so we can see the protocol
            if _ws_msg_count == 1:
                log("APEX WS MSG#1", f"{len(s)} bytes\n{s[:3000]}")
            elif _ws_msg_count % 20 == 0:
                log("APEX WS", f"msg #{_ws_msg_count} | teams={len(_teams)} | session={_apex_session.get('name','?')}")

            rows = []
            cell_updates = {}
            meta = {}
            if s[0] in ('{', '['):
                d = json.loads(s)
                rows = d if isinstance(d, list) else d.get('rows', d.get('data', d.get('grid', [])))
                if not rows and isinstance(d, dict):
                    html_s = d.get('html', d.get('content', d.get('grid_html', '')))
                    if html_s:
                        p = ApexParser(); p.feed(html_s); rows = p.rows; meta = p.meta
            elif '|' in s:
                # Apex Timing pipe-delimited protocol
                rows, cell_updates, meta = _parse_apex_pipe(s)
            else:
                p = ApexParser(); p.feed(s); rows = p.rows; meta = p.meta

            if meta:
                _process_meta(meta)
            if cell_updates:
                _apply_cell_updates(cell_updates)
                _apex_ok = True
            if _process_rows(rows):
                _apex_ok = True
                if _ws_msg_count <= 3:
                    log("APEX PARSED", f"{len(rows)} teams, meta={meta}")
        except Exception as e:
            if _ws_msg_count <= 3:
                log("APEX WS ERR", str(e))

    def on_open(ws):
        global _apex_ok, _ws_msg_count
        _ws_msg_count = 0
        _apex_ok = True
        log("APEX WS CONNECTED", ws_url)

    def on_close(ws, code, msg):
        global _apex_ok
        _apex_ok = False
        log("APEX WS CLOSED", f"code={code}")
        done_evt.set()

    def on_error(ws, _err):
        global _apex_ok
        _apex_ok = False
        log("APEX WS ERROR", str(_err))

    ws = _ws_mod.WebSocketApp(ws_url,
        on_message=on_msg, on_error=on_error,
        on_close=on_close, on_open=on_open)
    ws.run_forever(
        sslopt={"cert_reqs": _ssl.CERT_NONE},
        ping_interval=30, ping_timeout=10,
    )

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

def set_plan_stop(stop_idx: int, driver_id):
    plan = get_pit_plan()
    n = CFG["race"]["mandatory_pits"]
    while len(plan) < n:
        plan.append({"driver_id": None, "note": ""})
    if 0 <= stop_idx < n:
        plan[stop_idx]["driver_id"] = driver_id
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
            done = threading.Event()
            _ws_msg_count = 0
            t = threading.Thread(target=_ws_run, args=(ws_url, done), daemon=True)
            t.start()
            done.wait(timeout=600)  # reconnect after 10 min max or on disconnect
            t.join(timeout=5)
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


_LAP_GAP = re.compile(r'^\s*(\d+)\s*(?:lap|laps|volta|voltas|t)\b', re.I)

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

    Returns {kart: {"virtual_pos", "stops_owed", "debt_s", "virtual_gap_s"}}.
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

    ranked = sorted(known, key=lambda r: r["virtual"])
    front = ranked[0]["virtual"]
    out = {}
    for i, r in enumerate(ranked, start=1):
        out[r["kart"]] = {
            "virtual_pos":   i,
            "stops_owed":    r["owed"],
            "debt_s":        round(r["debt"], 1),
            # Measured off whoever actually leads once the stops are counted.
            "virtual_gap_s": round(r["virtual"] - front, 1),
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
        return {"verdict": "unknown", "best": best, "worst": worst,
                "detail": f"waiting: {fmt_delta(best)} to {fmt_delta(worst)}"}
    # Lower delta is a quicker kart, so an improvement is a fall in delta.
    if worst < current_delta:
        v = "better"            # even the unlucky draw is an upgrade
    elif best > current_delta:
        v = "worse"             # even the lucky draw is a downgrade
    else:
        v = "mixed"
    return {"verdict": v, "best": best, "worst": worst,
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
    return {
        "n": len(times),
        "median_s": round(med, 1),
        "median": fmt_mmss(med),
        "fastest": fmt_mmss(times[0]),
        "slowest": fmt_mmss(times[-1]),
        "over_minimum_s": round(over, 1),
        "configured_s": configured_loss_s,
        "note": (f"{len(times)} stops, median {fmt_mmss(med)} in the box "
                 f"({over:+.0f}s on the {fmt_mmss(minimum_s)} minimum). "
                 f"Pit loss is set to {configured_loss_s:.0f}s; the box alone "
                 f"is {med:.0f}s, so the in and out lap make up the rest."),
    }


def class_positions(teams: list, lap_s: Optional[float]) -> dict:
    """Position and gaps within a team's own category.

    We race AM (§2.5).  The leader on the timing screen is very likely a PRO
    team we are not classified against, so the overall gap is the wrong number
    to make a call on — what matters is the AM team directly ahead.

    Returns {kart: {"class", "class_pos", "class_of", "class_gap_s",
    "ahead_kart", "ahead_s"}}.  Teams whose gap cannot be read are left out
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
        leader = group[0]["gap"]
        for i, r in enumerate(group):
            ahead = group[i - 1] if i else None
            out[r["kart"]] = {
                "class":       cat,
                "class_pos":   i + 1,
                "class_of":    len(group),
                "class_gap_s": round(r["gap"] - leader, 1),
                "ahead_kart":  ahead["kart"] if ahead else "",
                "ahead_s":     round(r["gap"] - ahead["gap"], 1) if ahead else None,
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

# ── Strategy engine ────────────────────────────────────────────────────────────
def compute_strategy(stint_s: float, race_elapsed_s: float, pits_done: int,
                     my_avg5: Optional[float], prev_avg5: Optional[float],
                     light: str = "", box_now: dict = None,
                     my_delta: Optional[float] = None) -> dict:
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
        return {"label": label, "cls": cls, "detail": detail, "why": why,
                "stops_left": stops_left}

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
    if clock["ok"]:
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
    my_team = next((t for t in teams_raw if t.get("team", "") == my_name), None)
    my_avg5 = my_team["avg5_s"] if my_team else None

    # The timekeepers' pit count is the one that settles a protest, so use it
    # when the feed carries it and fall back to our own stint log when it does not.
    pits_done = stints_done
    if my_team:
        try:
            pits_done = int(str(my_team.get("pits", "")).strip())
        except (TypeError, ValueError):
            pass

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
    # The call depends on the flag and on what is waiting in the lanes, so it
    # is made after both are known.
    strat = compute_strategy(stint_s, race_elapsed, pits_done, my_avg5, _prev_avg5,
                             light=apex_session.get("light", ""), box_now=box_now,
                             my_delta=my_card["delta"] if my_card else None)
    R = CFG["race"]
    virt = virtual_positions(teams_raw, R["mandatory_pits"],
                             R.get("pit_loss_seconds") or R["pit_duration_seconds"],
                             track_avg_s)
    teams_out = []
    for t in teams_raw:
        held = kart_of.get(str(t.get("kart", "")))
        card = kart_card.get(held) if held else None
        teams_out.append({
            "pos":        t.get("pos", ""),
            "kart":       t.get("kart", ""),
            "team":       t.get("team", ""),
            "driver":     t.get("driver", ""),
            "category":   t.get("category", ""),
            "in_pit":     t.get("in_pit", False),
            "my_kart":    held or "",
            "kart_label": card["label"] if card else "",
            "kart_delta": card["delta"] if card else None,
            "last_lap":   t.get("last_lap", "-"),
            "avg5":       t.get("avg5", "-"),
            "avg10":      t.get("avg10", "-"),
            "best_lap":   t.get("best_lap", "-"),
            "total_laps": t.get("total_laps", "-"),
            "pits":       t.get("pits", "-"),
            "gap":        t.get("gap", "-"),
            "is_my_team": t.get("team", "") == my_name,
            **(virt.get(str(t.get("kart", ""))) or
               {"virtual_pos": None, "stops_owed": None,
                "debt_s": None, "virtual_gap_s": None}),
            **(klass.get(str(t.get("kart", ""))) or
               {"class": "", "class_pos": None, "class_of": None,
                "class_gap_s": None, "ahead_kart": "", "ahead_s": None}),
        })

    stint_pct = min(100, (stint_s / (CFG["race"]["stint_max_minutes"] * 60)) * 100) if stint_s else 0

    # Session mode (qualifying vs race)
    session_mode = kv_get("session_mode") or "race"

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
            "planned_fmt": fmt_duration((i + 1) * avg_stint_s),
            "done": i < len(pit_history),
            "actual_end": actual["end"] if actual else "",
        })

    return {
        "ts":               now.isoformat() + "Z",  # explicit UTC so JS Date() parses correctly
        "status":           status,
        "apex_ok":          apex_ok,
        "apex_session":     apex_session,
        "race_elapsed":     race_elapsed,
        "race_remaining":   race_remaining,
        "race_remaining_fmt": fmt_duration(race_remaining),
        "stint_s":          stint_s,
        "stint_running":    stint_running,
        "stint_fmt":        fmt_duration(stint_s),
        "stint_pct":        round(stint_pct, 1),
        "pit_remaining":    pit_remaining,
        "pit_remaining_fmt": fmt_mmss(pit_remaining),
        "pit_min_met":       pit_min_met,
        "pit_penalty_now":   pit_penalty_now,
        "strategy":         strat,
        "current_driver":   current_driver,
        "drivers":          drivers,
        "pits_done":        pits_done,
        "stints_done":      stints_done,
        "mandatory_pits":   CFG["race"]["mandatory_pits"],
        "stint_max_minutes": CFG["race"]["stint_max_minutes"],
        "next_karts":       candidates,
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
        "apex_url":         CFG.get("apex_url", ""),
        "teams":            teams_out,
        "session_mode":     session_mode,
        "pit_plan":         pit_plan_out,
        "clock_source":     clock_source,
        "race_elapsed_fmt": fmt_duration(race_elapsed),
        "kartpool":         pool,
        "next_driver_id":   kv_get("next_driver_id"),
        "auto_pit":         CFG["karts"].get("auto_pit", True),
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

@app.get("/stream")
def sse_stream():
    q = queue.Queue(maxsize=5)
    _sse_queues.append(q)

    def gen():
        try:
            try:
                yield "data: " + json.dumps(make_snapshot()) + "\n\n"
            except Exception:
                yield ": init-error\n\n"
            while True:
                try:
                    yield q.get(timeout=15)
                except queue.Empty:
                    yield ": ping\n\n"
        except GeneratorExit:
            pass
        finally:
            try:
                _sse_queues.remove(q)
            except ValueError:
                pass

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})

# ── Race control ───────────────────────────────────────────────────────────────
@app.post("/api/race/start")
def race_start():
    now = datetime.utcnow().isoformat()
    kv_set("race_start", now)
    kv_set("stint_start", now)   # stint clock always starts with the race
    kv_set("status", "racing")
    log("RACE START")
    broadcast()
    return jsonify(ok=True)

@app.post("/api/race/stop")
def race_stop():
    kv_set("status", "idle")
    log("RACE STOP")
    broadcast()
    return jsonify(ok=True)

@app.post("/api/race/reset")
def race_reset():
    for k, v in [("status","idle"),("race_start",""),("stint_start",""),
                  ("pit_start",""),("driver_id",""),("next_driver_id","")]:
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
    broadcast()
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
    broadcast()
    return jsonify(ok=True)

@app.post("/api/driver/add")
def driver_add():
    name = (request.json.get("name") or "").strip()
    if not name:
        return jsonify(ok=False, error="Name required"), 400
    with get_db() as con:
        con.execute("INSERT INTO drivers(name) VALUES(?)", (name,))
    log("DRIVER ADD", name)
    broadcast()
    return jsonify(ok=True)

@app.post("/api/driver/rename")
def driver_rename():
    did  = request.json.get("driver_id")
    name = (request.json.get("name") or "").strip()
    if not name:
        return jsonify(ok=False, error="Name required"), 400
    with get_db() as con:
        con.execute("UPDATE drivers SET name=? WHERE id=?", (name, did))
    broadcast()
    return jsonify(ok=True)

@app.post("/api/driver/delete")
def driver_delete():
    did = request.json.get("driver_id")
    with get_db() as con:
        con.execute("DELETE FROM drivers WHERE id=?", (did,))
    # Clear current driver if it was this one
    if kv_get("driver_id") == str(did):
        kv_set("driver_id", "")
    broadcast()
    return jsonify(ok=True)

@app.post("/api/driver/clear_time")
def driver_clear_time():
    did = request.json.get("driver_id")
    with get_db() as con:
        con.execute("UPDATE drivers SET total_seconds=0 WHERE id=?", (did,))
        con.execute("DELETE FROM stints WHERE driver_id=?", (did,))
    broadcast()
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
    broadcast()

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
    kv_set("next_driver_id", "")
    with get_db() as con:
        row = con.execute("SELECT name FROM drivers WHERE id=?", (new_did,)).fetchone()
        new_name = row["name"] if row else new_did
        # Against the stint that just ended — that is the stop it belongs to.
        if pit_elapsed > 0:
            con.execute("UPDATE stints SET box_seconds=? WHERE id="
                        "(SELECT MAX(id) FROM stints)", (pit_elapsed,))
    log("PIT DONE", f"driver={new_name}  pit_time={fmt_mmss(pit_elapsed)}  [{source}]")
    broadcast()

@app.post("/api/driver/next")
def driver_next():
    """Park the next driver server-side so a feed-driven stop can seat them."""
    kv_set("next_driver_id", str(request.json.get("driver_id") or ""))
    broadcast()
    return jsonify(ok=True)

# ── Pit plan route ─────────────────────────────────────────────────────────────
@app.post("/api/plan/set")
def api_plan_set():
    data = request.json or {}
    stop = int(data.get("stop", 1)) - 1  # 1-indexed from client
    driver_id = data.get("driver_id")    # None to unassign
    set_plan_stop(stop, driver_id)
    return jsonify(ok=True)

@app.post("/api/plan/reset")
def api_plan_reset():
    kv_set("pit_plan", "")
    return jsonify(ok=True)

# ── Session mode ────────────────────────────────────────────────────────────────
@app.post("/api/mode")
def api_mode():
    mode = (request.json or {}).get("mode", "race")
    if mode in ("race", "qualifying"):
        kv_set("session_mode", mode)
    broadcast()
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
            "SELECT ts, pilot, team_no, lap_s FROM kart_lap WHERE kart=? "
            "ORDER BY id DESC LIMIT 600", (num,))]
    by_pilot = defaultdict(list)
    for r in rows:
        r["pilot"] = (r["pilot"] or "").rsplit("|", 1)[-1]
        r["lap"] = fmt_laptime(r["lap_s"])
        by_pilot[r["pilot"]].append(r["lap_s"])

    times = [r["lap_s"] for r in rows]
    card = next((c for c in POOL.snapshot()["fleet"] if c["num"] == num), None)
    return jsonify(
        kart=num, laps=rows, count=len(rows),
        best=fmt_laptime(min(times)) if times else "-",
        avg=fmt_laptime(sum(times) / len(times)) if times else "-",
        label=(card or {}).get("label", "Unknown"),
        delta=(card or {}).get("delta"),
        reason=(card or {}).get("reason", ""),
        holder=(card or {}).get("holder", ""),
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
            "SELECT ts, pilot, kart, lap_s FROM kart_lap "
            "ORDER BY id DESC LIMIT 4000")]
    rows = [r for r in rows
            if (r["pilot"] or "").rsplit("|", 1)[-1].strip().lower() == name.lower()]
    by_kart = defaultdict(list)
    for r in rows:
        r["lap"] = fmt_laptime(r["lap_s"])
        by_kart[r["kart"]].append(r["lap_s"])

    times = [r["lap_s"] for r in rows]
    owed = max(0.0, CFG["race"].get("driver_min_minutes", 0) * 60
               - drv["total_seconds"])
    for st in stints:
        st["dur"] = fmt_duration(st["duration_seconds"] or 0)
    return jsonify(
        id=did, name=name, laps=rows[:600], count=len(rows),
        best=fmt_laptime(min(times)) if times else "-",
        avg=fmt_laptime(sum(times) / len(times)) if times else "-",
        total_fmt=fmt_duration(drv["total_seconds"]),
        owed_fmt=fmt_duration(owed), owed_seconds=owed,
        stints=stints[:40], stint_count=len(stints),
        karts=sorted(
            ({"kart": k, "laps": len(v), "best": fmt_laptime(min(v)),
              "avg": fmt_laptime(sum(v) / len(v))} for k, v in by_kart.items()),
            key=lambda d: -d["laps"]))

@app.get("/api/karts")
def api_karts():
    return jsonify(POOL.snapshot())

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
    broadcast()
    return jsonify(ok=True)

@app.post("/api/karts/reset")
def api_karts_reset():
    """Forget the fleet: every kart's measured pace and who is holding it.

    Separate from the race reset on purpose — this is the one that loses the
    qualifying data, so it should only happen when someone means it.
    """
    POOL.reset()
    log("KART POOL RESET", "ratings and assignments cleared")
    broadcast()
    return jsonify(ok=True)

@app.post("/api/kart/lane")
def api_kart_lane():
    """Answer the one question the feed cannot: which lane did they use."""
    d = request.json or {}
    POOL.resolve(int(d["stop_id"]),
                 lane=d.get("lane"),
                 no_change=bool(d.get("no_change")),
                 kart_out=d.get("kart"))
    broadcast()
    return jsonify(ok=True)

@app.post("/api/kart/add")
def api_kart_add():
    d = request.json or {}
    kart = str(d.get("kart", "")).strip()
    if not kart:
        return jsonify(ok=False, error="Kart number required"), 400
    POOL.lane_add(int(d.get("lane", 1)), kart)
    broadcast()
    return jsonify(ok=True)

@app.post("/api/kart/remove")
def api_kart_remove():
    d = request.json or {}
    POOL.lane_remove(int(d.get("lane", 1)), str(d.get("kart", "")).strip())
    broadcast()
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
    broadcast()
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
    broadcast()
    return jsonify(ok=True, stop_id=stop_id)

@app.post("/api/kart/undo")
def api_kart_undo():
    ok = POOL.undo()
    broadcast()
    return jsonify(ok=ok)

# ── Settings ───────────────────────────────────────────────────────────────────
@app.post("/api/settings")
def api_settings():
    data = request.json or {}
    if "apex_url" in data:
        CFG["apex_url"] = data["apex_url"].strip()
        global _ws_url_cache, _ws_url_checked_at, _ws_blocked
        _ws_url_cache, _ws_url_checked_at = None, 0.0  # force re-scan on next cycle
        # The new event has its own AJAX cursor and its own socket; carrying the
        # old track's over would ask Apex to resume a stream that is not ours.
        _reset_ajax_state()
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
                      ("swap_every_stop", bool)):
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
    broadcast()
    return jsonify(ok=True)

# ── Debug ──────────────────────────────────────────────────────────────────────
@app.get("/debug/apex")
def debug_apex():
    url = CFG.get("apex_url", "")
    result = {"url": url, "ws_url_cache": _ws_url_cache, "apex_ok": _apex_ok}
    if url:
        try:
            base = url.split('#')[0].rstrip('/')
            req = urllib.request.Request(base, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120"
            })
            with urllib.request.urlopen(req, timeout=8) as r:
                src = r.read().decode("utf-8", errors="ignore")
            result["page_length"] = len(src)
            result["page_snippet"] = src[:4000]
            # Find all script src references
            result["script_srcs"] = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', src)
            # Look for any port/ws patterns
            result["ws_matches"] = re.findall(r'.{0,60}(?:WebSocket|wsPort|ws_port|wss?://|port\s*[:=]\s*\d{3,5}).{0,60}', src)
        except Exception as e:
            result["error"] = str(e)
    return jsonify(result)

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
