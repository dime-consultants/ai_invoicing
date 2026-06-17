from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()


class RefreshTokenViewTests(TestCase):
    """Regression tests for /api/auth/refresh/.

    A bad/expired/blacklisted refresh cookie must return 401 (clean
    "session expired"), never a 500 from an uncaught simplejwt TokenError.
    """

    def setUp(self):
        self.url = reverse("auth-refresh")
        self.user = User.objects.create_user(
            email="refresh@example.com",
            password="pw-12345!",
            first_name="Ref",
            last_name="Resh",
        )
        # Return the 500 response instead of re-raising, so an unhandled
        # TokenError surfaces as a clean "500 != 401" assertion failure.
        self.client.raise_request_exception = False

    def _refresh(self, token=None):
        if token is not None:
            self.client.cookies["refresh_token"] = token
        return self.client.post(self.url)

    def test_invalid_refresh_token_returns_401(self):
        resp = self._refresh("not.a.valid.jwt")
        self.assertEqual(resp.status_code, 401)

    def test_blacklisted_refresh_token_returns_401(self):
        token = RefreshToken.for_user(self.user)
        token.blacklist()  # simulate a rotated/replayed token
        resp = self._refresh(str(token))
        self.assertEqual(resp.status_code, 401)

    def test_missing_refresh_cookie_returns_401(self):
        resp = self._refresh(None)
        self.assertEqual(resp.status_code, 401)

    def test_valid_refresh_token_returns_200(self):
        token = str(RefreshToken.for_user(self.user))
        resp = self._refresh(token)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("token", resp.json())
