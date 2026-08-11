# config/celery.py
"""
Celery application instance for the project.

Run a worker with:
    celery -A config worker -l info

Broker / result backend / time limits are configured in settings.py
under the CELERY_* keys (reuses the same Redis instance as Channels).
"""
import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")

# Pull all CELERY_* keys out of Django settings.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks.py in every INSTALLED_APPS app (ai_engine/tasks.py, etc.)
app.autodiscover_tasks()


@app.task(bind=True)
def debug_task(self):
    """Sanity-check task — run `celery -A config call config.celery.debug_task`
    to confirm the worker is alive and connected to the broker."""
    print(f"Request: {self.request!r}")