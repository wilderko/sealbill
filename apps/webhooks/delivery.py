"""Delivering webhook events: signed, retried, logged, and never sent anywhere private.

Each delivery is a POST of the event as canonical JSON (sorted keys, compact separators),
signed with HMAC-SHA256 over exactly those bytes (`X-Pay-Signature: sha256=<hex>`). A 2xx answer
is `sent`; anything else, a redirect included (redirects are never followed), waits
`BACKOFF_MINUTES` and is tried again, then is `failed` for a person to retry.

The destination must be https (http only with `WEBHOOK_ALLOW_HTTP`) and every address its host
resolves to must be public (`WEBHOOK_ALLOW_PRIVATE` lifts that for development) and not one of
`WEBHOOK_DENY_ADDRESSES`, this host's own (audit SEC-5): a URL under one of `WEBHOOK_ALLOW_URL_PREFIXES`, a
receiver that lives on that address too, is the one exception, by URL prefix and never by host. It is checked
when the endpoint is created, before every delivery, and again by the connection itself, which
opens a socket only to an address it has just checked: a name that answers one address to the
check and another to the connection (DNS rebinding) is still refused.

Never logs a URL, a header or an exception's message (any of them may carry a secret): only a
delivery's id, its attempt and the kind of failure.
"""
import contextvars
import datetime
import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import logging
import re
import secrets
import socket
import time
from urllib.parse import unquote, urlsplit

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import Min, Q
from django.urls import reverse
from django.utils import timezone
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError
from urllib3.util import connection as urllib3_connection

from apps.audit import services as audit
from apps.vault.fields import UndecryptableValue
from apps.vault.vault import VaultSealed, vault
from apps.webhooks import events as webhook_events
from apps.webhooks.models import WebhookDelivery, WebhookEndpoint, WebhookEvent

logger = logging.getLogger(__name__)

# Minutes to wait after each failed attempt, as for e-mail: about sixteen hours in all, so a
# receiver that is down for most of a day loses nothing. The attempt after the last wait is final.
BACKOFF_MINUTES = (2, 5, 15, 30, 60, 120, 240, 480)
MAX_ATTEMPTS = len(BACKOFF_MINUTES) + 1
TIMEOUT_S = 10  # each connect and each read
DELIVERY_DEADLINE_S = 20  # one attempt in all: a receiver that trickles its answer is cut off here
SECRET_BYTES = 32
REFUSAL_CODES = ("scheme", "private_address", "unresolvable", "bad_url", "port", "endpoint_limit")
MAX_ENDPOINTS_PER_ORG = 10  # active ones
FLUSH_BUDGET_S = 30  # `flush` starts no new attempt after this much wall clock
NO_RECIPIENTS_KEY = "worker:webhook-alert-no-recipients"


class WebhookRefused(Exception):
    """The destination is not one a webhook may go to. `code` is one of REFUSAL_CODES."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class DeliveryDeadline(Exception):
    """The attempt ran past DELIVERY_DEADLINE_S in all (a failed attempt of kind `deadline`)."""


def _monotonic() -> float:
    """The clock the flush budget runs on (a test replaces it)."""
    return time.monotonic()


# The system resolver, as imported. Under the test runner (WEBHOOK_DNS_ENABLED=False) a lookup
# is refused unless a test has replaced `socket.getaddrinfo`.
_SYSTEM_RESOLVER = socket.getaddrinfo


# ---------------------------------------------------------------------------
# Where a webhook may go
# ---------------------------------------------------------------------------
def _public(address) -> bool:
    # fec0::/10 (deprecated IPv6 site-local) counts as global to `ipaddress`; it is not.
    return (address.is_global and not address.is_multicast and not address.is_reserved
            and not getattr(address, "is_site_local", False))


def _resolve(host: str, port: int):
    if not getattr(settings, "WEBHOOK_DNS_ENABLED", True) and socket.getaddrinfo is _SYSTEM_RESOLVER:
        raise RuntimeError("webhook name lookups are disabled (WEBHOOK_DNS_ENABLED=False): mock socket.getaddrinfo")
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _addresses(host: str, port: int) -> list:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = _resolve(host, port)
    except (OSError, UnicodeError):
        raise WebhookRefused("unresolvable") from None
    found = []
    for *_rest, sockaddr in infos:
        try:
            address = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        except ValueError:
            raise WebhookRefused("unresolvable") from None
        if address not in found:
            found.append(address)
    if not found:
        raise WebhookRefused("unresolvable")
    return found


def _denied(address) -> bool:
    """Is `address` one of WEBHOOK_DENY_ADDRESSES (an IPv4-mapped IPv6 form counts as its IPv4)?"""
    from apps.core.checks import deny_address

    mapped = getattr(address, "ipv4_mapped", None)
    candidates = {address} | ({mapped} if mapped else set())
    for raw in getattr(settings, "WEBHOOK_DENY_ADDRESSES", None) or []:
        denied = deny_address(raw)  # an IPv4-mapped entry denies its IPv4 address too
        if denied is None:
            # The system check refuses to start with such an entry (webhooks.E001); never fail open.
            logger.error("WEBHOOK_DENY_ADDRESSES holds an entry that is not an IP address")
            return True
        if denied in candidates:
            return True
    return False


def _url_key(url) -> tuple | None:
    """(host, port, path) of an https URL as WEBHOOK_ALLOW_URL_PREFIXES compares it, or None for a URL that
    could be read two ways: the host lowercase without its one trailing dot, port 443 written or not, and a
    path with no `.` or `..` segment, percent-encoded or not (a server would resolve those to another path)."""
    if not isinstance(url, str) or "\\" in url or any(c.isspace() or ord(c) < 32 for c in url):
        return None
    try:
        parts = urlsplit(url)
        host, port = (parts.hostname or "").lower(), parts.port or 443
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not host or "@" in parts.netloc:
        return None
    host = host[:-1] if host.endswith(".") else host
    path = parts.path or "/"
    if any(segment in (".", "..") for segment in unquote(path).replace("\\", "/").split("/")):
        return None
    return host, port, path


def allow_prefix_problem(raw) -> bool:
    """True when a WEBHOOK_ALLOW_URL_PREFIXES entry is not usable: it must be an https URL on a host name
    (never an address), port 443, with no user, query or fragment, and a path ending in `/`, so that
    `/invoicing/` cannot be stretched to `/invoicingX`."""
    key = _url_key(str(raw or "").strip())
    if key is None:
        return True
    host, port, path = key
    parts = urlsplit(str(raw).strip())
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True  # an address would undo the deny list for itself
    except ValueError:
        pass
    labels = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    return (port != 443 or parts.query != "" or parts.fragment != "" or not path.endswith("/")
            or not re.fullmatch(rf"(?=.{{1,253}}$){labels}(\.{labels})*", host))


def allowed_url(url: str) -> bool:
    """Is `url` under one of WEBHOOK_ALLOW_URL_PREFIXES: delivered to although its host resolves to a denied
    address, because that receiver lives on this host's own address (audit SEC-5, review S5 M-1)? By URL
    prefix, not by host: the same host's other paths (`/server-status`) stay refused."""
    key = _url_key(url)
    if key is None:
        return False
    for raw in getattr(settings, "WEBHOOK_ALLOW_URL_PREFIXES", None) or []:
        prefix = None if allow_prefix_problem(raw) else _url_key(str(raw).strip())
        if prefix and key[:2] == prefix[:2] and key[2].startswith(prefix[2]):
            return True
    return False


# Set by `post` for the one URL it sends to, when that URL is under an allowed prefix: the connection's own
# check then lifts the deny list for that host and port, and for nothing else.
_url_allowance: contextvars.ContextVar[tuple | None] = contextvars.ContextVar("webhook_url_allowance", default=None)


def checked_addresses(host: str, port: int, *, allow_denied: bool = False) -> list[str]:
    """Every address `host` resolves to, each public and none denied; else WebhookRefused. `allow_denied`:
    the URL is under an allowed prefix, so a denied address is accepted (never a private one)."""
    found = _addresses(host, port)
    if not allow_denied and any(_denied(a) for a in found):
        raise WebhookRefused("private_address")
    if not getattr(settings, "WEBHOOK_ALLOW_PRIVATE", False) and not all(_public(a) for a in found):
        raise WebhookRefused("private_address")
    return [str(a) for a in found]


def _parts(url):
    if not isinstance(url, str) or not url or len(url) > 2000 or any(c.isspace() or ord(c) < 32 for c in url):
        raise WebhookRefused("bad_url")
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise WebhookRefused("bad_url") from None
    return parts, host, port


def check_destination(url: str) -> None:
    """Refuse (WebhookRefused) a URL a webhook may not be sent to. Resolves the host."""
    parts, host, port = _parts(url)
    scheme = parts.scheme.lower()
    if not scheme:
        raise WebhookRefused("bad_url")
    if scheme != "https" and not (scheme == "http" and getattr(settings, "WEBHOOK_ALLOW_HTTP", False)):
        raise WebhookRefused("scheme")
    if not host:
        raise WebhookRefused("bad_url")
    port = port or (443 if scheme == "https" else 80)
    # Any other port makes the delivery log an oracle for what listens where; development lifts it.
    # (port 80 stays open to plain http, which only exists where WEBHOOK_ALLOW_HTTP is on.)
    if port != 443 and not (scheme == "http" and port == 80) and not getattr(settings, "WEBHOOK_ALLOW_PRIVATE", False):
        raise WebhookRefused("port")
    checked_addresses(host, port, allow_denied=allowed_url(url))


def display_url(url: str) -> str:
    """The URL as it may be shown: scheme, host, port and path. Never the query string (a
    receiver's token often travels in it) or a user name and password."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname or "", parts.port
    except ValueError:
        return ""
    if ":" in host:
        host = f"[{host}]"
    shown = f"{parts.scheme}://{host}{f':{port}' if port else ''}{parts.path}"
    return shown + ("?..." if parts.query else "")


# ---------------------------------------------------------------------------
# The sender: a socket only to an address just checked, and a deadline for the whole attempt
# ---------------------------------------------------------------------------
_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("webhook_deadline", default=None)


def _time_left() -> float | None:
    """Seconds left of the attempt in hand (None outside `post`); DeliveryDeadline once none are."""
    deadline = _deadline.get()
    if deadline is None:
        return None
    left = deadline - _monotonic()
    if left <= 0:
        raise DeliveryDeadline()
    return left


class _DeadlineReader(io.RawIOBase):
    """The answer's bytes, each read waiting at most the per-read timeout and never past the
    attempt's deadline. A socket timeout applies to one read only, so a receiver sending a byte
    at a time would otherwise hold the worker for as long as it liked."""

    def __init__(self, sock, per_read):
        self._sock, self._per_read = sock, per_read

    def readable(self):
        return True

    def readinto(self, buffer):
        left = _time_left()
        wait = self._per_read if left is None else left if self._per_read is None else min(self._per_read, left)
        self._sock.settimeout(max(0.1, wait) if wait is not None else None)
        try:
            return self._sock.recv_into(buffer)
        except TimeoutError:
            _time_left()  # DeliveryDeadline when the deadline is why
            raise


class _DeadlineResponse(http.client.HTTPResponse):
    def __init__(self, sock, *args, **kwargs):
        super().__init__(sock, *args, **kwargs)
        plain, self.fp = self.fp, io.BufferedReader(_DeadlineReader(sock, sock.gettimeout()))
        plain.close()


class _GuardedConnection:
    response_class = _DeadlineResponse

    def _new_conn(self):
        last = None
        per_try = self.timeout if isinstance(self.timeout, (int, float)) else TIMEOUT_S
        host = self._dns_host.strip("[]")
        name = host.lower()[:-1] if host.endswith(".") else host.lower()
        allow = _url_allowance.get() == (name, self.port)
        for address in checked_addresses(host, self.port, allow_denied=allow):  # WebhookRefused propagates
            left = _time_left()
            try:
                sock = urllib3_connection.create_connection(
                    (address, self.port), per_try if left is None else min(per_try, left),
                    source_address=self.source_address, socket_options=self.socket_options)
            except TimeoutError:
                last = ConnectTimeoutError(self, "connection timed out")
                continue
            except OSError:
                last = NewConnectionError(self, "failed to establish a new connection")
                continue
            try:
                left = _time_left()
            except DeliveryDeadline:
                sock.close()
                raise
            if left is not None:
                sock.settimeout(min(per_try, left))  # the TLS handshake gets what is left, at most
            return sock
        raise last


class _GuardedHTTPConnection(_GuardedConnection, HTTPConnection):
    pass


class _GuardedHTTPSConnection(_GuardedConnection, HTTPSConnection):
    pass


class _GuardedHTTPPool(HTTPConnectionPool):
    ConnectionCls = _GuardedHTTPConnection


class _GuardedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _GuardedHTTPSConnection


class _GuardedAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _GuardedHTTPPool, "https": _GuardedHTTPSPool}


def _session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False  # no proxy from the environment: the guard must see the real destination
    adapter = _GuardedAdapter(max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class _attempt_deadline:
    """One deadline for a whole attempt, the destination's name lookup included: `post` joins it."""

    def __enter__(self):
        self._token = _deadline.set(_monotonic() + DELIVERY_DEADLINE_S)

    def __exit__(self, *exc):
        _deadline.reset(self._token)


def post(url: str, body: bytes, headers: dict) -> int:
    """POST `body` once and return the status code. Never follows a redirect, never reads the
    answer's body, and gives up (DeliveryDeadline) once DELIVERY_DEADLINE_S have passed in all.
    Refused under the test runner (`WEBHOOK_HTTP_ENABLED`, read at call time)."""
    if not getattr(settings, "WEBHOOK_HTTP_ENABLED", True):
        raise RuntimeError("outbound webhook HTTP is disabled (WEBHOOK_HTTP_ENABLED=False)")
    token = _deadline.set(_deadline.get() or _monotonic() + DELIVERY_DEADLINE_S)
    key = _url_key(url) if allowed_url(url) else None
    allowance = _url_allowance.set(key[:2] if key else None)
    try:
        with _session() as session:
            response = session.post(url, data=body, headers=headers, timeout=TIMEOUT_S, allow_redirects=False,
                                    stream=True)
            try:
                return response.status_code
            finally:
                response.close()
    finally:
        _url_allowance.reset(allowance)
        _deadline.reset(token)


# ---------------------------------------------------------------------------
# What is sent
# ---------------------------------------------------------------------------
def _iso(moment) -> str:
    return moment.astimezone(datetime.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def body_of(event: WebhookEvent) -> bytes:
    """The event as canonical JSON bytes: what is sent and what the signature covers."""
    from apps.orgs.models import Organization

    slug = Organization.objects.filter(pk=event.org_id).values_list("slug", flat=True).first() if event.org_id else None
    payload = {"id": str(event.id), "type": event.type, "created_at": _iso(event.created_at), "org": slug,
               "data": event.data}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def headers_for(event: WebhookEvent, secret: str, body: bytes) -> dict:
    return {
        "Content-Type": "application/json",
        "User-Agent": "sealbill-webhooks/1",
        "X-Pay-Event": event.type,
        "X-Pay-Event-Id": str(event.id),
        "X-Pay-Signature": sign(secret, body),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
def normalize_events(wanted) -> list[str]:
    """The event types as stored: known ones only, without repeats; `*` alone stands for all."""
    wanted = list(wanted or ["*"])
    unknown = [e for e in wanted if e != "*" and e not in webhook_events.EVENT_TYPES]
    if unknown:
        raise ValueError("unknown event type(s): " + ", ".join(sorted(set(map(str, unknown)))))
    if "*" in wanted:
        return ["*"]
    return list(dict.fromkeys(wanted))


def create_endpoint(org, url: str, *, events=None, description: str = "", actor=None) -> tuple[WebhookEndpoint, str]:
    """A new endpoint and its secret, which is returned here and never shown again. Refuses
    (WebhookRefused) a destination a webhook may not go to; ValueError for unknown events."""
    wanted = normalize_events(events)
    check_destination(url)
    if WebhookEndpoint.objects.filter(org=org, active=True).count() >= MAX_ENDPOINTS_PER_ORG:
        raise WebhookRefused("endpoint_limit")
    plain = secrets.token_urlsafe(SECRET_BYTES)
    with transaction.atomic():
        endpoint = WebhookEndpoint.objects.create(org=org, url=url, secret=plain, events=wanted,
                                                  description=(description or "")[:200])
        audit.record(actor, "webhook.create", endpoint, {"endpoint": endpoint.pk, "events": wanted})
    return endpoint, plain


def delete_endpoint(endpoint: WebhookEndpoint, actor=None) -> None:
    """The endpoint, its secret and its delivery log are deleted; the events stay."""
    with transaction.atomic():
        audit.record(actor, "webhook.delete", endpoint, {"endpoint": endpoint.pk})
        WebhookEndpoint.objects.filter(pk=endpoint.pk).delete()


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
def _due():
    return Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=timezone.now())


def _fail(delivery: WebhookDelivery, kind: str, *, status_code=None, final=False) -> None:
    attempts = delivery.attempts + 1
    state = "failed" if final or attempts >= MAX_ATTEMPTS else "queued"
    wait = None if state == "failed" else timezone.now() + datetime.timedelta(minutes=BACKOFF_MINUTES[attempts - 1])
    WebhookDelivery.objects.filter(pk=delivery.pk).update(
        attempts=attempts, state=state, next_attempt_at=wait, last_status_code=status_code,
        last_error_kind=kind[:64])
    # Never the URL or the exception's text: either may carry the receiver's secret.
    logger.warning("Webhook delivery #%s failed (attempt %s, %s)", delivery.pk, attempts, kind)
    if state == "failed":
        audit.record_system("webhook.failed", delivery, {
            "endpoint": delivery.endpoint_id, "event": str(delivery.event_id), "attempts": attempts, "error": kind,
        })


def _failure_kind(exc: Exception) -> str:
    if isinstance(exc, WebhookRefused):
        return f"refused:{exc.code}"
    if isinstance(exc, DeliveryDeadline):
        return "deadline"
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.SSLError):
        return "tls"
    if isinstance(exc, requests.RequestException):
        return "connection"
    return webhook_events.type_name(exc)


def flush(limit: int = 50) -> int:
    """Deliver due events, oldest first. Returns how many were sent; 0 while sealed.

    At least once: a crash after the receiver answered but before the row is committed as sent
    sends the event again. Receivers deduplicate on `X-Pay-Event-Id`.

    Each run takes at most one delivery per endpoint (its oldest due one), for at most `limit`
    endpoints, oldest first. The worker runs its jobs one at a time: a receiver that is down costs
    it one attempt (at most DELIVERY_DEADLINE_S) per run, never one per queued event; it cannot
    starve the other endpoints; and a newer delivery of its own goes while an older one waits out
    its backoff. A healthy endpoint receives one delivery per run (every 15 s).

    A run starts no new attempt after FLUSH_BUDGET_S of wall clock: what it did not reach keeps its
    schedule and is picked up by the next run, so slow receivers cannot hold the worker away from
    the chain scans and the e-mail."""
    if not vault.is_unsealed():
        return 0
    heads = list(
        WebhookDelivery.objects.filter(_due(), state="queued").values("endpoint_id")
        .annotate(first=Min("pk")).order_by("first").values_list("first", flat=True)[:limit]
    )
    sent = 0
    started = _monotonic()
    for pk in heads:
        if _monotonic() - started >= FLUSH_BUDGET_S:
            break  # the rest keep their schedule and go on the next run, so the money jobs get the worker
        with transaction.atomic():
            # Only this table's row is locked (OF SELF): no invoice or endpoint row stays
            # locked while the receiver is asked.
            locked = (
                WebhookDelivery.objects.select_for_update(skip_locked=True, of=("self",))
                .filter(_due(), pk=pk, state="queued")
                .only("pk", "state", "attempts", "next_attempt_at", "endpoint_id", "event_id").first()
            )
            if locked is None:
                continue
            if not WebhookEndpoint.objects.filter(pk=locked.endpoint_id, active=True).exists():
                WebhookDelivery.objects.filter(pk=pk).update(state="cancelled", next_attempt_at=None)
                logger.info("Webhook delivery #%s cancelled: the endpoint is inactive", pk)
                continue
            try:
                with transaction.atomic():  # savepoint: a database error must not poison the outer one
                    endpoint = WebhookEndpoint.objects.only("pk", "url", "secret").get(pk=locked.endpoint_id)
                    url, secret = endpoint.url, endpoint.secret
                    event = WebhookEvent.objects.get(pk=locked.event_id)
                    body = body_of(event)  # computed once per attempt and sent as it is
                    with _attempt_deadline():
                        check_destination(url)
                        _time_left()  # a lookup that took the whole deadline ends the attempt here
                        status = post(url, body, headers_for(event, secret, body))
            except VaultSealed:
                break  # sealed mid-run: stop cleanly, no attempt counted
            except UndecryptableValue:
                _fail(locked, "UndecryptableValue", final=True)  # retrying cannot help
                continue
            except Exception as exc:  # noqa: BLE001 - every failure is an attempt, with backoff
                _fail(locked, _failure_kind(exc))
                continue
            if 200 <= status < 300:
                WebhookDelivery.objects.filter(pk=pk).update(
                    state="sent", sent_at=timezone.now(), attempts=locked.attempts + 1, next_attempt_at=None,
                    last_status_code=status, last_error_kind="")
                sent += 1
            else:
                _fail(locked, "redirect" if 300 <= status < 400 else f"http_{status}", status_code=status)
    return sent


def retry_now(queryset, actor=None) -> int:
    """Queue failed (or waiting) deliveries again with a fresh set of attempts."""
    pks = list(queryset.filter(state__in=("failed", "queued")).values_list("pk", flat=True))
    WebhookDelivery.objects.filter(pk__in=pks).update(
        state="queued", attempts=0, next_attempt_at=None, staff_alerted_at=None)
    for pk in pks:
        audit.record(actor, "webhook.retry", WebhookDelivery(pk=pk), {})
    return len(pks)


def dismiss(delivery: WebhookDelivery, actor, reason: str) -> bool:
    """A person decided that a failed delivery will never be retried: it is cancelled for good and
    audited (who, why, the endpoint and the event type; never the URL). Only a `failed` delivery can
    be dismissed; False when it is not, and then nothing is recorded. `reason` is required."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("dismissing a delivery needs a reason")
    with transaction.atomic():
        row = WebhookDelivery.objects.filter(pk=delivery.pk).values_list("endpoint_id", "event__type").first()
        if row is None or not WebhookDelivery.objects.filter(pk=delivery.pk, state="failed").update(
                state="cancelled", next_attempt_at=None):
            return False
        audit.record(actor, "webhook.dismiss", WebhookDelivery(pk=delivery.pk),
                     {"endpoint": row[0], "event": row[1], "reason": reason})
    logger.info("Webhook delivery #%s dismissed by a person; it will not be retried", delivery.pk)
    return True


def failed_alert() -> None:
    """Tell staff once about every delivery that reached `failed`: one direct e-mail per batch,
    never through the outbox and never as a webhook. Plaintext columns only (no URL)."""
    rows = list(WebhookDelivery.objects.filter(state="failed", staff_alerted_at__isnull=True)
                .order_by("pk").values_list("pk", "endpoint_id", "last_error_kind"))
    if not rows:
        return
    pks = [pk for pk, _endpoint, _kind in rows]

    def mark_alerted():
        WebhookDelivery.objects.filter(pk__in=pks, state="failed", staff_alerted_at__isnull=True).update(
            staff_alerted_at=timezone.now())

    recipients = list(settings.STAFF_NOTIFY_EMAILS)
    if not recipients:
        if cache.add(NO_RECIPIENTS_KEY, True, timeout=None):
            logger.warning("%s webhook delivery(ies) failed and STAFF_NOTIFY_EMAILS is empty; nobody was told.",
                           len(rows))
        mark_alerted()
        return
    kinds, endpoints = {}, {}
    for _pk, endpoint_id, kind in rows:
        kinds[kind or "unknown"] = kinds.get(kind or "unknown", 0) + 1
        endpoints[endpoint_id] = endpoints.get(endpoint_id, 0) + 1
    page = settings.SITE_URL.rstrip("/") + reverse("staff:needs_person")
    subject = f"Webhook delivery failed: {len(rows)} delivery(ies) need a person"
    text = (
        f"{len(rows)} webhook delivery(ies) failed after every retry.\n\n"
        f"Why: {', '.join(f'{kind} x{n}' for kind, n in sorted(kinds.items()))}\n"
        f"Endpoints: {', '.join(f'#{pk} x{n}' for pk, n in sorted(endpoints.items()))}\n\n"
        f"They are listed under Needs a person, with a retry button: {page}\n"
        "Retry them once the receiver accepts deliveries again. A refused one (refused:...) names a "
        "destination that is not public, or a host that does not resolve.\n"
    )
    try:
        EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, recipients).send()
    except Exception as exc:  # noqa: BLE001
        logger.error("failed-webhook alert could not be sent: %s", webhook_events.type_name(exc))
        return  # tried again on the next run
    mark_alerted()
