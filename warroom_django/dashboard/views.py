import json
import logging

from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from timing.models import ManualState, RaceEvent, RaceSession, StrategySnapshot, TeamSnapshot

logger = logging.getLogger(__name__)


def _active_session():
    return RaceSession.objects.order_by('-created_at').first()


# ---------------------------------------------------------------------------
# Page views
# ---------------------------------------------------------------------------

def command_view(request):
    return render(request, 'dashboard/command.html', {'session': _active_session()})


def strategist_view(request):
    session = _active_session()
    drivers = json.dumps(
        list(session.drivers.values('name', 'color')) if session else []
    )
    manual = ManualState.objects.filter(pk=1).first()
    return render(request, 'dashboard/strategist.html', {
        'session': session,
        'drivers_json': drivers,
        'manual': manual,
    })


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

def api_state(request):
    session = _active_session()
    if not session:
        return JsonResponse({'error': 'No session — run: python manage.py setup_race'}, status=404)

    teams = list(
        TeamSnapshot.objects.filter(session=session)
        .order_by('position')[:20]
        .values(
            'position', 'kart_number', 'name',
            'last_lap_seconds', 'best_lap_seconds',
            'lap_count', 'pit_count',
            'gap', 'interval', 'is_our_team',
        )
    )

    snap = StrategySnapshot.objects.filter(pk=1).first()
    strategy = {}
    if snap:
        strategy = {
            'decision': snap.decision,
            'box_window': snap.box_window,
            'undercut': snap.undercut,
            'recommended_driver': snap.recommended_driver,
            'risk': snap.risk,
            'stint_elapsed': snap.stint_elapsed_minutes,
            'stint_remaining': snap.stint_remaining_minutes,
            'race_elapsed': snap.race_elapsed_minutes,
            'race_remaining': snap.race_remaining_minutes,
            'expected_pits': snap.expected_pits,
            'actual_pits': snap.actual_pits,
            'pit_plan': snap.pit_plan_status,
            'pace_drop': snap.pace_drop_seconds,
            'avg3': snap.avg3_seconds,
            'avg5': snap.avg5_seconds,
            'kart_rating': snap.kart_rating,
            'commands': snap.commands,
            'updated_at': snap.updated_at.isoformat() if snap.updated_at else None,
        }

    events = [
        {
            'time': e['occurred_at'].strftime('%H:%M:%S'),
            'level': e['level'],
            'category': e['category'],
            'message': e['message'],
        }
        for e in RaceEvent.objects.filter(session=session)
        .order_by('-occurred_at')
        .values('occurred_at', 'level', 'category', 'message')[:25]
    ]

    manual = ManualState.objects.filter(pk=1).first()

    return JsonResponse({
        'session': {
            'team_name': session.team_name,
            'started': session.is_active,
            'started_at': session.started_at.isoformat() if session.started_at else None,
            'elapsed_minutes': round(session.elapsed_minutes, 1),
            'remaining_minutes': round(session.remaining_minutes, 1),
            'race_duration': session.race_duration_minutes,
            'mandatory_pits': session.mandatory_pits,
            'mode': session.mode,
        },
        'teams': teams,
        'strategy': strategy,
        'events': events,
        'manual': {
            'current_driver': manual.current_driver if manual else '',
            'kart_rating': manual.kart_rating if manual else 'AUTO',
            'box_clear': manual.box_clear if manual else True,
            'stint_started_at': (
                manual.stint_started_at.isoformat()
                if manual and manual.stint_started_at else None
            ),
        },
    })


@csrf_exempt
@require_POST
def api_manual(request):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    manual, _ = ManualState.objects.get_or_create(pk=1)

    if 'current_driver' in data:
        manual.current_driver = data['current_driver']
    if 'kart_rating' in data:
        manual.kart_rating = data['kart_rating']
    if 'box_clear' in data:
        manual.box_clear = bool(data['box_clear'])
    if data.get('reset_stint'):
        manual.stint_started_at = timezone.now()
    manual.save()

    session = _active_session()
    if session:
        RaceEvent.objects.create(
            session=session, level='INFO', category='MANUAL',
            message=f"Override: {data}",
        )
        try:
            from timing.strategy import compute_strategy
            compute_strategy(session)
        except Exception:
            logger.exception("Strategy recompute failed")

    return JsonResponse({'ok': True})


@csrf_exempt
@require_POST
def api_race_control(request):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    session = _active_session()
    if not session:
        return JsonResponse({'error': 'No session'}, status=404)

    action = data.get('action')
    if action == 'start':
        now = timezone.now()
        session.started_at = now
        session.save()
        manual, _ = ManualState.objects.get_or_create(pk=1)
        manual.stint_started_at = now
        manual.save()
        RaceEvent.objects.create(session=session, level='INFO', category='MANUAL', message='Race started')

    elif action == 'stop':
        session.started_at = None
        session.save()
        RaceEvent.objects.create(session=session, level='WARNING', category='MANUAL', message='Race stopped')

    elif action == 'reset_stint':
        manual, _ = ManualState.objects.get_or_create(pk=1)
        manual.stint_started_at = timezone.now()
        manual.save()
        RaceEvent.objects.create(session=session, level='INFO', category='MANUAL', message='Stint timer reset')

    return JsonResponse({'ok': True})
