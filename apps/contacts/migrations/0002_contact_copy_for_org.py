"""The US-resident issuer's copies belong to one requesting organization (audit API-1).

A copy used to be found by address alone, so any organization naming an address reached the
issuer's contact for it: the issuer's own client, or the copy another organization had made.
`copy_for_org` now names the organization a copy is for, and an address is unique per
organization among its own contacts, and per organization and requester among its copies.

Copies made before this cannot be told from the issuer's own clients (both are contacts of the
issuer with an address), so every existing contact stays native (`copy_for_org` NULL): no
requesting organization reaches it again, and the next invoice each one requests gets a copy of
its own. How many contacts the US-resident issuer holds is logged, never which.
"""
import logging

import django.db.models.deletion
from django.db import migrations, models

logger = logging.getLogger(__name__)


def count_issuer_contacts(apps, schema_editor):
    Contact = apps.get_model("contacts", "Contact")
    count = (Contact.objects.using(schema_editor.connection.alias)
             .filter(org__is_us_resident_issuer=True).count())
    if count:
        logger.warning("%d contact(s) of the US-resident issuer predate per-organization copies; each is "
                       "now the issuer's own contact, and requesting organizations get copies of their own",
                       count)
    else:
        logger.info("the US-resident issuer holds no contacts: no copy predates per-organization copies")


class Migration(migrations.Migration):

    dependencies = [
        ('contacts', '0001_initial'),
        ('orgs', '0001_initial'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='contactemail',
            name='unique_email_per_org',
        ),
        migrations.AddField(
            model_name='contact',
            name='copy_for_org',
            field=models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='orgs.organization'),
        ),
        migrations.AddField(
            model_name='contactemail',
            name='copy_for_org',
            field=models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='orgs.organization'),
        ),
        migrations.AddConstraint(
            model_name='contactemail',
            constraint=models.UniqueConstraint(condition=models.Q(('copy_for_org__isnull', True)), fields=('org', 'address_index'), name='unique_native_email_per_org'),
        ),
        migrations.AddConstraint(
            model_name='contactemail',
            constraint=models.UniqueConstraint(condition=models.Q(('copy_for_org__isnull', False)), fields=('org', 'address_index', 'copy_for_org'), name='unique_copy_email_per_requester'),
        ),
        migrations.RunPython(count_issuer_contacts, migrations.RunPython.noop),
    ]
