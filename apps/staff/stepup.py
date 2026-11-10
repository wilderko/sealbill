"""The step-up for the staff powers that can move money or reroute a client (audit SEC-3).

These changes need a superuser who gave a second factor within the last ten minutes
(`passkeys.recently_verified`, the rule of the Security and Team pages):
- creating, rescoping or revoking an API token;
- a rail account and its credentials (and the BTCPay webhook, which stores a secret);
- an organization's US-resident issuer flag and its return hosts;
- a bank account or a payment link;
- adding a contact's e-mail address or making one primary (letters, pay links and PDFs go there);
- removing another staff member's second factor.

Without it nothing changes. A staff member who is not a superuser gets a 403. A superuser whose
second factor is older is sent to verify again, as the Security page does, and comes back to the page.

Every such change e-mails every active superuser through the outbox (`staff_change_staff`), in the
same transaction as the change where the change has one: who, what and when, by names and ids. Never
a secret, an IBAN, an e-mail address or a credential: staff letters go in plaintext.
"""
import datetime
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.utils import timezone

from apps.notify import services as notify

from . import passkeys
from .views_security import _reverify_url

logger = logging.getLogger(__name__)

KIND = "staff_change_staff"
SUPERUSER_ONLY = "Only a superuser may make this change. Nothing was changed."
CONFIRM_FIRST = ("Confirm your second factor first: this change needs one given in the last ten minutes. "
                 "Nothing was changed.")


def refusal(request, back: str):
    """None when `request` may make a step-up change; else the answer that refuses it, having changed
    nothing: a 403 for a staff member who is not a superuser, and for a superuser without a recent
    second factor the verify page, which returns to `back` (a path on this site)."""
    if not request.user.is_superuser:
        return HttpResponseForbidden(SUPERUSER_ONLY)
    if not passkeys.recently_verified(request):
        messages.warning(request, CONFIRM_FIRST)
        return redirect(_reverify_url(back))
    return None


def page_state(request) -> dict:
    """What the pages say about the step-up before anything is pressed (the notice include)."""
    return {"superuser": bool(request.user.is_superuser), "fresh": passkeys.recently_verified(request),
            "reverify_url": _reverify_url(request.get_full_path())}


def _recipients() -> list[str]:
    User = get_user_model()
    found = sorted({e.strip() for e in User._default_manager.filter(is_active=True, is_superuser=True)
                   .values_list("email", flat=True) if e and e.strip()})
    if found:
        return found
    # No superuser has an address: the people who get staff alerts are told instead, so the change is
    # never announced to nobody while someone could have been told.
    return list(settings.STAFF_NOTIFY_EMAILS)


def announce(actor, change: str, what: str, path: str):
    """Queue the e-mail about one step-up change to every active superuser. `change` is a few words for
    the subject ("API token revoked"), `what` the sentence naming it by names and ids, `path` the staff
    page it can be seen on. Returns the OutboxMessage, or None when there is nobody to tell (logged)."""
    to = _recipients()
    if not to:
        logger.warning("Step-up change %r made by %s; no superuser has an e-mail address and "
                       "STAFF_NOTIFY_EMAILS is empty, so nobody was told.", change, actor.get_username())
        return None
    return notify.enqueue(KIND, to=to, language="en", context={
        "ref": change,
        "who": actor.get_username(),
        "what": what,
        "when": timezone.now().astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "admin_url": settings.SITE_URL.rstrip("/") + path,
    })
