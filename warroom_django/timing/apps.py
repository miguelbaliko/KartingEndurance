import os
import logging
from django.apps import AppConfig

logger = logging.getLogger(__name__)


class TimingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'timing'

    def ready(self):
        # Only start in the child process (not the reloader watcher)
        if os.environ.get('RUN_MAIN') == 'true':
            try:
                from timing.models import RaceSession
                session = RaceSession.objects.filter(mode='LIVE').order_by('-created_at').first()
                if session:
                    from timing.apex_ws import start_apex_thread
                    start_apex_thread()
                else:
                    logger.info("No LIVE session found — Apex thread not started. Run setup_race first.")
            except Exception:
                logger.exception("Could not start Apex thread")
