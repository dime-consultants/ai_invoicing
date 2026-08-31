from functools import wraps

from rest_framework.exceptions import AuthenticationFailed, PermissionDenied

from .tokens import decode_org_access_token


def org_admin_or_api_credential_required(view_func):
    """
    Accepts EITHER:
      - an authenticated org-admin user (existing JWT frontend auth — same
        rule as users.decorators.org_admin_required), OR
      - a valid M2M organization access token (see organizations/tokens.py),
        presented via the X-Org-Token header and obtained from
        POST /api/organizations/token/.

    Views wrapped with this MUST also be decorated with
    @permission_classes([AllowAny]) — DRF's global IsAuthenticated default
    would otherwise reject the M2M case before this ever runs, since an
    M2M-only request has no user JWT on the Authorization header at all.

    Attaches request.organization either way, so the view body can use it
    consistently regardless of which auth path was used.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = request.user
        if (
            user
            and user.is_authenticated
            and getattr(user, "role", None) == "admin"
            and user.organization_id is not None
        ):
            request.organization = user.organization
            return view_func(request, *args, **kwargs)

        org_token = request.headers.get("X-Org-Token")
        if not org_token:
            raise AuthenticationFailed(
                "Provide an authenticated admin session or an X-Org-Token API credential."
            )

        organization, credential = decode_org_access_token(org_token)
        if organization is None:
            raise AuthenticationFailed("Invalid or expired API token.")
        if not organization.is_active:
            raise PermissionDenied("Organization is inactive.")

        credential.touch()
        request.organization = organization
        request.api_credential = credential
        return view_func(request, *args, **kwargs)

    return wrapper
