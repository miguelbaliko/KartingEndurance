from django.contrib import admin
from .models import (
    RaceSession, Driver, TeamSnapshot, LapRecord,
    PitEvent, RaceEvent, StrategySnapshot, ManualState,
)


class DriverInline(admin.TabularInline):
    model = Driver
    extra = 4
    fields = ['name', 'priority', 'color']


@admin.register(RaceSession)
class RaceSessionAdmin(admin.ModelAdmin):
    list_display = ['team_name', 'mode', 'apex_url', 'started_at', 'created_at']
    inlines = [DriverInline]


@admin.register(TeamSnapshot)
class TeamSnapshotAdmin(admin.ModelAdmin):
    list_display = ['position', 'kart_number', 'name', 'lap_count', 'pit_count',
                    'last_lap_seconds', 'best_lap_seconds', 'is_our_team', 'last_seen']
    list_filter = ['session', 'is_our_team']
    ordering = ['position']


@admin.register(LapRecord)
class LapRecordAdmin(admin.ModelAdmin):
    list_display = ['team', 'lap_number', 'lap_time_seconds', 'recorded_at']
    list_filter = ['team__session']
    ordering = ['-recorded_at']


@admin.register(PitEvent)
class PitEventAdmin(admin.ModelAdmin):
    list_display = ['team', 'event_type', 'lap_number', 'occurred_at']
    list_filter = ['event_type', 'team__session']


@admin.register(RaceEvent)
class RaceEventAdmin(admin.ModelAdmin):
    list_display = ['occurred_at', 'level', 'category', 'message']
    list_filter = ['level', 'category', 'session']


@admin.register(StrategySnapshot)
class StrategySnapshotAdmin(admin.ModelAdmin):
    list_display = ['decision', 'box_window', 'kart_rating', 'risk', 'updated_at']
    readonly_fields = ['updated_at']


@admin.register(ManualState)
class ManualStateAdmin(admin.ModelAdmin):
    list_display = ['current_driver', 'kart_rating', 'box_clear', 'stint_started_at', 'updated_at']
