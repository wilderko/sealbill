"""What the health endpoint and `manage.py healthcheck` both report: counts and ages only,
never a name, an address or a secret."""
from django.utils import timezone

from apps.core.models import WorkerHeartbeat
from apps.notify.models import OutboxMessage
from apps.rails import health as watcher_health
from apps.rails.jobs import WATCHER_ALERT_AFTER_S
from apps.vault.vault import vault
from apps.webhooks.models import WebhookDelivery

STALE_AFTER_S = 300


def snapshot() -> dict:
    """Raises when the database cannot be read; the caller turns that into "error"."""
    beat = WorkerHeartbeat.objects.filter(pk=1).first()
    age = None if beat is None else max(0, int((timezone.now() - beat.beat_at).total_seconds()))
    return {
        "vault": "unsealed" if vault.is_unsealed() else "sealed",
        "db": "ok",
        "worker_age_s": age,
        # As long as the watcher alert waits: one failed pass is a blip the next one usually clears.
        "failing_watchers": watcher_health.failing(WATCHER_ALERT_AFTER_S).count(),
        "outbox_failed": OutboxMessage.objects.filter(state="failed").count(),
        "webhooks_failed": WebhookDelivery.objects.filter(state="failed").count(),
    }


def is_degraded(snap: dict, max_worker_age: int = STALE_AFTER_S) -> bool:
    age = snap["worker_age_s"]
    return (age is None or age > max_worker_age or snap["vault"] == "sealed"
            or any(snap[k] > 0 for k in ("failing_watchers", "outbox_failed", "webhooks_failed")))
