from rest_framework import permissions


class CanUploadOrRunJobs(permissions.BasePermission):
    """Admin/finance role, and belongs to an organization.

    Shared by the uploads and tools apps — previously duplicated as
    uploads.views.CanUpload and tools.views.IsAdminOrFinance.
    """
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.organization_id is not None
            and request.user.role in ("admin", "finance")
        )


class IsOrgAdmin(permissions.BasePermission):
    """role == 'admin' and belongs to an organization.

    Replaces DRF's IsAdminUser (which checks is_staff, not this app's
    role field) for org-scoped admin surfaces such as user management.
    """
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.role == "admin"
            and request.user.organization_id is not None
        )
