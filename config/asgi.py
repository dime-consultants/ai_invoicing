# config/asgi.py

"""
ASGI config — serves both HTTP (Django) and WebSocket (Channels) traffic.

HTTP
    → Standard Django ASGI application

WebSocket
    → JWTAuthMiddleware
        → URLRouter
            → WebSocket URL patterns
"""

import os

from django.core.asgi import get_asgi_application


# Set the Django settings module
os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "config.settings",
)


# Initialize Django before importing anything that may touch Django models
django_asgi_app = get_asgi_application()


# Import Channels components after Django initialization
from channels.routing import ProtocolTypeRouter, URLRouter
from config.ws_middleware import JWTAuthMiddleware  
from uploads.consumers import websocket_urlpatterns as files_websocket_urlpatterns
from chat.consumers import websocket_urlpatterns as chat_websocket_urlpatterns


application = ProtocolTypeRouter(
    {
        # Standard Django HTTP traffic
        "http": django_asgi_app,

        # WebSocket traffic authenticated using JWT
        "websocket": JWTAuthMiddleware(
            URLRouter(files_websocket_urlpatterns + chat_websocket_urlpatterns)
        ),

    }
)