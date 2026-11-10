from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.rails.models import ScanGap, WatcherHealth


def ok(name: str) -> None:
    WatcherHealth.objects.update_or_create(
        name=name,
        defaults={"last_ok_at": timezone.now(), "failing_since": None, "alerted_at": None,
                  "webhook_alerted_at": None, "consecutive_failures": 0, "last_error_kind": ""},
    )


def failed(name: str, error_kind: str) -> None:
    h, _ = WatcherHealth.objects.get_or_create(name=name)
    WatcherHealth.objects.filter(pk=h.pk).update(
        consecutive_failures=F("consecutive_failures") + 1, last_error_kind=error_kind[:64]
    )
    WatcherHealth.objects.filter(pk=h.pk, failing_since__isnull=True).update(failing_since=timezone.now())


def failing_now(name: str) -> bool:
    return WatcherHealth.objects.filter(name=name, failing_since__isnull=False).exists()


CAPACITY_EXPLANATION = (
    "Fewer than 20 of the 100 unique amounts for one amount are free on this wallet, so new crypto quotes "
    "at that amount will soon be refused as \"too many payments open\". An amount is free again a day after "
    "its quote closes. Look for unusual quoting, such as many unpaid payment-link orders, and switch off a "
    "link that is being abused.")

SPEED_POLICY_EXPLANATION = (
    "No new BTCPay quote is made on this account: its config.speed_policy settles with no confirmation, "
    "or is not one we know. Set it to MediumSpeed, LowMediumSpeed or LowSpeed on its page under Settings > Rail accounts. Its existing "
    "quotes are still read.")


def explanation(name: str, kind: str) -> str | None:
    """What a failing watcher means when it is not "payments on it are not being seen", and what a
    person should do; None for an ordinary watcher. Used by the staff alert and Needs a person."""
    from apps.rails import fx

    if fx.leg_of(name) is not None:
        return fx.leg_explanation(name, kind)
    if name.startswith("btcpay:") and kind == "speed_policy":
        return SPEED_POLICY_EXPLANATION
    if name.startswith("amounts:"):
        return CAPACITY_EXPLANATION
    return None


def failing(min_age_s: int):
    return WatcherHealth.objects.filter(
        failing_since__isnull=False, failing_since__lte=timezone.now() - timedelta(seconds=min_age_s)
    )


def acknowledge_gap(gap: ScanGap, user, note: str = "") -> bool:
    """A person checked the wallet's history for the blocks `gap` skipped. False when someone
    already had. Audited under the staff member's name (they are signed in, so unsealed)."""
    from apps.audit import services as audit

    now = timezone.now()
    with transaction.atomic():
        if not ScanGap.objects.filter(pk=gap.pk, acknowledged_at__isnull=True).update(
                acknowledged_at=now, acknowledged_by=user):
            return False
        audit.record(user, "scan_gap.acknowledge", gap, {"network": gap.network, "from_block": gap.from_block,
                                                         "to_block": gap.to_block, "note": note})
    gap.acknowledged_at, gap.acknowledged_by = now, user
    return True
