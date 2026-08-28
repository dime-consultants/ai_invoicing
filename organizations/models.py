import uuid

from django.db import models
from django.utils.text import slugify
from .managers import (
    OrganizationManager,
    DepartmentManager,
)

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