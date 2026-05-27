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
    # POST /api/auth/signup/       create account (public)
    path("signup/", RegisterView.as_view(),  name="user-register"),

    # POST /api/auth/login/          session login (public)
    path("login/",    LoginView.as_view(),     name="user-login"),

    # POST /api/auth/logout/         clear session (authenticated)
    path("logout/",   LogoutView.as_view(),    name="user-logout"),

    # ── Own profile ───────────────────────────────────────────────────────────
    # GET   /api/users/me/            current user profile  (also served at /api/auth/me/)
    # PATCH /api/users/me/            update name, dept, phone
    path("me/",       ProfileView.as_view(),   name="user-profile"),

    # POST  /api/users/me/change-password/
    path("me/change-password/", ChangePasswordView.as_view(), name="user-change-password"),

    # ── Admin user management ─────────────────────────────────────────────────
    # GET   /api/users/               list all users (admin only)
    # POST  /api/users/               create user (admin only)
    path("",          UserListView.as_view(),  name="user-list"),

    # GET   /api/users/<id>/          user detail (admin only)
    # PUT   /api/users/<id>/          update user (admin only)
    # DELETE /api/users/<id>/         delete user (admin only)
    path("<int:pk>/", UserDetailView.as_view(), name="user-detail"),
]