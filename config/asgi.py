# config/asgi.py
"""
ASGI config — serves both HTTP (Django) and WebSocket (Channels) traffic.

HTTP  → standard Django ASGI application
WS    → Channels ProtocolTypeRouter
          └─ JWTAuthMiddleware (resolves user from ?token=<jwt>)
               └─ URLRouter (ws/notifications/, ws/chat/<id>/)
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Must call get_asgi_application() before importing anything that touches
# Django models, so the app registry is fully populated first.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from config.routing import websocket_urlpatterns             # noqa: E402
from config.ws_middleware import JWTAuthMiddleware           # noqa: E402

application = ProtocolTypeRouter({
    # Standard HTTP — handled by Django
    "http": django_asgi_app,

    # WebSocket — authenticated via JWT query-string token
    "websocket": JWTAuthMiddleware(
        URLRouter(websocket_urlpatterns)
    ),
})
