# config/celery.py
"""
Celery application instance for the project.

Run a worker with:
    celery -A config worker -l info

Broker / result backend / time limits are configured in settings.py
under the CELERY_* keys (reuses the same Redis instance as Channels).

Queues
------
CELERY_TASK_ROUTES (settings.py) sends tasks to one of three named queues:
    "chat"       — chat.tasks.run_chat_turn_task only. Short-lived; must never
                   be starved by long-running work, so it is served by its own
                   worker (docker-compose.yml "worker-chat" service, `-Q chat`).
    "extraction" — uploads.tasks.* (large PDF/spreadsheet text extraction).
    "default"    — ai_engine.tasks.run_ai_job_task and anything else.
A new task module lands on "default" unless CELERY_TASK_ROUTES is updated —
if it's long-running, add it to "extraction" or a new queue explicitly rather
than letting it compete with chat turns.
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