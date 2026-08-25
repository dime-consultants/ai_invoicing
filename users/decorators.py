from functools import wraps

from django.core.exceptions import PermissionDenied


def _is_authenticated_org_user(user):
    return(
        user
        and user.is_authenticated
        and user.organization_id is not None
    )

def org_member_required(view_func, *roles):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not (
            _is_authenticated_org_user(request.user)
            
        ):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return wrapper


def org_admin_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = request.user

        if not (
            _is_authenticated_org_user(user)
            and user.role == "admin"
        ):
            raise PermissionDenied  (
                "Organization adminstrator  privileges are required"
            )

        return view_func(request, *args, **kwargs)
    return wrapper
        

   

def org_finance_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = request.user

        if not (
            _is_authenticated_org_user(user)
            and user.role == "finance"
        ):
            raise PermissionDenied  (
                "Organization adminstrator  privileges are required"
            )

        return view_func(request, *args, **kwargs)
    return wrapper
        


def org_admin_or_finance_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = request.user

        if not (
            _is_authenticated_org_user(user)
            and user.role in {"admin", "finance"}
        ):
            raise PermissionDenied  (
                "Admin or finance  privileges are required"
            )

        return view_func(request, *args, **kwargs)
    return wrapper
        


def can_upload_or_run_jobs_required(view_func):
    """Mirrors CanUploadOrRunJobs in permissions.py."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = request.user

        if not (
            _is_authenticated_org_user(user)
            and user.role == {"admin", "finance"}
        ):
            raise PermissionDenied  (
                "Admin or finance   privileges are required"
            )

        return view_func(request, *args, **kwargs)
    return wrapper
        


def org_viewer_required(view_func):
    # Mirrors IsOrgViewer in permissions.py, which checks role == "finance"
    # rather than a distinct "viewer" role — kept consistent with it here.
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = request.user

        if not (
            _is_authenticated_org_user(user)
            and user.role == "viewer"
        ):
            raise PermissionDenied  (
                "Viewer  privileges are required"
            )

        return view_func(request, *args, **kwargs)
    return wrapper
        

