"""Outgoing webhooks: signed, thin, retried, logged, and never sent anywhere private.

Nothing here leaves the process: `requests.Session.request` is replaced by `Receiver`, and every
name resolution is answered by a mock of `socket.getaddrinfo`.
"""
import datetime
import hashlib
import hmac
import json
import socket
import threading
import time
from contextlib import ExitStack
from decimal import Decimal
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.core import mail
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.models import ApiToken
from apps.audit.models import AuditEntry
from apps.contacts.services import find_or_create_contact
from apps.core import jobs as core_jobs, worker
from apps.core.tests_settings import production_settings
from apps.invoices.services import create_invoice, send_invoice, void_invoice
from apps.keys import delivery as key_delivery
from apps.keys.models import EncryptionKey
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.payments.models import Payment
from apps.payments.services import apply_payments, record_manual_payment, reject_payment
from apps.payments import matching
from apps.payments.tests_matching import K0, TX, TX2, TX3, MatchingBase, transfer
from apps.rails import health, jobs as rails_jobs
from apps.rails.models import ScanGap, WatcherHealth
from apps.staff.models import StaffTOTPDevice
from apps.staff.tests import token_for
from apps.vault.testing import TEST_VAULT_PASSWORD, UnsealedVaultMixin
from apps.vault.vault import vault
from apps.webhooks import delivery, events
from apps.webhooks.models import WebhookDelivery, WebhookEndpoint, WebhookEvent
from apps.payments.testing import person

NAME, EMAIL = "Ján Žilinský", "jan.zilinsky@example.com"
ADDRESS = {"line1": "Hlavná 7", "city": "Košice", "country": "SK"}
PRICE = 7654321
HOST = "hooks.example.com"
URL = f"https://{HOST}/in/secret-path?token=s3cr3t-in-query"
PUBLIC_IP = "93.184.216.34"
LINE = {"name": "Service", "quantity": 1, "unit_price_minor": PRICE}
# What `data` may carry: ids, statuses and kinds. Never a name, an address, an e-mail or an amount.
THIN_KEYS = {"invoice_id", "payment_id", "declaration_id", "endpoint_id", "key_id", "status", "invoice_status",
             "kind", "rail", "reason", "count", "watchers", "networks"}


def addrinfo(*ips):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]


def resolving(*ips):
    """Every name resolves to `ips` (no DNS is asked)."""
    return mock.patch("socket.getaddrinfo", return_value=addrinfo(*ips))


class Receiver:
    """The receiving server. `requests.Session.request` is replaced, so nothing is sent; each POST
    is answered with the next of `answers` (a status code, or an exception to raise), the last
    one repeating. Names resolve to `resolves_to`."""

    def __init__(self, *answers, resolves_to=PUBLIC_IP, urls=None):
        self.answers, self.calls, self.resolves_to = list(answers) or [200], [], resolves_to
        self.urls = dict(urls or {})  # a fixed answer for these URLs

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if url in self.urls:
            answer = self.urls[url]
        else:
            answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        headers = {"Location": "https://169.254.169.254/latest/meta-data/"} if 300 <= answer < 400 else {}
        return mock.Mock(status_code=answer, headers=headers)

    def __enter__(self):
        self._stack = ExitStack()
        self._stack.enter_context(override_settings(WEBHOOK_HTTP_ENABLED=True))
        self._stack.enter_context(mock.patch.object(requests.Session, "request", side_effect=self))
        self._stack.enter_context(resolving(self.resolves_to))
        return self

    def __exit__(self, *exc):
        self._stack.close()


class TricklingServer:
    """A real receiver on 127.0.0.1 that reads the request, then answers one byte every `every`
    seconds (a status line, then headers that never end), for at most `give_up_after` seconds."""

    def __init__(self, every=0.1, give_up_after=8.0):
        self.every, self.give_up_after = every, give_up_after
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.listener.close()
        self.thread.join(5)

    def _serve(self):
        self.listener.settimeout(5)
        try:
            conn, _ = self.listener.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            try:
                conn.recv(65536)
            except OSError:
                return
            answer = b"HTTP/1.1 200 OK\r\n" + b"X-Slow: y\r\n" * 1000
            started = time.monotonic()
            for i in range(len(answer)):
                if self.stop.is_set() or time.monotonic() - started > self.give_up_after:
                    return
                try:
                    conn.sendall(answer[i:i + 1])
                except OSError:
                    return  # the client gave up
                self.stop.wait(self.every)


def signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class WebhookBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")
        add_bank(self.org)
        self.contact, _ = find_or_create_contact(self.org, EMAIL, legal_name=NAME, address=ADDRESS,
                                                 preferred_language="en")

    def endpoint(self, events=("*",), org=None, url=URL):
        with resolving(PUBLIC_IP):
            return delivery.create_endpoint(org or self.org, url, events=list(events), description="liberation.services")

    def invoice(self, send=True, org=None, contact=None):
        inv = create_invoice(org or self.org, contact or self.contact, currency="EUR", lines=[LINE])[0]
        return send_invoice(inv, email_client=False)[0] if send else inv

    def due_now(self):
        WebhookDelivery.objects.filter(state="queued").update(
            next_attempt_at=timezone.now() - datetime.timedelta(seconds=1))

    def flush_all(self):
        """Run the worker job until nothing more goes (one delivery per endpoint per run)."""
        while delivery.flush():
            pass

    def fail_to_the_end(self, answer=500):
        with Receiver(answer), self.assertLogs("apps.webhooks.delivery", "WARNING"):
            for _ in range(delivery.MAX_ATTEMPTS):
                delivery.flush()
                self.due_now()

    def admin_client(self, org=None, scopes=("admin",), name="agent"):
        _token, plain = ApiToken.issue(org or self.org, name, list(scopes))
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")
        return c


class SigningTests(WebhookBase):
    def test_signature_is_hmac_sha256_of_raw_body(self):
        endpoint, secret = self.endpoint(events=["invoice.sent"])
        inv = self.invoice()
        self.assertEqual(WebhookDelivery.objects.count(), 1)  # invoice.created is recorded, not subscribed
        with Receiver(200) as rx:
            self.assertEqual(delivery.flush(), 1)
        self.assertEqual(len(rx.calls), 1)
        call = rx.calls[0]
        body, headers = call["data"], call["headers"]
        self.assertIsInstance(body, bytes)
        self.assertEqual(call["url"], URL)
        self.assertTrue(hmac.compare_digest(headers["X-Pay-Signature"], signature(secret, body)))
        event = WebhookEvent.objects.get(type="invoice.sent")
        self.assertEqual(headers["X-Pay-Event"], "invoice.sent")
        self.assertEqual(headers["X-Pay-Event-Id"], str(event.id))
        self.assertEqual(headers["Content-Type"], "application/json")
        parsed = json.loads(body)
        # The raw bytes are the canonical form: sorted keys, compact separators. The signature covers them.
        self.assertEqual(body, json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode())
        self.assertEqual(set(parsed), {"id", "type", "created_at", "org", "data"})
        self.assertEqual((parsed["id"], parsed["type"], parsed["org"]), (str(event.id), "invoice.sent", "eu"))
        self.assertEqual(parsed["data"], {"invoice_id": inv.pk, "status": "sent"})
        self.assertEqual((call["timeout"], call["allow_redirects"]), (10, False))
        tampered = body.replace(b'"sent"', b'"paid"')
        self.assertFalse(hmac.compare_digest(headers["X-Pay-Signature"], signature(secret, tampered)))
        d = WebhookDelivery.objects.get()
        self.assertEqual((d.state, d.attempts, d.last_status_code, d.endpoint_id), ("sent", 1, 200, endpoint.pk))
        self.assertIsNotNone(d.sent_at)
        with Receiver(200) as again:
            self.assertEqual(delivery.flush(), 0)  # sent once
        self.assertEqual(again.calls, [])

    def test_payload_is_thin(self):
        self.endpoint()
        inv = self.invoice()
        record_manual_payment(inv, amount_minor=PRICE, rail="bank", note=f"paid by {NAME}", user=person())
        with Receiver(200) as rx:
            self.flush_all()
        types = [json.loads(c["data"])["type"] for c in rx.calls]
        self.assertEqual(types, ["invoice.created", "invoice.sent", "payment.confirmed", "invoice.paid"])
        for call in rx.calls:
            payload = json.loads(call["data"])
            readable = json.dumps(payload, ensure_ascii=False)
            for secret in (NAME, "Žilinský", EMAIL, "Hlavná", "Košice", str(PRICE), "76543", "Seller",
                           "SK0000000000009", "Service"):
                self.assertNotIn(secret, readable, payload["type"])
            self.assertLessEqual(set(payload["data"]), THIN_KEYS, payload["type"])
            for value in payload["data"].values():
                self.assertIsInstance(value, (int, str, type(None), list), payload["type"])
                self.assertNotEqual(value, PRICE)

    def test_emit_refuses_rich_data_and_never_raises(self):
        inv = self.invoice()
        with self.assertLogs("apps.webhooks.events", "ERROR") as logs:
            self.assertIsNone(events.emit("invoice.paid", org=self.org, invoice=inv, data={"amount": Decimal("1.5")}))
            self.assertIsNone(events.emit("invoice.nonsense", org=self.org, invoice=inv, data={}))
        self.assertFalse(WebhookEvent.objects.filter(type__in=["invoice.paid", "invoice.nonsense"]).exists())
        self.assertNotIn("1.5", "".join(logs.output))


class SealedTests(WebhookBase):
    def test_event_recorded_while_sealed_is_delivered_after_unlock(self):
        _endpoint, secret = self.endpoint(events=["invoice.paid"])
        inv = self.invoice()
        Payment.objects.create(invoice=inv, rail="bank", asset="EUR", amount_minor=PRICE,
                               received_amount=Decimal("76543.21"), state="confirmed", confirmed_at=timezone.now())
        vault.seal()
        self.assertEqual(apply_payments(inv), "paid")  # a watcher's path: runs sealed
        event = WebhookEvent.objects.get(type="invoice.paid")
        self.assertEqual(event.data, {"invoice_id": inv.pk, "status": "paid"})
        d = WebhookDelivery.objects.get(event=event)
        self.assertEqual((d.state, d.attempts), ("queued", 0))
        with Receiver(200) as rx:
            core_jobs.webhooks_flush()  # the worker job: the URL and the secret are encrypted
            self.assertEqual(delivery.flush(), 0)
            self.assertEqual(rx.calls, [])
            vault.unseal(TEST_VAULT_PASSWORD)
            core_jobs.webhooks_flush()
        self.assertEqual(len(rx.calls), 1)
        body = rx.calls[0]["data"]
        self.assertEqual(json.loads(body)["id"], str(event.id))
        self.assertEqual(rx.calls[0]["headers"]["X-Pay-Signature"], signature(secret, body))
        d.refresh_from_db()
        self.assertEqual(d.state, "sent")

    def test_rollback_removes_the_event_and_a_broken_emit_never_blocks_the_caller(self):
        self.endpoint()
        with self.assertRaises(RuntimeError), transaction.atomic():
            self.invoice(send=False)
            raise RuntimeError("the business transaction fails")
        self.assertFalse(WebhookEvent.objects.exists())
        self.assertFalse(WebhookDelivery.objects.exists())
        with mock.patch.object(WebhookDelivery.objects, "bulk_create", side_effect=IntegrityError("boom")), \
                self.assertLogs("apps.webhooks.events", "ERROR") as logs:
            with transaction.atomic():
                inv = self.invoice()  # created and sent: the webhook's failure stays its own
        self.assertEqual(inv.status, "sent")
        self.assertTrue(all("IntegrityError" in line and "boom" not in line for line in logs.output))
        self.assertFalse(WebhookEvent.objects.exists())  # the savepoint took the half-made event with it


class RetryTests(WebhookBase):
    def test_backoff_schedule_then_failed(self):
        self.assertEqual(delivery.BACKOFF_MINUTES, (2, 5, 15, 30, 60, 120, 240, 480))
        self.endpoint(events=["invoice.sent"])
        self.invoice()
        d = WebhookDelivery.objects.get()
        with Receiver(503) as rx, self.assertLogs("apps.webhooks.delivery", "WARNING"):
            for attempt, minutes in enumerate(delivery.BACKOFF_MINUTES, start=1):
                before = timezone.now()
                self.assertEqual(delivery.flush(), 0)
                d.refresh_from_db()
                self.assertEqual((d.state, d.attempts, d.last_status_code, d.last_error_kind),
                                 ("queued", attempt, 503, "http_503"))
                wait = d.next_attempt_at - before
                self.assertTrue(datetime.timedelta(minutes=minutes) <= wait
                                <= datetime.timedelta(minutes=minutes, seconds=5), (attempt, wait))
                delivery.flush()  # not due yet: not tried again
                self.assertEqual(len(rx.calls), attempt)
                self.due_now()
            delivery.flush()
            d.refresh_from_db()
            self.assertEqual((d.state, d.attempts, d.next_attempt_at), ("failed", 9, None))
            self.due_now()
            delivery.flush()  # a failed delivery waits for a person
        self.assertEqual(len(rx.calls), 9)
        entry = AuditEntry.objects.get(action="webhook.failed")
        self.assertEqual(entry.system_detail["error"], "http_503")

    @override_settings(STAFF_NOTIFY_EMAILS=["ops@example.com"], SITE_URL="https://pay.example")
    def test_delivery_fails_after_last_backoff_and_is_listed(self):
        self.endpoint(events=["invoice.sent"])
        self.invoice()
        d = WebhookDelivery.objects.get()
        self.fail_to_the_end(500)
        d.refresh_from_db()
        self.assertEqual(d.state, "failed")
        mail.outbox.clear()
        core_jobs.webhooks_failed_alert()
        core_jobs.webhooks_failed_alert()  # one e-mail per batch
        self.assertEqual(len(mail.outbox), 1)
        m = mail.outbox[0]
        self.assertEqual(m.to, ["ops@example.com"])
        self.assertIn("1", m.subject)
        self.assertIn("https://pay.example/", m.body)
        self.assertIn("http_500", m.body)
        self.assertNotIn(HOST, m.body)
        self.assertNotIn(HOST, m.subject)
        self.assertIsNotNone(WebhookDelivery.objects.get().staff_alerted_at)

        User = get_user_model()
        staff = User.objects.create_user("clerk", password="pw-clerk-1", is_staff=True)
        device = StaffTOTPDevice.objects.create(user=staff, name="t", confirmed=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.post("/verify/", {"token": token_for(device)}).status_code, 302)
        page = self.client.get("/").content.decode()
        self.assertIn("Webhooks not delivered", page)
        self.assertIn(f"/webhooks/{d.pk}/retry/", page)
        self.assertIn("invoice.sent", page)
        self.assertIn("http_500", page)
        self.assertNotIn(HOST, page)
        self.assertEqual(self.client.get(f"/webhooks/{d.pk}/retry/").status_code, 405)
        self.assertEqual(self.client.post(f"/webhooks/{d.pk}/retry/").status_code, 302)
        d.refresh_from_db()
        self.assertEqual((d.state, d.attempts, d.next_attempt_at, d.staff_alerted_at), ("queued", 0, None, None))
        self.assertTrue(AuditEntry.objects.filter(action="webhook.retry", actor=staff).exists())
        with Receiver(204):
            self.assertEqual(delivery.flush(), 1)
        d.refresh_from_db()
        self.assertEqual(d.state, "sent")
        core_jobs.webhooks_failed_alert()
        self.assertEqual(len(mail.outbox), 1)  # nothing new to tell
        self.assertNotIn(f"/webhooks/{d.pk}/retry/", self.client.get("/").content.decode())

    def test_a_receiver_that_is_down_costs_one_attempt_per_run(self):
        self.endpoint(events=["invoice.created", "invoice.sent"])
        healthy, _ = self.endpoint(events=["invoice.sent"], url="https://other.example.com/in")
        self.invoice()  # invoice.created and invoice.sent for the first endpoint, invoice.sent for the second
        with Receiver(requests.Timeout("slow"), 200) as rx, self.assertLogs("apps.webhooks.delivery", "WARNING"):
            self.assertEqual(delivery.flush(), 1)
        self.assertEqual([c["url"] for c in rx.calls], [URL, "https://other.example.com/in"])
        rows = {(d.endpoint_id == healthy.pk, d.event.type): (d.state, d.attempts)
                for d in WebhookDelivery.objects.select_related("event")}
        self.assertEqual(rows, {
            (False, "invoice.created"): ("queued", 1),  # timed out: waits two minutes
            (False, "invoice.sent"): ("queued", 0),  # not asked in this run, and not counted
            (True, "invoice.sent"): ("sent", 1),  # another endpoint is not held up
        })
        with Receiver(200) as rx:
            self.assertEqual(delivery.flush(), 1)  # the next run: the one left waiting goes
        self.assertEqual(len(rx.calls), 1)

    def test_down_endpoint_does_not_starve_others(self):
        down, _ = self.endpoint(events=["*"])
        healthy_url = "https://other.example.com/in"
        healthy, _ = self.endpoint(events=["invoice.sent"], url=healthy_url)
        for _ in range(3):
            self.invoice(send=False)  # the down endpoint's backlog is older than anything else
        self.invoice()  # invoice.created and invoice.sent for it, invoice.sent for the healthy one
        with Receiver(503, urls={healthy_url: 200}) as rx, self.assertLogs("apps.webhooks.delivery", "WARNING"):
            self.assertEqual(delivery.flush(limit=2), 1)
        self.assertEqual([c["url"] for c in rx.calls], [URL, healthy_url])
        self.assertEqual(WebhookDelivery.objects.get(endpoint=healthy).state, "sent")
        backlog = WebhookDelivery.objects.filter(endpoint=down).order_by("pk")
        self.assertEqual([d.attempts for d in backlog], [1, 0, 0, 0, 0])
        # The next run takes its next due delivery: the failed one waits out its backoff.
        with Receiver(503) as rx, self.assertLogs("apps.webhooks.delivery", "WARNING"):
            delivery.flush(limit=2)
        self.assertEqual(len(rx.calls), 1)
        self.assertEqual([d.attempts for d in backlog.all()], [1, 1, 0, 0, 0])

    @override_settings(WEBHOOK_HTTP_ENABLED=True, WEBHOOK_ALLOW_HTTP=True, WEBHOOK_ALLOW_PRIVATE=True)
    def test_trickling_receiver_hits_the_total_deadline(self):
        # Each read waits at most TIMEOUT_S, but a receiver sending one byte at a time never lets one
        # time out: only a deadline for the whole attempt stops it holding the (serial) worker.
        with TricklingServer(every=0.1, give_up_after=8.0) as server:
            self.endpoint(events=["invoice.sent"], url=f"http://127.0.0.1:{server.port}/hook")
            self.invoice()
            started = time.monotonic()
            with mock.patch.object(delivery, "DELIVERY_DEADLINE_S", 0.5), \
                    self.assertLogs("apps.webhooks.delivery", "WARNING"):
                self.assertEqual(delivery.flush(), 0)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3)
        d = WebhookDelivery.objects.get()
        self.assertEqual((d.state, d.attempts, d.last_error_kind, d.last_status_code),
                         ("queued", 1, "deadline", None))
        self.assertEqual(delivery.DELIVERY_DEADLINE_S, 20)

    def test_redirect_not_followed(self):
        self.endpoint(events=["invoice.sent"])
        self.invoice()
        with Receiver(302) as rx, self.assertLogs("apps.webhooks.delivery", "WARNING"):
            self.assertEqual(delivery.flush(), 0)
        self.assertEqual(len(rx.calls), 1)  # the Location (a metadata address) was never asked for
        self.assertIs(rx.calls[0]["allow_redirects"], False)
        d = WebhookDelivery.objects.get()
        self.assertEqual((d.state, d.attempts, d.last_status_code, d.last_error_kind), ("queued", 1, 302, "redirect"))

    def test_inactive_endpoint_gets_nothing_more(self):
        endpoint, _ = self.endpoint(events=["invoice.created", "invoice.sent"])
        self.invoice(send=False)
        WebhookEndpoint.objects.filter(pk=endpoint.pk).update(active=False)
        self.invoice(send=False)  # no delivery is made for an inactive endpoint
        with Receiver(200) as rx:
            delivery.flush()
        self.assertEqual(rx.calls, [])
        self.assertEqual(list(WebhookDelivery.objects.values_list("state", flat=True)), ["cancelled"])

    def test_worker_jobs_are_registered(self):
        worker.reset()
        self.addCleanup(worker.reset)
        core_jobs.register_all()
        self.assertEqual(worker._jobs["webhooks_flush"]["every_s"], 15)
        self.assertEqual(worker._jobs["webhooks_failed_alert"]["every_s"], 60)


class DestinationTests(WebhookBase):
    def assertRefused(self, url, code):
        with self.assertRaises(delivery.WebhookRefused) as caught:
            delivery.check_destination(url)
        self.assertEqual(caught.exception.code, code, url)

    def test_private_and_loopback_destinations_refused(self):
        for url in ("https://127.0.0.1/hook", "https://10.0.0.1/hook", "https://169.254.169.254/latest/meta-data/",
                    "https://[::1]/hook", "https://[::ffff:127.0.0.1]/hook", "https://0.0.0.0/"):
            with self.subTest(url=url):
                self.assertRefused(url, "private_address")
        # A name is judged by every address it resolves to.
        for ips in (("10.0.0.5",), (PUBLIC_IP, "127.0.0.1"), ("fd00::1",), ("fe80::1",), ("fec0::1",), ("100.64.0.1",),
                    ("224.0.0.1",), ("240.0.0.1",), ("192.168.1.10",)):
            with self.subTest(ips=ips), resolving(*ips):
                self.assertRefused(f"https://{HOST}/hook", "private_address")
        with resolving(PUBLIC_IP, "2606:4700::1111"):
            delivery.check_destination(f"https://{HOST}/hook")
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror(-2, "Name or service not known")):
            self.assertRefused(f"https://{HOST}/hook", "unresolvable")
        for url in ("https://", "hooks.example.com/hook", "https://exa mple.com/", "https://host:99999/", ""):
            with self.subTest(url=url):
                self.assertRefused(url, "bad_url")
        with override_settings(WEBHOOK_ALLOW_PRIVATE=True):
            delivery.check_destination("https://127.0.0.1/hook")  # a development receiver

        # At creation: refused, nothing stored.
        r = self.admin_client().post("/api/v1/webhooks", {"url": "https://169.254.169.254/latest/"}, format="json")
        self.assertEqual((r.status_code, r.json()["error"], r.json()["detail"]), (400, "webhook_refused", "private_address"))
        self.assertFalse(WebhookEndpoint.objects.exists())

        # Before every delivery: the name now points inside, so nothing is sent and it is retried.
        self.endpoint(events=["invoice.sent"])
        self.invoice()
        with Receiver(200, resolves_to="10.0.0.7") as rx, self.assertLogs("apps.webhooks.delivery", "WARNING"):
            self.assertEqual(delivery.flush(), 0)
        self.assertEqual(rx.calls, [])
        d = WebhookDelivery.objects.get()
        self.assertEqual((d.state, d.attempts, d.last_error_kind, d.last_status_code),
                         ("queued", 1, "refused:private_address", None))
        self.due_now()
        with Receiver(200) as rx:  # the name is public again: delivered
            self.assertEqual(delivery.flush(), 1)

    @override_settings(WEBHOOK_HTTP_ENABLED=True)
    def test_the_connection_itself_rechecks_the_address(self):
        # DNS rebinding: a name that answers differently after the check is refused when connecting.
        with mock.patch("socket.getaddrinfo", return_value=addrinfo("127.0.0.1")), \
                mock.patch("urllib3.util.connection.create_connection") as connect:
            with self.assertRaises(delivery.WebhookRefused) as caught:
                delivery.post(f"https://{HOST}/hook", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(caught.exception.code, "private_address")
        connect.assert_not_called()
        # A public answer: the socket goes to exactly the address that was checked.
        with mock.patch("socket.getaddrinfo", return_value=addrinfo(PUBLIC_IP)), \
                mock.patch("urllib3.util.connection.create_connection", side_effect=OSError("no")) as connect:
            with self.assertRaises(requests.ConnectionError):
                delivery.post(f"https://{HOST}/hook", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(connect.call_args[0][0], (PUBLIC_IP, 443))

    def test_outbound_http_is_refused_under_the_test_runner(self):
        with self.assertRaises(RuntimeError):
            delivery.post(f"https://{HOST}/hook", b"{}", {})

    def test_http_refused_unless_allowed(self):
        with resolving(PUBLIC_IP):
            for url in (f"http://{HOST}/in", f"ftp://{HOST}/in", "file:///etc/passwd", f"gopher://{HOST}/"):
                with self.subTest(url=url):
                    self.assertRefused(url, "scheme")
            with override_settings(WEBHOOK_ALLOW_HTTP=True):
                delivery.check_destination(f"http://{HOST}/in")
                self.assertRefused(f"ftp://{HOST}/in", "scheme")
            r = self.admin_client().post("/api/v1/webhooks", {"url": f"http://{HOST}/in"}, format="json")
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "scheme"))
        self.assertFalse(WebhookEndpoint.objects.exists())


class EmissionTests(WebhookBase):
    def test_paid_invoice_emits_invoice_paid_once(self):
        self.endpoint(events=["invoice.paid"])
        inv = self.invoice()
        record_manual_payment(inv, amount_minor=PRICE - 100, rail="bank", note="", user=person())
        record_manual_payment(inv, amount_minor=100, rail="bank", note="", user=person())
        apply_payments(inv)  # nothing new: nothing emitted
        paid = WebhookEvent.objects.filter(type="invoice.paid")
        self.assertEqual(paid.count(), 1)
        self.assertEqual(WebhookEvent.objects.filter(type="invoice.partially_paid").count(), 1)
        self.assertEqual(paid.get().data, {"invoice_id": inv.pk, "status": "paid"})
        self.assertEqual(paid.get().invoice_id, inv.pk)
        self.assertEqual(WebhookDelivery.objects.count(), 1)  # only invoice.paid is subscribed
        record_manual_payment(inv, amount_minor=1, rail="bank", note="", user=person())
        self.assertEqual(WebhookEvent.objects.filter(type="invoice.overpaid").count(), 1)
        self.assertEqual(paid.count(), 1)

    def test_lifecycle_events(self):
        self.endpoint()
        draft = self.invoice(send=False)
        void_invoice(draft, "mistake", None)
        inv = self.invoice()
        from apps.payments.services import declare_payment

        declaration = declare_payment(inv, network="", tx_hash="", note="I paid")
        manual = Payment.objects.create(invoice=inv, rail="bank", asset="EUR", amount_minor=5,
                                        received_amount=Decimal("0.05"), state="detected")
        reject_payment(manual, None, "no such money")
        rows = list(WebhookDelivery.objects.order_by("pk").values_list("event__type", "event__data"))
        self.assertEqual(rows, [
            ("invoice.created", {"invoice_id": draft.pk, "status": "draft", "kind": "invoice"}),
            ("invoice.voided", {"invoice_id": draft.pk, "status": "void"}),
            ("invoice.created", {"invoice_id": inv.pk, "status": "draft", "kind": "invoice"}),
            ("invoice.sent", {"invoice_id": inv.pk, "status": "sent"}),
            ("invoice.declared", {"invoice_id": inv.pk, "declaration_id": declaration.pk, "status": "declared"}),
            ("payment.rejected", {"payment_id": manual.pk, "invoice_id": inv.pk, "status": "rejected",
                                  "rail": "bank"}),
        ])

    def test_requesting_org_hears_about_invoices_issued_for_it(self):
        us = Organization.objects.create(slug="us", is_us_resident_issuer=True)
        add_bank(us)
        resident, _ = find_or_create_contact(self.org, "us@example.com", legal_name="US", us_tax_resident="yes")
        mine, _ = self.endpoint(events=["invoice.sent"])
        theirs, _ = self.endpoint(events=["invoice.sent"], org=Organization.objects.create(slug="other"))
        inv = self.invoice(contact=resident)
        self.assertEqual(inv.org_id, us.pk)  # issued by the US issuer, requested by eu
        self.assertEqual(list(WebhookDelivery.objects.values_list("endpoint_id", flat=True)), [mine.pk])
        self.assertEqual(WebhookEvent.objects.get(type="invoice.sent").org_id, us.pk)

    @override_settings(STAFF_NOTIFY_EMAILS=["ops@example.com"], SITE_URL="https://pay.example")
    def test_staff_alert_event_has_no_client_data(self):
        self.endpoint(events=["staff.alert"])
        inv = self.invoice()
        # A watcher failing for over ten minutes.
        health.failed("scan:evm:arbitrum:1", "timeout")
        WatcherHealth.objects.filter(name="scan:evm:arbitrum:1").update(
            failing_since=timezone.now() - datetime.timedelta(minutes=11))
        rails_jobs.watcher_alert()
        rails_jobs.watcher_alert()  # one episode, one event
        # An e-mail that failed for good.
        OutboxMessage.objects.create(kind="invoice", invoice=inv, language="en", to=[EMAIL], state="failed",
                                     failed_at=timezone.now(), last_error="SMTPException")
        core_jobs.outbox_failed_alert()
        # A client's key that stopped working at send time.
        key = EncryptionKey.objects.create(contact=self.contact, kind="pgp", material="-----BEGIN PGP-----",
                                           fingerprint="ABCDEF0123456789", state="active", source="staff")
        key_delivery.mark_failed(key, "expired", outbox_pk=1)
        alerts = [d.event for d in WebhookDelivery.objects.filter(event__type="staff.alert").order_by("pk")]
        self.assertEqual([e.data["kind"] for e in alerts], ["watcher", "outbox_failed", "key_failed"])
        self.assertEqual(alerts[0].data, {"kind": "watcher", "reason": "failing", "count": 1,
                                          "watchers": ["scan:evm:arbitrum:1"]})
        self.assertEqual(alerts[1].data, {"kind": "outbox_failed", "count": 1})
        self.assertEqual(alerts[2].data, {"kind": "key_failed", "key_id": key.pk, "reason": "expired"})
        self.assertEqual(alerts[2].org_id, self.org.pk)
        self.assertEqual(WebhookDelivery.objects.filter(event__type="staff.alert").count(), 3)
        with Receiver(200) as rx:
            self.flush_all()
        self.assertEqual(len(rx.calls), 3)
        for call in rx.calls:
            readable = json.dumps(json.loads(call["data"]), ensure_ascii=False)
            for secret in (NAME, EMAIL, "Hlavná", "ABCDEF0123456789", "SMTPException", str(PRICE)):
                self.assertNotIn(secret, readable)
            self.assertLessEqual(set(json.loads(call["data"])["data"]), THIN_KEYS)


class MatchingEventTests(MatchingBase):
    def test_chain_outcomes_are_announced_while_sealed(self):
        with resolving(PUBLIC_IP):
            delivery.create_endpoint(self.org, URL, events=["*"])
        vault.seal()  # the watchers' path
        detected = self.ingest(transfer(K0, final=False))
        self.adapter.statuses[TX] = "final"
        matching.refresh_finality()
        review = self.ingest(transfer(5_000_000, tx=TX2))  # 5 USDC: no quote holds it
        self.ingest(transfer(1000, tx=TX3))  # dust: listed for staff, announced to nobody
        rows = list(WebhookDelivery.objects.order_by("pk").values_list("event__type", "event__data"))
        self.assertEqual(rows, [
            ("payment.detected", {"payment_id": detected.pk, "invoice_id": self.invoice.pk, "status": "detected",
                                  "rail": "evm"}),
            ("payment.confirmed", {"payment_id": detected.pk, "invoice_id": self.invoice.pk, "status": "confirmed",
                                   "rail": "evm"}),
            ("invoice.paid", {"invoice_id": self.invoice.pk, "status": "paid"}),
            ("payment.needs_review", {"payment_id": review.pk, "invoice_id": None, "status": "needs_review",
                                      "rail": "evm", "reason": "no_matching_invoice"}),
        ])
        self.assertEqual(WebhookEvent.objects.get(type="payment.needs_review").org_id, self.org.pk)


class StaffAlertWithoutEmailTests(WebhookBase):
    @override_settings(STAFF_NOTIFY_EMAILS=[])
    def test_staff_alert_webhook_emitted_without_staff_emails(self):
        # Spec 8: staff alerts go out by e-mail AND as a webhook. A webhook-only installation is
        # told once per episode, and the episode is marked as it would be after an e-mail.
        self.endpoint(events=["staff.alert"])
        inv = self.invoice()
        health.failed("scan:evm:arbitrum:1", "timeout")
        WatcherHealth.objects.filter(name="scan:evm:arbitrum:1").update(
            failing_since=timezone.now() - datetime.timedelta(minutes=11))
        gap = ScanGap.objects.create(network="arbitrum", address="0x" + "ab" * 20, from_block=11, to_block=8000)
        failed = OutboxMessage.objects.create(kind="invoice", invoice=inv, language="en", to=[EMAIL], state="failed",
                                              failed_at=timezone.now(), last_error="SMTPException")
        key = EncryptionKey.objects.create(contact=self.contact, kind="pgp", material="-----BEGIN PGP-----",
                                           fingerprint="ABCDEF0123456789", state="active", source="staff")
        with self.assertLogs("apps.rails.jobs", "WARNING"), self.assertLogs("apps.core.jobs", "WARNING"):
            for _ in range(2):  # a second pass in the same episode says nothing more
                rails_jobs.watcher_alert()
                core_jobs.outbox_failed_alert()
        key_delivery.mark_failed(key, "expired", outbox_pk=failed.pk)
        self.assertEqual(len(mail.outbox), 0)
        alerts = [d.event.data for d in WebhookDelivery.objects.filter(event__type="staff.alert").order_by("pk")]
        self.assertEqual(alerts, [
            {"kind": "watcher", "reason": "failing", "count": 1, "watchers": ["scan:evm:arbitrum:1"]},
            {"kind": "watcher", "reason": "skipped_blocks", "count": 1, "networks": ["arbitrum"]},
            {"kind": "outbox_failed", "count": 1},
            {"kind": "key_failed", "key_id": key.pk, "reason": "expired"},
        ])
        self.assertIsNotNone(WatcherHealth.objects.get(name="scan:evm:arbitrum:1").alerted_at)
        self.assertIsNotNone(ScanGap.objects.get(pk=gap.pk).alerted_at)
        self.assertIsNotNone(OutboxMessage.objects.get(pk=failed.pk).staff_alerted_at)

    @override_settings(STAFF_NOTIFY_EMAILS=[])
    def test_with_no_channel_at_all_the_watcher_episode_stays_open(self):
        # Nobody to tell by e-mail or webhook: left unmarked, so whoever is configured next hears of it.
        health.failed("lightning:3", "refused")
        WatcherHealth.objects.filter(name="lightning:3").update(
            failing_since=timezone.now() - datetime.timedelta(minutes=11))
        self.endpoint(events=["invoice.paid"])  # an endpoint, but not one that wants staff alerts
        ScanGap.objects.create(network="arbitrum", address="0x" + "ab" * 20, from_block=11, to_block=8000)
        OutboxMessage.objects.create(kind="invoice", language="en", to=[EMAIL], state="failed",
                                     failed_at=timezone.now(), last_error="SMTPException")
        key = EncryptionKey.objects.create(contact=self.contact, kind="pgp", material="-----BEGIN PGP-----",
                                           fingerprint="ABCDEF0123456789", state="active", source="staff")
        with self.assertLogs("apps.rails.jobs", "WARNING"), self.assertLogs("apps.core.jobs", "WARNING"):
            for _ in range(3):
                rails_jobs.watcher_alert()
                core_jobs.outbox_failed_alert()
        key_delivery.mark_failed(key, "expired", outbox_pk=1)
        self.assertIsNone(WatcherHealth.objects.get(name="lightning:3").alerted_at)
        self.assertIsNone(OutboxMessage.objects.get(kind="invoice").staff_alerted_at)
        self.assertFalse(WebhookDelivery.objects.exists())
        self.assertEqual(WebhookEvent.objects.count(), 0)  # no orphan events, pass after pass


class StaffAlertChannelsTests(WebhookBase):
    """D1/m11: e-mail and the staff.alert event each keep their own bookkeeping. An SMTP outage
    still emits the event once per episode; a row is marked only once some channel told someone."""

    def episode(self):
        health.failed("scan:evm:arbitrum:1", "timeout")
        WatcherHealth.objects.filter(name="scan:evm:arbitrum:1").update(
            failing_since=timezone.now() - datetime.timedelta(minutes=11))
        gap = ScanGap.objects.create(network="arbitrum", address="0x" + "ab" * 20, from_block=11, to_block=8000)
        failed = OutboxMessage.objects.create(kind="invoice", language="en", to=[EMAIL], state="failed",
                                              failed_at=timezone.now(), last_error="SMTPException")
        return WatcherHealth.objects.get(name="scan:evm:arbitrum:1"), gap, failed

    def alerts(self):
        return [d.event.data for d in WebhookDelivery.objects.filter(event__type="staff.alert").order_by("pk")]

    def run_jobs(self):
        rails_jobs.watcher_alert()
        core_jobs.outbox_failed_alert()

    @override_settings(STAFF_NOTIFY_EMAILS=["ops@example.com"])
    def test_smtp_outage_still_emits_the_staff_alert_once_per_episode(self):
        self.endpoint(events=["staff.alert"])
        watcher, gap, failed = self.episode()
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("smtp down")), \
                self.assertLogs("apps.rails.jobs", "ERROR"), self.assertLogs("apps.core.jobs", "ERROR"):
            for _ in range(2):  # the second pass is the same episode: no second event
                self.run_jobs()
        self.assertEqual(self.alerts(), [
            {"kind": "watcher", "reason": "failing", "count": 1, "watchers": ["scan:evm:arbitrum:1"]},
            {"kind": "watcher", "reason": "skipped_blocks", "count": 1, "networks": ["arbitrum"]},
            {"kind": "outbox_failed", "count": 1},
        ])
        self.assertEqual(len(mail.outbox), 0)
        watcher.refresh_from_db(), gap.refresh_from_db(), failed.refresh_from_db()
        self.assertEqual((watcher.alerted_at, gap.alerted_at, failed.staff_alerted_at), (None, None, None))
        self.assertTrue(watcher.webhook_alerted_at and gap.webhook_alerted_at and failed.webhook_alerted_at)
        # SMTP is back: the e-mails go out, and the event is not repeated.
        self.run_jobs()
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(len(self.alerts()), 3)
        watcher.refresh_from_db(), gap.refresh_from_db(), failed.refresh_from_db()
        self.assertTrue(watcher.alerted_at and gap.alerted_at and failed.staff_alerted_at)
        self.run_jobs()
        self.assertEqual((len(mail.outbox), len(self.alerts())), (3, 3))

    @override_settings(STAFF_NOTIFY_EMAILS=[])
    def test_outbox_alert_marks_nothing_until_someone_is_told(self):
        self.endpoint(events=["invoice.paid"])  # an endpoint, but none that wants staff alerts
        _w, _g, failed = self.episode()
        with self.assertLogs("apps.core.jobs", "WARNING"), self.assertLogs("apps.rails.jobs", "WARNING"):
            self.run_jobs()
        failed.refresh_from_db()
        self.assertEqual((failed.staff_alerted_at, failed.webhook_alerted_at), (None, None))
        self.assertEqual(WebhookEvent.objects.count(), 0)
        # Whoever is configured next hears of it, once.
        self.endpoint(events=["staff.alert"])
        self.run_jobs()
        self.run_jobs()
        self.assertEqual([a["kind"] for a in self.alerts()], ["watcher", "watcher", "outbox_failed"])
        failed.refresh_from_db()
        self.assertTrue(failed.staff_alerted_at and failed.webhook_alerted_at)

    @override_settings(STAFF_NOTIFY_EMAILS=["ops@example.com"])
    def test_a_failed_emission_is_tried_again_without_a_second_email(self):
        self.endpoint(events=["staff.alert"])
        _w, _g, failed = self.episode()
        with mock.patch("apps.webhooks.events._record", side_effect=IntegrityError("x")), \
                self.assertLogs("apps.webhooks.events", "ERROR"):
            core_jobs.outbox_failed_alert()
        self.assertEqual(len(mail.outbox), 1)
        failed.refresh_from_db()
        self.assertIsNotNone(failed.staff_alerted_at)
        self.assertIsNone(failed.webhook_alerted_at)  # could not record: not "nobody to tell"
        core_jobs.outbox_failed_alert()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.alerts(), [{"kind": "outbox_failed", "count": 1}])

    @override_settings(STAFF_NOTIFY_EMAILS=["ops@example.com"])
    def test_an_episode_told_by_email_is_not_replayed_to_a_later_endpoint(self):
        watcher, _g, failed = self.episode()
        self.run_jobs()
        self.assertEqual(len(mail.outbox), 3)
        self.endpoint(events=["staff.alert"])
        self.run_jobs()
        self.assertEqual(self.alerts(), [])  # old news; the next episode reaches it
        health.ok(watcher.name)
        watcher.refresh_from_db()
        self.assertEqual((watcher.alerted_at, watcher.webhook_alerted_at), (None, None))
        from apps.notify import services as notify

        notify.retry_now(OutboxMessage.objects.filter(pk=failed.pk))
        failed.refresh_from_db()
        self.assertEqual((failed.staff_alerted_at, failed.webhook_alerted_at), (None, None))


class HardeningTests(WebhookBase):
    def test_dns_is_refused_under_the_test_runner_unless_patched(self):
        with self.assertRaises(RuntimeError):
            delivery.check_destination("https://hooks.invalid/in")  # a test that forgot to mock DNS
        with resolving(PUBLIC_IP):
            delivery.check_destination("https://hooks.invalid/in")
        delivery.check_destination(f"https://{PUBLIC_IP}/in")  # a literal address needs no resolver

    def test_production_logging_pins_urllib3_to_error(self):
        # urllib3 logs every new connection's host at DEBUG, and the full URL in a WARNING.
        self.assertEqual(production_settings().LOGGING["loggers"]["urllib3"]["level"], "ERROR")

    def test_llms_txt_names_every_data_key(self):
        text = APIClient().get("/llms.txt").content.decode()
        section = text.split("## Webhooks", 1)[1].split("\n## ", 1)[0]
        for key in sorted(THIN_KEYS):
            self.assertIn(key, section)

    def test_endpoint_admin_is_read_only(self):
        endpoint, _ = self.endpoint()
        root = get_user_model().objects.create_superuser("root", password="pw-root-1")
        device = StaffTOTPDevice.objects.create(user=root, name="t", confirmed=True)
        self.client.force_login(root)
        session = self.client.session
        session["otp_device_id"] = device.persistent_id
        session["staff_verified_at"] = int(time.time())  # a fresh second factor: admin changes need it
        session.save()
        listing = self.client.get("/hidden-admin-xyz/webhooks/webhookendpoint/")
        self.assertEqual(listing.status_code, 200)
        self.assertNotContains(listing, HOST)
        page = self.client.get(f"/hidden-admin-xyz/webhooks/webhookendpoint/{endpoint.pk}/change/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="active"')
        self.assertNotContains(page, HOST)
        self.assertEqual(self.client.post(f"/hidden-admin-xyz/webhooks/webhookendpoint/{endpoint.pk}/change/",
                                          {"description": "x"}).status_code, 403)
        self.assertEqual(self.client.get(f"/hidden-admin-xyz/webhooks/webhookendpoint/{endpoint.pk}/delete/").status_code, 403)
        self.client.post("/hidden-admin-xyz/webhooks/webhookendpoint/", {
            "action": "delete_selected", "_selected_action": [endpoint.pk], "post": "yes"})
        endpoint.refresh_from_db()  # still there, still active: changes go through the audited API
        self.assertTrue(endpoint.active)


class ApiTests(WebhookBase):
    def test_api_creates_endpoint_and_shows_secret_once(self):
        c = self.admin_client()
        with resolving(PUBLIC_IP):
            r = c.post("/api/v1/webhooks", {"url": URL, "events": ["invoice.paid", "payment.confirmed"],
                                            "description": "liberation.services"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        created = r.json()
        endpoint = WebhookEndpoint.objects.get()
        self.assertEqual(created["secret"], endpoint.secret)
        self.assertGreaterEqual(len(created["secret"]), 43)  # token_urlsafe(32)
        self.assertEqual(created["events"], ["invoice.paid", "payment.confirmed"])
        self.assertEqual(created["url"], f"https://{HOST}/in/secret-path?...")  # never the query string
        self.assertEqual(endpoint.url, URL)
        detail = c.get(f"/api/v1/webhooks/{endpoint.pk}").json()
        listed = c.get("/api/v1/webhooks").json()
        self.assertNotIn("secret", detail)
        self.assertNotIn("secret", listed["results"][0])
        self.assertNotIn(created["secret"], json.dumps([detail, listed]))
        self.assertNotIn("s3cr3t", json.dumps([created, detail, listed]))
        # Ping: that endpoint only, and its deliveries, without the URL.
        with resolving(PUBLIC_IP):
            other = self.endpoint(events=["*"])[0]
        r = c.post(f"/api/v1/webhooks/{endpoint.pk}/ping")
        self.assertEqual(r.status_code, 202, r.content)
        self.assertEqual(WebhookDelivery.objects.filter(event__type="ping").count(), 1)
        self.assertFalse(WebhookDelivery.objects.filter(endpoint=other).exists())
        with Receiver(500), self.assertLogs("apps.webhooks.delivery", "WARNING"):
            delivery.flush()
        deliveries = c.get(f"/api/v1/webhooks/{endpoint.pk}/deliveries").json()
        row = deliveries["results"][0]
        self.assertEqual((row["event_type"], row["state"], row["attempts"], row["last_status_code"],
                          row["last_error_kind"]), ("ping", "queued", 1, 500, "http_500"))
        self.assertNotIn(HOST, json.dumps(deliveries))
        # Delete: gone, and nothing more is sent to it.
        self.assertEqual(c.delete(f"/api/v1/webhooks/{endpoint.pk}").status_code, 204)
        self.assertFalse(WebhookEndpoint.objects.filter(pk=endpoint.pk).exists())
        self.assertEqual(c.get(f"/api/v1/webhooks/{endpoint.pk}").status_code, 404)
        self.assertTrue(AuditEntry.objects.filter(action="webhook.create").exists())
        self.assertTrue(AuditEntry.objects.filter(action="webhook.delete").exists())

    def test_api_webhooks_need_admin_scope(self):
        endpoint, _ = self.endpoint()
        weak = self.admin_client(scopes=("read", "invoices:write", "payments:confirm"), name="weak")
        calls = [("get", "/api/v1/webhooks"), ("post", "/api/v1/webhooks"), ("get", f"/api/v1/webhooks/{endpoint.pk}"),
                 ("delete", f"/api/v1/webhooks/{endpoint.pk}"), ("get", f"/api/v1/webhooks/{endpoint.pk}/deliveries"),
                 ("post", f"/api/v1/webhooks/{endpoint.pk}/ping")]
        for method, path in calls:
            with self.subTest(method=method, path=path):
                self.assertEqual(getattr(weak, method)(path, {"url": URL}, format="json").status_code, 403)
        # Another organization's admin token does not see this endpoint.
        stranger = self.admin_client(org=Organization.objects.create(slug="other"), name="stranger")
        for method, path in calls[2:]:
            with self.subTest(stranger=path):
                self.assertEqual(getattr(stranger, method)(path).status_code, 404)
        self.assertEqual(stranger.get("/api/v1/webhooks").json()["count"], 0)
        self.assertEqual(self.admin_client(name="admin").get("/api/v1/webhooks").json()["count"], 1)
        self.assertTrue(WebhookEndpoint.objects.filter(pk=endpoint.pk).exists())

    def test_api_refuses_unknown_events(self):
        with resolving(PUBLIC_IP):
            r = self.admin_client().post("/api/v1/webhooks", {"url": URL, "events": ["invoice.eaten"]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(WebhookEndpoint.objects.exists())

    def test_api_answers_503_while_sealed(self):
        endpoint, _ = self.endpoint()
        c = self.admin_client()
        vault.seal()
        self.assertEqual(c.get(f"/api/v1/webhooks/{endpoint.pk}").status_code, 503)


class LoggingTests(WebhookBase):
    @override_settings(STAFF_NOTIFY_EMAILS=["ops@example.com"])
    def test_url_never_logged(self):
        self.endpoint(events=["invoice.sent"])
        self.invoice()
        leaked = requests.ConnectionError(f"HTTPSConnectionPool(host='{HOST}'): Max retries exceeded with url: {URL}")
        answers = [leaked, requests.Timeout(URL), requests.exceptions.SSLError(URL), 500, 302]
        with self.assertLogs(level="DEBUG") as logs:
            with Receiver(*answers):
                for _ in range(len(answers)):
                    delivery.flush()
                    self.due_now()
            with Receiver(200, resolves_to="10.0.0.1"):
                delivery.flush()
                self.due_now()
            with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror(-2, HOST)):
                delivery.flush()
                self.due_now()
            with Receiver(500):  # not fail_to_the_end: its own log capture would hide these lines
                for _ in range(delivery.MAX_ATTEMPTS):
                    delivery.flush()
                    self.due_now()
            core_jobs.webhooks_failed_alert()
        d = WebhookDelivery.objects.get()
        self.assertEqual(d.state, "failed")
        output = "\n".join(logs.output)
        for secret in (HOST, "secret-path", "s3cr3t", URL):
            self.assertNotIn(secret, output)
            self.assertNotIn(secret, mail.outbox[-1].body)
            for entry in AuditEntry.objects.filter(action__startswith="webhook."):
                self.assertNotIn(secret, json.dumps(entry.system_detail or {}))
        kinds = set(WebhookDelivery.objects.values_list("last_error_kind", flat=True))
        self.assertEqual(kinds, {"http_500"})
        self.assertIn("timeout", output)
        self.assertIn("refused:private_address", output)
        self.assertIn("refused:unresolvable", output)
