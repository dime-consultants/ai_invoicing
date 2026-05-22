# users/urls.py
from django.urls import path
from .views import (
    RegisterView,
    LoginView,
    LogoutView,
    ProfileView,
    ChangePasswordView,
    UserListView,
    UserDetailView,
)

urlpatterns = [
    # ── Auth ─────────────────────────────────────────────────────────────────
    # POST /api/users/register/       create account (public)
    path("register/", RegisterView.as_view(),  name="user-register"),

    # POST /api/users/login/          session login (public)
    path("login/",    LoginView.as_view(),     name="user-login"),

    # POST /api/users/logout/         clear session (authenticated)
    path("logout/",   LogoutView.as_view(),    name="user-logout"),

    # ── Own profile ───────────────────────────────────────────────────────────
    # GET   /api/users/me/            current user profile
    # PATCH /api/users/me/            update name, dept, phone
    path("me/",       ProfileView.as_view(),   name="user-profile"),

    # POST  /api/users/me/change-password/
    path("me/change-password/", ChangePasswordView.as_view(), name="user-change-password"),

    # ── Admin user management ─────────────────────────────────────────────────
    # GET   /api/users/               list all users (admin only)
    # GET   /api/users/?role=finance
    path("",          UserListView.as_view(),  name="user-list"),

    # GET   /api/users/<id>/          user detail (admin only)
    # PATCH /api/users/<id>/          promote role / deactivate (admin only)
    path("<int:pk>/", UserDetailView.as_view(), name="user-detail"),
]