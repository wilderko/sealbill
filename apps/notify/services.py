import datetime
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone, translation
from django.utils.translation import gettext, gettext_noop

from apps.audit import services as audit
from apps.core.i18n import activate_for
from apps.keys import delivery
from apps.notify import mail as letters
from apps.notify.models import OutboxMessage, cleared_contents
from apps.vault.fields import UndecryptableValue
from apps.vault.vault import VaultSealed, vault

logger = logging.getLogger(__name__)

# Minutes to wait after each failed attempt: about sixteen hours in all, so an SMTP outage
# of most of a day loses nothing. The attempt after the last wait is the final one.
BACKOFF_MINUTES = (2, 5, 15, 30, 60, 120, 240, 480)
MAX_ATTEMPTS = len(BACKOFF_MINUTES) + 1
PLAINTEXT_FIELDS = ("pk", "kind", "state", "attempts", "created_at", "next_attempt_at", "sent_at",
                    "staff_alerted_at", "webhook_alerted_at", "invoice_id", "language", "recipient", "last_error")
SUBJECT_KEYS = ("ref", "org", "count")

# Marked for extraction here, translated when the message is rendered.
SUBJECTS = {
    "invoice": gettext_noop("Invoice %(ref)s from %(org)s"),
    "payment_request": gettext_noop("Payment request %(ref)s from %(org)s"),
    "reminder": gettext_noop("Reminder: invoice %(ref)s from %(org)s"),
    "reminder_request": gettext_noop("Reminder: payment request %(ref)s from %(org)s"),
    "receipt": gettext_noop("Receipt for %(ref)s from %(org)s"),
    "partial": gettext_noop("Partial payment received for %(ref)s"),
    "key_confirm": gettext_noop("Confirm your encryption key for %(org)s"),
    "key_replacement": gettext_noop("New encryption key for your e-mail from %(org)s"),
    "declared_staff": gettext_noop("Payment declared for %(ref)s"),
    "vault_sealed_staff": gettext_noop("The vault is sealed: %(count)s e-mails are waiting"),
    "review_staff": gettext_noop("Payment #%(ref)s needs review"),
    "confirm_staff": gettext_noop("Payment #%(ref)s is waiting for your confirmation"),
    "order_paid_staff": gettext_noop("Order %(ref)s was paid through a payment link"),
    "staff_change_staff": gettext_noop("Staff change: %(ref)s"),
}
PDF_KINDS = {"invoice": "invoice", "receipt": "receipt"}
# The outer Subject of an encrypted letter: the real one (with the invoice number) is inside.
OUTER_SUBJECT = gettext_noop("Message from %(org)s")
# Letters to staff are never encrypted, whatever key the invoice's contact holds.
STAFF_KINDS = frozenset(kind for kind in OutboxMessage.KINDS if kind.endswith("_staff"))
# Letters that go to the one key they name, or nowhere: each carries a link meant for that key's holder.
KEY_BOUND_KINDS = frozenset({"key_confirm", "key_replacement"})

# How the body of a message addressed at flush is written, by kind: msg -> context dict.
CONTEXT_BUILDERS = {
    "receipt": letters.receipt_context,
    "partial": letters.partial_context,
    "review_staff": letters.review_staff_context,
    "confirm_staff": letters.confirm_staff_context,
    "order_paid_staff": letters.order_paid_staff_context,
}
RECIPIENTS = ("contact", "staff")


class Letter(EmailMultiAlternatives):
    """Django encodes a non-ASCII Subject without knowing the "Subject: " in front of it, so a long
    one is folded with an empty first line and reaches the client with a leading space. Set again
    here, folded the way the encrypted envelope folds its own Subject."""

    def message(self, *args, **kwargs):
        msg = super().message(*args, **kwargs)
        msg.replace_header("Subject", delivery.encoded_subject(" ".join(str(self.subject).split())))
        return msg


class NoRecipient(Exception):
    """A message addressed at flush has nobody to go to (the contact has no e-mail address)."""


def enqueue(kind, *, to, invoice=None, language, context, cc=None, contact=None, key=None,
            plain_context=None) -> OutboxMessage:
    """Queue a message whose recipients and contents are known now (stored encrypted).

    `key`: encrypt it to this key, whatever the contact's active one is (the key-confirmation
    letter, which must go to the key it confirms and to no other, and the replacement notice, which
    must go to the key about to be replaced). `plain_context`: ids the flush needs before it decrypts
    anything (the replacement notice names the uploaded key), never a name or an address."""
    if kind not in OutboxMessage.KINDS:
        raise ValueError(f"unknown e-mail kind {kind!r}")
    if not to:
        raise ValueError("an e-mail needs at least one recipient")
    if kind in KEY_BOUND_KINDS and key is None:
        raise ValueError(f"a {kind} letter names the key it is encrypted to")
    return OutboxMessage.objects.create(
        kind=kind, invoice=invoice, language=language, to=list(to), cc=list(cc or []),
        subject_context={k: context[k] for k in SUBJECT_KEYS if k in context},
        body_context=dict(context), contact=contact, key=key,
        plain_context=audit.check_plain(plain_context) if plain_context is not None else None,
    )


def enqueue_addressed_later(kind: str, *, recipient: str, invoice=None, contact=None, plain_context: dict) -> OutboxMessage:
    """Queue a message whose recipients, language and contents are worked out when it is sent.

    Writes no encrypted column, so it works while the vault is sealed. `recipient` is "contact"
    (the contact's primary, else first, e-mail address, in the contact's language) or "staff"
    (STAFF_NOTIFY_EMAILS, in English). `plain_context` holds only plain facts (amounts,
    currency, ids, codes); `CONTEXT_BUILDERS[kind]` adds names and formatted money at flush.
    Only the primary keys of `invoice` and `contact` are used, so neither is loaded.
    """
    if kind not in OutboxMessage.KINDS:
        raise ValueError(f"unknown e-mail kind {kind!r}")
    if kind not in CONTEXT_BUILDERS:
        raise ValueError(f"e-mail kind {kind!r} cannot be addressed later: it has no context builder")
    if recipient not in RECIPIENTS:
        raise ValueError(f"unknown recipient {recipient!r}")
    if recipient == "contact" and contact is None:
        raise ValueError("a message to the contact needs the contact")
    if recipient == "contact" and kind in STAFF_KINDS:
        # Staff letters are never encrypted: this one would reach the client in plaintext.
        raise ValueError(f"e-mail kind {kind!r} is for staff and cannot go to the contact")
    return OutboxMessage.objects.create(
        kind=kind, recipient=recipient,
        invoice_id=invoice.pk if invoice is not None else None,
        contact_id=contact.pk if contact is not None else None,
        plain_context=audit.check_plain(plain_context),
    )


def _address(msg) -> None:
    """Fill in the recipients, language and contexts of a message addressed at flush (in memory
    only: what was stored stays plaintext). Raises NoRecipient when there is nobody to send to."""
    if not msg.recipient:
        return
    if msg.recipient == "contact":
        contact = msg.contact
        from apps.links.services import letter_details

        order = letter_details(msg.invoice) if msg.invoice_id else None
        if order is not None and order["to"]:
            # A payment link's order: the address and the language typed at checkout, never the
            # contact's stored ones (a public form never sends what a contact stores).
            msg.to, msg.language = [order["to"]], order["language"]
        else:
            row = None
            if contact is not None:
                row = contact.emails.filter(primary=True).first() or contact.emails.first()
            if row is None:
                raise NoRecipient("the contact has no e-mail address")
            msg.to, msg.language = [row.address], activate_for(contact)
    else:
        msg.to, msg.language = list(settings.STAFF_NOTIFY_EMAILS), "en"
        if not msg.to:
            raise NoRecipient("STAFF_NOTIFY_EMAILS is empty")
    msg.cc = []
    with translation.override(msg.language):
        context = CONTEXT_BUILDERS[msg.kind](msg)
    msg.subject_context = {k: context[k] for k in SUBJECT_KEYS if k in context}
    msg.body_context = context


def _subject(msg) -> str:
    key = msg.kind
    if key == "invoice" and (msg.body_context or {}).get("is_request"):
        key = "payment_request"
    elif key == "reminder" and (msg.body_context or {}).get("is_request"):
        key = "reminder_request"
    text = gettext(SUBJECTS[key]) % (msg.subject_context or {})
    return " ".join(text.split())  # no newline can reach a header


def _outer_subject(msg) -> str:
    """The generic Subject an encrypted letter carries outside, in the letter's language."""
    org = (msg.subject_context or {}).get("org")
    if not org:
        owner = msg.invoice.org if msg.invoice_id else msg.contact.org if msg.contact_id else None
        org = letters.org_name(owner) if owner is not None else ""
    with translation.override(msg.language):
        text = gettext(OUTER_SUBJECT) % {"org": org}
    return " ".join(text.split())


def _encryption_key(msg):
    """The key this letter must be encrypted to, or None when it goes as written.

    The key the message names (the key-confirmation letter, to a pending key; the replacement
    notice, to the key that was active), else the active key of the contact it goes to:
    `msg.contact`, or the invoice's contact for an invoice letter. Raises EncryptionRefused when
    that contact's key has failed (see delivery), and for a key-bound letter whose key is gone: it
    carries a link and goes to that key or nowhere."""
    if msg.kind in KEY_BOUND_KINDS and msg.key_id is None:
        raise delivery.EncryptionRefused("key_missing")
    if msg.recipient == "staff" or msg.kind in STAFF_KINDS:
        return None
    if msg.key_id is not None:
        from apps.keys.models import EncryptionKey

        key = EncryptionKey.objects.get(pk=msg.key_id)
        if key.state in delivery.WITHDRAWN_STATES:
            # Never encrypted to a withdrawn key (as mirroring promises); `_obsolete` cancels such a
            # letter first, this is the refusal should anything ever get past it.
            raise delivery.EncryptionRefused("key_withdrawn")
        return key
    contact_id = msg.contact_id or (msg.invoice.contact_id if msg.invoice_id else None)
    return delivery.key_for_contact(contact_id) if contact_id is not None else None


def _from_address(msg) -> str:
    org = msg.invoice.org if msg.invoice_id else msg.contact.org if msg.contact_id else None
    return (org.email_from if org and org.email_from else None) or settings.DEFAULT_FROM_EMAIL


def attachment_budget(encrypted: bool) -> int:
    """How large the PDF may be, measured before encryption. An encrypted letter gets half: PGP armor
    (base64 of a body that already holds the PDF as base64) roughly doubles the size, and the mail
    server stops at about 10 MB. Beyond it the PDF is left out, with a line saying so."""
    budget = settings.EMAIL_ATTACHMENT_BUDGET_BYTES
    return budget // 2 if encrypted else budget


def _build(msg, *, encrypted: bool = False) -> EmailMultiAlternatives:
    _address(msg)
    ctx = dict(msg.body_context or {})
    with translation.override(msg.language):
        attachment = None
        if msg.kind in PDF_KINDS:
            from apps.invoices import pdf

            render = pdf.render_invoice_pdf if msg.kind == "invoice" else pdf.render_receipt_pdf
            data = render(msg.invoice, msg.language)
            if len(data) <= attachment_budget(encrypted):
                attachment = data
            else:
                ctx["pdf_omitted"] = True
            ctx["pdf_attached"] = attachment is not None
        text = render_to_string(f"email/{msg.kind}.txt", ctx)
        html = render_to_string(f"email/{msg.kind}.html", {**ctx, "language": msg.language})
        subject = _subject(msg)
    sender = _from_address(msg)
    mail = Letter(
        subject, text, sender, list(msg.to), cc=list(msg.cc or []), reply_to=[sender],
    )
    mail.attach_alternative(html, "text/html")
    if attachment is not None:
        name = (ctx.get("ref") or "document").replace("/", "-").replace("#", "")
        mail.attach(f"{name}.pdf", attachment, "application/pdf")
    return mail


def _due():
    return Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=timezone.now())


def _error_name(exc) -> str:
    """The exception's class, and a refusal's code: never its message, which may carry client data."""
    name = type(exc).__name__
    return f"{name}({exc.code})" if isinstance(exc, delivery.EncryptionRefused) else name


def _fail(pk, kind, attempts, exc, *, final=False):
    attempts += 1
    state = "failed" if final or attempts >= MAX_ATTEMPTS else "queued"
    wait = None if state == "failed" else timezone.now() + datetime.timedelta(minutes=BACKOFF_MINUTES[attempts - 1])
    OutboxMessage.objects.filter(pk=pk).update(
        attempts=attempts, state=state, next_attempt_at=wait,
        failed_at=timezone.now() if state == "failed" else None, last_error=_error_name(exc)[:64],
    )
    # Never the address or the exception text: both can carry client data.
    logger.error("E-mail #%s failed (attempt %s, %s)", pk, attempts, _error_name(exc))
    if state == "failed":
        # The staff alert is sent by the worker's outbox_failed_alert job.
        audit.record(None, "outbox.failed", OutboxMessage(pk=pk),
                     {"kind": kind, "attempts": attempts, "error": _error_name(exc)})


def _obsolete(kind, invoice_status, key_state=None, key_sent_at=None, uploaded=None) -> str | None:
    """Why a queued message must no longer go out, or None.

    A key confirmation follows its key: once the key is no longer pending (confirmed, replaced,
    revoked, failed) or its link has expired, the letter has nothing left to ask. One whose key
    is gone is not cancelled here: `_encryption_key` refuses it.

    A replacement notice goes to the key it names (`key_state`), and never once that key was revoked
    or failed. It also follows the key that was uploaded (`uploaded`: its state and confirm_sent_at,
    or None when it is gone): it goes out while that key is or may still become the contact's key,
    even after it was confirmed (that is when its holder most needs to know), and not once it was
    withdrawn or can no longer be confirmed."""
    from apps.keys.models import CONFIRM_VALID_FOR

    if kind == "key_confirm" and key_state is not None:
        if key_state != "pending":
            return "the key is no longer waiting for confirmation"
        if key_sent_at is None or timezone.now() >= key_sent_at + CONFIRM_VALID_FOR:
            return "the confirmation link has expired"
        return None
    if kind == "key_replacement":
        if key_state in delivery.WITHDRAWN_STATES:
            return "the key it was for was withdrawn"
        state, sent_at = uploaded or ("revoked", None)
        if state == "revoked":
            return "the uploaded key was withdrawn"
        if state == "pending" and (sent_at is None or timezone.now() >= sent_at + CONFIRM_VALID_FOR):
            return "the uploaded key can no longer be confirmed"
        return None
    if kind == "invoice" and invoice_status in ("void", None):
        return "the invoice is void"
    if kind == "reminder" and invoice_status not in ("sent", "overdue", "partially_paid"):
        return "the invoice is no longer waiting for payment"
    if kind == "receipt" and invoice_status not in ("paid", "overpaid"):
        return "the invoice is no longer paid"
    if kind == "partial" and invoice_status != "partially_paid":
        return "the invoice is no longer partially paid"
    return None


def retry_now(queryset, actor=None) -> int:
    """Queue failed (or waiting) messages again with a fresh set of attempts."""
    pks = list(queryset.filter(state__in=("failed", "queued")).values_list("pk", flat=True))
    OutboxMessage.objects.filter(pk__in=pks).update(
        state="queued", attempts=0, next_attempt_at=None, staff_alerted_at=None, webhook_alerted_at=None,
        failed_at=None,
    )
    for pk in pks:
        audit.record(actor, "outbox.retry", OutboxMessage(pk=pk), {})
    return len(pks)


def dismiss(message, actor, reason: str) -> bool:
    """A person decided that a failed message will never be sent: it is cancelled for good, so no
    retry and no flush sends it again, encrypted or not. Audited with who and why, never to whom.

    Only a `failed` message can be dismissed; False when it is not (sent, retried meanwhile, or
    dismissed already), and then nothing is recorded. `reason` is required."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("dismissing a message needs a reason")
    with transaction.atomic():
        kind = OutboxMessage.objects.filter(pk=message.pk).values_list("kind", flat=True).first()
        if not OutboxMessage.objects.filter(pk=message.pk, state="failed").update(
                state="cancelled", next_attempt_at=None, **cleared_contents()):
            return False
        audit.record(actor, "outbox.dismiss", OutboxMessage(pk=message.pk), {"kind": kind, "reason": reason})
    logger.info("E-mail #%s dismissed by a person; it will not be sent", message.pk)
    return True


def flush_outbox(limit=50) -> int:
    """Send queued messages, oldest first. Returns how many went out; 0 while sealed.

    Delivery is at-least-once: a crash after the SMTP server accepted a message but
    before the row is committed as sent sends it again on the next flush.
    """
    if not vault.is_unsealed():
        return 0
    ids = list(
        OutboxMessage.objects.filter(_due(), state="queued").order_by("created_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    sent = 0
    for pk in ids:
        with transaction.atomic():
            # Lock only this table's row (OF SELF), and defer the encrypted columns so a
            # message that cannot be decrypted can still be marked failed. No invoice or
            # organization row stays locked while SMTP runs.
            locked = (
                OutboxMessage.objects.select_for_update(skip_locked=True, of=("self",))
                .filter(_due(), pk=pk, state="queued")
                .only("pk", "state", "attempts", "next_attempt_at").first()
            )
            if locked is None:
                continue
            kind, invoice_status, recipient, key_state, key_sent_at, plain = (
                OutboxMessage.objects.filter(pk=pk)
                .values_list("kind", "invoice__status", "recipient", "key__state", "key__confirm_sent_at",
                             "plain_context").get()
            )
            uploaded = None
            if kind == "key_replacement":
                from apps.keys.models import EncryptionKey

                uploaded = (EncryptionKey.objects.filter(pk=(plain or {}).get("key"))
                            .values_list("state", "confirm_sent_at").first())
            reason = _obsolete(kind, invoice_status, key_state, key_sent_at, uploaded)
            if reason is None and recipient == "staff" and not settings.STAFF_NOTIFY_EMAILS:
                reason = "no staff recipients"
            if reason:
                OutboxMessage.objects.filter(pk=pk).update(state="cancelled", next_attempt_at=None,
                                                           **cleared_contents())
                audit.record(None, "outbox.cancelled", OutboxMessage(pk=pk), {"kind": kind, "reason": reason})
                logger.info("E-mail #%s cancelled: %s", pk, reason)
                continue
            key = None
            try:
                with transaction.atomic():  # savepoint: a database error must not poison the outer one
                    msg = OutboxMessage.objects.select_related("invoice__org", "contact__org").get(pk=pk)
                    key = _encryption_key(msg)
                    mail = _build(msg, encrypted=key is not None)
                    if key is not None:
                        # Refuse rather than downgrade: if this raises, nothing is sent.
                        mail = delivery.protect(mail, key, outer_subject=_outer_subject(msg))
                    mail.send()
            except VaultSealed:
                break  # sealed mid-run: stop cleanly, no attempt counted
            except delivery.EncryptionRefused as exc:
                # Sending it unencrypted is not an option, and retrying cannot fix the key.
                _fail(pk, kind, locked.attempts, exc, final=True)
                if key is not None and exc.blames_key:
                    delivery.mark_failed(key, exc.code, outbox_pk=pk)
                continue
            except (UndecryptableValue, NoRecipient) as exc:
                _fail(pk, kind, locked.attempts, exc, final=True)  # retrying cannot help
                continue
            except Exception as exc:
                _fail(pk, kind, locked.attempts, exc)
                continue
            # The language is recorded as sent: a message addressed at flush only knew it now. Who it
            # went to and what it said are forgotten (DATA-4).
            OutboxMessage.objects.filter(pk=pk).update(
                state="sent", sent_at=timezone.now(), next_attempt_at=None, language=msg.language, last_error="",
                **cleared_contents(),
            )
            sent += 1
    return sent
