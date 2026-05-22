# users/admin.py
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """
    Extends Django's built-in UserAdmin to expose our custom fields
    (role, department, phone) in the correct sections.
    """

    # ── List view ─────────────────────────────────────────────────────────────
    list_display  = ("username", "email", "role", "department", "is_active", "is_staff")
    list_filter   = ("role", "is_active", "is_staff")
    search_fields = ("username", "email", "first_name", "last_name", "department")
    ordering      = ("username",)

    # ── Detail / edit view ────────────────────────────────────────────────────
    # Reuse all of BaseUserAdmin's fieldsets and append our custom section.
    fieldsets = BaseUserAdmin.fieldsets + (
        (_("Profile & Role"), {
            "fields": ("role", "department", "phone"),
        }),
    )

    # ── Add-user form ─────────────────────────────────────────────────────────
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        (_("Profile & Role"), {
            "fields": ("email", "role", "department", "phone"),
        }),
    )

    readonly_fields = ("created_at", "updated_at")