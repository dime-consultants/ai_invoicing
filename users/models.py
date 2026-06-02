# users/models.py
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    """Custom manager — email is the unique identifier, username is optional."""

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Email is required.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", "admin")

        if not extra_fields.get("is_staff"):
            raise ValueError("Superuser must have is_staff=True.")
        if not extra_fields.get("is_superuser"):
            raise ValueError("Superuser must have is_superuser=True.")

        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    """
    Custom user model.  Drop-in replacement for django.contrib.auth.User.

    Roles
    -----
    admin   — full access: manage users, batches, jobs, tools
    finance — upload files, run jobs, view all results
    viewer  — read-only access to results and insights
    """

    ROLE_CHOICES = [
        ("admin",   "Admin"),
        ("finance", "Finance"),
        ("viewer",  "Viewer"),
    ]

    username = models.CharField(
        max_length=150,
        unique=True,
        blank=True,
        null=True,
    )

    role = models.CharField(
        max_length=10,
        choices=ROLE_CHOICES,
        default="viewer",
        help_text="Controls what this user can see and do in the application.",
    )

    # Make email the login field and enforce uniqueness
    email = models.EmailField(unique=True)

    # Optional profile fields
    department = models.CharField(max_length=100, blank=True)
    phone      = models.CharField(max_length=20,  blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD  = "email"
    REQUIRED_FIELDS = []             # email + password only, no username prompt

    objects = UserManager()          # ← attach custom manager

    class Meta:
        verbose_name        = "User"
        verbose_name_plural = "Users"
        ordering            = ["email"]

    # ── Convenience helpers ───────────────────────────────────────────────────

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
        """Finance and admin users may create batches and upload files."""
        return self.role in ("admin", "finance")

    @property
    def can_run_jobs(self) -> bool:
        """Finance and admin users may trigger AI analysis jobs."""
        return self.role in ("admin", "finance")

    def __str__(self) -> str:
        return f"{self.email} ({self.get_role_display()})"