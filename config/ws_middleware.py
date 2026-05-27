# config/ws_middleware.py
"""
JWT authentication middleware for Django Channels WebSocket connections.

The browser cannot set the Authorization header on a WebSocket handshake,
so the token is passed as a query-string parameter instead:

    ws://host/ws/notifications/?token=<access_token>
    ws://host/ws/chat/42/?token=<access_token>

The middleware resolves the user from the token and attaches it to the
scope so consumers can access it via self.scope["user"].
"""
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth.models import AnonymousUser


@database_sync_to_async
def _get_user_from_token(token_str: str):
    """Validate a JWT access token and return the corresponding User, or AnonymousUser."""
    try:
        from rest_framework_simplejwt.tokens import AccessToken
        from django.contrib.auth import get_user_model

        User = get_user_model()
        token = AccessToken(token_str)
        user_id = token.get("user_id")
        return User.objects.get(pk=user_id)
    except Exception:
        return AnonymousUser()


class JWTAuthMiddleware(BaseMiddleware):
    """
    Attach the authenticated user to the WebSocket scope.
    Falls back to AnonymousUser if the token is missing or invalid.
    """

    async def __call__(self, scope, receive, send):
        # Extract token from query string: ?token=<jwt>
        query_string = scope.get("query_string", b"").decode("utf-8")
        params       = parse_qs(query_string)
        token_list   = params.get("token", [])
        token_str    = token_list[0] if token_list else ""

        scope["user"] = (
            await _get_user_from_token(token_str)
            if token_str
            else AnonymousUser()
        )

        return await super().__call__(scope, receive, send)
