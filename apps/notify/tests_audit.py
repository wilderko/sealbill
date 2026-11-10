"""Regression tests from the invoicing audit of 2026-11-03 that concern outgoing e-mail (DATA-4, DATA-7).

DATA-4: a letter's recipients and contents stay in the outbox (encrypted) only until it is sent,
dismissed or cancelled; then `to`, `cc`, `subject_context` and `body_context` are cleared, so the
client's working `/i/` and `/p/` links are no longer kept anywhere. Kind, state and timestamps stay.

DATA-7: the `declared_staff` letter carries ids and the staff link only, never the client's name or
the total: staff letters go in plaintext, whatever key the client holds.

`SentLetterKeepsLiveTokensTest` and `StaffLetterNamesAnEncryptedClientTest` are ported from
`docs/superpowers/audit/2026-11-03-invoicing/poc/data/poc_data_3.py` in the liberation.services
repository.
"""
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, TransactionTestCase, override_settings

from apps.contacts.services import find_or_create_contact
from apps.invoices import tokens
from apps.invoices.services import create_invoice, reissue_links, send_invoice, void_invoice
from apps.keys import services as keys
from apps.notify import services as notify
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.vault.testing import UnsealedVaultMixin

LINE = {"name": "Service", "quantity": 1, "unit_price_minor": 133700}
FIXTURES = Path(__import__("apps").__file__).resolve().parent / "keys" / "fixtures"
CONTENTS = ("to", "cc", "subject_context", "body_context")


class Base(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.",
                                               email_from="billing@seller.example")
        add_bank(self.org)
        self.contact, _ = find_or_create_contact(self.org, "alice@example.test", legal_name="Alice Example")

    def assertForgotten(self, pk, state):
        """Only the bookkeeping is left: who and what are gone."""
        msg = OutboxMessage.objects.get(pk=pk)
        self.assertEqual(msg.state, state)
        for field in CONTENTS:
            self.assertIsNone(getattr(msg, field), field)
        self.assertTrue(msg.kind)
        self.assertIsNotNone(msg.created_at)
        return msg


class SentLetterKeepsLiveTokensTest(Base):
    """DATA-4: the capability tokens are "stored only as hashes" (spec 6.9), but every sent invoice
    letter kept them, recoverable with the vault key, for ever."""

    def test_sent_outbox_row_holds_no_working_capability_token(self):
        inv, _view, _pay = send_invoice(create_invoice(self.org, self.contact, currency="EUR", lines=[LINE])[0])
        self.assertEqual(notify.flush_outbox(), 1)
        msg = OutboxMessage.objects.get(kind="invoice")
        self.assertEqual(msg.state, "sent")
        context = msg.body_context or {}  # adapted: a sent row now keeps no context at all
        urls = [context.get("view_url", ""), context.get("pay_url", "")]
        live = [u for u in urls if u and tokens.resolve(u.rstrip("/").rsplit("/", 1)[1],
                                                        "view" if "/i/" in u else "pay") == inv]
        self.assertEqual(live, [], "a sent letter keeps the client's live capability URLs")
        sent = self.assertForgotten(msg.pk, "sent")
        self.assertIsNotNone(sent.sent_at)
        self.assertEqual((sent.invoice_id, sent.language), (inv.pk, "en"))
        self.assertEqual(len(mail.outbox), 1)  # the letter itself went out whole
        self.assertIn(_view, mail.outbox[0].body)


class ClearedWhenDoneTest(Base):
    """Every way a letter stops being sendable clears what it was going to say, and to whom."""

    def test_a_key_confirmation_forgets_its_link_once_sent(self):
        keys.add_key(self.contact, (FIXTURES / "alice_pub.asc").read_text(), source="client")
        self.assertEqual(notify.flush_outbox(), 1)
        self.assertForgotten(OutboxMessage.objects.get(kind="key_confirm").pk, "sent")

    def test_a_letter_cancelled_at_flush_is_cleared(self):
        inv = send_invoice(create_invoice(self.org, self.contact, currency="EUR", lines=[LINE])[0])[0]
        void_invoice(inv, "a mistake", None)
        self.assertEqual(notify.flush_outbox(), 0)
        self.assertForgotten(OutboxMessage.objects.get(kind="invoice").pk, "cancelled")

    def test_a_letter_cancelled_by_reissued_links_is_cleared(self):
        inv = send_invoice(create_invoice(self.org, self.contact, currency="EUR", lines=[LINE])[0])[0]
        reissue_links(inv)
        first = OutboxMessage.objects.filter(kind="invoice").order_by("pk").first()
        self.assertForgotten(first.pk, "cancelled")

    def test_a_dismissed_letter_is_cleared(self):
        staff = get_user_model().objects.create_user("clerk", password="x", is_staff=True)
        msg = notify.enqueue("vault_sealed_staff", to=["ops@example.org"], language="en",
                             context={"count": 2, "unlock_url": "https://pay.example/unlock/"})
        OutboxMessage.objects.filter(pk=msg.pk).update(state="failed")
        self.assertTrue(notify.dismiss(msg, staff, "the alert is obsolete"))
        self.assertForgotten(msg.pk, "cancelled")

    def test_a_failed_letter_keeps_its_contents_for_a_retry(self):
        msg = notify.enqueue("vault_sealed_staff", to=["ops@example.org"], language="en",
                             context={"count": 2, "unlock_url": "https://pay.example/unlock/"})
        OutboxMessage.objects.filter(pk=msg.pk).update(state="failed")
        msg.refresh_from_db()
        self.assertEqual(msg.to, ["ops@example.org"])
        self.assertEqual(msg.body_context["count"], 2)


class ForgetFinishedLettersMigrationTest(UnsealedVaultMixin, TransactionTestCase):
    """Migration notify.0009 clears the letters sent or cancelled before the fix, and only those."""

    BEFORE = [("notify", "0008_staff_alert_per_channel")]
    AFTER = [("notify", "0009_forget_finished_letters")]

    def migrate(self, targets):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_finished_letters_are_cleared_and_waiting_ones_kept(self):
        old = self.migrate(self.BEFORE).get_model("notify", "OutboxMessage")
        contents = {"to": ["alice@example.test"], "cc": [], "subject_context": {"ref": "2026-0001"},
                    "body_context": {"view_url": "https://pay.example/i/token/"}}
        rows = {state: old.objects.create(kind="invoice", state=state, **contents).pk
                for state in ("sent", "cancelled", "queued", "failed")}
        self.migrate(self.AFTER)
        for state, pk in rows.items():
            msg = OutboxMessage.objects.get(pk=pk)
            if state in ("sent", "cancelled"):
                self.assertEqual([getattr(msg, f) for f in CONTENTS], [None] * 4, state)
            else:
                self.assertEqual(msg.to, ["alice@example.test"], state)
                self.assertEqual(msg.body_context, contents["body_context"], state)
            self.assertEqual((msg.kind, msg.state), ("invoice", state))


class StaffLetterNamesAnEncryptedClientTest(Base):
    """DATA-7: the client keeps an active key, yet a staff letter about them carried their legal
    name in plaintext over SMTP (every other staff letter carries ids only)."""

    def test_declared_staff_letter_carries_no_client_name(self):
        from apps.payments.services import declare_payment

        keys.add_key(self.contact, (FIXTURES / "alice_pub.asc").read_text(), source="staff")
        inv, _v, _p = send_invoice(create_invoice(self.org, self.contact, currency="EUR", lines=[LINE])[0])
        with self.settings(STAFF_NOTIFY_EMAILS=["ops@example.org"]):
            declare_payment(inv, note="paid from my account at Bank X")
            queued = sorted(OutboxMessage.objects.get(kind="declared_staff").body_context)
            notify.flush_outbox()
        staff = [m for m in mail.outbox if m.to == ["ops@example.org"]]
        client = [m for m in mail.outbox if m.to == ["alice@example.test"]]
        self.assertTrue(client and "pgp-encrypted" in client[0].message().as_string())
        body = staff[0].body if staff else ""
        self.assertNotIn("Alice Example", body, "the staff letter names the client in plaintext")
        self.assertEqual(queued, ["admin_url", "ref"])
        # Nor the total, nor anything the client wrote: the invoice number and the staff link only.
        html = staff[0].alternatives[0][0]
        for text in (body, html):
            for leak in ("1 337", "1,337", "1337", "EUR", "Bank X", "Alice"):
                self.assertNotIn(leak, text, leak)
            self.assertIn(inv.number, text)
            self.assertIn(f"/invoices/{inv.pk}/", text)

    @override_settings(STAFF_NOTIFY_EMAILS=["ops@example.org"])
    def test_the_letter_reads_well_without_them(self):
        from apps.payments.services import declare_payment

        inv = send_invoice(create_invoice(self.org, self.contact, currency="EUR", lines=[LINE])[0])[0]
        declare_payment(inv)
        mail.outbox.clear()
        notify.flush_outbox()
        letter = next(m for m in mail.outbox if m.to == ["ops@example.org"])
        self.assertIn(f"A client has declared a payment for {inv.number}. It needs your review.", letter.body)
        self.assertNotIn("Client:", letter.body)
        self.assertNotIn("Amount:", letter.body)
