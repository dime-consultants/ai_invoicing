from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Organization, OrganizationAPICredential


@receiver(post_save, sender=Organization)
def issue_organization_api_credential(sender, instance, created, **kwargs):
    if not created:
        return
    credential, raw_secret = OrganizationAPICredential.issue_for(instance)
    # Transient only — not persisted, just here so the current
    # request/response (e.g. the admin, or a serializer) can surface the
    # secret exactly once. It cannot be recovered after this.
    instance.api_consumer_key = credential.consumer_key
    instance.api_consumer_secret = raw_secret
