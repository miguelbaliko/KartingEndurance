import logging
from django.utils import timezone

logger = logging.getLogger(__name__)

# Thresholds
PACE_DROP_PREPARE = 0.30
PACE_DROP_BOX     = 0.50
STINT_PREPARE     = 40
STINT_ALERT       = 42
STINT_FORCED      = 44


def compute(config):
    """
    Recompute strategy decision from current DB state.
    Saves result to StrategyState (pk=1).
    Returns the updated StrategyState instance.
    """
    from timing.models import LiveTeam, ManualControl, StrategyState

    manual, _ = ManualControl.objects.get_or_create(pk=1)
    state, _  = StrategyState.objects.get_or_create(pk=1)

    our_team = (
        LiveTeam.objects.filter(config=config, is_ours=True).first()
        or LiveTeam.objects.filter(config=config).order_by('position').first()
    )

    # ── Timing ─────────────────────────────────────────────────────────────────
    race_elapsed   = config.elapsed_minutes
    race_remaining = config.remaining_minutes

    if manual.stint_started_at:
        stint_elapsed = (timezone.now() - manual.stint_started_at).total_seconds() / 60
    else:
        stint_elapsed = 0.0

    # ── Lap pace ───────────────────────────────────────────────────────────────
    laps = our_team.recent_laps(10) if our_team else []

    def _avg(n):
        s = laps[:n]
        return sum(s) / len(s) if len(s) >= n else None

    avg3 = _avg(3)
    avg5 = _avg(5)
    # Positive = degradation (latest laps slower than average)
    pace_drop = round((avg3 - avg5), 3) if avg3 and avg5 else 0.0

    # ── Kart rating ─────────────────────────────────────────────────────────────
    if manual.kart_override != 'AUTO':
        kart = manual.kart_override
    elif pace_drop >= PACE_DROP_BOX:
        kart = 'BAD'
    elif pace_drop >= 0.15:
        kart = 'MEDIUM'
    else:
        kart = 'GOOD'

    # ── Pit plan ────────────────────────────────────────────────────────────────
    actual_pits = our_team.pits if our_team else 0
    dur         = config.race_duration_minutes
    expected    = (race_elapsed / dur * config.mandatory_pits) if dur and race_elapsed > 0 else 0.0

    if actual_pits < expected - 1:
        pit_plan = 'BEHIND'
    elif actual_pits > expected + 1:
        pit_plan = 'AHEAD'
    else:
        pit_plan = 'ON PLAN'

    # ── Race phase ──────────────────────────────────────────────────────────────
    frac  = race_elapsed / dur if dur else 0
    phase = 'EARLY' if frac < 3/13 else 'FINAL' if frac > 9/13 else 'MID'

    # ── Decision tree ───────────────────────────────────────────────────────────
    decision   = 'HOLD'
    box_window = 'WAIT'
    risk       = 'LOW'
    undercut   = 'NO'
    commands   = []

    if race_remaining <= config.no_pit_last_minutes:
        decision   = 'CRITICAL'
        box_window = 'CLOSED'
        risk       = 'HIGH'
        commands   = [f'Noch {race_remaining:.0f} Min.', 'Keine Box mehr!', 'Fahre bis zum Ende']

    elif stint_elapsed >= STINT_FORCED:
        decision   = 'BOX'
        box_window = 'NOW'
        risk       = 'HIGH'
        commands   = ['SOFORT BOX!', 'Stint-Limit erreicht', 'Fahrerwechsel Pflicht']

    elif stint_elapsed >= STINT_ALERT:
        decision   = 'PREPARE'
        box_window = '1–2 RUNDEN'
        risk       = 'MEDIUM'
        commands   = ['Box vorbereiten', '1–2 Runden noch', 'Nächsten Fahrer bereit']

    elif stint_elapsed >= STINT_PREPARE:
        decision   = 'PREPARE'
        box_window = '2–4 RUNDEN'
        risk       = 'LOW'
        commands   = ['Fenster öffnet bald', 'Stint endet demnächst', 'Bereit machen']

    elif pace_drop >= PACE_DROP_BOX:
        decision   = 'BOX' if manual.box_clear else 'PREPARE'
        box_window = 'NOW' if manual.box_clear else 'BOX WENN FREI'
        risk       = 'HIGH'
        commands   = ['Tempo-Einbruch!', f'+{pace_drop:.2f}s Drop', 'Box wenn möglich']

    elif pace_drop >= PACE_DROP_PREPARE:
        decision   = 'PREPARE'
        box_window = 'NÄCHSTES FENSTER'
        risk       = 'MEDIUM'
        commands   = ['Tempo sinkt', 'Kart schlechter', 'Fenster vorbereiten']

    elif pit_plan == 'BEHIND':
        decision   = 'PREPARE'
        box_window = 'NÄCHSTES FENSTER'
        risk       = 'LOW'
        commands   = ['Hinter Pit-Plan', 'Stop einplanen', 'Fenster suchen']

    elif kart == 'GOOD' and pace_drop <= 0 and phase != 'FINAL':
        decision   = 'ATTACK'
        box_window = 'HOLD'
        risk       = 'LOW'
        commands   = ['Kart läuft gut!', 'Angriff möglich', 'Tempo halten']

    else:
        commands = ['Renntempo halten', pit_plan, 'Alles im Plan']

    # Undercut opportunity
    if 24 <= stint_elapsed <= STINT_PREPARE and kart in ('MEDIUM', 'BAD') \
            and decision not in ('BOX', 'CRITICAL'):
        undercut = 'MOEGLICH'

    # Recommended driver (round-robin)
    drivers  = list(config.drivers.order_by('priority').values_list('name', flat=True))
    rec_drv  = drivers[actual_pits % len(drivers)] if drivers else '—'

    StrategyState.objects.filter(pk=1).update(
        decision           = decision,
        box_window         = box_window,
        risk               = risk,
        undercut           = undercut,
        recommended_driver = rec_drv,
        kart_rating        = kart,
        commands           = commands,
        stint_elapsed_min  = round(stint_elapsed, 1),
        race_elapsed_min   = round(race_elapsed,  1),
        actual_pits        = actual_pits,
        expected_pits      = round(expected, 1),
        pit_plan           = pit_plan,
        pace_drop_sec      = pace_drop,
        avg3_sec           = round(avg3, 3) if avg3 else None,
        avg5_sec           = round(avg5, 3) if avg5 else None,
    )
    state.refresh_from_db()
    return state
