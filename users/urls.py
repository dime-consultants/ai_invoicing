# users/urls.py — mounted at /api/users/
from django.urls import path
from .views import (
    profile,
    change_password,
    user_list_create,
    user_detail,
    user_departments,
    admin_reset_password,
    login,
    logout,
)

urlpatterns = [
    path("login",   login, name="user-login"),
    path("login/",  login, name="user-login-slash"),
    path("logout",  logout, name="user-logout"),
    path("logout/", logout, name="user-logout-slash"),
    # ── Own profile ───────────────────────────────────────────────────────────
    # GET   /api/users/me      current user profile
    # PATCH /api/users/me      update name, dept, phone
    path("me",                  profile,        name="user-profile"),
    path("me/",                 profile,        name="user-profile-slash"),

    # POST  /api/users/me/change-password
    path("me/change-password",  change_password, name="user-change-password"),
    path("me/change-password/", change_password, name="user-change-password-slash"),

    # ── Admin user management ─────────────────────────────────────────────────
    # GET  /api/users      list all users
    # POST /api/users      create user (admin only)
    path("",            user_list_create,   name="user-list"),

    # GET /api/users/departments/   distinct department values in use (org-scoped)
    path("departments/", user_departments, name="user-departments"),

    # GET    /api/users/<id>   user detail
    # PUT    /api/users/<id>   update user
    # DELETE /api/users/<id>   delete user
    path("<int:pk>",    user_detail, name="user-detail"),
    path("<int:pk>/",   user_detail, name="user-detail-slash"),

    # POST /api/users/<id>/reset-password/   admin resets a user's password
    path("<int:pk>/reset-password/", admin_reset_password, name="user-reset-password"),
]
