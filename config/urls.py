# config/urls.py
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.views import (
    TokenRefreshView,
    TokenVerifyView,
)
from users.token_serializers import KNTokenObtainPairView


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    """GET /api/health — liveness probe, no auth required."""
    from django.db import connection
    db_ok = True
    try:
        connection.ensure_connection()
    except Exception:
        db_ok = False

    return Response({
        "status":    "ok" if db_ok else "degraded",
        "timestamp": timezone.now().isoformat(),
        "database":  "ok" if db_ok else "error",
        "version":   "1.0.0",
    })

urlpatterns = [
    path("admin/", admin.site.urls),

    # ── Health check — no auth required ──────────────────────────────────────
    path("api/health", health_check, name="health-check"),
    path("api/health/", health_check, name="health-check-slash"),

    # ── JWT Auth ──────────────────────────────────────────────────────────────
    # POST /api/auth/login/    → obtain access + refresh tokens (contract: returns token + user)
    # POST /api/auth/refresh/  → refresh access token
    # POST /api/auth/verify/   → verify token validity
    # GET  /api/auth/me/       → current user profile
    # POST /api/auth/logout/   → invalidate session
    path("api/auth/login/",   KNTokenObtainPairView.as_view(),  name="token_obtain_pair"),
    path("api/auth/refresh/", TokenRefreshView.as_view(),     name="token_refresh"),
    path("api/auth/verify/",  TokenVerifyView.as_view(),      name="token_verify"),
    path("api/auth/",         include("users.auth_urls")),

    # ── Users — auth + profile + admin user management ────────────────────────
    path("api/auth/", include("users.urls")),

    # ── Tools — registry + call log + direct run ──────────────────────────────
    path("api/tools/", include("tools.urls")),

    # ── AI Engine — jobs + insights ───────────────────────────────────────────
    path("api/ai/", include("ai_engine.urls")),

    # ── Uploads — batch + file ingestion ─────────────────────────────────────
    path("api/uploads/", include("uploads.urls")),

    # ── Files — contract-compatible file management ───────────────────────────
    path("api/files/", include("uploads.files_urls")),

    # ── Chat — conversations + messages + attachments ─────────────────────────
    path("api/chat/", include("chat.urls")),

    # ── Dashboard — stats + activity feed ────────────────────────────────────
    path("api/dashboard/", include("analytics.dashboard_urls")),

    # ── Analytics — overview + charts ────────────────────────────────────────
    path("api/analytics/", include("analytics.urls")),

    # ── Reports — list + generate + download ─────────────────────────────────
    path("api/reports/", include("analytics.report_urls")),
]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
