from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List

from .models import TeamTiming
from .timeutils import parse_lap_time


class ManualClient:
    """Fallback input: a CSV file edited by the strategist.

    CSV columns:
    position,team,last_lap,best_lap,laps,pits,gap,interval,delta,on_track
    """

    def __init__(self, config: Dict[str, Any]):
        self.path = Path(config.get("files", {}).get("manual_csv", "data/manual_input.csv"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._create_sample()

    def _create_sample(self) -> None:
        rows = [
            {"position": 1, "team": "RIVAL 1", "last_lap": "46.920", "best_lap": "46.600", "laps": 22, "pits": 0, "gap": "", "interval": "", "delta": "", "on_track": "12:30"},
            {"position": 2, "team": "APX GP", "last_lap": "46.640", "best_lap": "46.500", "laps": 22, "pits": 0, "gap": "+3.2", "interval": "+3.2", "delta": "-0.280", "on_track": "12:28"},
            {"position": 3, "team": "RIVAL 2", "last_lap": "47.120", "best_lap": "46.920", "laps": 22, "pits": 0, "gap": "+7.8", "interval": "+4.6", "delta": "+0.480", "on_track": "12:25"},
            {"position": 4, "team": "RIVAL 3", "last_lap": "47.030", "best_lap": "46.900", "laps": 21, "pits": 0, "gap": "+1L", "interval": "+1L", "delta": "", "on_track": "12:20"},
        ]
        with self.path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["position", "team", "last_lap", "best_lap", "laps", "pits", "gap", "interval", "delta", "on_track"])
            writer.writeheader()
            writer.writerows(rows)

    def fetch(self) -> List[TeamTiming]:
        if not self.path.exists():
            self._create_sample()
        rows: List[TeamTiming] = []
        with self.path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for raw in reader:
                name = (raw.get("team") or raw.get("name") or "").strip()
                if not name:
                    continue
                rows.append(TeamTiming(
                    position=_int_or_none(raw.get("position")),
                    name=name,
                    last_lap=parse_lap_time(raw.get("last_lap")),
                    best_lap=parse_lap_time(raw.get("best_lap")),
                    laps=_int_or_none(raw.get("laps")),
                    pits=_int_or_none(raw.get("pits")),
                    gap=raw.get("gap", ""),
                    interval=raw.get("interval", ""),
                    delta=raw.get("delta", ""),
                    on_track=raw.get("on_track", ""),
                    raw=dict(raw),
                ))
        return rows


def _int_or_none(value: object):
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(float(str(value).replace(",", ".")))
    except Exception:
        return None
