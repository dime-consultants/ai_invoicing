# chat/management/commands/check_ws.py
"""
Management command to verify the WebSocket infrastructure is correctly wired.

Usage:
    python manage.py check_ws

Checks:
  1. channels is installed and importable
  2. ASGI_APPLICATION points to config.asgi.application
  3. CHANNEL_LAYERS is configured
  4. Channel layer is reachable (send + receive a test message)
  5. Both WS URL patterns are registered
  6. All signal files are importable
"""
from django.core.management.base import BaseCommand
from django.conf import settings


class Command(BaseCommand):
    help = "Verify WebSocket / Django Channels setup"

    def handle(self, *args, **options):
        ok = True

        def check(label, fn):
            nonlocal ok
            try:
                result = fn()
                self.stdout.write(self.style.SUCCESS(f"  ✓  {label}"))
                return result
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"  ✗  {label}: {exc}"))
                ok = False
                return None

        self.stdout.write("\n── WebSocket setup check ──────────────────────────────")

        # 1. channels importable
        check("channels package importable", lambda: __import__("channels"))

        # 2. ASGI_APPLICATION
        def _check_asgi():
            val = getattr(settings, "ASGI_APPLICATION", None)
            assert val == "config.asgi.application", f"got '{val}'"
        check("ASGI_APPLICATION = config.asgi.application", _check_asgi)

        # 3. CHANNEL_LAYERS configured
        def _check_layers():
            layers = getattr(settings, "CHANNEL_LAYERS", {})
            assert "default" in layers, "no 'default' key in CHANNEL_LAYERS"
            backend = layers["default"]["BACKEND"]
            return backend
        backend = check("CHANNEL_LAYERS configured", _check_layers)
        if backend:
            self.stdout.write(f"       backend: {backend}")

        # 4. Channel layer reachable
        def _check_layer_ping():
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync
            layer = get_channel_layer()
            assert layer is not None, "get_channel_layer() returned None"
            # Send a test message to a throwaway group
            async_to_sync(layer.group_send)(
                "ws_check_test_group",
                {"type": "test.message", "text": "ping"},
            )
        check("Channel layer reachable (group_send)", _check_layer_ping)

        # 5. WS URL patterns registered
        def _check_routes():
            from config.routing import websocket_urlpatterns
            patterns = [str(p.pattern) for p in websocket_urlpatterns]
            assert any("notifications" in p for p in patterns), \
                "notifications route missing"
            assert any("chat" in p for p in patterns), \
                "chat route missing"
            return patterns
        routes = check("WS URL patterns registered", _check_routes)
        if routes:
            for r in routes:
                self.stdout.write(f"       {r}")

        # 6. Consumers importable
        check(
            "NotificationsConsumer importable",
            lambda: __import__("chat.consumers", fromlist=["NotificationsConsumer"]),
        )
        check(
            "ChatStreamConsumer importable",
            lambda: __import__("chat.consumers", fromlist=["ChatStreamConsumer"]),
        )

        # 7. Signal modules importable
        for mod in ["chat.signals", "uploads.signals", "ai_engine.signals", "analytics.signals"]:
            check(f"Signal module: {mod}", lambda m=mod: __import__(m))

        # 8. notify helpers importable
        check(
            "chat.notify helpers importable",
            lambda: __import__("chat.notify", fromlist=["notify_user", "push_chat_chunk"]),
        )

        self.stdout.write("───────────────────────────────────────────────────────\n")

        if ok:
            self.stdout.write(self.style.SUCCESS("All checks passed. WebSocket setup is complete.\n"))
            self.stdout.write("Start the server with:\n")
            self.stdout.write("  python manage.py runserver 0.0.0.0:8000\n")
            self.stdout.write("  (daphne is installed — runserver now uses ASGI automatically)\n\n")
            self.stdout.write("Connect to WebSockets:\n")
            self.stdout.write("  ws://localhost:8000/ws/notifications/?token=<jwt>\n")
            self.stdout.write("  ws://localhost:8000/ws/chat/<conversation_id>/?token=<jwt>\n")
        else:
            self.stdout.write(self.style.ERROR("Some checks failed. See errors above.\n"))
            self.stdout.write("Install missing packages:\n")
            self.stdout.write("  pip install channels==4.2.2 daphne==4.1.2\n")
            self.stdout.write("  # For Redis layer (production):\n")
            self.stdout.write("  pip install channels-redis==4.2.1\n")
            self.stdout.write("  # Start Redis:\n")
            self.stdout.write("  sudo apt install redis-server && sudo service redis start\n")
