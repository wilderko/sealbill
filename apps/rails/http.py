"""One HTTP seam for every chain and gateway call.

Never logs a URL, a query string or a header: keys travel in all three.
Only the service name and the kind of failure are recorded.

A URL staff typed (a BTCPay server, an LND, CLN or LNbits node) is called with a `guard`, which applies
the webhook resolver's private-address rule (audit SEC-4): the host must resolve only to public
addresses, the connection opens a socket only to an address it has just checked (so a name that
answers differently to a second lookup cannot steer it), and the scheme must be https.
`LOCAL_NODE` (LND, CLN) may also reach this machine, but only on `LOCAL_LIGHTNING_PORT`, and only there
over plain http. Unset, no loopback address is reachable at all: a credential header must not be sent to
whatever else listens on 127.0.0.1. Chain and price endpoints come from settings and are not guarded.
"""

import contextlib
import contextvars
import functools
import ipaddress
import logging
import socket
from collections.abc import Callable
from typing import TypeVar
from urllib.parse import urlsplit

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError
from urllib3.util import connection as urllib3_connection

logger = logging.getLogger(__name__)
T = TypeVar("T")

STAFF_URL = "staff_url"  # a URL staff typed: public addresses only, https only
LOCAL_NODE = "local_node"  # LND or CLN: also this machine, on LOCAL_LIGHTNING_PORT only
GUARDS = (STAFF_URL, LOCAL_NODE)


class Unavailable(Exception):
    """Could not get an answer (timeout, connection, 429, 5xx, bad JSON): try elsewhere or later."""

    def __init__(self, service: str, kind: str):
        super().__init__(f"{service} unavailable: {kind}")
        self.service, self.kind = service, kind


class Refused(Exception):
    """The service understood and said no (4xx other than 429)."""

    def __init__(self, service: str, status: int):
        super().__init__(f"{service} refused: {status}")
        self.service, self.status = service, status


_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("rails_http_deadline", default=None)


@contextlib.contextmanager
def request_budget(timeout: float = 3):
    """Inside a web request: every call here waits at most `timeout` seconds and `first_answer`
    tries only the first endpoint. Worker-side calls, outside the block, are unchanged."""
    token = _deadline.set(float(timeout))
    try:
        yield
    finally:
        _deadline.reset(token)


def capped(timeout):
    """`timeout`, cut to the active `request_budget` if there is one. For waits that are not an
    HTTP call made here (the NWC relay connection)."""
    cap = _deadline.get()
    return timeout if cap is None else min(timeout, cap)


_capped = capped


def _enabled():
    if not getattr(settings, "RAILS_HTTP_ENABLED", True):
        raise RuntimeError("outbound rails HTTP is disabled (RAILS_HTTP_ENABLED=False)")


def _unavailable(service, kind):
    logger.warning("rails http: %s unavailable (%s)", service, kind)
    return Unavailable(service, kind)


def _answer(call, service, *, parse=True):
    try:
        r = call()
    except requests.Timeout:
        raise _unavailable(service, "timeout") from None
    except requests.RequestException:
        raise _unavailable(service, "connection") from None
    status = r.status_code
    if 300 <= status < 400:  # redirects are never followed: key headers must not leave the host
        raise _unavailable(service, "redirect")
    if status == 429 or status >= 500:
        raise _unavailable(service, str(status))
    if status >= 400:
        logger.warning("rails http: %s refused (%s)", service, status)
        raise Refused(service, status)
    if not parse:
        return None
    try:
        return r.json()
    except ValueError:
        raise _unavailable(service, "invalid_json") from None


# ---------------------------------------------------------------------------
# Where a staff-chosen URL may lead (audit SEC-4)
# ---------------------------------------------------------------------------
class DestinationRefused(Exception):
    """The URL may not be called under its guard. `code`: bad_url, scheme, private_address, unresolvable."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def local_lightning_port() -> int | None:
    port = getattr(settings, "LOCAL_LIGHTNING_PORT", None)
    return port if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536 else None


def _shape(url: str, guard: str) -> tuple[str, int, bool]:
    """(host, port, plain http) of a URL `guard` may call as far as its text goes, else DestinationRefused.
    A string `urlsplit` and `requests` could read two ways is refused, so the host checked is the host
    called. Plain http only for the local node, on its port."""
    if guard not in GUARDS:
        raise ValueError(f"unknown guard {guard!r}")
    if not isinstance(url, str) or not url or "\\" in url or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127
                                                                 for ch in url):
        raise DestinationRefused("bad_url")
    try:
        parts = urlsplit(url)
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        raise DestinationRefused("bad_url") from None
    if "@" in parts.netloc or not host:
        raise DestinationRefused("bad_url")
    scheme = parts.scheme.lower()
    port = port or {"https": 443, "http": 80}.get(scheme, 0)
    if scheme == "https":
        return host, port, False
    if scheme == "http" and guard == LOCAL_NODE and port == local_lightning_port():
        return host, port, True
    raise DestinationRefused("scheme")


def _loopback(address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(address.is_loopback or (mapped is not None and mapped.is_loopback))


# The system resolver, as imported. Under the test runner (RAILS_DNS_ENABLED=False) a rail lookup is refused
# unless a test has replaced `socket.getaddrinfo`. The rails' own gate, not the webhooks' (review S5, M-3):
# pausing webhook lookups (WEBHOOK_DNS_ENABLED) must never stop a BTCPay or Lightning call.
_SYSTEM_RESOLVER = socket.getaddrinfo


def _addresses(host: str, port: int) -> list:
    """Every address `host` is (a literal) or resolves to; DestinationRefused("unresolvable") when it does
    not resolve."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    if not getattr(settings, "RAILS_DNS_ENABLED", True) and socket.getaddrinfo is _SYSTEM_RESOLVER:
        raise RuntimeError("rail name lookups are disabled (RAILS_DNS_ENABLED=False): mock socket.getaddrinfo")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise DestinationRefused("unresolvable") from None
    found = []
    for *_rest, sockaddr in infos:
        try:
            address = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        except ValueError:
            raise DestinationRefused("unresolvable") from None
        if address not in found:
            found.append(address)
    if not found:
        raise DestinationRefused("unresolvable")
    return found


def checked_addresses(host: str, port: int, guard: str, *, plain_http: bool = False) -> list[str]:
    """Every address `host` resolves to, each one `guard` allows; else DestinationRefused. The webhook
    resolver's rule: public addresses only. The local node may also use loopback on its port, and plain
    http only there. The lookup goes through the rails' own gate (`_addresses`)."""
    from apps.webhooks.delivery import _public

    found = _addresses(host.strip("[]"), port)
    local = guard == LOCAL_NODE and local_lightning_port() is not None and port == local_lightning_port()
    for address in found:
        if local and _loopback(address):
            continue
        if plain_http:
            raise DestinationRefused("scheme")  # never a credential in the clear beyond this machine
        if not _public(address):
            raise DestinationRefused("private_address")
    return [str(a) for a in found]


def destination_problem(url: str, guard: str) -> str | None:
    """Why `url` may not be called under `guard` (a DestinationRefused code), or None. Resolves the host:
    for checking a URL as it is saved."""
    try:
        host, port, plain = _shape(url, guard)
        checked_addresses(host, port, guard, plain_http=plain)
    except DestinationRefused as exc:
        return exc.code
    return None


class _GuardedConnection:
    _guard = STAFF_URL
    _plain = False

    def _new_conn(self):
        last = None
        per_try = self.timeout if isinstance(self.timeout, (int, float)) else 10
        for address in checked_addresses(self._dns_host, self.port, self._guard, plain_http=self._plain):
            try:
                return urllib3_connection.create_connection(
                    (address, self.port), per_try, source_address=self.source_address,
                    socket_options=self.socket_options)
            except TimeoutError:
                last = ConnectTimeoutError(self, "connection timed out")
            except OSError:
                last = NewConnectionError(self, "failed to establish a new connection")
        raise last


@functools.cache
def _pool_classes(guard: str) -> dict:
    """The connection pools of `guard`, one class each, made once (plain http only ever for the local node)."""
    conns = {
        "https": type("GuardedHTTPS", (_GuardedConnection, HTTPSConnection), {"_guard": guard}),
        "http": type("GuardedHTTP", (_GuardedConnection, HTTPConnection), {"_guard": guard, "_plain": True}),
    }
    return {
        "https": type("GuardedHTTPSPool", (HTTPSConnectionPool,), {"ConnectionCls": conns["https"]}),
        "http": type("GuardedHTTPPool", (HTTPConnectionPool,), {"ConnectionCls": conns["http"]}),
    }


class _GuardedAdapter(HTTPAdapter):
    def __init__(self, guard: str):
        self._pools = _pool_classes(guard)
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = self._pools


def _send(method: str, url: str, guard: str | None, service: str, **kwargs):
    if guard is None:
        return getattr(requests, method)(url, **kwargs)
    try:
        _shape(url, guard)
        with requests.Session() as session:
            session.trust_env = False  # no proxy from the environment: the guard must see the real destination
            adapter = _GuardedAdapter(guard)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            return getattr(session, method)(url, **kwargs)
    except DestinationRefused as exc:
        raise _unavailable(service, exc.code) from None


def get_json(url, *, params=None, headers=None, service: str, timeout=10, guard: str | None = None):
    _enabled()
    return _answer(lambda: _send("get", url, guard, service, params=params, headers=headers, timeout=_capped(timeout),
                                 allow_redirects=False), service)


def post_json(url, payload, *, headers=None, service: str, timeout=10, guard: str | None = None):
    _enabled()
    return _answer(lambda: _send("post", url, guard, service, json=payload, headers=headers, timeout=_capped(timeout),
                                 allow_redirects=False), service)


def delete(url, *, headers=None, service: str, timeout=10, guard: str | None = None):
    """DELETE; the answer's body (usually empty) is not read."""
    _enabled()
    return _answer(lambda: _send("delete", url, guard, service, headers=headers, timeout=_capped(timeout),
                                 allow_redirects=False), service, parse=False)


def first_answer(urls: list[str], call: Callable[[str], T]) -> T:
    last: Unavailable | None = None
    if _deadline.get() is not None:
        urls = urls[:1]  # no failover inside a web request
    for url in urls:
        try:
            return call(url)
        except Unavailable as exc:
            last = exc
    if last is None:
        raise ValueError("no endpoints")
    raise last
