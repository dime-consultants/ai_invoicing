from django.apps import AppConfig


class AiEngineConfig(AppConfig):
    name = "ai_engine"

    def ready(self):
        import ai_engine.signals  # noqa: F401 — registers signal handlers
