from django.db import models
from django.utils import timezone


class RaceConfig(models.Model):
    team_name              = models.CharField(max_length=100, default='APX GP')
    apex_url               = models.URLField(default='https://live.apex-timing.com/kartalcanede/')
    mode                   = models.CharField(max_length=10, choices=[('LIVE','Live'),('MANUAL','Manual')], default='LIVE')
    race_duration_minutes  = models.IntegerField(default=780)
    mandatory_pits         = models.IntegerField(default=23)
    max_stint_minutes      = models.IntegerField(default=45)
    pit_duration_minutes   = models.IntegerField(default=3)
    no_pit_last_minutes    = models.IntegerField(default=5)
    started_at             = models.DateTimeField(null=True, blank=True)
    created_at             = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Race Configuration'

    def __str__(self):
        return f"{self.team_name}"

    @property
    def is_running(self):
        return self.started_at is not None

    @property
    def elapsed_seconds(self):
        if not self.started_at:
            return 0.0
        return max(0.0, (timezone.now() - self.started_at).total_seconds())

    @property
    def elapsed_minutes(self):
        return self.elapsed_seconds / 60

    @property
    def remaining_seconds(self):
        return max(0.0, self.race_duration_minutes * 60 - self.elapsed_seconds)

    @property
    def remaining_minutes(self):
        return self.remaining_seconds / 60


class Driver(models.Model):
    config   = models.ForeignKey(RaceConfig, on_delete=models.CASCADE, related_name='drivers')
    name     = models.CharField(max_length=100)
    priority = models.IntegerField(default=1)
    color    = models.CharField(max_length=7, default='#00d4ff')

    class Meta:
        ordering = ['priority']

    def __str__(self):
        return self.name


class LiveTeam(models.Model):
    """One row per team per session. Updated on every Apex grid message."""
    config      = models.ForeignKey(RaceConfig, on_delete=models.CASCADE, related_name='teams')
    internal_id = models.CharField(max_length=20, default='')  # Apex internal id (e.g. "28")
    kart        = models.CharField(max_length=10)              # displayed kart number
    name        = models.CharField(max_length=100, default='')
    position    = models.IntegerField(default=0)
    last_lap    = models.FloatField(null=True, blank=True)     # seconds
    best_lap    = models.FloatField(null=True, blank=True)
    laps        = models.IntegerField(default=0)
    pits        = models.IntegerField(default=0)
    gap         = models.CharField(max_length=20, default='')
    interval    = models.CharField(max_length=20, default='')
    is_ours     = models.BooleanField(default=False)
    last_seen   = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [['config', 'kart']]
        ordering = ['position']

    def __str__(self):
        return f"P{self.position} #{self.kart} {self.name}"

    def recent_laps(self, n=10):
        return list(self.laps_qs.order_by('-lap_number').values_list('lap_sec', flat=True)[:n])


class LapRecord(models.Model):
    team       = models.ForeignKey(LiveTeam, on_delete=models.CASCADE, related_name='laps_qs')
    lap_number = models.IntegerField()
    lap_sec    = models.FloatField()
    recorded   = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [['team', 'lap_number']]
        ordering = ['lap_number']

    def __str__(self):
        return f"#{self.team.kart} Lap {self.lap_number}: {self.lap_sec:.3f}s"


class PitEvent(models.Model):
    team  = models.ForeignKey(LiveTeam, on_delete=models.CASCADE, related_name='pit_events')
    etype = models.CharField(max_length=3, choices=[('IN','In'),('OUT','Out')])
    lap   = models.IntegerField(null=True, blank=True)
    at    = models.DateTimeField(auto_now_add=True)


class RaceEvent(models.Model):
    config   = models.ForeignKey(RaceConfig, on_delete=models.CASCADE, related_name='events', null=True)
    at       = models.DateTimeField(auto_now_add=True)
    level    = models.CharField(max_length=8, choices=[('INFO','Info'),('WARN','Warn'),('CRIT','Crit')], default='INFO')
    category = models.CharField(max_length=10, default='SYSTEM')
    message  = models.TextField()

    class Meta:
        ordering = ['-at']

    def __str__(self):
        return f"[{self.level}] {self.message[:60]}"


class StrategyState(models.Model):
    """Singleton pk=1. Recomputed after every Apex cycle."""
    decision           = models.CharField(max_length=10, default='HOLD')
    box_window         = models.CharField(max_length=30, default='WAIT')
    risk               = models.CharField(max_length=10, default='LOW')
    undercut           = models.CharField(max_length=20, default='NO')
    recommended_driver = models.CharField(max_length=100, default='—')
    kart_rating        = models.CharField(max_length=10, default='GOOD')
    commands           = models.JSONField(default=list)
    stint_elapsed_min  = models.FloatField(default=0)
    race_elapsed_min   = models.FloatField(default=0)
    actual_pits        = models.IntegerField(default=0)
    expected_pits      = models.FloatField(default=0)
    pit_plan           = models.CharField(max_length=15, default='ON PLAN')
    pace_drop_sec      = models.FloatField(default=0)
    avg3_sec           = models.FloatField(null=True, blank=True)
    avg5_sec           = models.FloatField(null=True, blank=True)
    updated_at         = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Strategy State'


class ManualControl(models.Model):
    """Singleton pk=1. Strategist overrides."""
    current_driver   = models.CharField(max_length=100, default='')
    kart_override    = models.CharField(max_length=10, default='AUTO',
                       choices=[('GOOD','Good'),('MEDIUM','Medium'),('BAD','Bad'),('AUTO','Auto')])
    box_clear        = models.BooleanField(default=True)
    stint_started_at = models.DateTimeField(null=True, blank=True)
    updated_at       = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Manual Control'


class ApexMeta(models.Model):
    """Singleton pk=1. Live Apex session metadata."""
    connected    = models.BooleanField(default=False)
    track        = models.CharField(max_length=100, default='')
    session_name = models.CharField(max_length=100, default='')
    last_data    = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Apex Metadata'
