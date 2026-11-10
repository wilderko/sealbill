"""What happened, recorded for the organizations that asked to hear about it.

`emit` writes plaintext rows only (the event and one queued delivery per subscribed endpoint),
so it works while the vault is sealed; the deliveries go out once it is unsealed, because the
endpoints' URLs and secrets are encrypted. Call it inside the business transaction: a rollback
removes the event with everything else. It never raises: an error of its own is logged by type
and the caller carries on, so a webhook bug cannot block an invoice or a payment.
"""
import logging

from django.db import transaction

from apps.audit.services import check_plain
from apps.webhooks.models import WebhookDelivery, WebhookEndpoint, WebhookEvent

logger = logging.getLogger(__name__)

EVENT_TYPES = (
    "invoice.created", "invoice.sent", "invoice.voided", "invoice.declared",
    "invoice.partially_paid", "invoice.paid", "invoice.overpaid",
    "invoice.links_reissued",
    "payment.detected", "payment.confirmed", "payment.needs_review", "payment.rejected", "payment.reversed",
    "staff.alert", "ping",
)
# The invoice statuses whose arrival `apply_payments` announces as `invoice.<status>`.
PAID_STATUSES = ("partially_paid", "paid", "overpaid")


def emit(type: str, *, org, invoice=None, payment=None, data: dict | None = None, endpoint=None):
    """Record event `type` and queue it for every active endpoint subscribed to it. Returns the
    event, or None when it could not be recorded (logged, never raised).

    `org` (an organization or its pk) owns the event. The audience is that organization, and
    for an invoice event also the invoice's issuer and the organization that requested it (a
    US-resident invoice is issued by another organization than the one that asked). `org=None`
    with no invoice or payment is an alert about the service itself: every organization's
    subscribed endpoints get it. `endpoint` sends it to that endpoint alone (`ping`).

    `data` is checked like `record_system`: flat, plain values. Pass ids, statuses and kinds,
    never a name, an address, an e-mail or an amount. Only primary keys of `invoice` and
    `payment` are read, so stand-ins (`Invoice(pk=...)`) do."""
    try:
        with transaction.atomic():  # a savepoint: a database error here must not poison the caller's transaction
            return _record(type, org, invoice, payment, data, endpoint)
    except Exception as exc:  # noqa: BLE001 - the business transaction goes on whatever happens here
        logger.error("webhook event %s was not recorded (%s)", type if type in EVENT_TYPES else "?",
                     type_name(exc))
        return None


def type_name(exc: BaseException) -> str:
    """The exception's class name: never its message, which may carry a URL or client data."""
    return exc.__class__.__name__


def _record(event_type, org, invoice, payment, data, endpoint):
    if event_type not in EVENT_TYPES:
        raise ValueError("unknown event type")
    data = check_plain(data or {})
    org_id = getattr(org, "pk", org)
    invoice_id = getattr(invoice, "pk", None)
    payment_id = getattr(payment, "pk", None)
    event = WebhookEvent.objects.create(type=event_type, org_id=org_id, invoice_id=invoice_id,
                                        payment_id=payment_id, data=data)
    endpoints = WebhookEndpoint.objects.filter(active=True)
    if endpoint is not None:
        endpoints = endpoints.filter(pk=endpoint.pk)
    else:
        audience = _audience(org_id, invoice_id, payment)
        if audience is not None:
            endpoints = endpoints.filter(org_id__in=audience)
    targets = [pk for pk, wanted in endpoints.values_list("pk", "events")
               if endpoint is not None or "*" in (wanted or []) or event_type in (wanted or [])]
    if targets:
        WebhookDelivery.objects.bulk_create([WebhookDelivery(event=event, endpoint_id=pk) for pk in targets])
    return event


def _audience(org_id, invoice_id, payment) -> set[int] | None:
    """The organizations an event is for; None: every organization (an alert about the service)."""
    if org_id is None and invoice_id is None and payment is None:
        return None
    ids = {org_id} - {None}
    if invoice_id is not None:
        from apps.invoices.models import Invoice

        for issuer, requester in Invoice.objects.filter(pk=invoice_id).values_list("org_id", "requested_by_org_id"):
            ids |= {issuer, requester} - {None}
    elif payment is not None:
        # Money in the review queue that no invoice holds yet: every organization that may be
        # credited with it sees it (one wallet may serve several), as in the API.
        from apps.payments.services import payment_org_ids

        ids |= payment_org_ids(payment)
    return ids


def payment_event(event_type: str, payment, **extra):
    """Emit `event_type` about `payment`: its id, invoice, state and rail, plus `extra` (plain
    facts only). It belongs to the invoice's issuer, or to the organization whose wallet the
    money reached when no invoice holds it yet. Plaintext reads only."""
    from apps.invoices.models import Invoice
    from apps.payments.services import payment_org_id

    try:
        with transaction.atomic():  # a savepoint, as in emit
            if payment.invoice_id is not None:
                org_id = Invoice.objects.filter(pk=payment.invoice_id).values_list("org_id", flat=True).first()
                invoice = Invoice(pk=payment.invoice_id)
            else:
                org_id, invoice = payment_org_id(payment), None
    except Exception as exc:  # noqa: BLE001 - as in emit: never the caller's problem
        logger.error("webhook event %s was not recorded (%s)", event_type, type_name(exc))
        return None
    data = {"payment_id": payment.pk, "invoice_id": payment.invoice_id, "status": payment.state, "rail": payment.rail}
    if payment.review_reason and payment.state in ("needs_review", "rejected"):
        data["reason"] = payment.review_reason  # why a person must decide, or why it was refused
    data.update(extra)
    return emit(event_type, org=org_id, invoice=invoice, payment=payment, data=data)


def invoice_event(event_type: str, invoice, **extra):
    """Emit `event_type` about `invoice`: its id and status, plus `extra` (plain facts only)."""
    data = {"invoice_id": invoice.pk, "status": invoice.status, **extra}
    return emit(event_type, org=invoice.org_id, invoice=invoice, data=data)


def has_audience(event_type: str, *, org=None, invoice=None) -> bool:
    """Whether an active endpoint would receive `event_type` for `org` (None: any organization).
    With `invoice` the audience is the one an invoice event has: its issuer and its requester too."""
    endpoints = WebhookEndpoint.objects.filter(active=True)
    if org is not None or invoice is not None:
        audience = _audience(getattr(org, "pk", org), getattr(invoice, "pk", None), None)
        endpoints = endpoints.filter(org_id__in=audience)
    return any("*" in (wanted or []) or event_type in (wanted or [])
               for wanted in endpoints.values_list("events", flat=True))


def staff_alert_outcome(data: dict, *, org=None) -> str:
    """Emit `staff.alert` with `data` and say how it went: "told" (recorded, with at least one
    delivery queued), "nobody" (no active endpoint wants staff alerts; nothing is recorded) or
    "failed" (it could not be recorded: try again later). Never raises."""
    try:
        if not has_audience("staff.alert", org=org):
            return "nobody"
    except Exception as exc:  # noqa: BLE001 - as in emit: never the caller's problem
        logger.error("webhook event staff.alert was not recorded (%s)", type_name(exc))
        return "failed"
    event = emit("staff.alert", org=org, data=data)
    try:
        return "told" if event is not None and event.deliveries.exists() else "failed"
    except Exception as exc:  # noqa: BLE001
        logger.error("webhook event staff.alert: its deliveries could not be read (%s)", type_name(exc))
        return "failed"


def staff_alert(data: dict, *, org=None, invoice=None):
    """Emit `staff.alert` with `data` ({"kind": ...}, no client data) when an endpoint will receive
    it, else record nothing: an alert source that runs every minute must not leave a row behind
    on every pass. `org=None` is an alert about the service itself. `invoice` widens the audience
    to the invoice's issuer and requester, as for an invoice event (each endpoint once). Returns
    the event or None."""
    try:
        if not has_audience("staff.alert", org=org, invoice=invoice):
            return None
    except Exception as exc:  # noqa: BLE001 - as in emit: never the caller's problem
        logger.error("webhook event staff.alert was not recorded (%s)", type_name(exc))
        return None
    return emit("staff.alert", org=org, invoice=invoice, data=data)
