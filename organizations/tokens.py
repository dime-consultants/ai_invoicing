import datetime

from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken

from .models import Organization, OrganizationAPICredential

# Distinguishes an M2M organization token from a normal user access token —
# both use SimpleJWT's AccessToken under the hood (same signing key/algorithm
# as the rest of the app), but this one carries no user_id claim at all, so
# it must never be sent as a standard Authorization: Bearer header (DRF's
# global JWTAuthentication would try, and fail, to resolve it to a User).
# It travels via the separate X-Org-Token header instead.
ORG_TOKEN_SCOPE = "organization_api"
ORG_TOKEN_LIFETIME = datetime.timedelta(minutes=30)


def issue_org_access_token(credential: OrganizationAPICredential) -> str:
    token = AccessToken()
    token.set_exp(lifetime=ORG_TOKEN_LIFETIME)
    token["scope"] = ORG_TOKEN_SCOPE
    token["org_id"] = credential.organization_id
    token["credential_id"] = credential.pk
    return str(token)


def decode_org_access_token(raw_token: str):
    """Returns (organization, credential) for a valid token, else (None, None)."""
    try:
        token = AccessToken(raw_token)
    except TokenError:
        return None, None

    if token.get("scope") != ORG_TOKEN_SCOPE:
        return None, None

    credential_id = token.get("credential_id")
    org_id = token.get("org_id")
    if not credential_id or not org_id:
        return None, None

    try:
        credential = OrganizationAPICredential.objects.select_related("organization").get(
            pk=credential_id, organization_id=org_id, is_active=True,
        )
    except OrganizationAPICredential.DoesNotExist:
        return None, None

    return credential.organization, credential
