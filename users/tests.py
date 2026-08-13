from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from organizations.models import Organization

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


class CreateSuperuserTests(TestCase):
    """
    Regression test for a confirmed NameError bug: create_superuser
    referenced first_name/last_name as free variables that were never
    parameters, so createsuperuser (interactive or scripted) always crashed.
    """

    def test_create_superuser_does_not_raise(self):
        user = User.objects.create_superuser(email="root@example.com", password="pw12345!")
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertEqual(user.role, "admin")
        self.assertEqual(user.first_name, "")

    def test_create_superuser_accepts_explicit_names(self):
        user = User.objects.create_superuser(
            email="root2@example.com", password="pw12345!",
            first_name="Root", last_name="User",
        )
        self.assertEqual(user.first_name, "Root")
        self.assertEqual(user.last_name, "User")


class UserAdminOrgIsolationTests(TestCase):
    """
    UserListView/UserDetailView were previously gated on Django's is_staff
    (not this app's role field) and queried User.objects.all() with no org
    boundary at all — any staff user could see/edit/delete any user
    platform-wide. Phase 3 org-scopes both and switches the permission
    check to role=="admin" within an org.
    """

    def setUp(self):
        self.org_a = Organization.objects.create(name="Org A")
        self.org_b = Organization.objects.create(name="Org B")
        self.admin_a = User.objects.create_user(
            email="admin_a@example.com", password="pw12345!",
            first_name="Ad", last_name="A", role="admin", organization=self.org_a,
        )
        self.viewer_a = User.objects.create_user(
            email="viewer_a@example.com", password="pw12345!",
            first_name="V", last_name="A", role="viewer", organization=self.org_a,
        )
        self.admin_b = User.objects.create_user(
            email="admin_b@example.com", password="pw12345!",
            first_name="Ad", last_name="B", role="admin", organization=self.org_b,
        )
        self.client = APIClient()

    def test_org_admin_role_based_not_staff_based_grants_access(self):
        """admin_a has role='admin' but is_staff=False (normal signup path)
        — must still be able to use the admin user-management endpoint."""
        self.assertFalse(self.admin_a.is_staff)
        self.client.force_authenticate(user=self.admin_a)
        resp = self.client.get("/api/users/")
        self.assertEqual(resp.status_code, 200)

    def test_non_admin_role_is_rejected(self):
        self.client.force_authenticate(user=self.viewer_a)
        resp = self.client.get("/api/users/")
        self.assertEqual(resp.status_code, 403)

    def test_admin_only_sees_own_org_users(self):
        self.client.force_authenticate(user=self.admin_a)
        resp = self.client.get("/api/users/")
        self.assertEqual(resp.status_code, 200)
        emails = [u["email"] for u in resp.json()]
        self.assertIn(self.viewer_a.email, emails)
        self.assertNotIn(self.admin_b.email, emails)

    def test_cross_org_admin_cannot_view_or_edit_user_detail(self):
        self.client.force_authenticate(user=self.admin_b)
        resp = self.client.get(f"/api/users/{self.viewer_a.pk}/")
        self.assertEqual(resp.status_code, 404)

    def test_created_user_is_pinned_to_creating_admins_org_even_if_spoofed(self):
        """A malicious/buggy client sending organization=org_b in the create
        payload must not be able to place a new user into another org."""
        self.client.force_authenticate(user=self.admin_a)
        resp = self.client.post("/api/users/", {
            "email": "newhire@example.com",
            "first_name": "New", "last_name": "Hire",
            "role": "viewer",
            "organization": self.org_b.pk,
        }, format="json")
        self.assertEqual(resp.status_code, 201)
        created = User.objects.get(email="newhire@example.com")
        self.assertEqual(created.organization_id, self.org_a.pk)
