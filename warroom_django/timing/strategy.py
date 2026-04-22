import logging

logger = logging.getLogger(__name__)

# Thresholds (all in seconds unless stated)
PACE_DROP_PREPARE = 0.30
PACE_DROP_BOX = 0.50

# Stint thresholds in minutes
STINT_PREPARE = 40
STINT_ALERT = 42
STINT_BOX = 44


def _avg(laps, n):
    subset = laps[:n]
    return sum(subset) / len(subset) if len(subset) >= n else None


def compute_strategy(session):
    """Compute full strategy decision and persist to StrategySnapshot (pk=1)."""
    from django.utils import timezone
    from timing.models import TeamSnapshot, ManualState, StrategySnapshot

    # --- Fetch singletons ---
    manual, _ = ManualState.objects.get_or_create(pk=1)
    snapshot, _ = StrategySnapshot.objects.get_or_create(pk=1)

    # --- Our team ---
    our_team = (
        TeamSnapshot.objects.filter(session=session, is_our_team=True).first()
        or TeamSnapshot.objects.filter(session=session).order_by('position').first()
    )

    # --- Race timing ---
    race_elapsed = session.elapsed_minutes
    race_remaining = session.remaining_minutes

    # --- Stint timing ---
    if manual.stint_started_at:
        stint_elapsed = (timezone.now() - manual.stint_started_at).total_seconds() / 60
    else:
        stint_elapsed = 0.0
    stint_remaining = max(0.0, session.max_stint_minutes - stint_elapsed)

    # --- Lap pace ---
    recent_laps = our_team.recent_laps(10) if our_team else []
    avg3 = _avg(recent_laps, 3)
    avg5 = _avg(recent_laps, 5)

    # Positive = slower (pace dropped), negative = faster
    pace_drop = (avg3 - avg5) if (avg3 and avg5) else 0.0

    # --- Kart rating ---
    if manual.kart_rating != 'AUTO':
        kart_rating = manual.kart_rating
    elif pace_drop >= PACE_DROP_PREPARE:
        kart_rating = 'BAD'
    elif pace_drop >= 0.10:
        kart_rating = 'MEDIUM'
    else:
        kart_rating = 'GOOD'

    # --- Pit plan ---
    actual_pits = our_team.pit_count if our_team else 0
    if race_elapsed > 0 and session.race_duration_minutes > 0:
        expected_pits = (race_elapsed / session.race_duration_minutes) * session.mandatory_pits
    else:
        expected_pits = 0.0

    if actual_pits < expected_pits - 1:
        pit_plan = 'BEHIND'
    elif actual_pits > expected_pits + 1:
        pit_plan = 'AHEAD'
    else:
        pit_plan = 'ON PLAN'

    # --- Race phase ---
    frac = race_elapsed / session.race_duration_minutes if session.race_duration_minutes else 0
    if frac < 3 / 13:
        phase = 'EARLY'
    elif frac > 9 / 13:
        phase = 'FINAL'
    else:
        phase = 'MID'

    # --- Decision tree ---
    decision = 'HOLD'
    box_window = 'WAIT'
    undercut = 'NO'
    risk = 'LOW'
    commands = []

    if race_remaining <= session.no_pit_last_minutes:
        decision = 'CRITICAL'
        box_window = 'CLOSED'
        risk = 'HIGH'
        commands = [
            'Keine Boxenstopp!',
            f'Noch {race_remaining:.0f} Min.',
            'Rennen zu Ende fahren',
        ]

    elif stint_elapsed >= STINT_BOX:
        decision = 'BOX'
        box_window = 'NOW'
        risk = 'HIGH'
        commands = ['SOFORT BOX!', 'Stint-Limit erreicht', 'Fahrerwechsel Pflicht']

    elif stint_elapsed >= STINT_ALERT:
        decision = 'PREPARE'
        box_window = '1–2 LAPS'
        risk = 'MEDIUM'
        commands = ['Box vorbereiten', '1–2 Runden noch', 'Nächsten Fahrer bereit']

    elif stint_elapsed >= STINT_PREPARE:
        decision = 'PREPARE'
        box_window = '2–4 LAPS'
        risk = 'LOW'
        commands = ['Fenster öffnet bald', 'Stint endet demnächst', 'Strategie prüfen']

    elif pace_drop >= PACE_DROP_BOX:
        decision = 'BOX' if manual.box_clear else 'PREPARE'
        box_window = 'NOW' if manual.box_clear else 'NOW IF BOX CLEAN'
        risk = 'HIGH'
        commands = ['Tempo-Einbruch!', 'Kart prüfen', 'Box wenn frei']

    elif pace_drop >= PACE_DROP_PREPARE:
        decision = 'PREPARE'
        box_window = 'NEXT CLEAN WINDOW'
        risk = 'MEDIUM'
        commands = ['Tempo sinkt', 'Kart schlechter', 'Fenster vorbereiten']

    elif pit_plan == 'BEHIND':
        decision = 'PREPARE'
        box_window = 'NEXT CLEAN WINDOW'
        risk = 'LOW'
        commands = ['Hinter Plan', 'Boxenstopp einplanen', 'Gutes Fenster suchen']

    elif kart_rating == 'GOOD' and pace_drop <= 0 and phase != 'FINAL':
        decision = 'ATTACK'
        box_window = 'WAIT'
        risk = 'LOW'
        commands = ['Kart läuft!', 'Angriff möglich', 'Tempo halten']

    else:
        decision = 'HOLD'
        box_window = 'WAIT'
        risk = 'LOW'
        commands = ['Renntempo halten', pit_plan, 'Alles im Plan']

    # Undercut window
    if STINT_PREPARE > stint_elapsed >= 24 and kart_rating in ('MEDIUM', 'BAD') \
            and decision not in ('BOX', 'CRITICAL'):
        undercut = 'POSSIBLE'

    # Next driver recommendation (round-robin by priority)
    drivers = list(session.drivers.order_by('priority').values_list('name', flat=True))
    recommended_driver = drivers[actual_pits % len(drivers)] if drivers else '—'

    # --- Persist ---
    StrategySnapshot.objects.filter(pk=1).update(
        decision=decision,
        box_window=box_window,
        undercut=undercut,
        recommended_driver=recommended_driver,
        risk=risk,
        stint_elapsed_minutes=round(stint_elapsed, 1),
        stint_remaining_minutes=round(stint_remaining, 1),
        race_elapsed_minutes=round(race_elapsed, 1),
        race_remaining_minutes=round(race_remaining, 1),
        expected_pits=round(expected_pits, 1),
        actual_pits=actual_pits,
        pit_plan_status=pit_plan,
        pace_drop_seconds=round(pace_drop, 3),
        avg3_seconds=round(avg3, 3) if avg3 else None,
        avg5_seconds=round(avg5, 3) if avg5 else None,
        kart_rating=kart_rating,
        commands=commands,
    )
