from django.apps import AppConfig


class AiEngineConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ai_engine"

    def ready(self):
        import ai_engine.signals  # noqa: F401 — registers signal handlers
