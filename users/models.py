import uuid

# users/models.py
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        #set username to email prefix if not provided
        extra_fields.setdefault("username", email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        extra_fields.setdefault("is_active", True)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, username=None, password=None,
                          first_name="", last_name="", **extra_fields):
        #accept username but fallback to email
        extra_fields.setdefault("username", username or email)
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("role", "admin")

        if not extra_fields.get("is_staff") or not extra_fields.get("is_superuser"):
            raise ValueError("Superuser must have is_staff=True and is_superuser=True")

        extra_fields["first_name"] = first_name
        extra_fields["last_name"] = last_name
        return self._create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """Custom user model using email as the login identifier.

    Includes the extra fields from the previous AbstractUser customization
    such as `role`, optional `username`, `department`, `phone`, timestamps
    and convenience helpers.
    """

    ROLE_CHOICES = [
        ("admin", "Admin"),
        ("finance", "Finance"),
        ("viewer", "Viewer"),
    ]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    username = models.CharField(max_length=150, unique=True, blank=True, null=True)

    role = models.CharField(
        max_length=10, choices=ROLE_CHOICES, default="viewer",
        help_text="Controls what this user can see and do in the application.")

    # Primary login field
    email = models.EmailField(unique=True, null=False, blank=False)

    # Profile fields
    first_name = models.CharField(max_length=50, blank=True)
    last_name = models.CharField(max_length=50, blank=True)
    department = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email_verified = models.BooleanField(
        default=False,
        help_text="True after the user confirms an email OTP.",
    )

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="members",
        help_text="The partner organization this user belongs to. Data is scoped to this boundary.",
    )

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now, verbose_name='date joined')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    date_joined = models.DateTimeField(auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    class Meta:
        verbose_name = "User"
        verbose_name_plural = "Users"
        ordering = ["email"]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_finance(self) -> bool:
        return self.role == "finance"

    @property
    def is_viewer(self) -> bool:
        return self.role == "viewer"

    @property
    def can_upload(self) -> bool:
        return self.role in ("admin", "finance")

    @property
    def can_run_jobs(self) -> bool:
        return self.role in ("admin", "finance")

    def __str__(self) -> str:
        return f"{self.email} ({self.get_role_display()})"


class EmailOTP(models.Model):
    PURPOSE_EMAIL_VERIFICATION = "email_verification"
    PURPOSE_PASSWORD_RESET = "password_reset"

    PURPOSE_CHOICES = [
        (PURPOSE_EMAIL_VERIFICATION, "Email Verification"),
        (PURPOSE_PASSWORD_RESET, "Password Reset"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="email_otps",
    )
    purpose = models.CharField(max_length=30, choices=PURPOSE_CHOICES)
    code_hash = models.CharField(max_length=128)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "purpose", "consumed_at"]),
            models.Index(fields=["expires_at"]),
        ]

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    def __str__(self) -> str:
        return f"{self.user.email} {self.purpose} OTP"
