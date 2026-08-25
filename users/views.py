from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
import logging
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.exceptions import TokenError, InvalidToken

from .models import User
from .serializers import (
    RegisterSerializer,
    LoginSerializer,
    UserSerializer,
    UserProfileSerializer,
    UserAdminSerializer,
    ChangePasswordSerializer,
    EmailOTPRequestSerializer,
    EmailOTPVerifySerializer,
    LoginOTPVerifySerializer,
    PasswordResetRequestSerializer,
    PasswordResetConfirmSerializer,
)
from .permissions import IsOrgAdmin
from .decorators import org_member_required, org_admin_required, org_admin_or_finance_required
from .models import EmailOTP
from .services import OTPService


logger = logging.getLogger(__name__)


def _stage_otp_message(default: str, purpose: str) -> str:
    if getattr(settings, "APP_ENV", "").lower() == "stage":
        return f"Use staging {purpose} code 00000."
    return default


def _auth_response_for_user(user, *, message: str) -> Response:
    refresh = RefreshToken.for_user(user)
    access_token = str(refresh.access_token)

    response = Response({
        "message": message,
        "token": access_token,
        "user": UserSerializer(user).data,
    })
    response.set_cookie(
        key="refresh_token",
        value=str(refresh),
        httponly=True,
        secure=not settings.DEBUG,
        samesite="Lax",
        max_age=7 * 24 * 60 * 60,
    )
    return response


# ── Register ──────────────────────────────────────────────────────────────────

class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = serializer.save()
        OTPService.send(user, purpose=EmailOTP.PURPOSE_EMAIL_VERIFICATION)
        logger.info("Registered user=%s email=%s", user.pk, user.email)

        return Response(
            {
                "message": _stage_otp_message(
                    "Account created successfully. Check your email for the verification code.",
                    "verification",
                ),
                "requires_email_verification": True,
                "user": UserSerializer(user).data,
            },
            status=status.HTTP_201_CREATED,
        )


# ── Login ─────────────────────────────────────────────────────────────────────

class LoginView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = serializer.validated_data["user"]
        if not user.email_verified:
            OTPService.send(user, purpose=EmailOTP.PURPOSE_EMAIL_VERIFICATION)
            logger.info("Login blocked pending email verification user=%s", user.pk)
            return Response(
                {
                    "detail": "Please verify your email address. We sent you a new code.",
                    "requires_email_verification": True,
                    "email": user.email,
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        OTPService.send(user, purpose=EmailOTP.PURPOSE_LOGIN)
        logger.info("Login OTP sent user=%s", user.pk)
        return Response({
            "message": _stage_otp_message("We sent a login code to your email.", "login"),
            "requires_otp": True,
            "email": user.email,
        })


class VerifyLoginOTPView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = LoginOTPVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = User.objects.filter(email__iexact=serializer.validated_data["email"]).first()
        if not user or not user.is_active:
            return Response({"detail": "Invalid code."}, status=status.HTTP_400_BAD_REQUEST)

        ok, message = OTPService.verify(
            user,
            purpose=EmailOTP.PURPOSE_LOGIN,
            code=serializer.validated_data["code"],
        )
        if not ok:
            return Response({"detail": message}, status=status.HTTP_400_BAD_REQUEST)

        logger.info("Login OTP verified user=%s", user.pk)
        return _auth_response_for_user(user, message=f"Welcome back {user.username}")


class RequestEmailOTPView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = EmailOTPRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data.get("email") or getattr(request.user, "email", "")
        user = None
        if request.user.is_authenticated and not email:
            user = request.user
        elif email:
            user = User.objects.filter(email__iexact=email).first()

        if user:
            OTPService.send(user, purpose=EmailOTP.PURPOSE_EMAIL_VERIFICATION)
            logger.info("Email verification OTP requested user=%s", user.pk)

        return Response({
            "message": _stage_otp_message(
                "If that account exists, a verification code has been sent.",
                "verification",
            ),
        })


class VerifyEmailOTPView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = EmailOTPVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data.get("email") or getattr(request.user, "email", "")
        user = None
        if request.user.is_authenticated and not email:
            user = request.user
        elif email:
            user = User.objects.filter(email__iexact=email).first()

        if not user:
            return Response({"detail": "Invalid code."}, status=status.HTTP_400_BAD_REQUEST)

        ok, message = OTPService.verify(
            user,
            purpose=EmailOTP.PURPOSE_EMAIL_VERIFICATION,
            code=serializer.validated_data["code"],
        )
        if not ok:
            return Response({"detail": message}, status=status.HTTP_400_BAD_REQUEST)

        user.email_verified = True
        user.save(update_fields=["email_verified"])
        return Response({"message": "Email verified successfully."})


class PasswordResetRequestView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = User.objects.filter(email__iexact=serializer.validated_data["email"]).first()
        if user:
            OTPService.send(user, purpose=EmailOTP.PURPOSE_PASSWORD_RESET)
            logger.info("Password reset OTP requested user=%s", user.pk)

        return Response({
            "message": _stage_otp_message(
                "If that account exists, a reset code has been sent.",
                "reset",
            ),
        })


class PasswordResetConfirmView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = User.objects.filter(email__iexact=serializer.validated_data["email"]).first()
        if not user:
            return Response({"detail": "Invalid code."}, status=status.HTTP_400_BAD_REQUEST)

        ok, message = OTPService.verify(
            user,
            purpose=EmailOTP.PURPOSE_PASSWORD_RESET,
            code=serializer.validated_data["code"],
        )
        if not ok:
            return Response({"detail": message}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(serializer.validated_data["password"])
        user.email_verified = True
        user.save(update_fields=["password", "email_verified"])
        logger.info("Password reset completed user=%s", user.pk)
        return Response({"message": "Password reset successfully. You can now log in."})


# ── Refresh ───────────────────────────────────────────────────────────────────

class RefreshTokenView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        refresh_token = request.COOKIES.get("refresh_token")

        if not refresh_token:
            return Response(
                {"detail": "No refresh token"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        serializer = TokenRefreshSerializer(
            data={"refresh": refresh_token}
        )

        # simplejwt raises a bare TokenError (not a DRF APIException) for an
        # expired/blacklisted/malformed token; left uncaught it becomes a 500.
        # Convert it to InvalidToken (401) like simplejwt's own TokenRefreshView.
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as exc:
            raise InvalidToken(exc.args[0])

        access = serializer.validated_data["access"]

        response = Response({
            "token": access,
        })

        # rotated refresh token support
        if "refresh" in serializer.validated_data:
            response.set_cookie(
                key="refresh_token",
                value=serializer.validated_data["refresh"],
                httponly=True,
                secure=not settings.DEBUG,
                samesite="Lax",
                max_age=7 * 24 * 60 * 60,
            )

        return response


# ── Logout ────────────────────────────────────────────────────────────────────

class LogoutView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        response = Response({
            "message": "Logged out successfully."
        })

        response.delete_cookie("refresh_token")

        return response


# ── Profile ───────────────────────────────────────────────────────────────────

class ProfileView(generics.RetrieveUpdateAPIView):
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.request.method in ("PUT", "PATCH"):
            return UserProfileSerializer
        return UserSerializer

    def get_object(self):
        return self.request.user


# ── Change Password ───────────────────────────────────────────────────────────

class ChangePasswordView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(
            data=request.data,
            context={"request": request},
        )

        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response({
            "message": "Password updated successfully."
        })

# ── Admin User Management ───────────────────────────────────────────────────
# Scoped to the requesting admin's own organization — an org-admin manages
# their own org's roster only, never the whole platform.

class UserListView(generics.ListCreateAPIView):
    permission_classes = [IsOrgAdmin]

    def get_queryset(self):
        return User.objects.filter(organization=self.request.user.organization)

    def get_serializer_class(self):
        return UserAdminSerializer if self.request.method == "POST" else UserSerializer

    def perform_create(self, serializer):
        serializer.save(organization=self.request.user.organization)


class UserDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsOrgAdmin]

    def get_queryset(self):
        return User.objects.filter(organization=self.request.user.organization)

    def get_serializer_class(self):
        return UserAdminSerializer if self.request.method in ("PUT", "PATCH") else UserSerializer

    def perform_update(self, serializer):
        serializer.save(organization=self.request.user.organization)


class UserDepartmentsView(APIView):
    """GET /api/users/departments/ — distinct department values in use, org-scoped."""
    permission_classes = [IsOrgAdmin]

    def get(self, request):
        names = (
            User.objects.filter(organization=request.user.organization)
            .exclude(department="")
            .values_list("department", flat=True)
            .distinct()
            .order_by("department")
        )
        return Response(list(names))


class AdminResetPasswordView(APIView):
    """POST /api/users/<pk>/reset-password/ — admin-only, org-scoped."""
    permission_classes = [IsOrgAdmin]

    def post(self, request, pk):
        try:
            target = User.objects.get(pk=pk, organization=request.user.organization)
        except User.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        new_password = request.data.get("password", "")
        try:
            validate_password(new_password, user=target)
        except DjangoValidationError as exc:
            return Response({"password": exc.messages}, status=status.HTTP_400_BAD_REQUEST)

        target.set_password(new_password)
        target.save(update_fields=["password"])
        return Response({"message": "Password reset successfully."})
