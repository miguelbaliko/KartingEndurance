from django.db import models
from django.utils import timezone


class RaceSession(models.Model):
    team_name = models.CharField(max_length=100, default='APX GP')
    apex_url = models.URLField(default='https://live.apex-timing.com/kartalcanede/')
    mode = models.CharField(
        max_length=10,
        choices=[('LIVE', 'Live Apex'), ('MANUAL', 'Manual CSV')],
        default='LIVE',
    )
    race_duration_minutes = models.IntegerField(default=780)
    mandatory_pits = models.IntegerField(default=23)
    max_stint_minutes = models.IntegerField(default=45)
    pit_duration_minutes = models.IntegerField(default=3)
    no_pit_last_minutes = models.IntegerField(default=5)
    started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Race Session'

    def __str__(self):
        return f"{self.team_name} — {self.created_at.strftime('%Y-%m-%d')}"

    @property
    def is_active(self):
        return self.started_at is not None

    @property
    def elapsed_minutes(self):
        if not self.started_at:
            return 0.0
        return (timezone.now() - self.started_at).total_seconds() / 60

    @property
    def remaining_minutes(self):
        return max(0.0, self.race_duration_minutes - self.elapsed_minutes)


class Driver(models.Model):
    session = models.ForeignKey(RaceSession, on_delete=models.CASCADE, related_name='drivers')
    name = models.CharField(max_length=100)
    priority = models.IntegerField(default=1)
    color = models.CharField(max_length=7, default='#00d4ff')

    class Meta:
        ordering = ['priority']

    def __str__(self):
        return f"{self.name} (P{self.priority})"


class TeamSnapshot(models.Model):
    session = models.ForeignKey(RaceSession, on_delete=models.CASCADE, related_name='teams')
    kart_number = models.CharField(max_length=10)
    name = models.CharField(max_length=100)
    position = models.IntegerField(default=0)
    last_lap_seconds = models.FloatField(null=True, blank=True)
    best_lap_seconds = models.FloatField(null=True, blank=True)
    lap_count = models.IntegerField(default=0)
    pit_count = models.IntegerField(default=0)
    gap = models.CharField(max_length=20, default='')
    interval = models.CharField(max_length=20, default='')
    is_our_team = models.BooleanField(default=False)
    on_track_time = models.CharField(max_length=20, default='')
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['position']
        unique_together = [['session', 'kart_number']]

    def __str__(self):
        return f"P{self.position} #{self.kart_number} {self.name}"

    def recent_laps(self, n=10):
        return list(
            self.laps.order_by('-lap_number').values_list('lap_time_seconds', flat=True)[:n]
        )


class LapRecord(models.Model):
    team = models.ForeignKey(TeamSnapshot, on_delete=models.CASCADE, related_name='laps')
    lap_number = models.IntegerField()
    lap_time_seconds = models.FloatField()
    sector1 = models.FloatField(null=True, blank=True)
    sector2 = models.FloatField(null=True, blank=True)
    sector3 = models.FloatField(null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['lap_number']
        unique_together = [['team', 'lap_number']]

    def __str__(self):
        return f"#{self.team.kart_number} Lap {self.lap_number}: {self.lap_time_seconds:.3f}s"


class PitEvent(models.Model):
    IN = 'IN'
    OUT = 'OUT'
    TYPE_CHOICES = [(IN, 'Pit In'), (OUT, 'Pit Out')]

    team = models.ForeignKey(TeamSnapshot, on_delete=models.CASCADE, related_name='pit_events')
    event_type = models.CharField(max_length=3, choices=TYPE_CHOICES)
    lap_number = models.IntegerField(null=True, blank=True)
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-occurred_at']

    def __str__(self):
        return f"#{self.team.kart_number} Pit {self.event_type} @ lap {self.lap_number}"


class RaceEvent(models.Model):
    INFO = 'INFO'
    WARNING = 'WARNING'
    CRITICAL = 'CRITICAL'
    LEVEL_CHOICES = [(INFO, 'Info'), (WARNING, 'Warning'), (CRITICAL, 'Critical')]

    TIMING = 'TIMING'
    STRATEGY = 'STRATEGY'
    MANUAL = 'MANUAL'
    SYSTEM = 'SYSTEM'
    CATEGORY_CHOICES = [
        (TIMING, 'Timing'), (STRATEGY, 'Strategy'),
        (MANUAL, 'Manual'), (SYSTEM, 'System'),
    ]

    session = models.ForeignKey(RaceSession, on_delete=models.CASCADE, related_name='events', null=True)
    occurred_at = models.DateTimeField(auto_now_add=True)
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, default=INFO)
    category = models.CharField(max_length=10, choices=CATEGORY_CHOICES, default=SYSTEM)
    message = models.TextField()

    class Meta:
        ordering = ['-occurred_at']

    def __str__(self):
        return f"[{self.level}] {self.message[:60]}"


class StrategySnapshot(models.Model):
    HOLD = 'HOLD'
    PREPARE = 'PREPARE'
    BOX = 'BOX'
    ATTACK = 'ATTACK'
    CRITICAL = 'CRITICAL'
    DECISION_CHOICES = [
        (HOLD, 'Hold'), (PREPARE, 'Prepare'), (BOX, 'Box'),
        (ATTACK, 'Attack'), (CRITICAL, 'Critical'),
    ]

    decision = models.CharField(max_length=10, choices=DECISION_CHOICES, default=HOLD)
    box_window = models.CharField(max_length=30, default='WAIT')
    undercut = models.CharField(max_length=20, default='NO')
    recommended_driver = models.CharField(max_length=100, default='—')
    risk = models.CharField(max_length=10, default='LOW')
    stint_elapsed_minutes = models.FloatField(default=0)
    stint_remaining_minutes = models.FloatField(default=45)
    race_elapsed_minutes = models.FloatField(default=0)
    race_remaining_minutes = models.FloatField(default=780)
    expected_pits = models.FloatField(default=0)
    actual_pits = models.IntegerField(default=0)
    pit_plan_status = models.CharField(max_length=15, default='ON PLAN')
    pace_drop_seconds = models.FloatField(default=0)
    avg3_seconds = models.FloatField(null=True, blank=True)
    avg5_seconds = models.FloatField(null=True, blank=True)
    kart_rating = models.CharField(max_length=10, default='GOOD')
    commands = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Strategy Snapshot'

    def __str__(self):
        return f"{self.decision} — {self.updated_at.strftime('%H:%M:%S') if self.updated_at else '—'}"


class ManualState(models.Model):
    GOOD = 'GOOD'
    MEDIUM = 'MEDIUM'
    BAD = 'BAD'
    AUTO = 'AUTO'
    KART_CHOICES = [(GOOD, 'Good'), (MEDIUM, 'Medium'), (BAD, 'Bad'), (AUTO, 'Auto')]

    current_driver = models.CharField(max_length=100, default='')
    kart_rating = models.CharField(max_length=10, choices=KART_CHOICES, default=AUTO)
    box_clear = models.BooleanField(default=True)
    stint_started_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Manual State'

    def __str__(self):
        driver = self.current_driver or '—'
        return f"Driver: {driver} | Kart: {self.kart_rating}"
