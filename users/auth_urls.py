from django.urls import path

from .views import (
    LoginView,
    LogoutView,
    RegisterView,
    ProfileView,
    RefreshTokenView,
    RequestEmailOTPView,
    VerifyEmailOTPView,
    PasswordResetRequestView,
    PasswordResetConfirmView,
)

urlpatterns = [
    path("signup/", RegisterView.as_view(), name="auth-signup"),

    path("login/", LoginView.as_view(), name="auth-login"),

    path("refresh/", RefreshTokenView.as_view(), name="auth-refresh"),

    path("logout/", LogoutView.as_view(), name="auth-logout"),

    path("me/", ProfileView.as_view(), name="auth-me"),

    path("email-otp/request/", RequestEmailOTPView.as_view(), name="auth-email-otp-request"),
    path("email-otp/verify/", VerifyEmailOTPView.as_view(), name="auth-email-otp-verify"),
    path("password-reset/request/", PasswordResetRequestView.as_view(), name="auth-password-reset-request"),
    path("password-reset/confirm/", PasswordResetConfirmView.as_view(), name="auth-password-reset-confirm"),
]
