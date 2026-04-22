from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TeamTiming:
    position: Optional[int]
    name: str
    last_lap: Optional[float] = None
    best_lap: Optional[float] = None
    laps: Optional[int] = None
    pits: Optional[int] = None
    gap: str = ""
    interval: str = ""
    delta: str = ""
    on_track: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RaceEvent:
    timestamp: str
    event_type: str
    message: str
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StrategyDecision:
    status: str = "HOLD"  # HOLD / PREPARE / BOX / ATTACK / CRITICAL
    headline: str = "HOLD"
    reason: str = "No critical trigger."
    confidence: str = "MEDIUM"
    box_window: str = "WAIT"
    undercut: str = "NO"
    risk: str = "LOW"
    recommended_driver: str = ""
    commands: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TeamMetrics:
    name: str = ""
    position: Optional[int] = None
    current_driver: str = ""
    current_kart: str = ""
    last_lap: Optional[float] = None
    best_lap: Optional[float] = None
    avg3: Optional[float] = None
    avg5: Optional[float] = None
    avg10: Optional[float] = None
    previous_avg5: Optional[float] = None
    pace_drop: Optional[float] = None
    trend: str = "→"
    trend_label: str = "STABLE"
    kart_rating: str = "MEDIUM"
    gap_front: str = ""
    gap_back: str = ""
    pits: int = 0
    expected_pits: float = 0.0
    pit_plan_status: str = "ON PLAN"
    stint_seconds: int = 0
    stint_remaining_seconds: int = 2700
    race_elapsed_seconds: int = 0
    race_remaining_seconds: int = 46800
    phase: str = "EARLY"
    source_status: str = "BOOT"
    last_update: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
