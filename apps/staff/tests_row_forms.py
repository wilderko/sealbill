"""Row forms on the staff pages (final review C1): Enter in a note field runs the action that note
belongs to. A form holds one action: Confirm and Reject never share a form, so typing a reason for
a rejection and pressing Enter can no longer confirm the payment."""
import datetime
import re
from html.parser import HTMLParser
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.urls import resolve
from django.utils import timezone

from apps.audit.models import AuditEntry
from apps.links import services as links
from apps.links.models import LinkAlert
from apps.notify.models import OutboxMessage
from apps.payments.models import Payment
from apps.rails import health
from apps.rails.models import FxLegRate, RailAccount, ScanGap
from apps.staff.tests_crypto import CryptoBase
from apps.webhooks.models import WebhookDelivery, WebhookEndpoint, WebhookEvent

TEMPLATES = Path(settings.BASE_DIR) / "templates"
FORM = re.compile(r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>", re.S | re.I)
SUBMIT = re.compile(r"<button\b(?![^>]*\btype=\"button\")[^>]*>|<input\b[^>]*\btype=\"submit\"[^>]*>", re.I)


class _Forms(HTMLParser):
    """Every form of a page, as a browser would submit it: its action, its fields, its submit
    buttons (with their own formaction, name and value) and the queue it sits in."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms, self._form, self._button, self._region, self._depth = [], None, None, [], 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("section", "details") and a.get("id"):
            self._region.append((a["id"], self._depth))
        if tag in ("section", "details"):
            self._depth += 1
        if tag == "form":
            self._form = {"action": a.get("action") or "", "method": (a.get("method") or "get").lower(),
                          "fields": [], "buttons": [], "region": self._region[-1][0] if self._region else ""}
        elif self._form is not None and tag == "input":
            kind = (a.get("type") or "text").lower()
            if kind == "submit":
                self._form["buttons"].append({"formaction": a.get("formaction"), "name": a.get("name"),
                                              "value": a.get("value") or "", "text": a.get("value") or ""})
            elif kind not in ("checkbox", "radio") or "checked" in a:
                self._form["fields"].append((a.get("name"), a.get("value") or "", "required" in a))
        elif self._form is not None and tag == "textarea":
            self._form["fields"].append((a.get("name"), "", "required" in a))
        elif self._form is not None and tag == "button" and (a.get("type") or "submit").lower() == "submit":
            self._button = {"formaction": a.get("formaction"), "name": a.get("name"), "value": a.get("value") or "",
                            "text": ""}
            self._form["buttons"].append(self._button)

    def handle_endtag(self, tag):
        if tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None
        elif tag == "button":
            self._button = None
        if tag in ("section", "details"):
            self._depth -= 1
            if self._region and self._region[-1][1] == self._depth:
                self._region.pop()

    def handle_data(self, data):
        if self._button is not None:
            self._button["text"] += data


def forms_of(page: str) -> list[dict]:
    parser = _Forms()
    parser.feed(page)
    for form in parser.forms:
        for button in form["buttons"]:
            button["text"] = " ".join(button["text"].split())
    return parser.forms


# What a row's one button says, and the view it must run. Retry and Dismiss are named by their queue.
INTENDED = {
    "Confirm": "confirm", "Reject": "reject", "Assign": "assign", "Refunded": "refund",
    "Keep": "keep_gateway_invalid", "Acknowledge": "acknowledge_gap", "Resolved": "link_alert_resolve",
    "Reverse": "reverse", "Dismiss declaration": "dismiss",
}
BY_QUEUE = {("undelivered", "Retry now"): "retry_outbox", ("undelivered", "Dismiss for good"): "dismiss_outbox",
            ("undelivered-webhooks", "Retry now"): "retry_webhook",
            ("undelivered-webhooks", "Dismiss for good"): "dismiss_webhook"}


def intended(region: str, label: str) -> str:
    if label.startswith("Accept "):
        return "accept_fx_leg"
    return BY_QUEUE.get((region, label)) or INTENDED[label]


class BusyBase(CryptoBase):
    def busy(self):
        """One row in every queue that has a form."""
        now = timezone.now()
        made = [
            self.review(minor=10000, reason="after_expiry", tx_hash="0x" + "21" * 32),  # Confirm, Reject, Assign
            self.review(invoice=False, tx_hash="0x" + "22" * 32),                       # Reject, Assign
            self.review(final=False, minor=10000, reason="after_expiry", tx_hash="0x" + "23" * 32),  # Reject only
            self.review(invoice=False, reason="dust", tx_hash="0x" + "24" * 32, received_amount="0.0001",
                        amount_base_units="100"),
            Payment.objects.create(rail="manual", asset="XMR", amount_minor=10000, received_amount="0.5",
                                   state="detected", needs_staff_confirmation=True, invoice=self.invoice,
                                   external_id="xmr-1"),
        ]
        btcpay = RailAccount.objects.create(org=self.org, kind="btcpay", label="BTCPay", assets=["BTC"],
                                            config={"url": "https://btcpay.example", "store_id": "S1"})
        stuck = Payment.objects.create(rail="btcpay", network="bitcoin", asset="BTC", amount_minor=10000,
                                       received_amount="0.00123456", state="detected", invoice=self.invoice,
                                       external_id="btcpay:S1:stuck", rail_account=btcpay)
        Payment.objects.filter(pk=stuck.pk).update(detected_at=now - datetime.timedelta(hours=7))
        made.append(stuck)
        made.append(Payment.objects.create(rail="evm", network="arbitrum", asset="USDC", amount_minor=10000,
                                           received_amount="110", state="confirmed", invoice=self.invoice,
                                           refund_due_base_units="1500000", tx_hash="0x" + "25" * 32))
        made.append(Payment.objects.create(rail="btcpay", network="bitcoin", asset="BTC", amount_minor=1,
                                           received_amount="0.00000001", state="confirmed", invoice=self.invoice,
                                           external_id="btcpay:S1:bad", rail_account=btcpay, confirmed_at=now,
                                           gateway_invalid_at=now))
        health.failed("fx:EUR:CZK", "jump")
        FxLegRate.objects.create(base="EUR", quote="CZK", rate="25.1", source_date=now.date(), pending_rate="27.9",
                                 pending_source_date=now.date(), pending_seen_at=now)
        ScanGap.objects.create(network="arbitrum", address=self.acct.address, from_block=11, to_block=8000)
        OutboxMessage.objects.create(kind="invoice", state="failed", attempts=12, last_error="SMTP",
                                     invoice=self.invoice)
        link, _token = links.create_link(self.org, title="Consultation", currency="EUR", price_minor=10000)
        LinkAlert.objects.create(kind="no_us_issuer", link=link)
        endpoint = WebhookEndpoint.objects.create(org=self.org, url="https://hook.example/x", secret="s", events=["*"])
        event = WebhookEvent.objects.create(type="invoice.sent", org=self.org)
        WebhookDelivery.objects.create(event=event, endpoint=endpoint, state="failed", attempts=12)
        return made


class OneActionPerFormMarkupTests(BusyBase):
    def test_no_form_holds_a_second_action(self):
        """No POST form carries a formaction, and one with several submit buttons offers a choice of
        one field's value for one action (the theme switch), never a second action."""
        checked = 0
        for path in sorted(TEMPLATES.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            name = str(path.relative_to(TEMPLATES))
            for m in FORM.finditer(text):
                attrs, body = m.group("attrs"), m.group("body")
                if 'method="get"' in attrs.lower():
                    continue  # a GET form reads (the CSV downloads); it changes nothing
                checked += 1
                self.assertNotIn("formaction", body, f"{name}: a POST form with a second action")
                buttons = SUBMIT.findall(body)
                if len(buttons) > 1:
                    names = {re.search(r'\bname="([^"]*)"', b).group(1) if 'name="' in b else None for b in buttons}
                    self.assertEqual(len(names), 1, f"{name}: several submit buttons that are not one choice")
                    self.assertNotIn(None, names, f"{name}: several submit buttons that are not one choice")
        self.assertGreater(checked, 40)

    def test_confirm_and_reject_never_share_a_form_on_either_page(self):
        self.busy()
        self.sign_in()
        for url in ("/", f"/invoices/{self.invoice.pk}/"):
            for form in forms_of(self.client.get(url).content.decode()):
                if form["method"] != "post":
                    continue
                targets = {b["formaction"] or form["action"] for b in form["buttons"]}
                self.assertLessEqual(len(targets), 1, (url, form["action"], form["buttons"]))
                if "/confirm/" in form["action"]:
                    self.assertEqual([b["text"] for b in form["buttons"]], ["Confirm"], url)

def _value_for(name: str, invoice) -> str:
    return {"invoice": invoice.number or str(invoice.pk), "amount": "100.00"}.get(name, "checked by the test")


class EveryRowFormRunsItsOwnViewTests(BusyBase):
    """Each form is submitted the way pressing Enter in its note field submits it (its first submit
    button, with that button's formaction, if any), with the fields it renders, and the view its
    button names is the one that ran."""

    def submit_each(self, page_url: str, actions_under: str = "/"):
        self.sign_in()
        forms = [f for f in forms_of(self.client.get(page_url).content.decode())
                 if f["method"] == "post" and f["action"].startswith(actions_under) and f["action"] not in ("/logout/", "/theme/")]
        ran = []
        for form in forms:
            default = form["buttons"][0]  # what Enter in any field of the form submits
            target = default["formaction"] or form["action"]
            # Enter in a note runs what the note belongs to: every button of its form runs that view.
            self.assertEqual({b["formaction"] or form["action"] for b in form["buttons"]}, {target},
                             (form["region"], [b["text"] for b in form["buttons"]]))
            expected = intended(form["region"], default["text"])
            data = {name: value or _value_for(name, self.invoice) for name, value, _required in form["fields"] if name}
            if default["name"]:
                data[default["name"]] = default["value"]
            before = set(AuditEntry.objects.values_list("pk", flat=True))
            sid = transaction.savepoint()
            try:
                r = self.client.post(target, data)
                url_name = resolve(target).url_name
                self.assertEqual(url_name, expected, (form["region"], default["text"], target))
                self.assertEqual(r.status_code, 302, (form["region"], default["text"], target,
                                                      re.findall(r'role="alert">([^<]*)', r.content.decode())))
                actions = set(AuditEntry.objects.exclude(pk__in=before).values_list("action", flat=True))
                self.assertTrue(actions, (form["region"], default["text"], "nothing was recorded"))
                self.check_effect(url_name, target, actions)
            finally:
                transaction.savepoint_rollback(sid)
            ran.append((form["region"], url_name))
        return ran

    def check_effect(self, url_name: str, target: str, actions: set[str]):
        m = re.search(r"/payments/(\d+)/", target)
        if m is None:
            return
        state = Payment.objects.filter(pk=int(m.group(1))).values_list("state", flat=True).first()
        if url_name in ("confirm", "assign"):
            self.assertEqual(state, "confirmed", (url_name, actions))
            self.assertFalse(any("reject" in a for a in actions), actions)
        elif url_name == "reject":
            self.assertEqual(state, "rejected", actions)
            self.assertFalse(any("confirm" in a for a in actions), actions)

    def test_every_form_on_a_busy_needs_a_person_runs_the_view_it_names(self):
        self.busy()
        ran = self.submit_each("/")
        regions = {region for region, _ in ran}
        self.assertTrue({"reviews", "dust", "awaiting", "stuck-gateway", "refunds", "invalidated", "fx", "gaps",
                         "undelivered", "link-alerts", "undelivered-webhooks"} <= regions, regions)
        for pair in (("reviews", "confirm"), ("reviews", "reject"), ("reviews", "assign"), ("awaiting", "confirm"),
                     ("awaiting", "reject"), ("stuck-gateway", "confirm"), ("stuck-gateway", "reject"),
                     ("dust", "reject"), ("undelivered", "dismiss_outbox"), ("undelivered-webhooks", "dismiss_webhook")):
            self.assertIn(pair, ran)
        # Three review rows offer Reject: the confirmable one, the one with no invoice, the one not final yet.
        self.assertEqual(sum(1 for pair in ran if pair == ("reviews", "reject")), 3, ran)

    def test_every_payment_form_on_the_invoice_page_runs_the_view_it_names(self):
        self.busy()
        Payment.objects.create(rail="bank", asset="EUR", amount_minor=500, received_amount="5", state="confirmed",
                               invoice=self.invoice, confirmed_at=timezone.now())
        ran = [name for _region, name in self.submit_each(f"/invoices/{self.invoice.pk}/", "/payments/")]
        for name in ("confirm", "reject", "reverse"):
            self.assertIn(name, ran)
