"""Which contacts payment-link checkouts made, and which of them are clients of record (audit SEC-1).

`LinkOrder.buyer_contact` names the contact an order was booked to in the link's own organization (the
invoice names the US-resident issuer's copy instead when the order went there). It is filled in for every
existing order.

Then the contacts checkouts made before `contacts.0003` are classified. A checkout is the only thing that
creates a contact with no account behind it and no name (`contact.create` audited with neither): the API
acts as its token's service user, staff as themselves, the admin as its superuser. The issuer's copy made
for such an order is audited the same way. Such a contact, with the copies made for its organization,
becomes a link buyer (not a client of record) when it holds at least one invoice and every invoice they
hold is a payment-link order that nobody paid. A contact with no invoice at all was not made by a checkout,
which makes its invoice in the same transaction, and is left alone (review S1, F3). It stays a client of
record when one of its orders was paid, and when the API or the staff form has invoiced it since; both kinds
are logged by id, because a visitor's data may be on them (a stranger may have seeded the contact a real
buyer then paid under) and a person should check. Nothing else changes.

Only plaintext columns are read (`values_list`) and written (`update`), so a sealed deploy does not stop
here. Counts and contact ids are logged, never a name or an address.
"""
import logging
from collections import defaultdict

import django.db.models.deletion
from django.db import migrations, models

logger = logging.getLogger(__name__)

SETTLED = ("paid", "overpaid")


def classify(apps, schema_editor):
    db = schema_editor.connection.alias
    Contact = apps.get_model("contacts", "Contact")
    ContactEmail = apps.get_model("contacts", "ContactEmail")
    Invoice = apps.get_model("invoices", "Invoice")
    LinkOrder = apps.get_model("links", "LinkOrder")
    AuditEntry = apps.get_model("audit", "AuditEntry")

    contacts = {pk: (org_id, copy_for) for pk, org_id, copy_for
                in Contact.objects.using(db).values_list("pk", "org_id", "copy_for_org_id")}
    addresses = defaultdict(set)  # contact -> its address indexes
    natives = {}  # (organization, address index) -> its own contact
    copies = defaultdict(set)  # (requesting organization, address index) -> the issuer's copies made for it
    for contact_id, index in ContactEmail.objects.using(db).values_list("contact_id", "address_index"):
        addresses[contact_id].add(index)
        org_id, copy_for = contacts[contact_id]
        if copy_for is None:
            natives[(org_id, index)] = contact_id
        else:
            copies[(copy_for, index)].add(contact_id)
    invoices = defaultdict(list)  # contact -> [(payment link, status)]
    for contact_id, link_id, status in Invoice.objects.using(db).values_list("contact_id", "payment_link_id", "status"):
        invoices[contact_id].append((link_id, status))

    for order_pk, contact_id in LinkOrder.objects.using(db).values_list("pk", "invoice__contact_id"):
        _org_id, copy_for = contacts[contact_id]
        buyer = contact_id if copy_for is None else next(
            (natives[(copy_for, i)] for i in sorted(addresses[contact_id]) if (copy_for, i) in natives), None)
        if buyer is not None:
            LinkOrder.objects.using(db).filter(pk=order_pk).update(buyer_contact_id=buyer)

    unattended = {int(pk) for pk in AuditEntry.objects.using(db).filter(
        action="contact.create", model_label="contacts.contact", actor__isnull=True, actor_username="",
    ).values_list("object_pk", flat=True) if pk.isdigit()}
    demote, copies_demoted, paid, used = set(), 0, [], []
    for contact_id in sorted(unattended & contacts.keys()):
        org_id, copy_for = contacts[contact_id]
        if copy_for is not None:
            continue  # a copy goes with its original
        family = {contact_id} | {k for i in addresses[contact_id] for k in copies[(org_id, i)]}
        held = [row for k in family for row in invoices[k]]
        if not held:
            continue  # no checkout made it (F3)
        if any(link_id is None for link_id, _status in held):
            used.append(contact_id)
        elif any(status in SETTLED for _link_id, status in held):
            paid.append(contact_id)
        else:
            demote |= family
            copies_demoted += len(family) - 1
    if demote:
        Contact.objects.using(db).filter(pk__in=demote).update(of_record=False)
        ContactEmail.objects.using(db).filter(contact_id__in=demote).update(of_record=False)
        logger.warning("%d contact(s) made by unpaid payment-link checkouts are no longer clients of record (%d of "
                       "them the US-resident issuer's copies); each becomes one when its order is paid",
                       len(demote), copies_demoted)
    else:
        logger.info("no contact made by an unpaid payment-link checkout: nothing to classify")
    if paid:
        logger.warning("%d contact(s) made by payment-link checkouts were paid for and stay clients of record. A "
                       "visitor typed their details: check the name and address of contacts %s against the client",
                       len(paid), paid)
    if used:
        logger.warning("%d contact(s) made by payment-link checkouts have been invoiced through the API or the staff "
                       "form since and stay clients of record. A visitor typed their details: check the name and "
                       "address of contacts %s against the client", len(used), used)


class Migration(migrations.Migration):

    dependencies = [
        ('audit', '0003_auditentry_system_detail'),
        ('contacts', '0003_contact_of_record'),
        ('invoices', '0011_invoice_return_url'),
        ('links', '0002_one_open_issuerless_link_problem'),
    ]

    operations = [
        migrations.AddField(
            model_name='linkorder',
            name='buyer_contact',
            field=models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name='+', to='contacts.contact'),
        ),
        migrations.RunPython(classify, migrations.RunPython.noop),
    ]
