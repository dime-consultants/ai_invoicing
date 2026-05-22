# config/urls.py
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path("admin/", admin.site.urls),

    # Users — auth + profile + admin user management
    path("api/users/", include("users.urls")),

    # Tools — registry + call log + direct run
    path("api/tools/", include("tools.urls")),

    # AI Engine — jobs + insights  (added next)
    # path("api/ai/", include("ai_engine.urls")),

    # Uploads — batch + file ingestion  (added next)
    # path("api/uploads/", include("uploads.urls")),

    # Chat — conversations + WebSocket  (added next)
    # path("api/chat/", include("chat.urls")),
    # path("chat/", include("chat.urls")),
]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)