from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, urlparse

from apx.apex_client import ApexClient
from apx.config import load_config, save_config
from apx.manual_client import ManualClient
from apx.models import utc_now_iso
from apx.state_store import StateStore
from apx.strategy_engine import StrategyEngine
from apx.timeutils import parse_lap_time

ROOT = Path(__file__).resolve().parent


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


class WarRoomRuntime:
    def __init__(self, config_path: str = "config.json"):
        os.chdir(ROOT)
        self.config_path = Path(config_path)
        self.config = load_config(self.config_path)
        self.store = StateStore(self.config)
        self.engine = StrategyEngine(self.config, self.store)
        self.manual_client = ManualClient(self.config)
        self.apex_client = ApexClient(self.config)
        self.stop_event = threading.Event()
        self.source_status = "BOOT"

    def start(self) -> None:
        self._bootstrap_manual_times()
        t = threading.Thread(target=self.worker_loop, daemon=True)
        t.start()

    def _bootstrap_manual_times(self) -> None:
        manual = self.store.manual_override
        changed = False
        if not manual.get("race_started_at"):
            manual["race_started_at"] = utc_now_iso()
            changed = True
        if not manual.get("stint_started_at"):
            manual["stint_started_at"] = utc_now_iso()
            changed = True
        if changed:
            self.store.set_manual_override(**manual)

    def worker_loop(self) -> None:
        interval = max(2, int(self.config.get("refresh_interval_seconds", 5)))
        while not self.stop_event.is_set():
            try:
                rows = self.fetch_rows()
                if rows:
                    self.store.update_timing_rows(rows, source=self.config.get("mode", "MANUAL"))
                    self.source_status = "OK"
                else:
                    self.source_status = "NO_ROWS"
                self.engine.build_state(source_status=self.source_status)
            except Exception as exc:
                self.source_status = f"ERROR: {exc.__class__.__name__}"
                self.store.add_event("SOURCE_ERROR", str(exc), {"traceback": traceback.format_exc()[-2000:]})
                self.engine.build_state(source_status=self.source_status)
                log(f"Source error: {exc}")
            time.sleep(interval)

    def fetch_rows(self):
        mode = str(self.config.get("mode", "MANUAL")).upper()
        if mode == "LIVE_SCRAPE":
            rows = self.apex_client.fetch()
            if rows:
                return rows
            # Safety fallback: keep race control alive.
            self.store.add_event("LIVE_FALLBACK", "Live scrape returned no rows. Using manual CSV fallback.")
            return self.manual_client.fetch()
        return self.manual_client.fetch()

    def state(self) -> Dict[str, Any]:
        return self.store.get_state()

    def update_manual(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # Keys allowed from UI.
        allowed = {"current_driver", "current_kart", "box_status", "stint_started_at", "race_started_at"}
        updates = {k: payload.get(k) for k in allowed if k in payload}
        if payload.get("reset_stint"):
            updates["stint_started_at"] = utc_now_iso()
        if payload.get("reset_race"):
            updates["race_started_at"] = utc_now_iso()
            updates["stint_started_at"] = utc_now_iso()
        self.store.set_manual_override(**updates)
        self.engine.build_state(source_status=self.source_status)
        return {"ok": True, "manual": self.store.manual_override}

    def add_lap(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        team = str(payload.get("team") or self.config.get("team_name", "APX GP")).strip()
        lap_time = parse_lap_time(payload.get("lap_time"))
        if lap_time is None:
            return {"ok": False, "error": "Invalid lap_time"}
        lap = None
        try:
            if payload.get("lap") not in (None, ""):
                lap = int(payload.get("lap"))
        except Exception:
            lap = None
        self.store.add_manual_lap(team, lap_time, lap=lap)
        self.engine.build_state(source_status="MANUAL_LAP")
        return {"ok": True, "team": team, "lap_time": lap_time}


runtime = WarRoomRuntime()


class Handler(SimpleHTTPRequestHandler):
    server_version = "APXWarRoom/1.0"

    def translate_path(self, path: str) -> str:
        # Serve static files from ROOT.
        if path == "/":
            return str(ROOT / "templates" / "dashboard.html")
        if path == "/strategist":
            return str(ROOT / "templates" / "strategist.html")
        return str(ROOT / path.lstrip("/"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            self._json(runtime.state())
            return
        if parsed.path == "/api/health":
            self._json({"ok": True, "time": utc_now_iso(), "mode": runtime.config.get("mode"), "source_status": runtime.source_status})
            return
        if parsed.path == "/api/config":
            cfg = dict(runtime.config)
            self._json(cfg)
            return
        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {k: v[0] if v else "" for k, v in parse_qs(raw).items()}
        if parsed.path == "/api/manual":
            self._json(runtime.update_manual(payload))
            return
        if parsed.path == "/api/lap":
            self._json(runtime.add_lap(payload))
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")

    def _json(self, data: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        # quieter HTTP log
        log(fmt % args)


def main() -> None:
    runtime.start()
    host = runtime.config.get("host", "0.0.0.0")
    port = int(runtime.config.get("port", 8080))
    httpd = ThreadingHTTPServer((host, port), Handler)
    local_url = f"http://127.0.0.1:{port}"
    log("APX GP War Room started")
    log(f"Command dashboard: {local_url}/")
    log(f"Strategist dashboard: {local_url}/strategist")
    log("On other PCs: open http://<SERVER-LAPTOP-IP>:%s" % port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop_event.set()
        log("Stopping APX GP War Room")


if __name__ == "__main__":
    main()
