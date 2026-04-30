#!/usr/bin/env python3
"""APX GP War Room — Karting Endurance Strategy Tool"""

from flask import Flask, render_template, jsonify, request, Response
import threading, time, json, sqlite3, urllib.request, urllib.error, urllib.parse
import html.parser, re, os, queue
from datetime import datetime, timedelta
from typing import Optional

try:
    import websocket as _ws_mod
    import ssl as _ssl
    _HAS_WS = True
except ImportError:
    _HAS_WS = False

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
_CFG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

def load_cfg() -> dict:
    base = {
        "team_name": "MY TEAM",
        "apex_url": "",
        "refresh_interval": 5,
        "race": {
            "duration_minutes": 780,
            "mandatory_pits": 23,
            "stint_max_minutes": 45,
            "pit_duration_seconds": 180,
            "no_pit_last_minutes": 5,
            "pace_drop_warn": 0.30,
            "pace_drop_box": 0.50,
        },
    }
    if os.path.exists(_CFG_PATH):
        with open(_CFG_PATH) as f:
            saved = json.load(f)
        base.update(saved)
        if "race" in saved:
            base["race"].update(saved["race"])
    return base

CFG = load_cfg()

def save_cfg():
    with open(_CFG_PATH, "w") as f:
        json.dump(CFG, f, indent=2)

# ── Database ───────────────────────────────────────────────────────────────────
DB = os.path.join(os.path.dirname(__file__), "data", "race.db")

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
                """)

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
    "no": "kart", "kart": "kart",
    "dr": "team", "name": "team", "team": "team",
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
            self._row_did = did
            self._cur = None if self._is_head else {}
            self._meta_id = None
        elif tag in ("td", "th"):
            self._col = None
            dt = a.get("data-type", "")
            if dt in _CELL_MAP:
                # Header row: register column type; data row: map column
                if self._is_head and did:
                    self.col_types[did] = _CELL_MAP[dt]
                if self._cur is not None:
                    self._col = _CELL_MAP[dt]
            elif self._cur is not None:
                for cls in a.get("class", "").split():
                    if cls in _CELL_MAP:
                        self._col = _CELL_MAP[cls]
                        break
                # Fallback: use cell data-id to look up column field
                if self._col is None and did:
                    col_m = re.search(r'(c\d+)$', did)
                    if col_m:
                        self._col = _global_col_types.get(col_m.group(1))

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
            if self._cur and len(self._cur) >= 2:
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
_apex_session: dict = {"name": "", "light": "", "dyn1": "", "dyn2": ""}
_ws_msg_count = 0
_sse_queues: list = []

def _process_meta(meta: dict):
    """Update session state from parsed data-id elements.
    Handles both ApexParser dict format {text,cls} and pipe parser plain strings."""
    global _apex_session

    def _val(v):
        return (v.get("text", "") or v.get("cls", "")) if isinstance(v, dict) else str(v)

    updated = {}
    for key, field in [
        ("title1", "name"), ("title2", "name"), ("name", "name"),
        ("light", "light"),
        ("dyn1", "dyn1"), ("dyn2", "dyn2"),
        ("track", "track"),
    ]:
        if key in meta and field not in updated:
            v = _val(meta[key])
            if v:
                updated[field] = v

    if updated:
        with _lock:
            _apex_session.update(updated)
        log("APEX SESSION", "  ".join(f"{k}={v}" for k, v in updated.items() if v))

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
def _enrich(t: dict) -> dict:
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
    return True

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
    return True

_ws_url_cache: Optional[str] = None   # "" means checked and not found
_ws_url_checked_at: float = 0.0

def _find_ws_url(page_url: str) -> Optional[str]:
    """Fetch the Apex Timing event page + config.js and extract WebSocket URL.
    Result is cached for 5 minutes."""
    global _ws_url_cache, _ws_url_checked_at
    now_t = time.time()
    if _ws_url_checked_at and now_t - _ws_url_checked_at < 300:
        return _ws_url_cache or None
    _ws_url_checked_at = now_t

    if not page_url:
        _ws_url_cache = ""
        return None

    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120"}

    def _fetch(url):
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read().decode("utf-8", errors="ignore")

    try:
        base = page_url.split('#')[0].rstrip('/')
        src = _fetch(base)

        # 1. Apex Timing pattern: event-specific config.js holds configPort
        for script_src in re.findall(r'''<script[^>]+src=['"]([^'"]+)['"]''', src):
            if "config.js" in script_src:
                config_url = urllib.parse.urljoin(base + "/", script_src)
                try:
                    cfg_src = _fetch(config_url)
                    m = re.search(r'configPort\s*=\s*(\d+)', cfg_src)
                    if m:
                        ws_url = f"wss://www.apex-timing.com:{int(m.group(1)) + 3}/"
                        _ws_url_cache = ws_url
                        print(f"[apex] WS URL from configPort: {ws_url}", flush=True)
                        return ws_url
                except Exception:
                    pass

        # 2. Fallback: explicit WebSocket URL in page JS
        m = re.search(r'''new\s+WebSocket\s*\(\s*['"]([^'"]+)['"]''', src)
        if m:
            _ws_url_cache = m.group(1)
            print(f"[apex] WS URL explicit: {_ws_url_cache}", flush=True)
            return _ws_url_cache

        # 3. Fallback: generic port variable
        m = re.search(r'''(?:configPort|wsPort|ws_port)\s*=\s*(\d{3,5})''', src, re.I)
        if m:
            _ws_url_cache = f"wss://www.apex-timing.com:{int(m.group(1)) + 3}/"
            print(f"[apex] WS URL from inline port: {_ws_url_cache}", flush=True)
            return _ws_url_cache

        print("[apex] No WS URL found — using HTTP fallback", flush=True)
    except Exception as e:
        print(f"[apex] Discovery error: {e}", flush=True)
    _ws_url_cache = ""
    return None

def _fetch_http(page_url: str) -> list:
    """Try AJAX endpoint to get timing data (HTTP fallback when WS not available)."""
    if not page_url:
        return []
    m = re.search(r'apex-timing\.com/([^/#"\'? ]+)', page_url)
    event = m.group(1) if m else ""
    for endpoint, body in [
        ("https://live.apex-timing.com/commonv2/functions/live_ajax.php",
         urllib.parse.urlencode({"action": "getGrid", "event": event}).encode()),
        (f"https://live.apex-timing.com/{event}/grid.json", None),
    ]:
        try:
            headers = {"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest",
                       "Referer": page_url}
            if body:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            req = urllib.request.Request(endpoint, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=4) as r:
                text = r.read().decode("utf-8", errors="ignore")
            text = text.strip()
            if not text:
                continue
            if text[0] in ('{', '['):
                d = json.loads(text)
                rows = d if isinstance(d, list) else d.get('rows', d.get('data', d.get('grid', [])))
                if rows:
                    return rows
            p = ApexParser()
            p.feed(text)
            if p.rows:
                return p.rows
        except Exception:
            continue
    return []

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

    return rows, cell_updates, meta

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
def worker():
    global _apex_ok, _apex_session
    while True:
        url = CFG.get("apex_url", "")

        if not url:
            time.sleep(5)
            continue

        if _HAS_WS:
            ws_url = _find_ws_url(url)
            if ws_url:
                done = threading.Event()
                t = threading.Thread(target=_ws_run, args=(ws_url, done), daemon=True)
                t.start()
                done.wait(timeout=600)  # reconnect after 10 min max or on disconnect
                t.join(timeout=5)
                time.sleep(3)  # brief pause before reconnect to avoid tight loop
                continue

        # HTTP/AJAX polling fallback (no WebSocket library or no WS URL found)
        rows = _fetch_http(url)
        if _process_rows(rows):
            log("HTTP DATA", f"{len(rows)} teams")
        else:
            with _lock:
                _apex_ok = False
            log("HTTP POLL", "no data received")
        time.sleep(CFG.get("refresh_interval", 5))

# ── Strategy engine ────────────────────────────────────────────────────────────
def compute_strategy(stint_s: float, race_elapsed_s: float, pits_done: int,
                     my_avg5: Optional[float], prev_avg5: Optional[float]) -> dict:
    R = CFG["race"]
    total_s   = R["duration_minutes"] * 60
    max_s     = R["stint_max_minutes"] * 60
    no_pit_s  = R["no_pit_last_minutes"] * 60
    remaining = total_s - race_elapsed_s

    if remaining <= no_pit_s:
        return {"label": "HOLD", "cls": "hold", "detail": "No pits allowed in final 5 min"}

    if stint_s >= max_s - 60:
        return {"label": "BOX NOW", "cls": "box", "detail": "STINT LIMIT — BOX IMMEDIATELY"}

    if stint_s >= max_s - 4 * 60:
        return {"label": "PREPARE", "cls": "prepare", "detail": f"Box in 1-3 laps · stint {fmt_duration(stint_s)}"}

    if stint_s >= max_s - 10 * 60:
        return {"label": "PREPARE", "cls": "prepare", "detail": "Approaching limit — stay alert"}

    # Pace drop
    if my_avg5 and prev_avg5 and prev_avg5 > 0:
        drop = my_avg5 - prev_avg5
        if drop > R.get("pace_drop_box", 0.50):
            return {"label": "BOX NOW", "cls": "box", "detail": f"Pace collapsed +{drop:.2f}s — box now"}
        if drop > R.get("pace_drop_warn", 0.30):
            return {"label": "PREPARE", "cls": "prepare", "detail": f"Pace dropping +{drop:.2f}s — prepare"}

    # Behind pit plan
    if total_s > 0:
        expected = (race_elapsed_s / total_s) * R["mandatory_pits"]
        if pits_done < expected - 1.5:
            return {"label": "PREPARE", "cls": "prepare",
                    "detail": f"Behind pit plan ({pits_done}/{R['mandatory_pits']})"}

    return {"label": "HOLD", "cls": "hold", "detail": "On plan — hold position"}

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

    # Race elapsed
    race_start = kv_get("race_start")
    race_elapsed = 0.0
    if race_start and status in ("racing", "pitting"):
        race_elapsed = (now - datetime.fromisoformat(race_start)).total_seconds()
    race_remaining = max(0.0, CFG["race"]["duration_minutes"] * 60 - race_elapsed)

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
        pit_remaining = max(0.0, CFG["race"]["pit_duration_seconds"] - pit_elapsed)
        pit_min_met   = pit_elapsed >= CFG["race"]["pit_duration_seconds"]

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
            d["active"]    = str(row["id"]) == str(driver_id)
            if d["active"]:
                current_driver = d
            drivers.append(d)

        pits_done = con.execute("SELECT COUNT(*) FROM stints").fetchone()[0]

        for i, row in enumerate(con.execute("""
            SELECT s.id, s.driver_id, s.start_ts, s.end_ts, s.duration_seconds,
                   d.name as driver_name
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
            })

    # My team
    my_name = CFG.get("team_name", "")
    my_team = next((t for t in teams_raw if t.get("team", "") == my_name), None)
    my_avg5 = my_team["avg5_s"] if my_team else None

    strat = compute_strategy(stint_s, race_elapsed, pits_done, my_avg5, _prev_avg5)
    _prev_avg5 = my_avg5

    # Track-wide average of all avg5 values
    all_avgs  = [t["avg5_s"] for t in teams_raw if t.get("avg5_s")]
    track_avg = fmt_laptime(sum(all_avgs) / len(all_avgs)) if all_avgs else "-"

    # Serialize teams (drop raw floats the frontend doesn't need)
    teams_out = []
    for t in teams_raw:
        teams_out.append({
            "pos":        t.get("pos", ""),
            "kart":       t.get("kart", ""),
            "team":       t.get("team", ""),
            "last_lap":   t.get("last_lap", "-"),
            "avg5":       t.get("avg5", "-"),
            "avg10":      t.get("avg10", "-"),
            "best_lap":   t.get("best_lap", "-"),
            "total_laps": t.get("total_laps", "-"),
            "pits":       t.get("pits", "-"),
            "gap":        t.get("gap", "-"),
            "is_my_team": t.get("team", "") == my_name,
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
        "pit_min_met":      pit_min_met,
        "strategy":         strat,
        "current_driver":   current_driver,
        "drivers":          drivers,
        "pits_done":        pits_done,
        "mandatory_pits":   CFG["race"]["mandatory_pits"],
        "stint_max_minutes": CFG["race"]["stint_max_minutes"],
        "track_avg":        track_avg,
        "pit_history":      pit_history,
        "team_name":        my_name,
        "apex_url":         CFG.get("apex_url", ""),
        "teams":            teams_out,
        "session_mode":     session_mode,
        "pit_plan":         pit_plan_out,
        "my_team":          {
            "pos":   my_team.get("pos", "?"),
            "kart":  my_team.get("kart", "?"),
            "avg5":  my_team.get("avg5", "-"),
            "laps":  my_team.get("total_laps", "-"),
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
                  ("pit_start",""),("driver_id","")]:
        kv_set(k, v)
    with get_db() as con:
        con.execute("DELETE FROM stints")
        con.execute("UPDATE drivers SET total_seconds=0")
    log("RACE RESET")
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
    data        = request.json or {}
    offset_s    = float(data.get("offset_seconds", 0))
    now         = datetime.utcnow()
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
    log("BOX NOW", f"driver={driver_name}  stint={fmt_duration(dur if did and stint_start else 0)}{offset_note}")
    broadcast()
    return jsonify(ok=True)

@app.post("/api/pit/done")
def pit_done():
    """New driver seated — start fresh stint."""
    new_did = str(request.json.get("driver_id") or kv_get("driver_id"))
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
    log("PIT DONE", f"driver={new_name}  pit_time={fmt_mmss(pit_elapsed)}")
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

# ── Settings ───────────────────────────────────────────────────────────────────
@app.post("/api/settings")
def api_settings():
    data = request.json or {}
    if "apex_url" in data:
        CFG["apex_url"] = data["apex_url"].strip()
        global _ws_url_cache, _ws_url_checked_at
        _ws_url_cache, _ws_url_checked_at = None, 0.0  # force re-scan on next cycle
    if "team_name" in data:
        CFG["team_name"] = data["team_name"].strip()
    if "duration_minutes" in data:
        CFG["race"]["duration_minutes"] = int(data["duration_minutes"])
    if "mandatory_pits" in data:
        CFG["race"]["mandatory_pits"] = int(data["mandatory_pits"])
    if "stint_max_minutes" in data:
        CFG["race"]["stint_max_minutes"] = int(data["stint_max_minutes"])
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
threading.Thread(target=worker, daemon=True).start()

# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"\n  APX GP WAR ROOM  ->  http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
