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
    # Supports both trailing-slash and no-slash (APPEND_SLASH=False)
    # path("api/auth/login",    KNTokenObtainPairView.as_view(), name="token_obtain_pair"),
    # path("api/auth/login/",   KNTokenObtainPairView.as_view(), name="token_obtain_pair_slash"),
    # path("api/auth/login",    LoginView.as_view(),                 name="auth-login"),
    # path("api/auth/login/",   LoginView.as_view(),                 name="auth-login-slash"),
    # path("api/auth/refresh",  TokenRefreshView.as_view(),      name="token_refresh"),
    # path("api/auth/refresh/", TokenRefreshView.as_view(),      name="token_refresh_slash"),
    # path("api/auth/verify",   TokenVerifyView.as_view(),       name="token_verify"),
    # path("api/auth/verify/",  TokenVerifyView.as_view(),       name="token_verify_slash"),

    # signup, me, logout — handled in users/auth_urls.py
    path("api/auth/", include("users.auth_urls")),

    # ── Users — profile + admin user management ───────────────────────────────
    path("api/users/", include("users.urls")),

    # ── Organizations — org + department administration ──────────────────────
    # organizations/urls.py's own patterns already start with "organizations/"
    # and "departments/", so this mounts at the bare api/ root.
    path("api/", include("organizations.urls")),

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
