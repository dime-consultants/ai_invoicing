# users/views.py
from django.contrib.auth import login, logout
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import User
from .serializers import (
    RegisterSerializer,
    LoginSerializer,
    UserSerializer,
    UserProfileSerializer,
    ChangePasswordSerializer,
)


# ── Register ──────────────────────────────────────────────────────────────────

class RegisterView(generics.CreateAPIView):
    """
    POST /api/users/register/

    Public endpoint — no authentication required.
    New users are created with role='viewer'.
    An admin must promote them to 'finance' or 'admin'.

    Request:
        {
            "username":   "jdoe",
            "email":      "jdoe@example.com",
            "first_name": "John",
            "last_name":  "Doe",
            "password":   "strongpassword",
            "password2":  "strongpassword",
            "department": "Finance",   // optional
            "phone":      "+254..."    // optional
        }
    """
    serializer_class   = RegisterSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            {
                "message": "Account created. An admin will activate your access.",
                "user":    UserSerializer(user).data,
            },
            status=status.HTTP_201_CREATED,
        )


# ── Login ─────────────────────────────────────────────────────────────────────

class LoginView(APIView):
    """
    POST /api/users/login/

    Public endpoint. Creates a Django session on success.

    Request:  { "username": "jdoe", "password": "..." }
    Response: { "message": "...", "user": { ... } }
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        login(request, user)
        return Response(
            {
                "message": f"Welcome back, {user.get_full_name() or user.username}.",
                "user":    UserSerializer(user).data,
            }
        )


# ── Logout ────────────────────────────────────────────────────────────────────

class LogoutView(APIView):
    """
    POST /api/users/logout/

    Clears the session. Requires authentication.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        logout(request)
        return Response({"message": "Logged out successfully."})


# ── Profile ───────────────────────────────────────────────────────────────────

class ProfileView(generics.RetrieveUpdateAPIView):
    """
    GET   /api/users/me/   — return the current user's profile
    PATCH /api/users/me/   — update editable fields (name, dept, phone)
    """
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.request.method in ("PUT", "PATCH"):
            return UserProfileSerializer
        return UserSerializer

    def get_object(self):
        return self.request.user


# ── Change Password ───────────────────────────────────────────────────────────

class ChangePasswordView(APIView):
    """
    POST /api/users/me/change-password/

    Request: { "old_password": "...", "new_password": "..." }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"message": "Password updated successfully."})


# ── User list (admin only) ────────────────────────────────────────────────────

class UserListView(generics.ListCreateAPIView):
    """
    GET  /api/users/   — list all users  → { "users": [...] }
    POST /api/users/   — admin: create a new user
    """
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.request.method == "POST":
            return RegisterSerializer
        return UserSerializer

    def get_queryset(self):
        qs = User.objects.order_by("username")
        # Non-admins only see themselves
        if not self.request.user.is_admin:
            qs = qs.filter(pk=self.request.user.pk)
        role = self.request.query_params.get("role")
        if role:
            qs = qs.filter(role=role)
        return qs

    def list(self, request, *args, **kwargs):
        qs = self.get_queryset()
        serializer = UserSerializer(qs, many=True)
        return Response({"users": serializer.data})

    def create(self, request, *args, **kwargs):
        if not request.user.is_admin:
            return Response(
                {"error": {"code": "forbidden", "message": "Only admins can create users.", "details": None}},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class UserDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/users/<id>/   — admin: view any user
    PUT    /api/users/<id>/   — admin: update user
    PATCH  /api/users/<id>/   — admin: partial update
    DELETE /api/users/<id>/   — admin: delete user
    """
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        return UserSerializer

    def get_queryset(self):
        if not self.request.user.is_admin:
            return User.objects.none()
        return User.objects.all()

    def update(self, request, *args, **kwargs):
        if not request.user.is_admin:
            return Response(
                {"error": "Only admins can update user records."},
                status=status.HTTP_403_FORBIDDEN,
            )
        # Allow admin to change role and is_active only
        allowed = {"role", "is_active", "first_name", "last_name", "department", "phone"}
        data = {k: v for k, v in request.data.items() if k in allowed}
        instance = self.get_object()
        serializer = UserProfileSerializer(instance, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        # Handle role separately — not in UserProfileSerializer
        if "role" in data:
            instance.role = data["role"]
        if "is_active" in data:
            instance.is_active = data["is_active"]
        instance.save()
        return Response(UserSerializer(instance).data)