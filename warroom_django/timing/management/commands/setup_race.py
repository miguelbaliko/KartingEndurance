from django.core.management.base import BaseCommand
from timing.models import RaceSession, Driver, ManualState, StrategySnapshot


class Command(BaseCommand):
    help = 'Create initial race session, drivers, and singleton records'

    def add_arguments(self, parser):
        parser.add_argument('--team', default='APX GP', help='Team name')
        parser.add_argument('--url', default='https://live.apex-timing.com/kartalcanede/')
        parser.add_argument('--mode', default='LIVE', choices=['LIVE', 'MANUAL'])

    def handle(self, *args, **options):
        session, created = RaceSession.objects.get_or_create(
            apex_url=options['url'],
            defaults={
                'team_name': options['team'],
                'mode': options['mode'],
                'race_duration_minutes': 780,
                'mandatory_pits': 23,
                'max_stint_minutes': 45,
                'pit_duration_minutes': 3,
                'no_pit_last_minutes': 5,
            },
        )

        if created:
            self.stdout.write(self.style.SUCCESS(f'Created session: {session}'))
            drivers = [
                ('Driver 1', 1, '#00d4ff'),
                ('Driver 2', 2, '#00e676'),
                ('Driver 3', 3, '#ff9800'),
                ('Driver 4', 4, '#e040fb'),
            ]
            for name, priority, color in drivers:
                Driver.objects.create(session=session, name=name, priority=priority, color=color)
            self.stdout.write(f'  Created {len(drivers)} placeholder drivers — edit via /admin/')
        else:
            self.stdout.write(f'Session already exists: {session}')

        ManualState.objects.get_or_create(pk=1)
        StrategySnapshot.objects.get_or_create(pk=1)
        self.stdout.write(self.style.SUCCESS('Setup complete. Edit drivers at /admin/'))
