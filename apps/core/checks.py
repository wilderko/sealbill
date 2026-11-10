"""What production must not run with (audit WEB-8), said by `check --deploy` and `manage.py healthcheck`.

An empty PROXY_SECRET is allowed only when ALLOW_EMPTY_PROXY_SECRET says so, and production settings
refuse to start otherwise (red-team A2; residual R2 was the Compose path, with no Apache in front to
send it). It is never silent: without it every local account that can reach the socket can forge the
address the rate limits are keyed on. The `change-me` placeholder that `deploy/env.example` ships is
refused for PROXY_SECRET and for SECRET_KEY, here and at start. Only under production settings; the
messages name no value.
"""
from django.conf import settings
from django.core import checks

PLACEHOLDER = "change-me"

_MESSAGES = {
    "proxy_secret_empty": (
        checks.Warning, "core.W001",
        "PROXY_SECRET is empty: requests are not checked for the reverse proxy's X-Pay-Proxy-Secret, "
        "so any local account that reaches the app can forge X-Forwarded-For.",
        "Set PROXY_SECRET and have the proxy send it (README). Leave it empty only where no other "
        "account can reach the app.",
    ),
    "proxy_secret_placeholder": (
        checks.Error, "core.E001", "PROXY_SECRET is still the placeholder from the example file.",
        "Generate one: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'",
    ),
    "secret_key_placeholder": (
        checks.Error, "core.E002", "SECRET_KEY is still the placeholder from the example file.",
        "Generate one: python3 -c 'import secrets; print(secrets.token_urlsafe(50))'",
    ),
}


def _placeholder(value) -> bool:
    return PLACEHOLDER in str(value or "").strip().lower()


def problems() -> list[str]:
    """The healthcheck codes of what is wrong; always empty outside production settings."""
    if not getattr(settings, "PRODUCTION", False):
        return []
    found = []
    proxy_secret = str(settings.PROXY_SECRET or "").strip()
    if not proxy_secret:
        found.append("proxy_secret_empty")
    elif _placeholder(proxy_secret):
        found.append("proxy_secret_placeholder")
    if _placeholder(settings.SECRET_KEY):
        found.append("secret_key_placeholder")
    return found


@checks.register(checks.Tags.security, deploy=True)
def proxy_secret_and_secret_key(app_configs=None, **kwargs):
    messages = []
    for code in problems():
        level, check_id, msg, hint = _MESSAGES[code]
        messages.append(level(msg, hint=hint, id=check_id))
    return messages


def deny_address(raw):
    """One WEBHOOK_DENY_ADDRESSES entry as the address it denies (an IPv4-mapped IPv6 form as its IPv4),
    or None when it is not an IP address."""
    import ipaddress

    try:
        address = ipaddress.ip_address(str(raw).strip())
    except ValueError:
        return None
    return getattr(address, "ipv4_mapped", None) or address


@checks.register(checks.Tags.security)
def webhook_deny_addresses(app_configs=None, **kwargs):
    """Every WEBHOOK_DENY_ADDRESSES entry must be an IP address (final review F2). An entry that is not
    used to be skipped with a log line, which left the host's own addresses reachable by a webhook: it
    stops `check --deploy` and the migrate pay-web starts with instead. Under every settings; the
    message names the entry's position, never its value."""
    return [
        checks.Error(f"WEBHOOK_DENY_ADDRESSES entry {i + 1} is not an IP address.",
                     hint="List single addresses (no ranges or names), separated by commas.", id="webhooks.E001")
        for i, raw in enumerate(getattr(settings, "WEBHOOK_DENY_ADDRESSES", None) or [])
        if deny_address(raw) is None
    ]


@checks.register(checks.Tags.security)
def webhook_allow_url_prefixes(app_configs=None, **kwargs):
    """Every WEBHOOK_ALLOW_URL_PREFIXES entry must be an https URL on a host name, port 443, with no user,
    query or fragment and a path ending in "/" (audit SEC-5, review S5 M-1). Anything else would lift the
    deny list for more than the one receiver, or for nothing; both stop `check --deploy` and pay-web's start.
    The message names the position only."""
    from apps.webhooks.delivery import allow_prefix_problem

    return [
        checks.Error(f"WEBHOOK_ALLOW_URL_PREFIXES entry {i + 1} is not an https URL prefix ending in a slash.",
                     hint="List full prefixes such as https://shop.example.com/payments/webhook/invoicing/ "
                          "(a host name, not an address; no user, port, query or wildcard), separated by commas.",
                     id="webhooks.E002")
        for i, raw in enumerate(getattr(settings, "WEBHOOK_ALLOW_URL_PREFIXES", None) or [])
        if allow_prefix_problem(raw)
    ]


@checks.register(checks.Tags.security)
def webhook_allow_hosts_replaced(app_configs=None, **kwargs):
    """WEBHOOK_ALLOW_HOSTS allowed every path of a host, `/server-status` included (review S5, M-1). It is
    gone; an environment that still sets it is told what replaced it rather than silently ignored."""
    import os

    if not os.environ.get("WEBHOOK_ALLOW_HOSTS", "").strip():
        return []
    return [checks.Error("WEBHOOK_ALLOW_HOSTS is no longer read: it allowed every path of a host.",
                         hint="Remove it and list the receiver's URL prefix in WEBHOOK_ALLOW_URL_PREFIXES instead, "
                              "such as https://shop.example.com/payments/webhook/invoicing/.",
                         id="webhooks.E003")]
