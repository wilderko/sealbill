"""A gateway rail with nothing to poll is still asked whether it answers.

`lightning_poll` and `btcpay_poll` read a rail only while it has quotes that may have news, and only
a read records its health. On 2026-11-10 Alby Hub came back from an update locked, `lightning:4` was
marked failing, its last quote left the 24-hour poll window before anyone unlocked the hub, and the
rail stayed "failing" with nothing left to ask: the health check mailed every ten minutes about a node
that had been working for hours. The other way round, a rail that stopped answering while it had no
quotes was noticed only when a client tried to pay.

So each pass also asks every watched rail of its kind that nothing has asked for `PROBE_SECONDS`
(a poll counts) with one read-only call (`gateway.probe()`), and records the answer as a poll would.
A gateway with nothing to ask with (no info call, no invoice ever made) records nothing. Logs carry
the watcher name and error kinds only.
"""
import logging

from django.core.cache import cache

from apps.quotes.errors import QuoteError
from apps.rails import health, http
from apps.rails.gateways import registry
from apps.vault.vault import VaultSealed, vault

logger = logging.getLogger(__name__)

PROBE_SECONDS = 300
_KEY = "rails:idle-probe:{}"


def probe_idle(kind: str, polled, answered=None) -> None:
    """Probe the watched `kind` rails not in `polled` (account pks) that nothing asked within the last
    `PROBE_SECONDS`. `answered(name, gateway)` records a rail that answered; it defaults to `health.ok`.
    Stops at once when the vault seals."""
    from apps.rails.models import RailAccount

    for pk in polled:  # just asked by the poll: its health is recorded, no probe for a while
        cache.set(_KEY.format(f"{kind}:{pk}"), 1, PROBE_SECONDS)
    record_ok = answered or (lambda name, _gateway: health.ok(name))
    for account in RailAccount.objects.watched().filter(kind=kind).exclude(pk__in=list(polled)).order_by("pk"):
        name = f"{kind}:{account.pk}"
        if not cache.add(_KEY.format(name), 1, PROBE_SECONDS):
            continue  # asked within the last PROBE_SECONDS
        try:
            gateway = registry.for_account(account)
            asked = gateway.probe()
        except VaultSealed:
            return
        except QuoteError:
            if not vault.is_unsealed():
                return
            continue  # not configured: the poll reports that once the rail has a quote
        except http.Unavailable as exc:
            health.failed(name, exc.kind)
            continue
        except http.Refused:
            health.failed(name, "refused")
            continue
        except Exception as exc:  # noqa: BLE001 - one rail must not stop the others
            logger.error("idle probe failed for %s (%s)", name, type(exc).__name__)
            health.failed(name, "error")
            continue
        if asked:
            record_ok(name, gateway)
