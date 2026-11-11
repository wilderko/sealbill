"""The worker's crypto jobs. Each is a thin call into the module that owns the work."""
import logging

from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.db.models import F, Q
from django.utils import timezone
from django.urls import reverse

from apps.core import alerting, worker
from apps.rails import fx, health, http
from apps.rails.models import PendingTransfer, RailAccount, ScanGap, WatcherHealth, ton_blocked
from apps.rails.networks import NETWORKS
from apps.vault.registry import plaintext_columns
from apps.vault.vault import vault

logger = logging.getLogger(__name__)

WATCHER_ALERT_AFTER_S = 600
CHAIN_KINDS = ("evm", "tron", "solana", "ton")
NO_RECIPIENTS_KEY = "worker:watcher-alert-no-recipients"
GAP_NO_RECIPIENTS_KEY = "worker:gap-alert-no-recipients"
_ACCOUNT_FIELDS = plaintext_columns(RailAccount)


def quotes_expire():
    from apps.quotes import services
    services.expire_quotes(timezone.now())


def quotes_release():
    from apps.quotes import services
    services.release_quotes(timezone.now())


def quote_capacity():
    from apps.quotes import services
    services.check_capacity()


def finality_refresh():
    from apps.payments import matching
    matching.refresh_finality()


def declarations_verify():
    from apps.payments import matching, services
    matching.verify_open_declarations()
    services.tell_staff_about_declarations()  # only the claims a person has to act on


def btcpay_poll():
    if not vault.is_unsealed():
        return  # the gateway keys are encrypted; the sealed alert covers it
    from apps.rails.gateways import btcpay
    btcpay.btcpay_poll()


def lightning_poll():
    if not vault.is_unsealed():
        return
    from apps.rails.gateways import lightning
    lightning.lightning_poll()


def chain_scan():
    """Every chain account a watcher still reads (active, or switched off while a quote of it is
    unreleased: `RailAccount.objects.watched`) on every network it enables. The adapters record their own
    health (`scan:<family>:<network>:<pk>`); an unexpected error is recorded here under that
    name. One bad network never stops the others. Runs sealed: only plaintext is read.

    The adapter has moved its cursor once `scan` returns, so a transfer whose recording fails is
    never dropped: it goes to `PendingTransfer`, the rest of the batch is still recorded, and the
    queue is retried at the start of every pass (`ingest:<family>:<network>:<pk>` fails while it
    holds rows for that account and network). TON accounts are skipped while `TON_ENABLED` is off."""
    from apps.payments import matching
    from apps.rails.chains.base import adapter_for

    still_pending = _retry_pending_isolated(matching)
    accounts = RailAccount.objects.watched().filter(kind__in=CHAIN_KINDS).defer("credentials").order_by("pk")
    for account in accounts:
        if ton_blocked(account.kind):
            continue  # TON waits for the credit leg to be verified (settings.TON_ENABLED)
        for network in account.networks or []:
            net = NETWORKS.get(network)
            if net is None or net.family != account.kind:
                continue
            try:
                transfers = adapter_for(network).scan(account, network)
            except http.Unavailable:
                continue  # the adapter has already recorded why
            except Exception as exc:  # noqa: BLE001 - one bad network must not stop the rest
                kind = type(exc).__name__
                logger.error("chain scan of %s for account %s failed: %s", network, account.pk, kind)
                try:
                    health.failed(f"scan:{net.family}:{network}:{account.pk}", kind)
                except Exception:  # noqa: BLE001 - the log line stands; the other networks still run
                    pass
                continue
            for transfer in transfers:
                try:
                    matching.ingest(transfer, account=account)
                except Exception as exc:  # noqa: BLE001 - the next transfer is recorded all the same
                    _queue(account, transfer, type(exc).__name__)
                    continue
                key = (transfer.network, transfer.tx_hash, transfer.log_index)
                if key in still_pending:  # a scan read it again (an overlap) and this time it was recorded
                    try:
                        PendingTransfer.objects.filter(network=key[0], tx_hash=key[1], log_index=key[2]).delete()
                    except Exception as exc:  # noqa: BLE001 - the row stays; the next pass ingests it again (idempotent)
                        logger.error("a recorded transfer could not be taken off the retry queue (%s)",
                                     type(exc).__name__)
    try:
        _sync_ingest_health()
    except Exception as exc:  # noqa: BLE001 - bookkeeping only; the next pass syncs it
        logger.error("syncing the ingest health failed (%s)", type(exc).__name__)


RETRY_HEALTH = "ingest-retry"


def _retry_pending_isolated(matching) -> set[tuple]:
    """`_retry_pending`, unable to stop a scan: whatever its own bookkeeping raises (a locked
    database, say) is logged by type and recorded as a failing watcher, and the scan goes on."""
    try:
        still = _retry_pending(matching)
    except Exception as exc:  # noqa: BLE001
        kind = type(exc).__name__
        logger.error("retrying the queued transfers failed (%s); scanning goes on", kind)
        try:
            health.failed(RETRY_HEALTH, kind)
        except Exception:  # noqa: BLE001 - nothing more can be recorded; the log line stands
            pass
        return set()
    try:
        if WatcherHealth.objects.filter(name=RETRY_HEALTH, failing_since__isnull=False).exists():
            health.ok(RETRY_HEALTH)
    except Exception:  # noqa: BLE001
        pass
    return still


def _queue(account, transfer, kind: str) -> None:
    """Remember a transfer whose recording failed, for the next pass."""
    logger.error("a transfer on %s for account %s could not be recorded (%s); queued for retry",
                 transfer.network, account.pk, kind)
    try:
        row, created = PendingTransfer.objects.get_or_create(
            network=transfer.network, tx_hash=transfer.tx_hash, log_index=transfer.log_index,
            defaults={**PendingTransfer.fields_of(transfer), "account": account, "attempts": 1,
                      "last_error_kind": kind[:64]})
        if not created:
            PendingTransfer.objects.filter(pk=row.pk).update(attempts=F("attempts") + 1, last_error_kind=kind[:64])
    except Exception as exc:  # noqa: BLE001 - nothing else can hold it now; say so loudly
        logger.critical("a transfer on %s for account %s was seen but could not be queued either (%s)",
                        transfer.network, account.pk, type(exc).__name__)


def _retryable(rows):
    """The queued transfers that may be recorded now. TON rows stay queued (never deleted)
    while `TON_ENABLED` is off, and come back when it is on."""
    return rows.exclude(account__kind="ton") if ton_blocked("ton") else rows


def _retry_pending(matching) -> set[tuple]:
    """Record what earlier passes could not; a row goes only once `ingest` has succeeded.
    Returns the (network, tx_hash, log_index) of the rows still waiting."""
    rows = list(_retryable(PendingTransfer.objects.order_by("pk")))
    if not rows:
        return set()
    accounts = RailAccount.objects.only(*_ACCOUNT_FIELDS).in_bulk({row.account_id for row in rows})
    still, kinds = set(), {}
    for row in rows:
        try:
            matching.ingest(row.transfer(), account=accounts[row.account_id])
        except Exception as exc:  # noqa: BLE001 - kept, and tried again next pass
            kind = type(exc).__name__
            PendingTransfer.objects.filter(pk=row.pk).update(attempts=F("attempts") + 1, last_error_kind=kind[:64])
            still.add((row.network, row.tx_hash, row.log_index))
            kinds[kind] = kinds.get(kind, 0) + 1
        else:
            row.delete()
    for kind, count in sorted(kinds.items()):
        logger.error("%s queued transfer(s) still could not be recorded (%s)", count, kind)
    return still


def _ingest_name(account_id: int, network: str) -> str:
    net = NETWORKS.get(network)
    return f"ingest:{net.family if net else 'unknown'}:{network}:{account_id}"


def _sync_ingest_health() -> None:
    """`ingest:...` fails while the queue holds rows for that account and network, and is ok
    once they are all recorded."""
    waiting = {}
    for account_id, network, kind in _retryable(PendingTransfer.objects.order_by("pk")).values_list(
            "account_id", "network", "last_error_kind"):
        waiting[_ingest_name(account_id, network)] = kind or "unknown"
    for name, kind in waiting.items():
        health.failed(name, kind)
    drained = (WatcherHealth.objects.filter(name__startswith="ingest:", failing_since__isnull=False)
               .exclude(name__in=list(waiting)).values_list("name", flat=True))
    for name in list(drained):
        health.ok(name)


def watcher_alert():
    """One e-mail per failing episode per watcher, sent directly (the outbox cannot run sealed),
    and the same alert as a `staff.alert` webhook. Names and error kinds only. Blocks a watcher
    skipped (`ScanGap`) are announced once each, until a person acknowledges them.

    E-mail and webhook each keep their own mark (`apps.core.alerting`): an SMTP outage still emits
    the event once per episode, and an event that could not be recorded never resends the e-mail.
    A webhook-only installation (STAFF_NOTIFY_EMAILS empty) is marked as after an e-mail once the
    event is recorded. Only when nobody at all can be told is the episode left unmarked, for
    whoever is configured next. `health.ok` clears both marks, ending the episode."""
    _alert_failing_watchers()
    _alert_scan_gaps()


def _recipients(no_recipients_key: str, what: str) -> list[str]:
    recipients = list(settings.STAFF_NOTIFY_EMAILS)
    if not recipients and cache.add(no_recipients_key, True, timeout=3600):
        logger.warning("%s and STAFF_NOTIFY_EMAILS is empty; nobody was told by e-mail.", what)
    return recipients


def _send(subject: str, text: str, recipients: list[str]) -> str:
    """alerting.TOLD, NOBODY (no recipients) or FAILED (tried again on the next run)."""
    if not recipients:
        return alerting.NOBODY
    try:
        EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, recipients).send()
    except Exception as exc:  # noqa: BLE001
        logger.error("watcher alert could not be sent: %s", type(exc).__name__)
        return alerting.FAILED
    return alerting.TOLD


def _page() -> str:
    return settings.SITE_URL.rstrip("/") + reverse("staff:needs_person")


def _watcher_letter(rows) -> tuple[str, str]:
    """Subject and text for failing watchers. Most mean payments on them are not being seen; some
    mean something else (an exchange-rate leg: quotes in that currency are refused), and those say
    what it is and what to do (`health.explanation`)."""
    minutes = WATCHER_ALERT_AFTER_S // 60
    unseen, other = [], []
    for _pk, name, kind, *_rest in rows:
        said = health.explanation(name, kind or "")
        (other if said else unseen).append((name, kind, said))
    parts = []
    if unseen:
        listing = "\n".join(f"  {name}: {kind or 'unknown'}" for name, kind, _said in unseen)
        parts.append(f"{len(unseen)} watcher(s) have been failing for over {minutes} minutes, so payments on them "
                     f"are not being seen:\n\n{listing}")
    if other:
        listing = "\n".join(f"  {name}: {said}" for name, _kind, said in other)
        parts.append(f"These have been failing for over {minutes} minutes and need a person:\n\n{listing}")
    if unseen:
        subject = f"Payment watcher failing: {len(unseen)} could not look"
    else:
        legs = [fx.leg_of(name) for name, _kind, _said in other]
        refused = sorted({leg[1] for leg in legs if leg})
        bits = ([f"crypto quotes refused in {', '.join(refused)}"] if refused else []) + \
               [name for (name, _kind, _said), leg in zip(other, legs) if not leg]
        subject = f"Needs a person: {'; '.join(bits)}"
    text = ("\n\n".join(parts) + f"\n\nThey are listed under Needs a person: {_page()}\n"
            "You will not be told again about these until they recover and fail again.\n")
    return subject, text


def _alert_failing_watchers() -> None:
    rows = list(health.failing(WATCHER_ALERT_AFTER_S)
                .filter(Q(alerted_at__isnull=True) | Q(webhook_alerted_at__isnull=True)).order_by("name")
                .values_list("pk", "name", "last_error_kind", "failing_since", "alerted_at", "webhook_alerted_at"))
    if not rows:
        return
    for_email = [row for row in rows if row[4] is None]
    for_webhook = [row for row in rows if row[5] is None]
    webhook = alerting.webhook({"kind": "watcher", "reason": "failing", "count": len(for_webhook),
                                "watchers": [row[1] for row in for_webhook]}) if for_webhook else None
    email = None
    if for_email:
        subject, text = _watcher_letter(for_email)
        email = _send(subject, text, _recipients(NO_RECIPIENTS_KEY, f"{len(for_email)} watcher(s) keep failing"))
    by_email, by_webhook = alerting.marks([(row[0], row[4] is not None, row[5] is not None) for row in rows],
                                          email, webhook)
    since = {row[0]: row[3] for row in rows}
    for column, pks in (("alerted_at", by_email), ("webhook_alerted_at", by_webhook)):
        for pk in pks:
            # Only the episode we reported: one that recovered and failed again meanwhile has a new start.
            WatcherHealth.objects.filter(pk=pk, failing_since=since[pk], **{f"{column}__isnull": True}).update(
                **{column: timezone.now()})


# What a person is asked to do about blocks a watcher left for them (`ScanGap.reason`).
GAP_ALERTS = {
    "skipped_blocks": (
        ("skipped", "unreadable"),
        "Payment watcher skipped blocks: {n} range(s) not scanned",
        "A watcher moved past blocks it did not read, or that no node would list, so a transfer in them "
        "may be missing:",
    ),
    "unexplained_balance": (
        ("unexplained_balance",),
        "Payment watcher: balance rose with no visible transaction in {n} range(s)",
        "A wallet's native balance rose across these blocks and no transaction in them explains it "
        "(an internal transfer from a contract, for example). Nothing was recorded for it:",
    ),
}


def _alert_scan_gaps() -> None:
    for reason, (kinds, subject, opening) in GAP_ALERTS.items():
        _alert_gaps(ScanGap.objects.filter(reason__in=kinds), reason, subject, opening)


def _alert_gaps(rows, reason: str, subject: str, opening: str) -> None:
    gaps = list(rows.filter(acknowledged_at__isnull=True)
                .filter(Q(alerted_at__isnull=True) | Q(webhook_alerted_at__isnull=True)).order_by("pk")
                .values_list("pk", "network", "from_block", "to_block", "alerted_at", "webhook_alerted_at"))
    if not gaps:
        return
    for_email = [gap for gap in gaps if gap[4] is None]
    for_webhook = [gap for gap in gaps if gap[5] is None]
    webhook = alerting.webhook({"kind": "watcher", "reason": reason, "count": len(for_webhook),
                                "networks": sorted({gap[1] for gap in for_webhook})}) if for_webhook else None
    email = None
    if for_email:
        listing = "\n".join(f"  {network}: blocks {lo} to {hi}" for _pk, network, lo, hi, *_rest in for_email)
        email = _send(
            subject.format(n=len(for_email)),
            f"{opening}\n\n{listing}\n\nCheck the wallet's history for those blocks, record anything you find, "
            f"then acknowledge each range under Needs a person: {_page()}\nYou will not be told again about "
            "these ranges.\n",
            _recipients(GAP_NO_RECIPIENTS_KEY, f"{len(for_email)} block range(s) need a person"),
        )
    by_email, by_webhook = alerting.marks([(gap[0], gap[4] is not None, gap[5] is not None) for gap in gaps],
                                          email, webhook)
    for column, pks in (("alerted_at", by_email), ("webhook_alerted_at", by_webhook)):
        if pks:
            ScanGap.objects.filter(pk__in=pks, **{f"{column}__isnull": True}).update(**{column: timezone.now()})


def register_all():
    worker.register("quotes_expire", 60, quotes_expire)
    worker.register("quotes_release", 3600, quotes_release)
    worker.register("quote_capacity", 300, quote_capacity)
    worker.register("chain_scan", 60, chain_scan)
    worker.register("finality_refresh", 60, finality_refresh)
    worker.register("declarations_verify", 300, declarations_verify)
    worker.register("btcpay_poll", 60, btcpay_poll)
    worker.register("lightning_poll", 30, lightning_poll)
    worker.register("watcher_alert", 60, watcher_alert)
