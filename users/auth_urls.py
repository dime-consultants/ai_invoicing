# users/auth_urls.py
# Mounted at /api/auth/ — provides me/ and logout/ shortcuts
from django.urls import path
from .views import ProfileView, LogoutView

urlpatterns = [
    # GET  /api/auth/me/      → current user profile (same as /api/users/me/)
    path("me/",      ProfileView.as_view(), name="auth-me"),
    # POST /api/auth/logout/  → clear session
    path("logout/",  LogoutView.as_view(),  name="auth-logout"),
]
