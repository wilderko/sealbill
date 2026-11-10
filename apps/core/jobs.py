import logging

from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone, translation
from django.urls import reverse

from apps.core import alerting, cache_purge as cache_purging, worker
from apps.invoices.services import mark_overdue
from apps.notify.models import OutboxMessage
from apps.notify.services import SUBJECTS, flush_outbox
from apps.vault.vault import vault

logger = logging.getLogger(__name__)

SEALED_KEY = "worker:sealed-episode"


def outbox():
    flush_outbox()


def overdue():
    # Only plaintext columns are read and written, so this also runs while sealed.
    mark_overdue(timezone.localdate())


def invoice_reminders():
    """The one 30-day payment reminder (`apps.invoices.reminders`)."""
    from apps.invoices.reminders import send_due_reminders

    send_due_reminders()


def cache_purge():
    removed = cache_purging.purge_expired()
    if removed:
        logger.info("cache: %s expired entries deleted", removed)


# Used only while the cache fails; the worker is one process, so this still alerts once.
_sealed_announced_locally = False


def _first_announcement() -> bool:
    """True once per sealed episode: the shared flag, or this process's memory if the cache fails."""
    global _sealed_announced_locally
    try:
        added = cache.add(SEALED_KEY, True, timeout=None)
        # The cache failed earlier in this episode and we already announced from memory.
        return added and not _sealed_announced_locally
    except Exception as exc:  # noqa: BLE001 - a broken cache must not silence the alert
        logger.error("sealed-alert flag: cache unavailable (%s); using this process's memory", type(exc).__name__)
        first, _sealed_announced_locally = not _sealed_announced_locally, True
        return first


def _end_episode():
    global _sealed_announced_locally
    _sealed_announced_locally = False
    try:
        cache.delete(SEALED_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.error("sealed-alert flag: cache unavailable (%s)", type(exc).__name__)


def sealed_alert():
    """One staff e-mail per sealed episode. Sent directly: the outbox cannot run sealed."""
    if vault.is_unsealed():
        _end_episode()
        return
    if not _first_announcement():
        return  # already announced in this episode
    recipients = list(settings.STAFF_NOTIFY_EMAILS)
    if not recipients:
        logger.warning("The vault is sealed and STAFF_NOTIFY_EMAILS is empty; nobody was told.")
        return
    from django.utils.translation import gettext

    ctx = {
        "count": OutboxMessage.objects.filter(state="queued").count(),
        "unlock_url": settings.SITE_URL.rstrip("/") + "/unlock/",
    }
    with translation.override("en"):
        text = render_to_string("email/vault_sealed_staff.txt", ctx)
        html = render_to_string("email/vault_sealed_staff.html", {**ctx, "language": "en"})
        subject = " ".join((gettext(SUBJECTS["vault_sealed_staff"]) % ctx).split())
    mail = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, recipients)
    mail.attach_alternative(html, "text/html")
    try:
        mail.send()
    except Exception as exc:  # noqa: BLE001
        _end_episode()  # try again on the next run
        logger.error("sealed-vault alert could not be sent: %s", type(exc).__name__)
        return
    logger.warning("The vault is sealed; alert sent to staff (%s queued e-mails).", ctx["count"])


FAILED_NO_RECIPIENTS_KEY = "worker:failed-alert-no-recipients"


def outbox_failed_alert():
    """Tell staff once about every e-mail that reached `failed`: by a direct e-mail (never through
    the outbox it reports on) and as a `staff.alert` event, each channel with its own mark
    (`apps.core.alerting`). A row is marked only once some channel told someone. Only plaintext
    columns are read (no addresses, no contents)."""
    rows = list(
        OutboxMessage.objects.filter(state="failed")
        .filter(Q(staff_alerted_at__isnull=True) | Q(webhook_alerted_at__isnull=True))
        .values_list("pk", "kind", "failed_at", "last_error", "staff_alerted_at", "webhook_alerted_at")
    )
    if not rows:
        return
    for_email = [row for row in rows if row[4] is None]
    for_webhook = [row for row in rows if row[5] is None]
    # The event: a count, no address or kind of letter.
    webhook = alerting.webhook({"kind": "outbox_failed", "count": len(for_webhook)}) if for_webhook else None
    email = _outbox_failed_email(for_email) if for_email else None
    by_email, by_webhook = alerting.marks([(row[0], row[4] is not None, row[5] is not None) for row in rows],
                                          email, webhook)
    failed_at = {row[0]: row[2] for row in rows}

    def mark(pks, column):
        # Only rows still in the failure we reported: one retried and failed again meanwhile
        # carries a new failed_at and must be announced on the next run.
        for pk in pks:
            same = {"failed_at": failed_at[pk]} if failed_at[pk] else {"failed_at__isnull": True}
            OutboxMessage.objects.filter(pk=pk, state="failed", **{f"{column}__isnull": True}, **same).update(
                **{column: timezone.now()})

    mark(by_email, "staff_alerted_at")
    mark(by_webhook, "webhook_alerted_at")


def _outbox_failed_email(rows) -> str:
    """The alert e-mail about `rows`: alerting.TOLD, NOBODY (no STAFF_NOTIFY_EMAILS) or FAILED."""
    recipients = list(settings.STAFF_NOTIFY_EMAILS)
    if not recipients:
        if cache.add(FAILED_NO_RECIPIENTS_KEY, True, timeout=None):
            logger.warning("%s e-mail(s) failed and STAFF_NOTIFY_EMAILS is empty; nobody was told by e-mail.",
                           len(rows))
        return alerting.NOBODY
    kinds = {}
    for _pk, kind, *_rest in rows:
        kinds[kind] = kinds.get(kind, 0) + 1
    listing = ", ".join(f"{kind} x{n}" for kind, n in sorted(kinds.items()))
    page = settings.SITE_URL.rstrip("/") + reverse("staff:needs_person")
    subject = f"E-mail delivery failed: {len(rows)} message(s) need a person"
    text = (
        f"{len(rows)} e-mail(s) could not be sent ({listing}).\n\n"
        f"They are listed under Needs a person, with a retry button: {page}\n\n"
        + "\n".join(_failure_causes([row[3] for row in rows])) + "\n"
    )
    try:
        EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, recipients).send()
    except Exception as exc:  # noqa: BLE001
        logger.error("failed-e-mail alert could not be sent: %s", type(exc).__name__)
        return alerting.FAILED  # tried again on the next run
    return alerting.TOLD


def _failure_causes(errors: list[str]) -> list[str]:
    """One sentence per kind of failure, from `OutboxMessage.last_error` (a class name and a
    refusal code, never client data). Each says only what that failure is known to mean."""
    key = openssl = missing = withdrawn = vault = nobody = 0
    other = {}
    for error in errors:
        code = error[len("EncryptionRefused("):-1] if error.startswith("EncryptionRefused(") else None
        if code == "openssl_missing":
            openssl += 1
        elif code == "key_missing":
            missing += 1
        elif code == "key_withdrawn":
            withdrawn += 1
        elif code is not None:
            key += 1
        elif error == "UndecryptableValue":
            vault += 1
        elif error == "NoRecipient":
            nobody += 1
        else:
            other[error or "unrecorded"] = other.get(error or "unrecorded", 0) + 1

    def count(n):
        return f"{n} was" if n == 1 else f"{n} were"

    lines = []
    if key:
        lines.append(f"{count(key)} refused because the contact's encryption key stopped working; nothing was "
                     "sent unencrypted. The key is listed on the same page: retry once the contact has a working key.")
    if openssl:
        lines.append(f"{count(openssl)} refused because openssl is not installed on this server, and S/MIME to an "
                     "EC certificate needs it. Install openssl, then retry.")
    if missing:
        lines.append(f"{count(missing)} refused: a key confirmation letter goes to the key it confirms or nowhere, "
                     "and that key no longer exists. Nothing was sent; retrying will not help (the client can "
                     "upload the key again).")
    if withdrawn:
        lines.append(f"{count(withdrawn)} refused: a letter that goes to one key (a key confirmation or a "
                     "replacement notice) is never encrypted to a key that was revoked or failed since. Nothing "
                     "was sent; retrying will not help.")
    if vault:
        lines.append(f"{count(vault)} stored in a form the current vault key cannot decrypt; retrying will not help.")
    if nobody:
        lines.append(f"{count(nobody)} addressed to nobody: the contact has no e-mail address, or "
                     "STAFF_NOTIFY_EMAILS is empty.")
    if other:
        names = ", ".join(f"{name} x{n}" for name, n in sorted(other.items()))
        lines.append(f"{count(sum(other.values()))} still failing after every retry ({names}). Retry them once the "
                     "cause is fixed: for a mail error, once the mail server accepts mail again.")
    return lines


def webhooks_flush():
    """Deliver due webhook events; nothing while sealed (endpoint URLs and secrets are encrypted)."""
    from apps.webhooks import delivery

    delivery.flush()


def webhooks_failed_alert():
    """One staff e-mail per batch of webhook deliveries that reached `failed`."""
    from apps.webhooks import delivery

    delivery.failed_alert()


def accounting_rates():
    """Fill the cached daily rates the CSV export converts with; plaintext columns only, so it runs sealed."""
    from apps.exports import rates

    rates.backfill(limit=50)


def link_orders():
    """Payment-link orders: lapse holds that ran out, void orders unpaid after 7 days (as the system), and
    forget the links of orders whose receipt is out (`apps.links.services.run_jobs`)."""
    from apps.links.services import run_jobs

    run_jobs()


def register_all():
    worker.register("outbox", 15, outbox)
    worker.register("overdue", 3600, overdue)
    worker.register("invoice_reminders", 86400, invoice_reminders)
    worker.register("sealed_alert", 60, sealed_alert)
    worker.register("cache_purge", 3600, cache_purge)
    worker.register("outbox_failed_alert", 60, outbox_failed_alert)
    worker.register("webhooks_flush", 15, webhooks_flush)
    worker.register("webhooks_failed_alert", 60, webhooks_failed_alert)
    worker.register("accounting_rates", 3600, accounting_rates)
    from apps.rails import jobs as rails_jobs
    rails_jobs.register_all()
    # After the rails' jobs: in one pass, quotes have expired before an unpaid order is looked at.
    worker.register("link_orders", 60, link_orders)
