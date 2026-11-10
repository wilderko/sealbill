import datetime

from django.db import transaction

from apps.orgs.models import Organization


def allocate_number(org: Organization, when: datetime.date) -> str:
    """Hand out the next invoice number; a number is never reused."""
    with transaction.atomic():
        locked = Organization.objects.select_for_update().get(pk=org.pk)
        number = locked.format_number(when, locked.next_number)
        locked.next_number += 1
        locked.save(update_fields=["next_number"])
    org.next_number = locked.next_number
    return number
