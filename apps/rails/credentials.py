"""Setting the encrypted credentials of a BTCPay or Lightning rail account, and registering the
BTCPay webhook. Used by `manage.py set_rail_credentials` and by the staff pages, so the receive-only
rules (audit DATA-3) live in one place.

Only a credential that provably cannot spend is stored: an NWC connection with no `pay*` method, an
LND macaroon within invoices:read, invoices:write and info:read, a Core Lightning rune whose methods
are limited to invoice, listinvoices, waitinvoice and getinfo, and a BTCPay key whose permissions, as
BTCPay lists them, are within creating and reading invoices, managing webhooks and viewing the
store's settings. An LNbits invoice key cannot be told from its admin key, so the operator vouches for
it and the audit entry records that they did.

Messages never carry a secret, and neither does the audit entry: it names fields only.
"""
import secrets
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.views.decorators.debug import sensitive_variables

from apps.audit import services as audit
from apps.rails import http
from apps.rails.gateways import btcpay, receive_only
from apps.rails.gateways.lightning import SECRET_FIELD, nwc
from apps.rails.models import CHAIN_KINDS, RailAccount

BTCPAY_FIELDS = ("api_key", "webhook_secret")
NWC_PREFIX = "nostr+walletconnect://"
# Any method whose name contains "pay" can spend: pay_invoice, multi_pay_invoice, pay_keysend,
# multi_pay_keysend, pay_offer (BOLT12) and whatever pay_* a later NIP-47 adds. Refused by name.
SPENDING_MARK = "pay"
REQUIRED_METHODS = ("make_invoice", "lookup_invoice")
LNBITS_FLAG = "--lnbits-key-is-invoice-key"


class CredentialRefused(Exception):
    """The credential was not stored; `.message` says why and never carries a secret."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def fields_for(account: RailAccount) -> tuple[str, ...]:
    if account.kind == "btcpay":
        return BTCPAY_FIELDS
    if account.kind == "lightning":
        backend = (account.config or {}).get("backend")
        if backend not in SECRET_FIELD:
            raise CredentialRefused(f"set config.backend first: one of {', '.join(SECRET_FIELD)}")
        return (SECRET_FIELD[backend],)
    if account.kind in CHAIN_KINDS:
        raise CredentialRefused(f"a {account.kind} account has no credentials: it is watched by its address alone")
    raise CredentialRefused(f"unknown kind {account.kind!r}")


@sensitive_variables()
def set_credentials(account: RailAccount, given: dict[str, str], *, actor=None, actor_name: str = "",
                    lnbits_vouched: bool = False) -> list[str]:
    """Merge `given` (empty values dropped) over the stored credentials after the receive-only checks,
    save and audit. Returns the saved field names, sorted; [] when nothing was given. The row is locked
    for the whole read-modify-write, so two saves cannot revert each other's field."""
    with transaction.atomic():
        # NO KEY UPDATE: a plain FOR UPDATE would block the foreign-key checks of a quote or payment
        # being inserted for this account while we wait on the network.
        locked = RailAccount.objects.select_for_update(no_key=True).get(pk=account.pk)
        names = fields_for(locked)
        stored = dict(locked.credentials or {})
        backend = (locked.config or {}).get("backend") if locked.kind == "lightning" else None
        if lnbits_vouched and backend != "lnbits":
            raise CredentialRefused(f"{LNBITS_FLAG} is only for an LNbits account")
        given = {k: v for k, v in given.items() if k in names and v}
        for name in names:
            if name not in given and not stored.get(name):
                raise CredentialRefused(f"{name} is required")
        if "nwc_url" in given and not given["nwc_url"].startswith(NWC_PREFIX):
            raise CredentialRefused(f"nwc_url must be a {NWC_PREFIX}... connection string (the value is not shown)")
        if not given:
            return []
        merged = {**stored, **given}
        _require_receive_only(locked, backend, given, merged, lnbits_vouched)
        _store_credentials(locked, merged, sorted(given), actor, actor_name, lnbits_vouched)
    account.credentials = merged
    return sorted(given)


def _store_credentials(locked, merged, fields, actor, actor_name="", lnbits_vouched=False) -> None:
    """Save and audit credentials already checked by the caller, under its row lock."""
    detail = {"rail_account": locked.pk, "kind": locked.kind, "fields": fields}
    if lnbits_vouched:
        detail["lnbits_key_is_invoice_key"] = True
    if actor is not None:
        detail["via"] = "staff"  # a signed-in person; the command line names itself with actor_name
    locked.credentials = merged
    locked.save(update_fields=["credentials"])
    audit.record(actor, "rail_account.credentials", locked, detail, actor_name=actor_name)


def _require_receive_only(account, backend, given, merged, lnbits_vouched) -> None:
    """Refuse a credential that can spend, cannot do the job, or cannot be shown not to spend."""
    try:
        if account.kind == "btcpay":
            # The key in effect is checked even when only the webhook secret changes: a key
            # stored before this check existed is not grandfathered in.
            _require_btcpay_receive_only(account, merged["api_key"])
        elif backend == "nwc":
            _require_nwc_receive_only(given["nwc_url"])
        elif backend == "lnd":
            receive_only.check_lnd(given["macaroon"])
        elif backend == "cln":
            receive_only.check_cln(given["rune"])
        elif backend == "lnbits" and not lnbits_vouched:
            raise CredentialRefused(
                "LNbits' Admin key and its Invoice/read key cannot be told apart without asking LNbits, and "
                "the Admin key can spend. Paste the wallet's Invoice/read key (never the Admin key) and run "
                f"again with {LNBITS_FLAG}: the audit log records that you vouched for it.")
    except receive_only.NotReceiveOnly as exc:
        raise CredentialRefused(str(exc)) from None


def _require_btcpay_receive_only(account, api_key: str) -> None:
    """Ask BTCPay which permissions the key has (a real request outside tests)."""
    cfg = account.config or {}
    base, store = str(cfg.get("url") or "").strip(), str(cfg.get("store_id") or "")
    if not base or not store:
        raise CredentialRefused("set config.url and config.store_id first: BTCPay is asked what the key may do")
    try:
        permissions = btcpay.key_permissions(base, api_key)
    except http.Refused:
        raise CredentialRefused("BTCPay refused this API key (it does not exist, was revoked, or is for "
                                "another server); nothing was stored") from None
    except http.Unavailable:
        raise CredentialRefused("could not ask BTCPay which permissions this key has; check that BTCPay is "
                                "reachable and try again (nothing was stored)") from None
    receive_only.check_btcpay(permissions, store)


RELAY_REFUSALS = {
    "private_address": "a relay of this connection is on this machine or a private network",
    "unresolvable": "a relay of this connection could not be resolved",
    "bad_url": "a relay of this connection is not a usable address",
}


def _require_public_relays(nwc_url: str) -> None:
    """Every `relay=` of the connection must be wss:// on a host that resolves to public addresses only
    (review S5, M-2). nostr-sdk opens its own websocket to each one, outside the guard every other rail URL
    goes through (`http.destination_problem`, STAFF_URL), so this is checked as the credential is saved; it
    resolves again when it connects. Messages never name a relay: the connection string is a secret."""
    from urllib.parse import parse_qs

    try:
        relays = [r.strip() for r in parse_qs(urlsplit(nwc_url).query).get("relay", [])]
    except ValueError:
        relays = []
    if not relays:
        raise CredentialRefused("this connection names no relay: copy the whole connection string from Alby Hub")
    for relay in relays:
        if not relay.lower().startswith("wss://"):
            raise CredentialRefused("every relay of this connection must be a wss:// address")
        problem = http.destination_problem("https://" + relay[len("wss://"):], http.STAFF_URL)
        if problem:
            reason = RELAY_REFUSALS.get(problem, RELAY_REFUSALS["bad_url"])
            raise CredentialRefused(f"{reason}; nothing was stored. Use a public wss:// relay, such as Alby's")


def _require_nwc_receive_only(nwc_url: str) -> None:
    """Refuse a connection whose relays are not public, that can spend, or that cannot do the job. Messages
    never carry the URL."""
    _require_public_relays(nwc_url)
    try:
        methods = nwc.capabilities(nwc_url)
    except http.Unavailable:
        raise CredentialRefused("could not ask the wallet which methods this connection allows; "
                                "unlock Alby Hub and try again") from None
    spending = sorted(m for m in methods if SPENDING_MARK in m)
    if spending:
        raise CredentialRefused("this connection can spend money (" + ", ".join(spending) + "): create a "
                                "receive-only connection in Alby Hub (make_invoice and lookup_invoice only)")
    missing = [m for m in REQUIRED_METHODS if m not in methods]
    if missing:
        raise CredentialRefused("this connection lacks " + ", ".join(missing) + ": create a connection that "
                                "allows make_invoice and lookup_invoice")


def _norm_url(url) -> str:
    parts = urlsplit(str(url or "").strip())
    path = parts.path[:-1] if parts.path.endswith("/") else parts.path
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}"


def _find_hook(listing, hook_url):
    if not isinstance(listing, list):
        return None
    return next((h for h in listing if isinstance(h, dict) and _norm_url(h.get("url")) == _norm_url(hook_url)), None)


def register_btcpay_webhook(account: RailAccount, *, actor) -> str:
    """Create the webhook on the BTCPay store that calls us, with a fresh secret, and store the secret.
    Refuses when one for our URL already exists: its secret is not known here, so a second one could not
    be told from it. Once a webhook may exist in BTCPay no message says "nothing was registered": the
    hook is deleted again if its secret cannot be kept, and if that fails too its id is named."""
    if account.kind != "btcpay":
        raise CredentialRefused("only a BTCPay account has a webhook")
    hook_id = None  # None: nothing was created in BTCPay by us
    locked = None
    try:
        # Nothing in this block may register an on_commit callback that can raise: an exception after the
        # commit would DELETE a hook whose secret was stored.
        with transaction.atomic():
            locked = RailAccount.objects.select_for_update(no_key=True).get(pk=account.pk)
            cfg = locked.config or {}
            base, store = str(cfg.get("url") or "").strip().rstrip("/"), str(cfg.get("store_id") or "")
            api_key = (locked.credentials or {}).get("api_key", "")
            if not (base and store and api_key):
                raise CredentialRefused("set the URL, the store id and the API key first")
            try:
                _require_btcpay_receive_only(locked, api_key)  # a key that can do more never writes; checked once
            except receive_only.NotReceiveOnly as exc:
                raise CredentialRefused(str(exc)) from None
            hook_url = settings.SITE_URL.rstrip("/") + reverse("btcpay_webhook", kwargs={"account_pk": locked.pk})
            path = f"{base}/api/v1/stores/{store}/webhooks"
            headers = {"Authorization": f"token {api_key}", "Accept": "application/json"}
            try:
                existing = http.get_json(path, headers=headers, service=btcpay.SERVICE, guard=http.STAFF_URL)
                if not isinstance(existing, list):
                    raise CredentialRefused("BTCPay answered in a shape not understood; nothing was registered")
                if _find_hook(existing, hook_url):
                    raise CredentialRefused("a webhook for this address is already registered in BTCPay: delete it "
                                            "there first, because its secret is not known here")
            except http.Refused:
                raise CredentialRefused("BTCPay refused to list webhooks with this key; nothing was "
                                        "registered") from None
            except http.Unavailable:
                raise CredentialRefused("could not reach BTCPay; nothing was registered, try again") from None
            secret = secrets.token_urlsafe(32)
            body = {"enabled": True, "automaticRedelivery": True, "url": hook_url,
                    "authorizedEvents": {"everything": True}, "secret": secret}
            try:
                created = http.post_json(path, body, headers=headers, service=btcpay.SERVICE, guard=http.STAFF_URL)
                hook_id = _id_of(created)
            except http.Refused:
                raise CredentialRefused("BTCPay refused to create the webhook; nothing was registered") from None
            except http.Unavailable:
                # The outcome is unknown: BTCPay may have created it. We saw none a moment ago, so one for
                # our URL now is ours.
                try:
                    listing = http.get_json(path, headers=headers, service=btcpay.SERVICE, guard=http.STAFF_URL)
                    found = _find_hook(listing, hook_url)
                except (http.Unavailable, http.Refused):
                    raise CredentialRefused(
                        f"BTCPay did not confirm the webhook and could not be asked again. Open the webhooks of "
                        f"the store in BTCPay and delete any webhook for {hook_url} before trying again.") from None
                if not found:
                    raise CredentialRefused(
                        f"BTCPay did not confirm the webhook and none is listed yet; if a webhook for {hook_url} "
                        f"appears in BTCPay, delete it there before trying again.") from None
                hook_id = _id_of(found)
            # The key was checked above under this same lock: the secret is stored without asking again.
            _store_credentials(locked, {**(locked.credentials or {}), "webhook_secret": secret}, ["webhook_secret"], actor)
            audit.record(actor, "rail_account.btcpay_webhook", locked,
                         {"rail_account": locked.pk, "webhook_id": hook_id, **({"via": "staff"} if actor is not None else {})})
    except Exception:
        if hook_id is None:
            raise
        # A hook exists and keeping its secret failed (also at commit): remove it, or name it.
        _remove_hook(path, hook_id, hook_url, headers)
        raise CredentialRefused(
            "the webhook was created in BTCPay but its secret could not be stored here; it was removed "
            "again. Nothing is registered, try again.") from None
    account.credentials = locked.credentials
    return hook_id


def _id_of(answer) -> str:
    value = answer.get("id") if isinstance(answer, dict) else None
    return "" if value is None else str(value)


def _remove_hook(path, hook_id, hook_url, headers) -> None:
    """Delete the hook we just created. Raises CredentialRefused naming it when that is not possible."""
    if hook_id:
        try:
            http.delete(f"{path}/{hook_id}", headers=headers, service=btcpay.SERVICE, guard=http.STAFF_URL)
            return
        except (http.Unavailable, http.Refused):
            pass
    which = f"webhook {hook_id}" if hook_id else f"the webhook for {hook_url}"
    raise CredentialRefused(f"{which} exists in BTCPay but its secret could not be stored here and it could not "
                            f"be removed. Delete it in BTCPay before trying again.")
