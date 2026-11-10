"""Contacts on the staff pages. Staff see every organization. There is no delete and no create here:
contacts come into being with an invoice, and an invoice keeps naming its contact."""
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from apps.audit import services as audit
from apps.contacts.models import Contact, ContactEmail
from apps.contacts.services import NotPromotable, promote_link_buyer
from apps.invoices.models import Invoice
from apps.keys import gpg, validation
from apps.keys.models import EncryptionKey
from apps.invoices.pdf import format_money
from apps.invoices.money import stored_total
from apps.vault.fields import blind_index

from . import stepup
from .forms_settings import ContactForm
from .orgnames import org_name, org_names
from .views import NO_NAME, staff_required

PAGE_SIZE = 50


# A value that cannot be decrypted raises when its row is loaded, before any view code can guard the
# read: `staff_required` turns that into one page (409) for every staff view.
def _name(contact) -> str:
    return contact.display_name or contact.legal_name or NO_NAME


def _primary_address(contact) -> str:
    rows = sorted(contact.emails.all(), key=lambda e: (not e.primary, e.pk))
    return rows[0].address if rows else ""


@staff_required
def contact_list(request):
    org = request.GET.get("org", "")
    q = request.GET.get("q", "").strip()[:320]
    kind = request.GET.get("kind", "")
    # Link buyers (contacts payment-link checkouts made, `of_record` False) are listed too, marked, so a paying
    # customer is never invisible to staff (review S1, F2); `kind` narrows to them or to clients of record.
    rows = Contact.objects.select_related("org").prefetch_related("emails").order_by("-pk")
    if kind in KINDS:
        rows = rows.filter(of_record=KINDS[kind])
    else:
        kind = ""
    if org.isascii() and org.isdigit() and len(org) <= 9:
        rows = rows.filter(org_id=int(org))
    else:
        org = ""
    if q:
        # Encrypted fields cannot be searched; an e-mail is found through its blind index, as in the admin.
        rows = rows.filter(pk__in=ContactEmail.objects.filter(address_index=blind_index(q)).values("contact_id"))
    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page"))
    names = org_names()
    for contact in page.object_list:
        contact.name = _name(contact)
        contact.org_name = names.get(contact.org_id, str(contact.org_id))
        contact.email_text = _primary_address(contact)
    return render(request, "staff/contacts/list.html", {
        "page": page, "org": org, "q": q, "kind": kind, "organizations": list(names.items()),
        "query": urlencode([(k, v) for k, v in (("org", org), ("q", q), ("kind", kind)) if v]),
    })


KINDS = {"record": True, "link": False}  # the list's filter: clients of record, link buyers


@staff_required
@require_POST
def contact_promote(request, pk):
    """Make a link buyer a client of record, once a person has checked the name and address a visitor typed
    (review S1, F2). Paying never does this by itself."""
    contact = get_object_or_404(Contact.objects.only("id", "org_id", "copy_for_org_id", "of_record"), pk=pk)
    try:
        promote_link_buyer(contact, actor=request.user)
    except NotPromotable as exc:
        held = f" It is contact #{exc.holder}." if exc.holder else ""
        messages.error(request, f"{exc}{held}")
        return _back(pk)
    messages.success(request, "This contact is now a client of record.")
    return _back(pk)


@staff_required
def contact_detail(request, pk):
    contact = get_object_or_404(Contact.objects.select_related("org"), pk=pk)
    fields = {"legal_name": contact.legal_name, "display_name": contact.display_name, "phone": contact.phone,
              "tax_ids": contact.tax_ids, "notes": contact.notes}
    address = contact.address if isinstance(contact.address, dict) else {}
    emails = [{"pk": e.pk, "address": e.address, "primary": e.primary} for e in contact.emails.order_by("-primary", "pk")]
    invoices = []
    for inv in Invoice.objects.filter(contact=contact).select_related("org").order_by("-pk")[:200]:
        inv.total_text = format_money(stored_total(inv), inv.currency)
        invoices.append(inv)
    return render(request, "staff/contacts/detail.html", {
        "contact": contact, "fields": fields, "address": address, "emails": emails,
        "invoices": invoices, "related": _related_contacts(contact), "org_name": org_name(contact.org), "name": _name(contact),
    })


@staff_required
def contact_edit(request, pk):
    contact = get_object_or_404(Contact.objects.select_related("org"), pk=pk)
    old_flag = contact.us_tax_resident
    form = ContactForm(request.POST or None, instance=contact)
    if request.method == "POST" and form.is_valid():
        changed = sorted(form.changed_data)
        if changed:
            with transaction.atomic():
                form.save()
                detail = {"fields": changed, "via": "staff"}
                if "us_tax_resident" in changed:
                    detail["us_tax_resident"] = {"from": old_flag, "to": contact.us_tax_resident}
                audit.record(request.user, "contact.update", contact, detail)
        messages.success(request, "Saved the contact." if changed else "No changes.")
        return redirect(reverse("staff:contact_detail", args=[contact.pk]))
    return render(request, "staff/contacts/form.html", {"form": form, "contact": contact, "name": _name(contact),
                                                         "org_name": org_name(contact.org)})


def _emails_changed(request, contact):
    audit.record(request.user, "contact.update", contact, {"fields": ["emails"], "via": "staff"})  # never the address


def _locked(pk):
    """The contact row locked for the length of the transaction, so two staff members editing its
    addresses at once cannot both pass the "last address" or "one primary" checks."""
    return get_object_or_404(Contact.objects.select_for_update(no_key=True)
                             .only("id", "org_id", "copy_for_org_id", "of_record"), pk=pk)


def _key_would_lose_its_address(contact_pk, removing_pk) -> bool:
    """True when an active key of the contact names no address that would remain: `protect` would then
    refuse with `email_mismatch` and hold every letter to the contact. The key is checked the way the
    sender checks it. A key that cannot be checked right now counts as at risk."""
    remaining = [e.address for e in ContactEmail.objects.filter(contact_id=contact_pk).exclude(pk=removing_pk) if e.address]
    for key in EncryptionKey.objects.filter(contact_id=contact_pk, state="active"):
        try:
            validation.inspect(key.material, remaining)
        except validation.KeyRejected as exc:
            if exc.args and exc.args[0] == "email_mismatch":
                return True
        except (gpg.GpgFailed, OSError):
            return True
    return False


def _related_contacts(contact) -> list:
    """The issuer's copies of this contact, or the contacts this one is a copy of: the same address in
    another contact, linked by `copy_for_org`."""
    indexes = list(ContactEmail.objects.filter(contact=contact).values_list("address_index", flat=True))
    if not indexes or not contact.of_record:  # a checkout's own contact is linked to nothing (audit SEC-1)
        return []
    if contact.copy_for_org_id:
        scope = Contact.objects.filter(org_id=contact.copy_for_org_id, copy_for_org__isnull=True, of_record=True)
    else:
        scope = Contact.objects.filter(copy_for_org_id=contact.org_id, of_record=True)
    return list(scope.filter(emails__address_index__in=indexes).exclude(pk=contact.pk).distinct().order_by("pk"))


def _back(contact_pk):
    return redirect(reverse("staff:contact_detail", args=[contact_pk]))


def _announce_emails(request, contact, change, what):
    """Letters, pay links and PDFs go to these addresses: every addition and every new primary is told to
    the superusers (audit SEC-3), by the contact's id only (staff letters go in plaintext)."""
    stepup.announce(request.user, change, f"{what} contact #{contact.pk}.",
                    reverse("staff:contact_detail", args=[contact.pk]))


@staff_required
@require_POST
def contact_email_add(request, pk):
    if refused := stepup.refusal(request, reverse("staff:contact_detail", args=[pk])):
        return refused
    address = (request.POST.get("address") or "").strip()
    try:
        if not address or len(address) > 320:
            raise ValidationError("Enter an e-mail address.")
        validate_email(address)
    except ValidationError:
        messages.error(request, "That is not a valid e-mail address.")
        return _back(pk)
    try:
        with transaction.atomic():
            contact = _locked(pk)
            # Unique among clients of record; a checkout's own contact only ever clashes with itself.
            taken = (ContactEmail.objects.filter(org_id=contact.org_id, address_index=blind_index(address),
                                                 copy_for_org_id=contact.copy_for_org_id, of_record=True)
                     if contact.of_record else
                     ContactEmail.objects.filter(contact_id=contact.pk, address_index=blind_index(address))).first()
            if taken is not None:
                messages.error(request, "This contact already has that address." if taken.contact_id == contact.pk
                               else "Another contact of this organization already has that address.")
                return _back(pk)
            ContactEmail.objects.create(contact=contact, address=address, primary=False)
            _emails_changed(request, contact)
            _announce_emails(request, contact, "contact e-mail address added", "Added an e-mail address to")
    except IntegrityError:  # lost a race for the same address between the check and the insert
        messages.error(request, "Another contact of this organization already has that address.")
        return _back(pk)
    messages.success(request, "Added the e-mail address.")
    return _back(pk)


@staff_required
@require_POST
def contact_email_primary(request, pk, email_pk):
    if refused := stepup.refusal(request, reverse("staff:contact_detail", args=[pk])):
        return refused
    with transaction.atomic():
        contact = _locked(pk)
        row = get_object_or_404(ContactEmail, pk=email_pk, contact_id=contact.pk)
        if row.primary:
            messages.success(request, "No changes.")
            return _back(pk)
        ContactEmail.objects.filter(contact_id=contact.pk).exclude(pk=row.pk).update(primary=False)
        ContactEmail.objects.filter(pk=row.pk).update(primary=True)
        _emails_changed(request, contact)
        _announce_emails(request, contact, "contact primary e-mail changed", "Made another e-mail address primary on")
    messages.success(request, "Made that address the primary one.")
    return _back(pk)


@staff_required
@require_POST
def contact_email_remove(request, pk, email_pk):
    with transaction.atomic():
        contact = _locked(pk)
        row = get_object_or_404(ContactEmail, pk=email_pk, contact_id=contact.pk)
        if row.primary:
            messages.error(request, "The primary address cannot be removed. Make another address primary first.")
            return _back(pk)
        if ContactEmail.objects.filter(contact_id=contact.pk).count() <= 1:
            messages.error(request, "The last address of a contact cannot be removed.")
            return _back(pk)
        if _key_would_lose_its_address(contact.pk, row.pk):
            messages.error(request, "An active encryption key names this address: revoke the key first.")
            return _back(pk)
        row.delete()
        _emails_changed(request, contact)
    messages.success(request, "Removed the e-mail address.")
    return _back(pk)
