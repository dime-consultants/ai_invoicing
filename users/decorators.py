from functools import wraps

from django.core.exceptions import PermissionDenied


def _is_authenticated_org_user(user):
    return bool(user and user.is_authenticated and user.organization_id is not None)


def authenticated_required(view_func):
    """Just needs to be logged in — no org/role gate."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not (request.user and request.user.is_authenticated):
            raise PermissionDenied("Authentication is required.")
        return view_func(request, *args, **kwargs)
    return wrapper


def org_member_required(view_func):
    """Any role, but must belong to an organization."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not _is_authenticated_org_user(request.user):
            raise PermissionDenied("Organization membership is required.")
        return view_func(request, *args, **kwargs)
    return wrapper


def _roles_required(*roles, message):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            user = request.user
            if not (_is_authenticated_org_user(user) and user.role in roles):
                raise PermissionDenied(message)
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


def org_admin_required(view_func):
    return _roles_required("admin", message="Organization administrator privileges are required.")(view_func)


def org_finance_required(view_func):
    return _roles_required("finance", message="Organization finance privileges are required.")(view_func)


def org_admin_or_finance_required(view_func):
    return _roles_required("admin", "finance", message="Admin or finance privileges are required.")(view_func)


def can_upload_or_run_jobs_required(view_func):
    """Mirrors CanUploadOrRunJobs in permissions.py."""
    return _roles_required("admin", "finance", message="Admin or finance privileges are required.")(view_func)


def org_viewer_required(view_func):
    # Mirrors IsOrgViewer's intent in permissions.py — role == "viewer" is
    # the correct check (users/models.py ROLE_CHOICES confirms "viewer" is
    # a real role value); IsOrgViewer itself checks "finance", which is the
    # actual bug there, not here.
    return _roles_required("viewer", message="Viewer privileges are required.")(view_func)
