from __future__ import annotations

import csv
import json
import threading
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional

from .models import RaceEvent, TeamTiming, utc_now_iso


class StateStore:
    """Thread-safe memory + file store.

    This is deliberately simple: one server laptop, one process, local files.
    It is safer on a race day than a heavy database dependency.
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        files = config.get("files", {})
        self.state_path = Path(files.get("state_json", "data/state.json"))
        self.laps_path = Path(files.get("laps_csv", "data/laps.csv"))
        self.events_path = Path(files.get("events_csv", "data/events.csv"))
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.laps_path.parent.mkdir(parents=True, exist_ok=True)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)

        self.lock = threading.RLock()
        self.latest_rows: List[TeamTiming] = []
        self.lap_history: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=60))
        self.last_lap_count: Dict[str, Optional[int]] = defaultdict(lambda: None)
        self.last_pits: Dict[str, int] = defaultdict(int)
        self.last_avg5: Dict[str, Optional[float]] = defaultdict(lambda: None)
        self.events: Deque[RaceEvent] = deque(maxlen=100)
        self.last_strategy_state: Dict[str, Any] = {}
        self.manual_override: Dict[str, Any] = dict(config.get("manual", {}))
        self._ensure_csv_headers()

    def _ensure_csv_headers(self) -> None:
        if not self.laps_path.exists():
            with self.laps_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["timestamp", "team", "lap", "lap_time", "source"])
                writer.writeheader()
        if not self.events_path.exists():
            with self.events_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["timestamp", "type", "message", "payload_json"])
                writer.writeheader()

    def update_timing_rows(self, rows: Iterable[TeamTiming], source: str) -> None:
        timestamp = utc_now_iso()
        rows = list(rows)
        with self.lock:
            self.latest_rows = rows
            for row in rows:
                if row.last_lap is not None:
                    prev_laps = self.last_lap_count[row.name]
                    if row.laps is None or row.laps != prev_laps:
                        self.lap_history[row.name].append(float(row.last_lap))
                        self._append_lap(timestamp, row.name, row.laps, float(row.last_lap), source)
                    self.last_lap_count[row.name] = row.laps
                if row.pits is not None:
                    old_pits = self.last_pits[row.name]
                    if row.pits > old_pits:
                        self.add_event("PIT_DETECTED", f"{row.name}: pit count {old_pits} -> {row.pits}", {"team": row.name, "pits": row.pits})
                    self.last_pits[row.name] = row.pits

    def _append_lap(self, timestamp: str, team: str, lap: Optional[int], lap_time: float, source: str) -> None:
        with self.laps_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["timestamp", "team", "lap", "lap_time", "source"])
            writer.writerow({"timestamp": timestamp, "team": team, "lap": lap or "", "lap_time": lap_time, "source": source})

    def add_manual_lap(self, team: str, lap_time: float, lap: Optional[int] = None) -> None:
        with self.lock:
            self.lap_history[team].append(float(lap_time))
            self._append_lap(utc_now_iso(), team, lap, float(lap_time), "MANUAL_API")

    def add_event(self, event_type: str, message: str, payload: Optional[Dict[str, Any]] = None) -> None:
        ev = RaceEvent(timestamp=utc_now_iso(), event_type=event_type, message=message, payload=payload or {})
        with self.lock:
            self.events.appendleft(ev)
            with self.events_path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["timestamp", "type", "message", "payload_json"])
                writer.writerow({
                    "timestamp": ev.timestamp,
                    "type": ev.event_type,
                    "message": ev.message,
                    "payload_json": json.dumps(ev.payload, ensure_ascii=False),
                })

    def set_manual_override(self, **kwargs: Any) -> None:
        with self.lock:
            for k, v in kwargs.items():
                if v is not None:
                    self.manual_override[k] = v
            self.add_event("MANUAL_OVERRIDE", "Manual race control update", kwargs)

    def get_rows(self) -> List[TeamTiming]:
        with self.lock:
            return list(self.latest_rows)

    def get_laps(self, team: str, limit: int = 60) -> List[float]:
        with self.lock:
            return list(self.lap_history[team])[-limit:]

    def set_strategy_state(self, state: Dict[str, Any]) -> None:
        with self.lock:
            self.last_strategy_state = state
            self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_state(self) -> Dict[str, Any]:
        with self.lock:
            state = dict(self.last_strategy_state)
            state["events"] = [e.to_dict() for e in list(self.events)[:20]]
            return state
