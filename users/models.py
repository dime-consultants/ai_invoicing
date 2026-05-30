# users/models.py
from django.contrib.auth.models import AbstractUser
from django.db import models


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
        unique = True,
        blank = True,
        null = True,
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

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []


    class Meta:
        verbose_name        = "User"
        verbose_name_plural = "Users"
        ordering            = ["username"]

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
        return f"{self.username} ({self.get_role_display()})"