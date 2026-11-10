"""Organizations as staff read them: the legal name, never the slug, unless the name cannot be read.

The name is encrypted, and pages that must not break on one unreadable value load plaintext columns
only. So names are read here one organization at a time, and an unreadable one shows its slug.
"""
from apps.orgs.models import Organization
from apps.vault.fields import UndecryptableValue
from apps.vault.vault import VaultSealed


def org_name(org) -> str:
    """The display name of an organization object that is already loaded."""
    try:
        return org.legal_name or org.slug
    except (UndecryptableValue, VaultSealed):
        return org.slug


def org_names() -> dict[int, str]:
    """{pk: display name} of every organization (a handful)."""
    names = {}
    for pk, slug in Organization.objects.order_by("slug").values_list("pk", "slug"):
        try:
            name = Organization.objects.filter(pk=pk).values_list("legal_name", flat=True).get()
        except (UndecryptableValue, VaultSealed):
            name = None
        names[pk] = name or slug
    return names
