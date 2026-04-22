from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Tuple

from .models import StrategyDecision, TeamMetrics, TeamTiming, utc_now_iso
from .timeutils import seconds_since


def avg(values: List[float], n: int) -> Optional[float]:
    if not values:
        return None
    sample = values[-n:]
    if not sample:
        return None
    return float(statistics.mean(sample))


def find_team(rows: List[TeamTiming], team_name: str) -> Optional[TeamTiming]:
    target = team_name.strip().lower()
    for row in rows:
        if row.name.strip().lower() == target:
            return row
    for row in rows:
        if target in row.name.strip().lower():
            return row
    return None


def sort_rows(rows: List[TeamTiming]) -> List[TeamTiming]:
    return sorted(rows, key=lambda r: (r.position is None, r.position or 9999))


class StrategyEngine:
    def __init__(self, config: Dict[str, Any], store):
        self.config = config
        self.store = store
        self.team_name = config.get("team_name", "APX GP")
        self.race_cfg = config.get("race", {})
        self.thresholds = config.get("thresholds", {})

    def build_state(self, source_status: str = "OK") -> Dict[str, Any]:
        rows = sort_rows(self.store.get_rows())
        target = find_team(rows, self.team_name)
        if target is None and rows:
            target = rows[0]
        metrics = self._build_metrics(target, rows, source_status)
        decision = self._decide(metrics, rows)
        state = {
            "team_name": self.team_name,
            "generated_at": utc_now_iso(),
            "metrics": metrics.to_dict(),
            "decision": decision.to_dict(),
            "top_table": [self._row_with_metrics(r).to_dict() for r in rows[:8]],
            "laps": self.store.get_laps(metrics.name or self.team_name, limit=40),
            "manual": dict(self.store.manual_override),
            "cameras": self.config.get("cameras", []),
            "config_summary": {
                "duration_minutes": self.race_cfg.get("duration_minutes", 780),
                "mandatory_pit_stops": self.race_cfg.get("mandatory_pit_stops", 23),
                "stint_max_minutes": self.race_cfg.get("stint_max_minutes", 45),
                "pit_stop_min_seconds": self.race_cfg.get("pit_stop_min_seconds", 180),
                "no_pit_last_minutes": self.race_cfg.get("no_pit_last_minutes", 5),
                "mode": self.config.get("mode", "MANUAL"),
            },
        }
        self.store.set_strategy_state(state)
        return state

    def _row_with_metrics(self, row: TeamTiming) -> TeamMetrics:
        laps = self.store.get_laps(row.name, limit=60)
        avg5 = avg(laps, 5)
        previous_avg5 = self.store.last_avg5.get(row.name)
        trend, trend_label = self._trend(avg5, previous_avg5)
        if avg5 is not None:
            self.store.last_avg5[row.name] = avg5
        return TeamMetrics(
            name=row.name,
            position=row.position,
            last_lap=row.last_lap,
            best_lap=row.best_lap,
            avg3=avg(laps, 3),
            avg5=avg5,
            avg10=avg(laps, 10),
            previous_avg5=previous_avg5,
            pace_drop=(avg5 - previous_avg5) if (avg5 is not None and previous_avg5 is not None) else None,
            trend=trend,
            trend_label=trend_label,
            gap_front=row.interval or row.gap,
            pits=row.pits or 0,
            source_status="OK",
            last_update=utc_now_iso(),
        )

    def _build_metrics(self, target: Optional[TeamTiming], rows: List[TeamTiming], source_status: str) -> TeamMetrics:
        manual = self.store.manual_override
        team = target or TeamTiming(None, self.team_name)
        laps = self.store.get_laps(team.name, limit=60)
        avg3 = avg(laps, 3)
        avg5 = avg(laps, 5)
        avg10 = avg(laps, 10)
        previous_avg5 = self.store.last_avg5.get(team.name)
        pace_drop = (avg5 - previous_avg5) if (avg5 is not None and previous_avg5 is not None) else None
        trend, trend_label = self._trend(avg5, previous_avg5)
        if avg5 is not None:
            self.store.last_avg5[team.name] = avg5

        race_elapsed = self._race_elapsed_seconds()
        race_duration_seconds = int(self.race_cfg.get("duration_minutes", 780) * 60)
        race_remaining = max(0, race_duration_seconds - race_elapsed)
        stint_seconds = seconds_since(manual.get("stint_started_at"), fallback=0)
        stint_max_seconds = int(self.race_cfg.get("stint_max_minutes", 45) * 60)
        stint_remaining = max(0, stint_max_seconds - stint_seconds)
        mandatory_pits = int(self.race_cfg.get("mandatory_pit_stops", 23))
        expected_pits = (race_elapsed / race_duration_seconds) * mandatory_pits if race_duration_seconds else 0.0
        pits_done = int(team.pits or 0)
        pit_plan_status = self._pit_plan(pits_done, expected_pits)
        phase = self._phase(race_elapsed, race_duration_seconds)
        kart_rating = self._kart_rating(avg5, pace_drop, manual.get("current_kart", ""))
        gap_front, gap_back = self._gaps(team, rows)

        return TeamMetrics(
            name=team.name,
            position=team.position,
            current_driver=manual.get("current_driver", ""),
            current_kart=manual.get("current_kart", ""),
            last_lap=team.last_lap,
            best_lap=team.best_lap,
            avg3=avg3,
            avg5=avg5,
            avg10=avg10,
            previous_avg5=previous_avg5,
            pace_drop=pace_drop,
            trend=trend,
            trend_label=trend_label,
            kart_rating=kart_rating,
            gap_front=gap_front,
            gap_back=gap_back,
            pits=pits_done,
            expected_pits=round(expected_pits, 2),
            pit_plan_status=pit_plan_status,
            stint_seconds=stint_seconds,
            stint_remaining_seconds=stint_remaining,
            race_elapsed_seconds=race_elapsed,
            race_remaining_seconds=race_remaining,
            phase=phase,
            source_status=source_status,
            last_update=utc_now_iso(),
        )

    def _race_elapsed_seconds(self) -> int:
        manual = self.store.manual_override
        start = manual.get("race_started_at") or self.race_cfg.get("start_time_iso")
        if start:
            return seconds_since(start, fallback=0)
        # If no race start is set, use 0. Dashboard still works for testing.
        return 0

    def _trend(self, avg5: Optional[float], previous_avg5: Optional[float]) -> Tuple[str, str]:
        if avg5 is None or previous_avg5 is None:
            return "→", "NO DATA"
        threshold = float(self.thresholds.get("trend_seconds", 0.08))
        diff = avg5 - previous_avg5
        if diff < -threshold:
            return "↑", "FASTER"
        if diff > threshold:
            return "↓", "SLOWER"
        return "→", "STABLE"

    def _pit_plan(self, pits_done: int, expected_pits: float) -> str:
        diff = pits_done - expected_pits
        if diff < -0.75:
            return "BEHIND"
        if diff > 1.25:
            return "AHEAD"
        return "ON PLAN"

    def _phase(self, elapsed: int, duration: int) -> str:
        if duration <= 0:
            return "EARLY"
        ratio = elapsed / duration
        if ratio < 3 / 13:
            return "EARLY"
        if ratio < 9 / 13:
            return "MID"
        return "FINAL"

    def _kart_rating(self, avg5: Optional[float], pace_drop: Optional[float], kart: str) -> str:
        if kart and kart.upper() in {"GREEN", "GOOD", "GUT"}:
            return "GOOD"
        if kart and kart.upper() in {"RED", "BAD", "SCHLECHT"}:
            return "BAD"
        if pace_drop is None:
            return "MEDIUM"
        if pace_drop > float(self.thresholds.get("pace_drop_prepare", 0.30)):
            return "BAD"
        if pace_drop < -0.15:
            return "GOOD"
        return "MEDIUM"

    def _gaps(self, team: TeamTiming, rows: List[TeamTiming]) -> Tuple[str, str]:
        if not team.position:
            return team.interval or team.gap, ""
        front = ""
        back = ""
        for row in rows:
            if row.position == team.position - 1:
                front = team.interval or team.gap or ""
            if row.position == team.position + 1:
                back = row.interval or row.gap or ""
        return front, back

    def _decide(self, metrics: TeamMetrics, rows: List[TeamTiming]) -> StrategyDecision:
        t = self.thresholds
        race_cfg = self.race_cfg
        st_min = metrics.stint_seconds / 60.0
        no_pit_last = int(race_cfg.get("no_pit_last_minutes", 5)) * 60
        status = "HOLD"
        headline = "HOLD"
        reason = "Pace and stint are under control."
        risk = "LOW"
        confidence = "MEDIUM"
        box_window = "WAIT"
        undercut = "NO"
        commands = ["Rhythmus halten.", "Keine unnötigen Risiken."]

        if metrics.race_remaining_seconds <= no_pit_last:
            return StrategyDecision(
                status="CRITICAL",
                headline="NO PIT WINDOW",
                reason="Last 5 minutes: pit stops are not allowed by regulation window.",
                confidence="HIGH",
                box_window="CLOSED",
                undercut="NO",
                risk="HIGH",
                recommended_driver=metrics.current_driver,
                commands=["Keine Box mehr.", "Position sichern.", "Kontakt vermeiden."],
            )

        # Stint limit has priority. It is cheaper to lose a strategic window than to take a stint penalty.
        if st_min >= float(t.get("stint_box_minutes", 44)):
            status, headline, reason, risk, confidence, box_window = "BOX", "BOX FORCED", "Stint is near 45-minute hard limit.", "HIGH", "HIGH", "NOW"
            commands = ["Box jetzt vorbereiten.", "Keine Diskussion.", "Fahrer auf sichere Boxeneinfahrt hinweisen."]
        elif st_min >= float(t.get("stint_alert_minutes", 42)):
            status, headline, reason, risk, confidence, box_window = "PREPARE", "PREPARE BOX", "Stint is inside alert zone.", "MEDIUM", "HIGH", "1–2 LAPS"
            commands = ["Nächster Fahrer bereit.", "Lastro prüfen.", "Boxkamera beobachten."]
        elif st_min >= float(t.get("stint_prepare_minutes", 40)):
            status, headline, reason, risk, confidence, box_window = "PREPARE", "WINDOW SOON", "Stint is approaching strategic box window.", "MEDIUM", "HIGH", "2–4 LAPS"
            commands = ["Box vorbereiten.", "Nicht über 44 Minuten riskieren."]

        # Pace drop can override HOLD/PREPARE but not forced stint logic.
        pace_drop = metrics.pace_drop
        if pace_drop is not None and status != "BOX":
            if pace_drop > float(t.get("pace_drop_box", 0.50)):
                status, headline, reason, risk, confidence, box_window = "BOX", "PACE COLLAPSE", f"AVG5 dropped by {pace_drop:.2f}s.", "HIGH", "MEDIUM", "NOW IF BOX CLEAN"
                commands = ["Box prüfen.", "Wenn Box nicht voll: rein.", "Wenn Box voll: maximal 1–2 Runden halten."]
            elif pace_drop > float(t.get("pace_drop_prepare", 0.30)) and status == "HOLD":
                status, headline, reason, risk, confidence, box_window = "PREPARE", "PACE DROP", f"AVG5 dropped by {pace_drop:.2f}s.", "MEDIUM", "MEDIUM", "2–3 LAPS"
                commands = ["Fahrer fragen: Kart gut/mittel/schlecht.", "Boxfenster vorbereiten."]

        if metrics.pit_plan_status == "BEHIND" and status == "HOLD":
            status, headline, reason, risk, confidence, box_window = "PREPARE", "BEHIND PIT PLAN", "Team is behind the 23-stop projection.", "MEDIUM", "HIGH", "NEXT CLEAN WINDOW"
            commands = ["Früheres Fenster suchen.", "Nicht ins letzte Rennende schieben."]

        # Attack when kart and driver are working and there is no stint/pace threat.
        if status == "HOLD" and metrics.kart_rating == "GOOD" and metrics.trend in {"↑", "→"}:
            if self._closing_front(metrics, rows):
                status, headline, reason, risk, confidence, box_window = "ATTACK", "ATTACK STINT", "Good kart and closing to front.", "LOW", "MEDIUM", "KEEP OUT"
                commands = ["+0.1 bis +0.2 push.", "Keine erzwungene Überholaktion.", "Kart ausnutzen."]

        undercut = self._undercut(metrics, status)
        recommended_driver = self._recommend_driver(metrics)
        return StrategyDecision(status=status, headline=headline, reason=reason, confidence=confidence, box_window=box_window, undercut=undercut, risk=risk, recommended_driver=recommended_driver, commands=commands)

    def _closing_front(self, metrics: TeamMetrics, rows: List[TeamTiming]) -> bool:
        if not metrics.position or metrics.position <= 1:
            return False
        team_avg = metrics.avg5
        if team_avg is None:
            return False
        for row in rows:
            if row.position == metrics.position - 1:
                rival_laps = self.store.get_laps(row.name, limit=10)
                rival_avg5 = avg(rival_laps, 5)
                if rival_avg5 is not None:
                    return (rival_avg5 - team_avg) >= float(self.thresholds.get("attack_gain_per_lap", 0.15))
        return False

    def _undercut(self, metrics: TeamMetrics, status: str) -> str:
        st_min = metrics.stint_seconds / 60.0
        if status in {"BOX", "CRITICAL"}:
            return "NO - FORCED/CRITICAL"
        if st_min < float(self.thresholds.get("min_stint_for_undercut_minutes", 24)):
            return "NO - TOO EARLY"
        if st_min > float(self.thresholds.get("max_stint_for_undercut_minutes", 40)):
            return "NO - STINT LATE"
        if metrics.pace_drop is not None and metrics.pace_drop > float(self.thresholds.get("undercut_loss_per_lap", 0.20)):
            return "POSSIBLE"
        if metrics.kart_rating == "BAD":
            return "POSSIBLE"
        return "NO"

    def _recommend_driver(self, metrics: TeamMetrics) -> str:
        drivers = sorted(self.config.get("drivers", []), key=lambda d: d.get("priority", 99))
        if not drivers:
            return metrics.current_driver
        if metrics.phase == "FINAL":
            return drivers[0].get("name", metrics.current_driver)
        # Avoid repeating current driver if possible.
        for d in drivers:
            if d.get("name") != metrics.current_driver:
                return d.get("name", "")
        return drivers[0].get("name", metrics.current_driver)
