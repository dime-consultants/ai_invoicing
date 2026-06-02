from django.test import SimpleTestCase
from django.urls import reverse, resolve

from .views import (
    RegisterView,
    LoginView,
    LogoutView,
    ProfileView,
    RefreshTokenView,
)


class AuthUrlsTest(SimpleTestCase):
    def test_signup_url_resolves(self):
        path = reverse("auth-signup")
        self.assertEqual(path, "/api/auth/signup/")
        match = resolve(path)
        self.assertEqual(match.func.view_class, RegisterView)

    def test_login_url_resolves(self):
        path = reverse("auth-login")
        self.assertEqual(path, "/api/auth/login/")
        match = resolve(path)
        self.assertEqual(match.func.view_class, LoginView)

    def test_refresh_url_resolves(self):
        path = reverse("auth-refresh")
        self.assertEqual(path, "/api/auth/refresh/")
        match = resolve(path)
        self.assertEqual(match.func.view_class, RefreshTokenView)

    def test_logout_url_resolves(self):
        path = reverse("auth-logout")
        self.assertEqual(path, "/api/auth/logout/")
        match = resolve(path)
        self.assertEqual(match.func.view_class, LogoutView)

    def test_me_url_resolves(self):
        path = reverse("auth-me")
        self.assertEqual(path, "/api/auth/me/")
        match = resolve(path)
        self.assertEqual(match.func.view_class, ProfileView)
