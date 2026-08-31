from django.contrib import admin, messages

from .models import Organization, Department, OrganizationAPICredential


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")

    def save_model(self, request, obj, form, change):
        is_new = obj.pk is None
        super().save_model(request, obj, form, change)
        if is_new and hasattr(obj, "api_consumer_secret"):
            messages.warning(
                request,
                f"API credential issued — consumer key: {obj.api_consumer_key} — "
                f"secret (shown once, cannot be recovered): {obj.api_consumer_secret}",
            )


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "slug", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "organization__name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(OrganizationAPICredential)
class OrganizationAPICredentialAdmin(admin.ModelAdmin):
    list_display = ("organization", "consumer_key", "is_active", "created_at", "rotated_at", "last_used_at")
    list_filter = ("is_active",)
    search_fields = ("organization__name", "consumer_key")
    readonly_fields = ("consumer_key", "secret_hash", "created_at", "rotated_at", "last_used_at")

    actions = ["rotate_credentials"]

    @admin.action(description="Rotate consumer key/secret for selected organizations")
    def rotate_credentials(self, request, queryset):
        for credential in queryset:
            raw_secret = credential.rotate()
            messages.warning(
                request,
                f"{credential.organization.name} — new consumer key: {credential.consumer_key} — "
                f"secret (shown once): {raw_secret}",
            )
