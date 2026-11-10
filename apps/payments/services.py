from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.audit import services as audit
from apps.invoices.errors import InvoiceError
from apps.invoices.models import Invoice
from apps.invoices.money import stored_total
from apps.invoices.services import SETTLED, confirmed_total_minor, lock_invoice, recompute_status
from apps.payments.models import Declaration, Payment
from apps.quotes.models import Quote
from apps.rails import addresses
from apps.rails.models import CHAIN_KINDS, RailAccount
from apps.rails.networks import NETWORKS
from apps.vault.registry import plaintext_columns
from apps.webhooks import events as webhooks


# Locking these columns only (never the encrypted notes) keeps the payment path sealed-safe.
PAYMENT_PLAIN_FIELDS: tuple[str, ...] = plaintext_columns(Payment)
DECLARATION_PLAIN_FIELDS: tuple[str, ...] = plaintext_columns(Declaration)

DECLARABLE = ("sent", "overdue", "partially_paid")
ALREADY_DECLARED = ("declared",)
# Open claims an invoice holds at most (audit WEB-1): each one is stored, audited and looked up on
# chain, so one pay link must not be able to queue thousands. Staff dismissing one makes room.
MAX_OPEN_DECLARATIONS = 3


class TooManyDeclarations(InvoiceError):
    """The invoice already holds MAX_OPEN_DECLARATIONS open declarations."""


def declare_payment(invoice: Invoice, *, network: str = "", tx_hash: str = "", note: str = "") -> Declaration:
    """Record a client's claim to have paid; the status follows `recompute_status`.

    Allowed from sent, overdue, partially_paid and declared. A claim naming the network and
    transaction of an open one is folded into it: that declaration is returned and nothing new is
    recorded. A fourth open claim is refused with TooManyDeclarations. Staff are told, and
    `invoice.declared` is emitted, when the claim is the invoice's only open one (a repeat while
    another is open must not mail them or the webhooks again).
    Re-reads the row under a lock so a void racing with this loses cleanly.
    """
    from apps.payments.matching import _tx_key  # matching imports this module

    with transaction.atomic():
        fresh = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if fresh.status not in DECLARABLE + ALREADY_DECLARED:
            raise InvoiceError(f"a {fresh.status} invoice cannot be declared paid")
        open_rows = list(fresh.declarations.filter(state="open").only(*DECLARATION_PLAIN_FIELDS))
        if tx_hash.strip():
            key = _tx_key(network, tx_hash)
            for row in open_rows:
                if row.network == network and row.tx_hash.strip() and _tx_key(row.network, row.tx_hash) == key:
                    invoice.status = fresh.status
                    return row
        if len(open_rows) >= MAX_OPEN_DECLARATIONS:
            raise TooManyDeclarations(f"the invoice already holds {len(open_rows)} open declarations")
        first_open = not open_rows
        declaration = Declaration.objects.create(
            invoice=fresh, network=network, tx_hash=tx_hash, note=note or None
        )
        audit.record(None, "declaration.create", declaration,
                     {"invoice": fresh.pk, "network": network, "tx_hash": tx_hash}, actor_name="client")
        recompute_status(fresh)
        if first_open:
            _notify_staff(fresh)
            webhooks.invoice_event("invoice.declared", fresh, declaration_id=declaration.pk)
    invoice.status = fresh.status
    return declaration


def _notify_staff(invoice: Invoice) -> None:
    recipients = list(settings.STAFF_NOTIFY_EMAILS)
    if not recipients:
        return
    from apps.notify import services as notify

    # The invoice number and the staff link only (audit DATA-7): staff letters go in plaintext, even
    # when the client's own letters are encrypted, so they never name the client or the amount. The
    # page behind the link (TOTP) shows both.
    notify.enqueue(
        "declared_staff", to=recipients, invoice=invoice, language="en",
        context={
            "ref": invoice.number or str(invoice),
            "admin_url": settings.SITE_URL.rstrip("/") + reverse("staff:invoice", args=[invoice.pk]),
        },
    )


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------
RAILS = ("bank", "fiat_link", "manual")
CLOSED_FOR_PAYMENT = ("draft", "void")
MAX_PAYMENT_MINOR = 10**12


def _check_amount(amount_minor) -> None:
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
        raise InvoiceError("a payment must be a positive amount")
    if amount_minor > MAX_PAYMENT_MINOR:
        raise InvoiceError("that amount is larger than any payment this system accepts")


def confirmed_minor(invoice: Invoice) -> int:
    return confirmed_total_minor(invoice)  # the one sum


def remaining_minor(invoice: Invoice) -> int:
    """What is still owed: total minus confirmed payments, never negative."""
    return max(0, stored_total(invoice) - confirmed_minor(invoice))


def _decimal_major(amount_minor: int, currency: str) -> Decimal:
    from apps.invoices.currencies import minor_units

    return (Decimal(amount_minor).scaleb(-minor_units(currency))).quantize(Decimal(1).scaleb(-minor_units(currency)))


def record_manual_payment(
    invoice: Invoice, *, amount_minor: int, rail: str, note: str, user, idempotency_key: str = ""
) -> Payment:
    """Staff saw the money arrive: a confirmed payment, applied at once. Only a person says so: no chain or
    gateway stands behind it, so an API token (an inactive service user) is refused with NeedsPerson, before
    anything is written (review S1, F1)."""
    if not is_a_person(user):
        raise NeedsPerson("only a person records a manual payment, on the staff pages")
    _check_amount(amount_minor)
    if rail not in RAILS:
        raise InvoiceError(f"unknown payment rail {rail!r}")
    with transaction.atomic():
        fresh = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if fresh.status in CLOSED_FOR_PAYMENT:
            raise InvoiceError(f"a {fresh.status} invoice cannot receive payments")
        payment = Payment.objects.create(
            invoice=fresh, rail=rail, asset=fresh.currency, amount_minor=amount_minor,
            received_amount=_decimal_major(amount_minor, fresh.currency),
            state="confirmed", confirmed_at=timezone.now(),
            confirmed_by=user if getattr(user, "pk", None) else None,
            evidence_note=note or None, idempotency_key=idempotency_key or "",
        )
        audit.record(user, "payment.record", payment, {"invoice": fresh.pk, "amount_minor": amount_minor, "rail": rail, "note": note})
        webhooks.payment_event("payment.confirmed", payment)
        apply_payments(fresh, actor=user)
    invoice.status, invoice.paid_at = fresh.status, fresh.paid_at
    return payment


def _consume_quote(payment: Payment, invoice: Invoice) -> None:
    """A payment staff accepted is the payment of its quote: a second transfer of the same
    amount is then a duplicate, not a second payment. Call it holding the invoice lock, and
    only for a quote of `invoice` (see `assign_payment`)."""
    if payment.quote_id:
        from apps.quotes.services import close_quotes
        close_quotes(invoice, paid_quote=payment.quote)


def _match_claims(payment: Payment) -> None:
    """The client's open claim naming the transaction staff just confirmed is borne out, so the
    pay page asks for the rest of a partial payment instead of saying it is being checked (audit
    finding E2E-1). A settled invoice has had every claim matched by `apply_payments` already."""
    from apps.payments.matching import match_declarations  # matching imports this module

    match_declarations(payment)


def awaiting_finality(payment: Payment) -> bool:
    """A chain transfer the chain has not finalized yet. It is decided when it is final (it may
    still vanish), so no person confirms it before then. XMR and ZEC are the exception: nothing
    watches them, so `needs_staff_confirmation` hands them to a person."""
    return payment.rail in CHAIN_KINDS and not payment.final and not payment.needs_staff_confirmation


def _check_final(payment: Payment, action: str) -> None:
    if awaiting_finality(payment):
        raise InvoiceError(f"this transfer is not final on the chain yet, so it cannot be {action}; "
                           "it is decided once it is")


class NeedsPerson(InvoiceError):
    """The payment waits for a person (`needs_staff_confirmation`) and the actor is not one. The API
    answers 409 `needs_person`."""

    code = "needs_person"


def is_a_person(user) -> bool:
    """An active staff account: someone signed in on the staff pages. An API token acts as its service
    user, which is never active, so no token is a person (audit SEC-2)."""
    return bool(getattr(user, "pk", None) and getattr(user, "is_active", False) and getattr(user, "is_staff", False))


def _check_person(payment: Payment, user, action: str) -> None:
    """Only a person moves on a payment that waits for one. Checked here, in the services, so that no
    caller (the API, a script, a later view) can go around it. The flag decides, never the asset."""
    if payment.needs_staff_confirmation and not is_a_person(user):
        raise NeedsPerson(f"this payment waits for a person, so it can be {action} only by staff on the "
                          "Needs a person page")


def confirm_payment(payment: Payment, user, note: str = "") -> Payment:
    with transaction.atomic():
        if payment.invoice_id is None:
            raise InvoiceError("a payment with no invoice must be assigned to one first")
        invoice = lock_invoice(payment.invoice_id)
        fresh = Payment.objects.select_for_update().get(pk=payment.pk)
        _check_person(fresh, user, "confirmed")
        if fresh.state not in ("detected", "needs_review"):
            raise InvoiceError(f"a {fresh.state} payment cannot be confirmed")
        _check_final(fresh, "confirmed")
        if fresh.invoice_id != invoice.pk:
            raise InvoiceError("the payment was moved to another invoice; reload it")
        _check_amount(fresh.amount_minor)
        if invoice.status in CLOSED_FOR_PAYMENT:
            raise InvoiceError(f"a {invoice.status} invoice cannot receive payments")
        fresh.state = "confirmed"
        fresh.confirmed_at = timezone.now()
        fresh.confirmed_by = user if getattr(user, "pk", None) else None
        if note:
            fresh.evidence_note = note
        fresh.save()
        audit.record(user, "payment.confirm", fresh, {"invoice": invoice.pk, "amount_minor": fresh.amount_minor, "note": note})
        webhooks.payment_event("payment.confirmed", fresh)
        _consume_quote(fresh, invoice)
        apply_payments(invoice, actor=user)
        _match_claims(fresh)
    payment.state, payment.confirmed_at, payment.confirmed_by = fresh.state, fresh.confirmed_at, fresh.confirmed_by
    return fresh


def payment_org_id(payment: Payment) -> int | None:
    """The organization whose money `payment` is: its rail account's (the address it reached),
    else its invoice's; None for a payment tied to neither. Plaintext reads only."""
    if payment.rail_account_id:
        return RailAccount.objects.filter(pk=payment.rail_account_id).values_list("org_id", flat=True).first()
    if payment.invoice_id:
        return Invoice.objects.filter(pk=payment.invoice_id).values_list("org_id", flat=True).first()
    return None


def payment_org_ids(payment: Payment) -> set[int]:
    """The organizations that may be credited with `payment`: the one it reached (see
    `payment_org_id`) and every organization with an active account on that network for the
    address it was sent to, since one wallet may serve several. Empty: tied to none, any may."""
    ids = set()
    owner = payment_org_id(payment)
    if owner is not None:
        ids.add(owner)
    net = NETWORKS.get(payment.network)
    if net is not None and payment.to_address:
        try:
            address = addresses.normalize(net.family, payment.to_address)
        except ValueError:
            address = None
        if address:
            for org_id, networks in RailAccount.objects.filter(
                    active=True, kind=net.family, address=address).values_list("org_id", "networks"):
                if payment.network in (networks or []):
                    ids.add(org_id)
    return ids


def assign_payment(payment: Payment, invoice: Invoice, user, *, amount_minor: int, note: str) -> Payment:
    """Staff decide that a payment in the review queue belongs to `invoice`: it is attached,
    confirmed for `amount_minor` (in the invoice currency) and applied. Sealed-safe except for
    the note, which is encrypted and so needs the key staff have when they sit at this page.

    Never across organizations that do not hold the address: money that reached an address is
    credited only to an organization with an active account on it (one wallet may serve
    several; invoice numbers are unique only per organization)."""
    note = (note or "").strip()
    if not note:
        raise InvoiceError("a note is required to assign a payment")
    _check_amount(amount_minor)
    with transaction.atomic():
        locked = lock_invoice(invoice.pk)
        if locked.status in CLOSED_FOR_PAYMENT:
            raise InvoiceError(f"a {locked.status} invoice cannot receive payments")
        fresh = Payment.objects.select_for_update().only(*PAYMENT_PLAIN_FIELDS).get(pk=payment.pk)
        _check_person(fresh, user, "assigned")
        if fresh.state != "needs_review":
            raise InvoiceError(f"a {fresh.state} payment cannot be assigned")
        _check_final(fresh, "assigned")
        owners = payment_org_ids(fresh)
        if owners and locked.org_id not in owners:
            raise InvoiceError("that invoice belongs to another organization than this payment")
        before, was_quote = fresh.invoice_id, fresh.quote_id
        if fresh.quote_id and not Quote.objects.filter(pk=fresh.quote_id, invoice_id=locked.pk).exists():
            fresh.quote_id = None  # attributed to another invoice's quote: that quote paid nothing
        fresh.invoice_id = locked.pk
        fresh.amount_minor = amount_minor
        fresh.state = "confirmed"
        fresh.confirmed_at = timezone.now()
        fresh.confirmed_by = user if getattr(user, "pk", None) else None
        fresh.evidence_note = note
        fresh.applied = False
        fresh.save(update_fields=["invoice", "quote", "amount_minor", "state", "confirmed_at", "confirmed_by",
                                  "evidence_note", "applied"])
        audit.record(user, "payment.assign", fresh,
                     {"invoice": locked.pk, "was_invoice": before, "quote": fresh.quote_id, "was_quote": was_quote,
                      "amount_minor": amount_minor, "note": note})
        webhooks.payment_event("payment.confirmed", fresh)
        _consume_quote(fresh, locked)
        apply_payments(locked, actor=user)
        _match_claims(fresh)
    for name in ("invoice_id", "quote_id", "amount_minor", "state", "confirmed_at", "confirmed_by", "evidence_note"):
        setattr(payment, name, getattr(fresh, name))
    invoice.status, invoice.paid_at = locked.status, locked.paid_at
    return fresh


def acknowledge_refund(payment: Payment, user, note: str) -> Payment:
    """Staff refunded (or decided not to refund) the amount in `refund_due_base_units`."""
    note = (note or "").strip()
    if not note:
        raise InvoiceError("a note is required to acknowledge a refund")
    with transaction.atomic():
        fresh = Payment.objects.select_for_update().only(*PAYMENT_PLAIN_FIELDS).get(pk=payment.pk)
        if not fresh.refund_due_base_units:
            raise InvoiceError("this payment has no refund due")
        if fresh.refund_acknowledged_at:
            raise InvoiceError("this refund was already acknowledged")
        fresh.refund_acknowledged_at = timezone.now()
        fresh.refund_acknowledged_by = user if getattr(user, "pk", None) else None
        fresh.save(update_fields=["refund_acknowledged_at", "refund_acknowledged_by"])
        audit.record(user, "payment.refund_ack", fresh,
                     {"invoice": fresh.invoice_id, "refund_due_base_units": fresh.refund_due_base_units, "note": note})
    payment.refund_acknowledged_at, payment.refund_acknowledged_by = (
        fresh.refund_acknowledged_at, fresh.refund_acknowledged_by)
    return fresh


def acknowledge_gateway_invalid(payment: Payment, user, note: str) -> Payment:
    """Staff checked a confirmed payment the gateway later called invalid and keep it as it is
    (reversing it is `reverse_payment`, from the invoice). The payment and the invoice do not
    change; the decision is audited under the staff member's name (audit MONEY-4)."""
    note = (note or "").strip()
    if not note:
        raise InvoiceError("a note is required to keep a payment the gateway calls invalid")
    with transaction.atomic():
        fresh = Payment.objects.select_for_update().only(*PAYMENT_PLAIN_FIELDS).get(pk=payment.pk)
        if fresh.gateway_invalid_at is None:
            raise InvoiceError("the gateway has not called this payment invalid")
        if fresh.gateway_invalid_acknowledged_at:
            raise InvoiceError("this payment was already kept")
        if fresh.state != "confirmed":
            raise InvoiceError(f"a {fresh.state} payment is no longer counted; there is nothing to keep")
        fresh.gateway_invalid_acknowledged_at = timezone.now()
        fresh.gateway_invalid_acknowledged_by = user if getattr(user, "pk", None) else None
        fresh.save(update_fields=["gateway_invalid_acknowledged_at", "gateway_invalid_acknowledged_by"])
        audit.record(user, "payment.gateway_invalid_ack", fresh, {"invoice": fresh.invoice_id, "note": note})
    payment.gateway_invalid_acknowledged_at, payment.gateway_invalid_acknowledged_by = (
        fresh.gateway_invalid_acknowledged_at, fresh.gateway_invalid_acknowledged_by)
    return fresh


def reject_payment(payment: Payment, user, note: str) -> Payment:
    with transaction.atomic():
        fresh = Payment.objects.select_for_update().get(pk=payment.pk)
        if fresh.state not in ("detected", "needs_review"):
            raise InvoiceError(f"a {fresh.state} payment cannot be rejected")
        fresh.state = "rejected"
        if note:
            fresh.evidence_note = note
        fresh.save()
        audit.record(user, "payment.reject", fresh, {"invoice": fresh.invoice_id, "amount_minor": fresh.amount_minor, "note": note})
        webhooks.payment_event("payment.rejected", fresh)
    payment.state = fresh.state
    return fresh


def apply_payments(invoice: Invoice, actor=None) -> str:
    """Bring the invoice status in line with newly confirmed payments; returns the status.

    Idempotent: each confirmed payment is counted once (`applied`). With no new money
    nothing changes and nothing is sent, however often this runs. Sealed-safe: rows are
    locked through plaintext columns, totals come from plaintext columns, declarations are
    matched by a plain update, and the client's letter is addressed only when it is sent.
    """
    with transaction.atomic():
        fresh = lock_invoice(invoice.pk)
        if fresh.status in CLOSED_FOR_PAYMENT:
            raise InvoiceError(f"a {fresh.status} invoice cannot receive payments")
        confirmed = list(
            Payment.objects.select_for_update().only(*PAYMENT_PLAIN_FIELDS).filter(invoice=fresh, state="confirmed")
        )
        new_money = [p for p in confirmed if not p.applied]
        old = fresh.status
        if not new_money:
            return old
        paid = sum(p.amount_minor for p in confirmed)
        total = stored_total(fresh)
        new = recompute_status(fresh)
        Payment.objects.filter(pk__in=[p.pk for p in new_money]).update(applied=True)
        if new != old and new in webhooks.PAID_STATUSES:
            webhooks.invoice_event(f"invoice.{new}", fresh)
        if new in SETTLED:
            from apps.quotes.services import close_quotes

            close_quotes(fresh)  # nothing more is owed: no quote may be paid any more
            matched = list(Declaration.objects.filter(invoice=fresh, state="open").values_list("pk", flat=True))
            Declaration.objects.filter(pk__in=matched).update(state="matched")
            for pk in matched:
                _audit(actor, "declaration.matched", Declaration(pk=pk), {"invoice": fresh.pk})
        if new in SETTLED and old not in SETTLED:
            if fresh.payment_link_id:  # a payment link's order: its hold becomes a use (apps.links)
                from apps.links.services import order_paid

                order_paid(fresh)
            _notify_client(fresh, "receipt", paid, total)
        elif new == "partially_paid":
            _notify_client(fresh, "partial", paid, total)
    invoice.status, invoice.paid_at = fresh.status, fresh.paid_at
    return fresh.status


def _audit(actor, action: str, obj, detail: dict) -> None:
    """A staff member's action names them (vault unsealed: they are signed in); an action the
    system takes on its own may run sealed, so its plain detail is stored in plaintext."""
    if getattr(actor, "pk", None):
        audit.record(actor, action, obj, detail)
    else:
        audit.record_system(action, obj, detail)


def reverse_payment(payment: Payment, user, reason: str) -> Payment:
    """A confirmed payment turned out not to be money (bounced, mistaken). Never e-mails the client."""
    reason = (reason or "").strip()
    if not reason:
        raise InvoiceError("a reason is required to reverse a payment")
    with transaction.atomic():
        invoice = lock_invoice(payment.invoice_id)
        fresh = Payment.objects.select_for_update().only(*PAYMENT_PLAIN_FIELDS).get(pk=payment.pk)
        if fresh.state != "confirmed":
            raise InvoiceError(f"a {fresh.state} payment cannot be reversed")
        fresh.state = "reversed"
        fresh.evidence_note = reason
        fresh.save(update_fields=["state", "evidence_note"])
        audit.record(user, "payment.reverse", fresh, {"invoice": invoice.pk, "amount_minor": fresh.amount_minor, "reason": reason})
        recompute_status(invoice)
        webhooks.payment_event("payment.reversed", fresh, invoice_status=invoice.status)
    payment.state = fresh.state
    return fresh


def dismiss_declaration(declaration: Declaration, user, reason: str = "") -> Declaration:
    """Staff found no money behind the claim. The invoice leaves `declared` once no claim is open."""
    with transaction.atomic():
        invoice = lock_invoice(declaration.invoice_id)
        fresh = Declaration.objects.select_for_update().only(*DECLARATION_PLAIN_FIELDS).get(pk=declaration.pk)
        if fresh.state != "open":
            raise InvoiceError(f"a {fresh.state} declaration cannot be dismissed")
        fresh.state = "dismissed"
        fresh.save(update_fields=["state"])
        audit.record(user, "declaration.dismiss", fresh, {"invoice": fresh.invoice_id, "reason": reason})
        recompute_status(invoice)
    declaration.state = "dismissed"
    return fresh


def _notify_client(invoice: Invoice, kind: str, paid: int, total: int) -> None:
    """Queue the receipt / partial-payment letter; it is addressed when it is sent.

    This may run while the vault is sealed, so only plain facts are stored. At flush the
    letter goes to the contact's primary (else first) e-mail address, in the contact's
    language, with names and money filled in (see notify.mail).

    Neither letter carries a link: the capability tokens are stored only as hashes, so a
    link would mean replacing the ones the client already holds. The receipt is the PDF; the
    partial letter points the client back to the pay link from the original e-mail. A payment
    link's order is the exception (nobody was mailed its links): it keeps them encrypted until
    its receipt is out, and its letters go to the address typed at checkout (`apps.links`).
    """
    from apps.contacts.models import Contact
    from apps.notify import services as notify

    if not invoice.client_letters:  # the caller writes to its own customer; staff alerts are separate
        return
    notify.enqueue_addressed_later(
        kind, recipient="contact", invoice=invoice,
        contact=Contact.objects.only("pk").get(pk=invoice.contact_id),
        plain_context={"paid_minor": paid, "total_minor": total, "currency": invoice.currency},
    )
