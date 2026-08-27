from django.test import SimpleTestCase
from django.urls import reverse, resolve

from .views import (
    register,
    login,
    logout,
    profile,
    refresh_token,
)


class AuthUrlsTest(SimpleTestCase):
    def test_signup_url_resolves(self):
        path = reverse("auth-signup")
        self.assertEqual(path, "/api/auth/signup/")
        match = resolve(path)
        self.assertEqual(match.func, register)

    def test_login_url_resolves(self):
        path = reverse("auth-login")
        self.assertEqual(path, "/api/auth/login/")
        match = resolve(path)
        self.assertEqual(match.func, login)

    def test_refresh_url_resolves(self):
        path = reverse("auth-refresh")
        self.assertEqual(path, "/api/auth/refresh/")
        match = resolve(path)
        self.assertEqual(match.func, refresh_token)

    def test_logout_url_resolves(self):
        path = reverse("auth-logout")
        self.assertEqual(path, "/api/auth/logout/")
        match = resolve(path)
        self.assertEqual(match.func, logout)

    def test_me_url_resolves(self):
        path = reverse("auth-me")
        self.assertEqual(path, "/api/auth/me/")
        match = resolve(path)
        self.assertEqual(match.func, profile)
