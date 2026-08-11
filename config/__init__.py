# config/__init__.py
"""
Ensures the Celery app is loaded when Django starts, so that
@shared_task-decorated functions (e.g. ai_engine/tasks.py) are
registered with Celery via autodiscovery.
"""
from .celery import app as celery_app

__all__ = ("celery_app",)