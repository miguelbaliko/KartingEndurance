#!/usr/bin/env python3
"""APX GP War Room — Karting Endurance Strategy Tool"""

from flask import Flask, render_template, jsonify, request, Response
import threading, time, json, sqlite3, urllib.request, urllib.error
import html.parser, re, os, queue
from datetime import datetime, timedelta
from typing import Optional

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
}

class ApexParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self._cur, self._col = [], None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._cur = {}
        elif tag in ("td", "th") and self._cur is not None:
            self._col = None
            for cls in a.get("class", "").split():
                if cls in _CELL_MAP:
                    self._col = _CELL_MAP[cls]
                    break

    def handle_data(self, data):
        if self._col and self._cur is not None:
            v = data.strip()
            if v:
                self._cur.setdefault(self._col, v)
            self._col = None

    def handle_endtag(self, tag):
        if tag == "tr" and self._cur and len(self._cur) >= 2:
            self.rows.append(self._cur)
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

# ── Apex fetch ─────────────────────────────────────────────────────────────────
def fetch_apex() -> list:
    url = CFG.get("apex_url", "")
    if not url:
        return []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            html_text = r.read().decode("utf-8", errors="ignore")

        p = ApexParser()
        p.feed(html_text)
        if p.rows:
            return p.rows

        # Fallback: embedded JSON grid variable
        m = re.search(
            r'(?:grid|rows|data|results)\s*=\s*(\[.*?\]);',
            html_text, re.DOTALL | re.IGNORECASE
        )
        if m:
            return json.loads(m.group(1))
    except Exception:
        pass
    return []

# ── Shared live state ──────────────────────────────────────────────────────────
_lock = threading.Lock()
_teams: list = []
_lap_hist: dict = {}   # team_key -> [float, ...]
_apex_ok = False
_sse_queues: list = []

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

# ── Background worker ──────────────────────────────────────────────────────────
def worker():
    global _teams, _apex_ok
    while True:
        raw = fetch_apex()
        with _lock:
            if raw:
                _apex_ok = True
                built = []
                for r in raw:
                    t = dict(r)
                    key = t.get("kart") or t.get("team") or str(t.get("pos", ""))
                    ll = parse_laptime(t.get("last_lap", ""))
                    hist = _lap_hist.setdefault(key, [])
                    if ll and (not hist or hist[-1] != ll):
                        hist.append(ll)
                        del hist[:-30]
                    t["last_lap_s"] = ll
                    t["best_lap_s"] = parse_laptime(t.get("best_lap", ""))
                    t["avg5_s"]  = sum(hist[-5:])  / len(hist[-5:])  if hist else None
                    t["avg10_s"] = sum(hist[-10:]) / len(hist[-10:]) if hist else None
                    t["avg5"]    = fmt_laptime(t["avg5_s"])
                    t["avg10"]   = fmt_laptime(t["avg10_s"])
                    built.append(t)
                _teams = built
            else:
                _apex_ok = False
        broadcast()
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
        teams_raw = list(_teams)
        apex_ok   = _apex_ok

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

    return {
        "ts":               now.isoformat() + "Z",  # explicit UTC so JS Date() parses correctly
        "status":           status,
        "apex_ok":          apex_ok,
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
            yield "data: " + json.dumps(make_snapshot()) + "\n\n"
            while True:
                try:
                    yield q.get(timeout=25)
                except queue.Empty:
                    yield ": ping\n\n"
        except GeneratorExit:
            try:
                _sse_queues.remove(q)
            except ValueError:
                pass

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ── Race control ───────────────────────────────────────────────────────────────
@app.post("/api/race/start")
def race_start():
    now = datetime.utcnow().isoformat()
    kv_set("race_start", now)
    kv_set("stint_start", now)   # stint clock always starts with the race
    kv_set("status", "racing")
    broadcast()
    return jsonify(ok=True)

@app.post("/api/race/stop")
def race_stop():
    kv_set("status", "idle")
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
    broadcast()
    return jsonify(ok=True)

# ── Driver management ──────────────────────────────────────────────────────────
@app.post("/api/driver/set")
def driver_set():
    did = str(request.json.get("driver_id", ""))
    kv_set("driver_id", did)
    # No timer logic here — set before or during the race freely without side effects
    broadcast()
    return jsonify(ok=True)

@app.post("/api/driver/add")
def driver_add():
    name = (request.json.get("name") or "").strip()
    if not name:
        return jsonify(ok=False, error="Name required"), 400
    with get_db() as con:
        con.execute("INSERT INTO drivers(name) VALUES(?)", (name,))
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
    broadcast()
    return jsonify(ok=True)

@app.post("/api/pit/done")
def pit_done():
    """New driver seated — start fresh stint."""
    new_did = str(request.json.get("driver_id") or kv_get("driver_id"))
    kv_set("driver_id",   new_did)
    kv_set("stint_start", datetime.utcnow().isoformat())
    kv_set("pit_start",   "")
    kv_set("status",      "racing")
    broadcast()
    return jsonify(ok=True)

# ── Settings ───────────────────────────────────────────────────────────────────
@app.post("/api/settings")
def api_settings():
    data = request.json or {}
    if "apex_url" in data:
        CFG["apex_url"] = data["apex_url"].strip()
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

# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    threading.Thread(target=worker, daemon=True).start()
    print("\n  APX GP WAR ROOM  ->  http://localhost:8080\n")
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)
