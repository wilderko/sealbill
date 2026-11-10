"""The one payment reminder, a set number of days after an invoice was sent and is still unpaid.

Only for invoices this system mails itself (`client_letters`): a shop's invoices are its own to chase.
Nothing is voided or archived. A plain token is never stored, so the reminder reissues the links and
carries the new ones (the `invoice.links_reissued` event tells an integrator that stored the old ones).
"""
import datetime
import logging

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone, translation

from apps.audit import services as audit
from apps.invoices import migration, money
from apps.keys import delivery
from apps.invoices.errors import InvoiceError
from apps.invoices.models import Invoice
from apps.invoices.services import confirmed_total_minor, lock_invoice, primary_address, reissue_links
from apps.notify.models import OutboxMessage
from apps.vault.vault import VaultSealed, vault
from apps.webhooks import events as webhooks

logger = logging.getLogger(__name__)

REMINDABLE = ("sent", "overdue", "partially_paid")  # not `declared`: the client says they paid


def reminder_days() -> int | None:
    days = getattr(settings, "INVOICE_REMINDER_DAYS", None)
    return days if days and days > 0 else None


def _was_mailed():
    """The system e-mailed the invoice letter when it was sent (or staff had it mailed again): a row of
    kind `invoice` in the outbox, whatever became of it. Staff who sent it without the e-mail chose
    another channel, and this letter must not cross it. Plaintext columns only."""
    return Exists(OutboxMessage.objects.filter(invoice_id=OuterRef("pk"), kind="invoice"))


def _candidates(cutoff):
    return list(
        Invoice.objects.filter(status__in=REMINDABLE, client_letters=True, reminder_sent_at__isnull=True,
                               sent_at__isnull=False, sent_at__lte=cutoff, payment_link__isnull=True,
                               contact__of_record=True)
        .filter(_was_mailed())
        .order_by("sent_at", "pk").values_list("pk", flat=True)
    )


def _client_is_paying(pk: int) -> bool:
    """An open quote, or money seen that is not final or still to be reviewed: reminding now would
    kill the page the client is paying on. A later run asks again."""
    return (Invoice.objects.filter(pk=pk, quotes__state="open").exists()
            or Invoice.objects.filter(pk=pk, payments__state__in=("detected", "needs_review")).exists())


def remind(pk: int, days: int, now=None) -> bool:
    """Queue the reminder for one invoice if it still qualifies, under the row lock. True if queued."""
    from apps.core.i18n import activate_for
    from apps.notify import mail, services as notify

    now = now or timezone.now()
    with transaction.atomic():
        locked = lock_invoice(pk)
        if (locked.status not in REMINDABLE or not locked.client_letters or locked.reminder_sent_at is not None
                or locked.sent_at is None or locked.sent_at > now - datetime.timedelta(days=days)
                or locked.payment_link_id is not None):
            return False
        if not Invoice.objects.filter(_was_mailed(), pk=pk).exists():
            return False
        if migration.refusal(pk) is not None:  # history, or cannot be told (sealed, unreadable)
            return False
        invoice = Invoice.objects.select_related("contact", "org").get(pk=pk)
        contact = invoice.contact
        if not contact.of_record:  # anyone can create a checkout contact with any address (SEC-1)
            return False
        recipient = primary_address(contact)
        if not recipient:
            return False
        if contact.is_us_tax_resident and not invoice.org.is_us_resident_issuer:
            return False  # the staff "wrong issuer" queue: a person voids it and issues it again
        if _client_is_paying(pk):
            return False  # not while a quote is open or a payment awaits finality or review
        try:
            delivery.key_for_contact(contact.pk)
        except delivery.EncryptionRefused:
            # The letter would be refused at flush: do not kill the links the client holds for nothing.
            logger.error("reminder for invoice #%s skipped: the contact's key has failed", pk)
            return False
        view_plain, pay_plain = reissue_links(invoice)
        language = activate_for(contact)
        context = mail.invoice_context(invoice, language, view_url=invoice.view_url(view_plain),
                                       pay_url=invoice.pay_url(pay_plain))
        from apps.invoices.pdf import format_money

        owed = max(0, money.stored_total(invoice) - confirmed_total_minor(invoice))
        with translation.override(language):  # "1.337,00 EUR" in German, not "1,337.00"
            context["total"] = format_money(owed, invoice.currency)
        notify.enqueue("reminder", to=[recipient], invoice=invoice, language=language, context=context)
        Invoice.objects.filter(pk=pk).update(reminder_sent_at=now)
        delay = (now - locked.sent_at).days
        audit.record(None, "invoice.reminder", invoice, {"days": delay})
        webhooks.invoice_event("invoice.links_reissued", invoice)
    return True


def send_due_reminders(now=None) -> int:
    """Remind every invoice that qualifies; returns how many. Needs the vault open (the letter and
    the migration marker are encrypted), so a sealed vault does nothing and tries again later."""
    days = reminder_days()
    if days is None or not vault.is_unsealed():
        return 0
    now = now or timezone.now()
    sent = 0
    for pk in _candidates(now - datetime.timedelta(days=days)):
        try:
            if remind(pk, days, now):
                sent += 1
        except VaultSealed:
            break
        except InvoiceError:
            continue
        except Exception as exc:  # noqa: BLE001 - one invoice must not stop the others
            # The class only: a message can carry client data.
            logger.error("reminder for invoice #%s failed (%s)", pk, type(exc).__name__)
    if sent:
        logger.info("invoice reminders: %s queued", sent)
    return sent
