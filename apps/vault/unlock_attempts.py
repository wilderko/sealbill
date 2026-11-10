"""Wrong passwords at /unlock/ (audit DATA-5).

The page faces the internet whenever the vault is sealed, and staff used to hear nothing of a guess.
Each wrong password is now logged, with no address and no password, and the tenth of an episode is
announced once as a `staff.alert` about the service (recorded while sealed, delivered once unsealed:
the endpoints' URLs and secrets are encrypted). An episode ends when the vault is unsealed, from the
page or with `unseal_vault`, or a day after its first failure.
"""
import logging

from django.core.cache import caches

from .ratelimit import count_attempt, reset

logger = logging.getLogger(__name__)

ALERT_AFTER = 10
EPISODE_S = 24 * 60 * 60
BUCKET = "unlock-failures"
IDENT = "vault"
ALERTED_KEY = "rl:unlock-failures-alerted:vault"


def wrong_password() -> None:
    logger.warning("vault unlock: wrong password")
    failures = count_attempt(BUCKET, IDENT, EPISODE_S)
    if failures >= ALERT_AFTER and _first_alert(failures):
        from apps.webhooks import events

        events.staff_alert({"kind": "vault_unlock_failures", "count": failures})


def _first_alert(failures: int) -> bool:
    try:
        return caches["ratelimit"].add(ALERTED_KEY, True, EPISODE_S)
    except Exception as exc:  # noqa: BLE001 - the counts went to this process's memory as well
        logger.error("unlock-failure alert flag: store unavailable (%s)", type(exc).__name__)
        return failures == ALERT_AFTER


def unsealed() -> None:
    """The episode is over: the next wrong password starts counting from one."""
    try:
        reset(BUCKET, IDENT)
        caches["ratelimit"].delete(ALERTED_KEY)
    except Exception as exc:  # noqa: BLE001 - never stand between a correct password and the vault
        logger.error("unlock-failure counter: store unavailable (%s)", type(exc).__name__)
