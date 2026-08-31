import hashlib
import secrets
import uuid

from django.db import models
from django.utils import timezone
from django.utils.text import slugify
from .managers import (
    OrganizationManager,
    DepartmentManager,
)


def generate_consumer_key() -> str:
    return f"ck_{secrets.token_urlsafe(24)}"


def generate_consumer_secret() -> str:
    return secrets.token_urlsafe(48)


def hash_secret(raw_secret: str) -> str:
    return hashlib.sha256(raw_secret.encode()).hexdigest()

# class Organization(models.Model):
#     """
#     A partner organization / tenant. Deliberately minimal — this is a
#     scoping boundary for data isolation, not a billing/plan SaaS entity.
#     """
#     public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
#     name = models.CharField(max_length=150, unique=True)
#     slug = models.SlugField(max_length=160, unique=True, blank=True)
#     is_active = models.BooleanField(default=True)
#     created_at = models.DateTimeField(auto_now_add=True)
#     updated_at = models.DateTimeField(auto_now=True)


#     class Meta:
#         ordering = ["name"]

#     def save(self, *args, **kwargs):
#         if not self.slug:
#             self.slug = slugify(self.name)
#         super().save(*args, **kwargs)

#     def __str__(self):
#         return self.name

# class Department(models.Model):
#     public_id = models.UUIDField(
#         default = uuid.uuid4,
#         unique = True,
#         editable = False,
#         db_index =True,
#     )
#     organization = models.ForeignKey(
#         Organization,
#         on_delete = models.CASCADE,
#         related_name = "departments",
#     )
#     name = models.CharField(max_length = 100)
#     slug = models.SlugField(max_length = 120, unique = True, blank = True)
#     is_active = models.BooleanField(default = True)
#     created_at = models.DateTimeField(auto_now_add = True)
#     updated_at = models.DateTimeField(auto_now = True)

class Organization(models.Model):
    public_id = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True,
    )

    name = models.CharField(
        max_length=150,
        unique=True,
    )

    slug = models.SlugField(
        max_length=160,
        unique=True,
        blank=True,
    )

    is_active = models.BooleanField(
        default=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    objects = OrganizationManager()

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)

        super().save(*args, **kwargs)

    def __str__(self):
        return self.name



class Department(models.Model):
    public_id = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True,
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="departments",
    )

    name = models.CharField(
        max_length=100,
    )

    slug = models.SlugField(
        max_length=120,
        blank=True,
    )

    is_active = models.BooleanField(
        default=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    objects = DepartmentManager()

    class Meta:
        ordering = [
            "organization__name",
            "name",
        ]

        constraints = [
            models.UniqueConstraint(
                fields=[
                    "organization",
                    "name",
                ],
                name="unique_department_name_per_organization",
            )
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.organization.name} - {self.name}"


class OrganizationAPICredential(models.Model):
    """
    Consumer key / secret pair used to authenticate machine-to-machine (M2M)
    API requests on behalf of an Organization — a separate auth path from
    the JWT user auth the frontend uses, for third-party integrations.

    Only the key and a hash of the secret are ever persisted. The raw
    secret is returned once, at issue/rotation time, and never again.
    """

    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="api_credential",
    )

    consumer_key = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        editable=False,
    )

    secret_hash = models.CharField(
        max_length=128,
        editable=False,
    )

    is_active = models.BooleanField(
        default=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    rotated_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    last_used_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["organization__name"]

    def set_secret(self, raw_secret: str) -> None:
        self.secret_hash = hash_secret(raw_secret)

    def verify_secret(self, raw_secret: str) -> bool:
        return secrets.compare_digest(self.secret_hash, hash_secret(raw_secret))

    @classmethod
    def issue_for(cls, organization: Organization) -> tuple["OrganizationAPICredential", str]:
        """Create a fresh credential pair for an org. Returns (instance, raw_secret)."""
        raw_secret = generate_consumer_secret()
        instance = cls(organization=organization, consumer_key=generate_consumer_key())
        instance.set_secret(raw_secret)
        instance.save()
        return instance, raw_secret

    def rotate(self) -> str:
        """Invalidate the old key/secret and issue a new pair. Returns raw_secret."""
        raw_secret = generate_consumer_secret()
        self.consumer_key = generate_consumer_key()
        self.set_secret(raw_secret)
        self.rotated_at = timezone.now()
        self.save(update_fields=["consumer_key", "secret_hash", "rotated_at"])
        return raw_secret

    def touch(self) -> None:
        self.last_used_at = timezone.now()
        self.save(update_fields=["last_used_at"])

    def __str__(self):
        return f"Credential<{self.organization.name}>"