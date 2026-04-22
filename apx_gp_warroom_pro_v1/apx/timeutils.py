from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def seconds_since(value: Optional[str], fallback: int = 0) -> int:
    dt = parse_iso(value)
    if not dt:
        return fallback
    return max(0, int((now_utc() - dt).total_seconds()))


def seconds_to_clock(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def parse_lap_time(value: object) -> Optional[float]:
    """Parse common timing formats into seconds.

    Accepts: 46.512, "46.512", "1:02.345", "+3.2" (returns 3.2), blanks -> None.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    if not text or text in {"-", "--", "N/A"}:
        return None
    text = text.replace("+", "")
    try:
        if ":" in text:
            parts = text.split(":")
            total = 0.0
            for p in parts:
                total = total * 60 + float(p)
            return total
        return float(text)
    except ValueError:
        return None


def fmt_seconds(value: Optional[float]) -> str:
    if value is None:
        return "--"
    return f"{value:.3f}"
