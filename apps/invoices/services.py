import datetime
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.audit import services as audit
from apps.contacts.models import Contact
from apps.contacts.services import create_unrecorded_contact, find_contact, find_or_create_contact
from apps.invoices import limits, money, snapshot, tokens
from apps.invoices.errors import InvoiceError
from apps.invoices.models import Invoice, LineItem
from apps.orgs.models import BankAccount, FiatLink, Organization
from apps.orgs.numbering import allocate_number
from apps.rails.models import RailAccount
from apps.vault.registry import plaintext_columns
from apps.webhooks import events as webhooks

__all__ = [
    "InvoiceError", "resolve_issuer", "find_existing", "create_invoice", "send_invoice",
    "void_invoice", "mark_overdue", "reissue_links", "recompute_status", "default_methods",
    "INVOICE_PLAIN_FIELDS", "keep_overpayment", "overpaid_needing_a_person", "lock_invoice", "queue_invoice_letter", "update_draft", "primary_address",
    "forget_link_copies",
]

SETTLED = ("paid", "overpaid")
MAX_TOTAL_MINOR = 10**12  # the same bound as a single payment

# Every column of an invoice that is not encrypted, read off the model (FKs by attname).
INVOICE_PLAIN_FIELDS: tuple[str, ...] = plaintext_columns(Invoice)


def lock_invoice(pk: int) -> Invoice:
    """The invoice row, locked (call inside transaction.atomic) and loaded without any encrypted
    column, so payments and lifecycle changes work while the vault is sealed. An encrypted
    attribute read from the result is loaded on access and then needs the vault."""
    return Invoice.objects.select_for_update().only(*INVOICE_PLAIN_FIELDS).get(pk=pk)


_CONTACT_FIELDS = (
    "legal_name", "display_name", "address", "tax_ids", "phone", "notes",
    "preferred_language", "us_tax_resident",
)


def resolve_issuer(org: Organization, contact: Contact, *, typed_answer: str | None = None) -> tuple[Organization, bool]:
    """Who issues an invoice `org` asks for: the US-resident issuer for a US tax resident, else `org`.

    `typed_answer`: what the buyer answered on a payment link's form, "yes" or "no". The order is then
    routed on that answer alone, never on what the contact stores (spec 2.3a item 3, revised by the
    owner on 2026-11-04: a public link must reveal nothing stored); a contradiction is a staff item,
    raised by the caller. Nothing stored changes."""
    if typed_answer is not None:
        if typed_answer not in ("yes", "no"):
            raise InvoiceError("the buyer's US tax resident answer must be yes or no")
        us_resident = typed_answer == "yes"
    else:
        us_resident = contact.is_us_tax_resident
    if us_resident and not org.is_us_resident_issuer:
        issuer = Organization.objects.filter(is_us_resident_issuer=True).first()
        if issuer is None:
            raise InvoiceError("no organization is marked as the US-resident issuer")
        return issuer, True
    return org, False


def default_methods(org: Organization, currency: str) -> list[str]:
    """What an invoice in `currency` offers when the caller names nothing: the issuer's active
    bank accounts in that currency, then its active fiat links that accept it, then its active
    crypto rails (any currency: a quote converts)."""
    currency = currency.upper()
    banks = BankAccount.objects.filter(org=org, active=True, currency__iexact=currency).order_by("pk")
    links = FiatLink.objects.filter(org=org, active=True).order_by("pk").only("pk", "currencies")
    rails = RailAccount.objects.filter(org=org, active=True).order_by("pk")
    return [f"bank:{pk}" for pk in banks.values_list("pk", flat=True)] + [
        f"fiat:{link.pk}" for link in links if currency in {str(c).upper() for c in link.currencies or []}
    ] + [f"rail:{pk}" for pk in rails.values_list("pk", flat=True)]


def _check_fiat_currencies(issuer: Organization, methods, currency: str) -> None:
    """An explicitly offered fiat link must accept the invoice currency, as a bank account must
    be in it. A key that names no link of the issuer is left to the callers that validate it."""
    currency = currency.upper()
    for key in methods or []:
        kind, _, raw = str(key).partition(":")
        if kind != "fiat" or not (raw.isascii() and raw.isdigit()):
            continue
        link = FiatLink.objects.filter(pk=int(raw), org=issuer).only("pk", "currencies").first()
        if link is not None and currency not in {str(c).upper() for c in link.currencies or []}:
            raise InvoiceError(f"fiat link {link.pk} does not accept {currency}")


def _live_methods(invoice: Invoice) -> list[str]:
    """The offered methods that still point at an active account of the issuer."""
    live = []
    for key in invoice.offered_methods or []:
        kind, _, raw = str(key).partition(":")
        model = {"bank": BankAccount, "fiat": FiatLink, "rail": RailAccount}.get(kind)
        if (model and raw.isascii() and raw.isdigit()
                and model.objects.filter(pk=int(raw), org_id=invoice.org_id, active=True).exists()):
            live.append(key)
    return live


def _contact_in(org: Organization, contact: Contact, actor=None, contact_fields=None) -> Contact:
    """The issuer's copy of `contact`: the one it keeps for the contact's organization, made from
    the contact when there is none. Never the issuer's own client with that address, and never
    the copy made for another requesting organization: what they hold is not this one's to read
    or rewrite."""
    if contact.org_id == org.pk:
        return contact
    # The original's row before the copy's (which may be updated below), as every key change does.
    Contact.objects.select_for_update().filter(pk=contact.pk).only("pk").first()
    row = contact.emails.filter(primary=True).first() or contact.emails.first()
    if row is None:
        raise InvoiceError("the contact has no e-mail address to carry over to the issuer")
    if not contact.of_record:
        # A payment-link checkout's own contact (audit SEC-1): its copy is that checkout's alone, made from
        # what was typed. It never lands on the copy the issuer keeps of a client of record, and never
        # makes that copy from a visitor's data. A fresh contact has no keys to mirror.
        return create_unrecorded_contact(org, row.address, copy_for=contact.org_id, actor=actor,
                                         audit_detail={"copied_from": contact.pk},
                                         **{f: getattr(contact, f) for f in _CONTACT_FIELDS})
    copy = find_contact(org, row.address, copy_for=contact.org_id)
    if copy is None:
        copy, _ = find_or_create_contact(
            org, row.address, copy_for=contact.org_id, actor=actor, audit_detail={"copied_from": contact.pk},
            **{f: getattr(contact, f) for f in _CONTACT_FIELDS},
        )
    elif contact_fields:
        from apps.contacts.services import apply_typed_fields

        apply_typed_fields(copy, contact_fields, actor=actor)
    _sync_keys(contact, copy, actor)
    return copy


def _sync_keys(source: Contact, target: Contact, actor=None) -> None:
    """The issuer's copy follows the keys of every original that shares it, this one included
    (`apps.keys.mirroring`): its letters are never less protected than the original's, and one
    requesting organization's invoice never strips a key another's contact holds."""
    from apps.keys import mirroring

    for attempt in (1, 2):
        try:
            with transaction.atomic():
                mirroring.reconcile(target.pk, sources=(source.pk,), actor=actor)
            return
        except IntegrityError:  # a concurrent first invoice made the active mirror; look again
            if attempt == 2:
                raise


def _existing(org, idempotency_key, external_reference):
    for field, value in (("idempotency_key", idempotency_key), ("external_reference", external_reference)):
        if value:
            found = Invoice.objects.filter(requested_by_org=org, **{field: value}).first()
            if found:
                return found
    return None


def _quantity(raw) -> Decimal:
    try:
        q = Decimal(str(raw))
    except InvalidOperation:
        raise InvoiceError(f"invalid quantity: {raw!r}")
    if not q.is_finite() or q.normalize().as_tuple().exponent < -2:
        raise InvoiceError("quantity may have at most 2 decimal places")
    return q


def _checked_quantities(lines) -> list[Decimal]:
    """Each line's quantity as a Decimal; the invoice total is held to MAX_TOTAL_MINOR."""
    quantities = [_quantity(line.get("quantity", 1)) for line in lines]
    total = money.totals_of(
        SimpleNamespace(quantity=qty, unit_price_minor=line["unit_price_minor"],
                        tax_rate_bps=line.get("tax_rate_bps", 0), discount_rate_bps=line.get("discount_rate_bps", 0),
                        discount_amount_minor=line.get("discount_amount_minor", 0))
        for line, qty in zip(lines, quantities)
    ).total
    if abs(total) > MAX_TOTAL_MINOR:
        raise InvoiceError("the invoice total may not exceed 10^12 minor units")
    return quantities


def find_existing(org, idempotency_key, external_reference):
    """The invoice this org already created under that key or reference, if any."""
    return _existing(org, idempotency_key, external_reference)


def create_invoice(
    org, contact, *, currency, lines, kind="invoice", issue_date=None, due_days=None,
    title="", notes="", external_reference=None, idempotency_key=None, offered_methods=None,
    request_fingerprint="", actor=None, contact_fields=None, typed_us_answer=None, offered_pairs=None,
    payment_link=None, coupon=None, return_url="",
):
    from apps.invoices.currencies import check_issuable

    check_issuable(currency)  # ValueError for unknown and carried-over-only currencies
    if contact.org_id != org.pk:
        raise InvoiceError("the contact belongs to another organization")
    if not contact.of_record and payment_link is None:
        # Only its own checkout invoices a contact a payment-link checkout made, until it is paid (SEC-1).
        raise InvoiceError("this contact was made by an unpaid payment-link checkout; invoice the client by e-mail")
    lines = list(lines)
    limits.check_content(lines, title, notes)
    quantities = _checked_quantities(lines)
    issuer, by_rule = resolve_issuer(org, contact, typed_answer=typed_us_answer)
    found = _existing(org, idempotency_key, external_reference)
    if found:
        return found, False
    if issue_date is None:
        issue_date = datetime.date.today()
    else:  # a date the caller chose; today is never out of bounds
        limits.check_issue_date(issue_date)
    due_date = limits.due_date(issue_date, due_days)
    try:
        with transaction.atomic():
            if by_rule:
                contact = _contact_in(issuer, contact, actor, contact_fields)
            if offered_methods is None:
                offered_methods = default_methods(issuer, currency)
            else:
                _check_fiat_currencies(issuer, offered_methods, currency)
            invoice = Invoice.objects.create(
                org=issuer, requested_by_org=org, contact=contact, kind=kind,
                currency=currency.upper(),
                issue_date=issue_date, due_date=due_date, title=title or None,
                notes=notes or None, external_reference=external_reference or None,
                idempotency_key=idempotency_key or None,
                offered_methods=list(offered_methods or []), issued_by_rule=by_rule,
                request_fingerprint=request_fingerprint or "",
                offered_pairs=[[str(n), str(a)] for n, a in (offered_pairs or [])],
                payment_link=payment_link, coupon=coupon, return_url=return_url or "",
            )
            for i, (line, qty) in enumerate(zip(lines, quantities)):
                LineItem.objects.create(
                    invoice=invoice,
                    position=line.get("position", i),
                    name=line.get("name"),
                    description=line.get("description"),
                    quantity=qty,
                    unit_price_minor=line["unit_price_minor"],
                    tax_rate_bps=line.get("tax_rate_bps", 0),
                    discount_rate_bps=line.get("discount_rate_bps", 0),
                    discount_amount_minor=line.get("discount_amount_minor", 0),
                )
            detail = {
                "issued_by_rule": by_rule, "issuer_org": issuer.slug, "requested_by_org": org.slug,
                "kind": kind, "currency": invoice.currency, "offered_methods": invoice.offered_methods,
            }
            if payment_link is not None:
                detail.update({"payment_link": payment_link.pk, "coupon": coupon.pk if coupon is not None else None})
            audit.record(actor, "invoice.create", invoice, detail,
                         actor_name="client" if payment_link is not None and actor is None else "")
            webhooks.invoice_event("invoice.created", invoice, kind=invoice.kind)
    except IntegrityError:
        found = _existing(org, idempotency_key, external_reference)
        if found is None:
            raise
        return found, False
    return invoice, True


def update_draft(
    invoice: Invoice, *, kind, currency, issue_date, due_days, title, notes, lines, offered_methods=None, actor=None,
) -> Invoice:
    """Change a draft: its header, its offered methods (None = the issuer's defaults) and its lines,
    which are replaced as a set. Anything but a draft is refused; the model guards it too."""
    from apps.invoices.currencies import check_issuable

    check_issuable(currency)
    lines = list(lines)
    limits.check_content(lines, title, notes)
    limits.check_issue_date(issue_date)
    due_date = limits.due_date(issue_date, due_days)
    quantities = _checked_quantities(lines)
    with transaction.atomic():
        fresh = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if fresh.status != "draft":
            raise InvoiceError("only a draft invoice can be edited")
        if offered_methods is None:
            offered_methods = default_methods(fresh.org, currency)
        else:
            _check_fiat_currencies(fresh.org, offered_methods, currency)
        fresh.kind, fresh.currency = kind, currency.upper()
        fresh.issue_date, fresh.due_date = issue_date, due_date
        fresh.title, fresh.notes = title or None, notes or None
        fresh.offered_methods = list(offered_methods)
        fresh.save()
        fresh.lines.all().delete()
        for i, (line, qty) in enumerate(zip(lines, quantities)):
            LineItem.objects.create(
                invoice=fresh, position=i, name=line.get("name"), description=line.get("description"),
                quantity=qty, unit_price_minor=line["unit_price_minor"], tax_rate_bps=line.get("tax_rate_bps", 0),
                discount_rate_bps=line.get("discount_rate_bps", 0),
                discount_amount_minor=line.get("discount_amount_minor", 0),
            )
        audit.record(actor, "invoice.update", fresh, {"kind": kind, "currency": fresh.currency, "lines": len(lines)})
    return fresh


def send_invoice(invoice: Invoice, *, email_client: bool = True, actor=None, buyer: dict | None = None,
                 keep_links: bool = False, client_letters: bool = True):
    """Number, tokens and snapshot in one place; the invoice letter too when `email_client`.

    `buyer`: the snapshot's buyer block as typed on a payment link's form, instead of the contact's
    stored fields (spec 2.3: a public form never shows what a contact stores). `keep_links`: keep the
    tokens encrypted beside their hashes (`view_token_copy`, `pay_token_copy`), so the order's receipt
    can carry its links (2.3a item 2).

    `client_letters=False`: the caller handles this invoice's client, so no letter to the client is
    ever queued for it (invoice, receipt, partial payment, reissued links). It contradicts
    `email_client=True`, which is refused."""
    if not client_letters and email_client:
        raise InvoiceError("client_letters=false contradicts email_client=true")
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if invoice.status != "draft":
            raise InvoiceError("only a draft invoice can be sent")
        # The contact may have become a US tax resident after this draft was made: never send
        # it from the wrong issuer, and never re-issue silently. A payment link's order is routed on
        # the buyer's typed answer instead (2.3a item 3); a stored flag it contradicts is a staff item.
        if not invoice.payment_link_id:
            _issuer, switched = resolve_issuer(invoice.org, invoice.contact)
            if switched:
                raise InvoiceError("contact is a US tax resident: delete this draft and create it again")
        recipient = None
        if email_client:
            row = invoice.contact.emails.filter(primary=True).first() or invoice.contact.emails.first()
            if row is None:
                raise InvoiceError("the contact has no e-mail address to send the invoice to")
            recipient = row.address
        if money.totals(invoice).total <= 0:
            raise InvoiceError("an invoice with a total of zero or less cannot be sent")
        if not _live_methods(invoice):
            raise InvoiceError("no payment method offered")
        if invoice.kind == "invoice":
            invoice.number = allocate_number(invoice.org, invoice.issue_date)
        view_plain, view_hash = tokens.new_token()
        pay_plain, pay_hash = tokens.new_token()
        invoice.view_token_hash, invoice.pay_token_hash = view_hash, pay_hash
        # The parties and payment details as sent: what the PDF, the receipt and the client's
        # pages show from now on, whatever later happens to those rows.
        invoice.snapshot = snapshot.build(invoice, buyer=buyer)
        invoice.status = "sent"
        invoice.sent_at = timezone.now()
        invoice.client_letters = client_letters
        fields = ["number", "view_token_hash", "pay_token_hash", "snapshot", "status", "sent_at",
                  "client_letters"]
        if keep_links:
            invoice.view_token_copy, invoice.pay_token_copy = view_plain, pay_plain
            fields += ["view_token_copy", "pay_token_copy"]
        invoice.save(update_fields=fields)
        audit.record(actor, "invoice.send", invoice, {"number": invoice.number, "email_client": email_client,
                                               "client_letters": client_letters},
                     actor_name="client" if buyer is not None and actor is None else "")
        webhooks.invoice_event("invoice.sent", invoice)
        if email_client:
            queue_invoice_letter(invoice, recipient, view_plain, pay_plain)
    return invoice, view_plain, pay_plain


def forget_link_copies(invoice_pks) -> int:
    """Drop the encrypted token copies of these invoices (a plain update: works while sealed)."""
    return Invoice.objects.filter(pk__in=list(invoice_pks)).filter(
        Q(view_token_copy__isnull=False) | Q(pay_token_copy__isnull=False)).update(
        view_token_copy=None, pay_token_copy=None)


def primary_address(contact: Contact) -> str | None:
    row = contact.emails.filter(primary=True).first() or contact.emails.first()
    return row.address if row else None


def queue_invoice_letter(invoice: Invoice, recipient: str, view_plain: str, pay_plain: str):
    """Queue the invoice letter carrying these links (encrypted at flush when the contact has a key).

    Returns None, queueing nothing, for an invoice whose caller handles the client (`client_letters`)."""
    if not invoice.client_letters:
        return None
    from apps.core.i18n import activate_for
    from apps.notify import mail, services as notify

    from apps.links.services import order_recipient

    typed = order_recipient(invoice)  # a payment link's order: written in its checkout's language
    language = typed[1] if typed else activate_for(invoice.contact)
    return notify.enqueue(
        "invoice", to=[recipient], invoice=invoice, language=language,
        context=mail.invoice_context(
            invoice, language, view_url=invoice.view_url(view_plain), pay_url=invoice.pay_url(pay_plain)
        ),
    )


def confirmed_total_minor(invoice: Invoice) -> int:
    """Confirmed money on the invoice, in its currency's minor units: the one sum `recompute_status`
    decides by and the API reports as `amount_paid_minor`. Plaintext columns only."""
    return invoice.payments.filter(state="confirmed").aggregate(s=Sum("amount_minor"))["s"] or 0


def keep_overpayment(invoice: Invoice, user, note: str) -> Invoice:
    """A person keeps the money paid over the total instead of refunding it (owner, 2026-11-09).

    The invoice stays `overpaid`, which is what the money says; the decision, the confirmed sum it
    covers and the note are recorded, and the invoice leaves the needs-person queue until the
    confirmed sum changes again."""
    note = (note or "").strip()
    if not note:
        raise InvoiceError("a note is required to keep an overpayment")
    with transaction.atomic():
        fresh = lock_invoice(invoice.pk)
        if fresh.status != "overpaid":
            raise InvoiceError("only an overpaid invoice can have its overpayment kept")
        paid = confirmed_total_minor(fresh)
        total = money.stored_total(fresh)
        now = timezone.now()
        Invoice.objects.filter(pk=fresh.pk).update(
            overpayment_kept_minor=paid, overpayment_kept_at=now,
            overpayment_kept_by=user if getattr(user, "pk", None) else None)
        audit.record(user, "invoice.overpayment_keep", fresh,
                     {"note": note, "confirmed_minor": paid, "total_minor": total, "excess_minor": paid - total})
    invoice.overpayment_kept_minor, invoice.overpayment_kept_at = paid, now
    invoice.overpayment_kept_by = user if getattr(user, "pk", None) else None
    return invoice


def overpaid_needing_a_person():
    """Overpaid invoices nobody has decided about yet, or whose confirmed sum moved since."""
    confirmed = Sum("payments__amount_minor", filter=Q(payments__state="confirmed"))
    return (Invoice.objects.filter(status="overpaid").annotate(confirmed_sum=confirmed)
            .filter(Q(overpayment_kept_minor__isnull=True) | ~Q(overpayment_kept_minor=F("confirmed_sum"))))


def recompute_status(invoice: Invoice, *, today: datetime.date | None = None) -> str:
    """Bring `invoice.status` (and `paid_at`) in line with the one rule; returns the status.

    Call it holding the invoice row lock (select_for_update inside transaction.atomic). Any
    confirmed money decides by its sum (partially_paid / paid / overpaid); otherwise an open
    declaration makes it `declared`; otherwise a past due date `overdue`; otherwise `sent`.
    Draft and void invoices are left alone. Reads and writes plaintext columns only, so it
    also runs while the vault is sealed.
    """
    if invoice.status in ("draft", "void"):
        return invoice.status
    today = today or timezone.localdate()
    paid = confirmed_total_minor(invoice)
    if paid > 0:
        total = money.stored_total(invoice)
        new = "paid" if paid == total else "partially_paid" if paid < total else "overpaid"
    elif invoice.declarations.filter(state="open").exists():
        new = "declared"
    elif invoice.due_date is not None and invoice.due_date < today:
        new = "overdue"
    else:
        new = "sent"
    changes = {}
    if new != invoice.status:
        changes["status"] = new
    if new in SETTLED and invoice.paid_at is None:
        changes["paid_at"] = timezone.now()
    elif new not in SETTLED and invoice.paid_at is not None:
        changes["paid_at"] = None
    if changes:
        # Lifecycle columns only, written without loading (and decrypting) the rest of the row.
        Invoice.objects.filter(pk=invoice.pk).update(**changes)
        for name, value in changes.items():
            setattr(invoice, name, value)
    return new


def void_invoice(invoice: Invoice, reason: str, user) -> Invoice:
    with transaction.atomic():
        invoice = lock_invoice(invoice.pk)
        if invoice.status in ("partially_paid", "paid", "overpaid", "void"):
            raise InvoiceError(f"a {invoice.status} invoice cannot be voided")
        if invoice.payments.filter(state="detected").exists():
            raise InvoiceError("a payment for this invoice is still being confirmed")
        if invoice.payments.filter(state="confirmed").exists():
            raise InvoiceError("an invoice with confirmed payments cannot be voided; reverse them first")
        invoice.status = "void"
        invoice.voided_at = timezone.now()
        invoice.void_reason = reason or None
        invoice.voided_by = user if getattr(user, "pk", None) else None
        # A void order's links are never mailed again: their encrypted copies go with it.
        invoice.view_token_copy = invoice.pay_token_copy = None
        invoice.save(update_fields=["status", "voided_at", "void_reason", "voided_by", "view_token_copy",
                                    "pay_token_copy"])
        webhooks.invoice_event("invoice.voided", invoice)
        # A void invoice can no longer be paid, so its open quotes and claims are closed with it.
        from apps.quotes.services import close_quotes

        close_quotes(invoice)
        for declaration in invoice.declarations.select_for_update().filter(state="open").only("pk", "state"):
            declaration.state = "dismissed"
            declaration.save(update_fields=["state"])
            audit.record(user, "declaration.dismiss", declaration,
                         {"invoice": invoice.pk, "reason": "invoice voided"})
    return invoice


def mark_overdue(today: datetime.date) -> int:
    """Past-due `sent` invoices, and `declared` ones with no open declaration, become overdue.

    Runs while sealed: the rows are locked and read through plaintext columns only."""
    candidates = list(
        Invoice.objects.filter(status__in=("sent", "declared"), due_date__lt=today).values_list("pk", flat=True)
    )
    moved = 0
    for pk in candidates:
        with transaction.atomic():
            invoice = (
                Invoice.objects.select_for_update().only("pk", "status", "due_date", "paid_at")
                .filter(pk=pk, status__in=("sent", "declared")).first()
            )
            if invoice is None:
                continue
            before = invoice.status
            if recompute_status(invoice, today=today) == "overdue":
                moved += 1
                audit.record_system("invoice.overdue", invoice, {"from": before, "to": "overdue"})
    return moved


def reissue_links(invoice: Invoice, *, actor=None) -> tuple[str, str]:
    with transaction.atomic():
        fresh = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if fresh.status in ("draft", "void"):
            raise InvoiceError("links can only be reissued for a sent invoice")
        view_plain, view_hash = tokens.new_token()
        pay_plain, pay_hash = tokens.new_token()
        fresh.view_token_hash, fresh.pay_token_hash = view_hash, pay_hash
        fields = ["view_token_hash", "pay_token_hash"]
        if Invoice.objects.filter(pk=fresh.pk, pay_token_copy__isnull=False).exists():
            # A link order still keeping its links: its next letter must carry the new ones.
            fresh.view_token_copy, fresh.pay_token_copy = view_plain, pay_plain
            fields += ["view_token_copy", "pay_token_copy"]
        fresh.save(update_fields=fields)
        # A security event: the links the client held stop working.
        audit.record(actor, "invoice.reissue_links", fresh, {"links": "replaced", "status": fresh.status})
        _cancel_letters_with_dead_links(fresh, actor)
    invoice.view_token_hash, invoice.pay_token_hash = view_hash, pay_hash
    return view_plain, pay_plain


def _cancel_letters_with_dead_links(invoice: Invoice, actor=None) -> None:
    """Invoice letters not yet sent carry the links just replaced: cancelled, so the client is never
    handed dead ones. A failed letter too, or a retry would send them. Waits for a letter being sent
    right now (its row lock), and then leaves it alone if it went. Call inside the transaction."""
    from apps.notify.models import OutboxMessage, cleared_contents

    stale = list(OutboxMessage.objects.select_for_update(of=("self",))
                 .filter(invoice_id=invoice.pk, kind__in=("invoice", "reminder"), state__in=("queued", "failed"))
                 .values_list("pk", "kind"))
    for pk, kind in stale:
        if OutboxMessage.objects.filter(pk=pk, state__in=("queued", "failed")).update(
                state="cancelled", next_attempt_at=None, **cleared_contents()):
            audit.record(actor, "outbox.cancelled", OutboxMessage(pk=pk),
                         {"kind": kind, "reason": "the links were reissued"})
