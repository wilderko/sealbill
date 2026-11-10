"""Where the pay page's Back button may lead: an https address on a host the requesting
organization listed in `settings["return_url_hosts"]`.

The URL is parsed strictly. `urlsplit` and `requests` read some strings differently (a backslash
ends the host for one and not for the other; userinfo hides the real host), so anything that
could be read two ways is refused outright; what remains has one unambiguous host."""
import re
from urllib.parse import urlsplit

MAX_LENGTH = 500
_HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


def is_hostname(value: str) -> bool:
    """A bare lowercase hostname: no scheme, port, path or wildcard."""
    return bool(_HOSTNAME.match(value))


def allowed_hosts(org) -> set:
    stored = org.settings.get("return_url_hosts") if isinstance(org.settings, dict) else None
    if not isinstance(stored, list):
        return set()
    return {h.strip().lower() for h in stored if isinstance(h, str) and h.strip()}


def host_of(url: str) -> str:
    """The host of a URL that passes `check`, else ""."""
    try:
        if not url or len(url) > MAX_LENGTH or "\\" in url \
                or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
            return ""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port  # ValueError for a port that is not a number
    except ValueError:
        return ""
    if parts.scheme != "https" or not host or "@" in parts.netloc or port not in (None, 443):
        return ""
    return host


def check(url: str, org) -> bool:
    """True when `url` is https, unambiguous, within 500 characters, and on a host `org` allows."""
    host = host_of(url)
    return bool(host) and host in allowed_hosts(org)
