# chat/routing.py
# NOTE: This file is intentionally minimal.
# The authoritative WebSocket routing is in config/routing.py,
# which is loaded by config/asgi.py.
#
# This file is kept for reference only and is NOT imported by asgi.py.
# Do not add routes here — add them to config/routing.py instead.
from django.urls import re_path

from .consumers import ChatConsumer

websocket_urlpatterns = [
    re_path(
        r"ws/chat/(?P<conversation_id>\d+)/$",
        ChatConsumer.as_asgi(),
    ),
]