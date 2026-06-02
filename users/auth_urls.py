from django.urls import path

from .views import (
    LoginView,
    LogoutView,
    RegisterView,
    ProfileView,
    RefreshTokenView,
)

urlpatterns = [
    path("signup/", RegisterView.as_view(), name="auth-signup"),

    path("login/", LoginView.as_view(), name="auth-login"),

    path("refresh/", RefreshTokenView.as_view(), name="auth-refresh"),

    path("logout/", LogoutView.as_view(), name="auth-logout"),

    path("me/", ProfileView.as_view(), name="auth-me"),
]

