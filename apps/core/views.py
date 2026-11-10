import logging

from django.http import JsonResponse

from apps.core import health
from apps.vault.vault import vault

logger = logging.getLogger(__name__)


def healthz(request):
    try:
        snap = health.snapshot()
    except Exception as exc:  # noqa: BLE001 - any failure to read means the database cannot be read
        logger.error("healthz: database unreadable (%s)", type(exc).__name__)
        return JsonResponse({"status": "error", "vault": "unsealed" if vault.is_unsealed() else "sealed",
                             "db": "error", "worker_age_s": None, "failing_watchers": None,
                             "outbox_failed": None, "webhooks_failed": None}, status=503)
    return JsonResponse({"status": "degraded" if health.is_degraded(snap) else "ok", **snap})
