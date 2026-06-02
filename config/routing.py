# config/routing.py
"""
WebSocket URL routing — mounted by asgi.py under the 'websocket' protocol.

Routes:
    ws://host/ws/notifications/              → NotificationsConsumer
    ws://host/ws/chat/<conversation_id>/     → ChatStreamConsumer
"""
from django.urls import re_path
from chat.consumers import NotificationsConsumer, ChatStreamConsumer

websocket_urlpatterns = [
    # WS /ws/notifications/
    re_path(r"^ws/notifications/$", NotificationsConsumer.as_asgi()),

    # WS /ws/chat/<conversation_id>/
    re_path(r"^ws/chat/(?P<conversation_id>\d+)/$", ChatStreamConsumer.as_asgi()),
]
