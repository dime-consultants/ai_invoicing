# chat/apps.py
from django.apps import AppConfig


class ChatConfig(AppConfig):
    name = "chat"
    verbose_name = "Invoice Processing Chat"

    def ready(self):
        import chat.signals  # noqa: F401 — registers signal handlers
