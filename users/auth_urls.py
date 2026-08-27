from django.urls import path

from .views import (
    login,
    verify_login_otp,
    logout,
    register,
    profile,
    refresh_token,
    request_email_otp,
    verify_email_otp,
    password_reset_request,
    password_reset_confirm,
)

urlpatterns = [
    path("signup/", register, name="auth-signup"),

    path("login/", login, name="auth-login"),
    path("login/verify-otp/", verify_login_otp, name="auth-login-verify-otp"),

    path("refresh/", refresh_token, name="auth-refresh"),

    path("logout/", logout, name="auth-logout"),

    path("me/", profile, name="auth-me"),

    path("email-otp/request/", request_email_otp, name="auth-email-otp-request"),
    path("email-otp/verify/", verify_email_otp, name="auth-email-otp-verify"),
    path("password-reset/request/", password_reset_request, name="auth-password-reset-request"),
    path("password-reset/confirm/", password_reset_confirm, name="auth-password-reset-confirm"),
]
