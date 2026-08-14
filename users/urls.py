# users/urls.py — mounted at /api/users/
from django.urls import path
from .views import (
    ProfileView,
    ChangePasswordView,
    UserListView,
    UserDetailView,
    UserDepartmentsView,
    AdminResetPasswordView,
    LoginView,
    LogoutView,
)

urlpatterns = [
    path("login",   LoginView.as_view(), name="user-login"),
    path("login/",  LoginView.as_view(), name="user-login-slash"),
    path("logout",  LogoutView.as_view(), name="user-logout"),
    path("logout/", LogoutView.as_view(), name="user-logout-slash"),
    # ── Own profile ───────────────────────────────────────────────────────────
    # GET   /api/users/me      current user profile
    # PATCH /api/users/me      update name, dept, phone
    path("me",                  ProfileView.as_view(),        name="user-profile"),
    path("me/",                 ProfileView.as_view(),        name="user-profile-slash"),

    # POST  /api/users/me/change-password
    path("me/change-password",  ChangePasswordView.as_view(), name="user-change-password"),
    path("me/change-password/", ChangePasswordView.as_view(), name="user-change-password-slash"),

    # ── Admin user management ─────────────────────────────────────────────────
    # GET  /api/users      list all users
    # POST /api/users      create user (admin only)
    path("",            UserListView.as_view(),   name="user-list"),

    # GET /api/users/departments/   distinct department values in use (org-scoped)
    path("departments/", UserDepartmentsView.as_view(), name="user-departments"),

    # GET    /api/users/<id>   user detail
    # PUT    /api/users/<id>   update user
    # DELETE /api/users/<id>   delete user
    path("<int:pk>",    UserDetailView.as_view(), name="user-detail"),
    path("<int:pk>/",   UserDetailView.as_view(), name="user-detail-slash"),

    # POST /api/users/<id>/reset-password/   admin resets a user's password
    path("<int:pk>/reset-password/", AdminResetPasswordView.as_view(), name="user-reset-password"),
]
