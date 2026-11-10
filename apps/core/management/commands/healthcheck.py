"""Exit 0 when the service is healthy, 1 with one line per problem (a short code and a count).

For cron or an external monitor. Prints no names and no secrets. Unless `--no-network` it also
asks the rate sources for BTC/EUR directly: that is the source of truth for "can we price". Under
production settings it also reports what `check --deploy` refuses or warns about (apps.core.checks).
"""
import logging
import sys

from django.core.management.base import BaseCommand

from apps.core import checks as deploy_checks, health

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Check the worker heartbeat, the vault, the watchers, the queues and (unless --no-network) the rates."

    def add_arguments(self, parser):
        parser.add_argument("--max-worker-age", type=int, default=health.STALE_AFTER_S)
        parser.add_argument("--no-network", action="store_true", help="do not ask the rate sources")

    def handle(self, *args, max_worker_age, no_network, **options):
        problems = []
        try:
            snap = health.snapshot()
        except Exception as exc:  # noqa: BLE001
            logger.error("healthcheck: database unreadable (%s)", type(exc).__name__)
            snap = None
            problems.append("db_error 1")
        if snap is not None:
            age = snap["worker_age_s"]
            if age is None:
                problems.append("worker_missing 1")
            elif age > max_worker_age:
                problems.append(f"worker_stale {age}")
            if snap["vault"] == "sealed":
                problems.append("vault_sealed 1")
            for key in ("failing_watchers", "outbox_failed", "webhooks_failed"):
                if snap[key] > 0:
                    problems.append(f"{key} {snap[key]}")
        # Under production settings: an empty or placeholder PROXY_SECRET, a placeholder SECRET_KEY.
        problems.extend(f"{code} 1" for code in deploy_checks.problems())
        if not no_network:
            from apps.rails import fx

            try:
                fx.price_in("BTC", "EUR", use_cache=False)
            except Exception as exc:  # noqa: BLE001 - unavailable, diverging, or anything else: cannot price
                logger.warning("healthcheck: rates probe failed (%s)", type(exc).__name__)
                problems.append("fx_unavailable 1")
        if problems:
            for line in problems:
                self.stdout.write(line)
            sys.exit(1)
