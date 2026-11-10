import datetime
from decimal import Decimal

from django.test import TestCase

from apps.contacts.services import find_or_create_contact
from apps.invoices import tokens
from apps.invoices.money import totals
from apps.invoices.models import Invoice, LineItem
from apps.invoices.services import (
    InvoiceError, create_invoice, mark_overdue, reissue_links, send_invoice, void_invoice,
)
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.vault.testing import UnsealedVaultMixin

LINE = {"name": "Consulting", "quantity": 1, "unit_price_minor": 10000}


class InvoiceServiceTests(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu")
        self.us = Organization.objects.create(slug="us", is_us_resident_issuer=True)
        add_bank(self.org)
        self.contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="A")

    def make(self, **kw):
        return create_invoice(self.org, self.contact, currency="EUR", lines=[LINE], **kw)[0]

    def test_us_resident_switches_issuer(self):
        self.contact.us_tax_resident = "yes"
        self.contact.save()
        inv, created = create_invoice(self.org, self.contact, currency="USD", lines=[LINE])
        self.assertTrue(created)
        self.assertTrue(inv.issued_by_rule)
        self.assertEqual(inv.org, self.us)
        self.assertEqual(inv.contact.org, self.us)

    def test_us_resident_without_issuer_refuses(self):
        self.us.delete()
        self.contact.us_tax_resident = "yes"
        self.contact.save()
        with self.assertRaises(InvoiceError):
            create_invoice(self.org, self.contact, currency="USD", lines=[LINE])

    def test_idempotency_key_returns_same_invoice(self):
        a, c1 = create_invoice(self.org, self.contact, currency="EUR", lines=[LINE], idempotency_key="k")
        b, c2 = create_invoice(self.org, self.contact, currency="EUR", lines=[LINE], idempotency_key="k")
        self.assertEqual((a.pk, c1, c2), (b.pk, True, False))

    def test_external_reference_returns_same_invoice(self):
        a, _ = create_invoice(self.org, self.contact, currency="EUR", lines=[LINE], external_reference="x")
        b, c2 = create_invoice(self.org, self.contact, currency="EUR", lines=[LINE], external_reference="x")
        self.assertEqual((a.pk, c2), (b.pk, False))

    def test_send_assigns_number_once(self):
        inv = self.make()
        inv, _, _ = send_invoice(inv)
        number = inv.number
        self.assertTrue(number)
        with self.assertRaises(InvoiceError):
            send_invoice(inv)
        inv.refresh_from_db()
        self.assertEqual(inv.number, number)

    def test_send_refuses_zero_total(self):
        inv = create_invoice(
            self.org, self.contact, currency="EUR", lines=[{**LINE, "unit_price_minor": 0}]
        )[0]
        with self.assertRaises(InvoiceError):
            send_invoice(inv)

    def test_sent_invoice_cannot_be_edited(self):
        inv, _, _ = send_invoice(self.make())
        inv.currency = "USD"
        with self.assertRaisesMessage(InvoiceError, "sent invoices cannot be edited"):
            inv.save()
        with self.assertRaises(InvoiceError):
            LineItem.objects.create(invoice=inv, unit_price_minor=1)
        with self.assertRaises(InvoiceError):
            inv.lines.first().delete()

    def test_void_from_sent(self):
        inv, _, _ = send_invoice(self.make())
        inv = void_invoice(inv, "mistake", None)
        inv.refresh_from_db()
        self.assertEqual(inv.status, "void")
        self.assertIsNotNone(inv.voided_at)
        with self.assertRaises(InvoiceError):
            void_invoice(inv, "again", None)

    def test_payment_request_has_no_number(self):
        inv, _, _ = send_invoice(self.make(kind="payment_request"))
        self.assertIsNone(inv.number)
        self.assertEqual(inv.status, "sent")

    def test_tokens_stored_hashed(self):
        inv, view, pay = send_invoice(self.make())
        inv.refresh_from_db()
        for f in inv._meta.concrete_fields:
            value = str(getattr(inv, f.attname))
            self.assertNotIn(view, value)
            self.assertNotIn(pay, value)
        self.assertEqual(inv.view_token_hash, tokens.hash_token(view))
        self.assertIn(view, inv.view_url(view))

    def test_reissue_links_revokes_old_token(self):
        inv, view, pay = send_invoice(self.make())
        self.assertEqual(tokens.resolve(view, "view"), inv)
        new_view, new_pay = reissue_links(inv)
        self.assertIsNone(tokens.resolve(view, "view"))
        self.assertIsNone(tokens.resolve(pay, "pay"))
        self.assertEqual(tokens.resolve(new_view, "view").pk, inv.pk)
        self.assertEqual(tokens.resolve(new_pay, "pay").pk, inv.pk)

    def test_mark_overdue(self):
        inv, _, _ = send_invoice(self.make(due_days=5))
        today = inv.issue_date
        self.assertEqual(mark_overdue(today + datetime.timedelta(days=5)), 0)
        self.assertEqual(mark_overdue(today + datetime.timedelta(days=6)), 1)
        inv.refresh_from_db()
        self.assertEqual(inv.status, "overdue")


class InvoiceFixRound1Tests(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu")
        self.org_b = Organization.objects.create(slug="eu2")
        self.us = Organization.objects.create(slug="us", is_us_resident_issuer=True)
        add_bank(self.org)
        self.contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="A")

    def make(self, **kw):
        return create_invoice(self.org, self.contact, currency="EUR", lines=[LINE], **kw)[0]

    def test_stale_object_cannot_add_or_delete_lines(self):
        inv = self.make()
        send_invoice(inv)  # caller keeps the stale draft-looking object
        self.assertEqual(inv.status, "draft")
        with self.assertRaises(InvoiceError):
            LineItem.objects.create(invoice=inv, unit_price_minor=-9000)
        with self.assertRaises(InvoiceError):
            inv.lines.first().delete()

    def test_number_cannot_be_rewritten_and_status_cannot_revert(self):
        inv, _, _ = send_invoice(self.make())
        inv.number = "X-1"
        with self.assertRaises(InvoiceError):
            inv.save()
        inv.refresh_from_db()
        inv.status = "draft"
        with self.assertRaises(InvoiceError):
            inv.save()
        with self.assertRaises(InvoiceError):
            inv.save(update_fields=["status"])

    def test_sent_invoice_cannot_be_deleted(self):
        inv, _, _ = send_invoice(self.make())
        with self.assertRaises(InvoiceError):
            inv.delete()
        with self.assertRaises(InvoiceError):
            Invoice.objects.filter(pk=inv.pk).delete()
        draft = self.make()
        draft.delete()

    def test_void_and_reissue_reread_the_row(self):
        inv, _, _ = send_invoice(self.make())
        stale = Invoice.objects.get(pk=inv.pk)
        Invoice.objects.filter(pk=inv.pk).update(status="paid")
        with self.assertRaises(InvoiceError):
            void_invoice(stale, "x", None)
        Invoice.objects.filter(pk=inv.pk).update(status="void")
        with self.assertRaises(InvoiceError):
            reissue_links(stale)

    def test_void_refuses_money_received(self):
        for status in ("partially_paid", "paid", "overpaid"):
            inv, _, _ = send_invoice(self.make())
            Invoice.objects.filter(pk=inv.pk).update(status=status)
            with self.assertRaises(InvoiceError):
                void_invoice(inv, "x", None)

    def test_references_do_not_leak_between_requesting_orgs(self):
        a_contact = self.contact
        a_contact.us_tax_resident = "yes"
        a_contact.save()
        a, _ = create_invoice(
            self.org, a_contact, currency="USD", lines=[LINE], external_reference="order-167"
        )
        self.assertEqual(a.org, self.us)
        b_contact, _ = find_or_create_contact(
            self.org_b, "b@example.com", us_tax_resident="yes"
        )
        b, created = create_invoice(
            self.org_b, b_contact, currency="USD", lines=[LINE], external_reference="order-167"
        )
        self.assertTrue(created)
        self.assertNotEqual(a.pk, b.pk)
        again, created = create_invoice(
            self.org, a_contact, currency="USD", lines=[LINE], external_reference="order-167"
        )
        self.assertEqual((again.pk, created), (a.pk, False))

    def test_invoice_contact_must_belong_to_org(self):
        inv = self.make()
        inv.org = self.org_b
        with self.assertRaises(InvoiceError):
            inv.save()

    def test_quantity_precision(self):
        with self.assertRaises(InvoiceError):
            create_invoice(self.org, self.contact, currency="EUR",
                           lines=[{**LINE, "quantity": "0.125"}])
        inv = create_invoice(self.org, self.contact, currency="EUR",
                             lines=[{**LINE, "quantity": 1.5}])[0]
        self.assertEqual(inv.lines.first().quantity, Decimal("1.5"))

    def test_foreign_contact_refused_without_us_rule(self):
        other, _ = find_or_create_contact(self.org_b, "z@example.com")
        with self.assertRaises(InvoiceError):
            create_invoice(self.org, other, currency="EUR", lines=[LINE])

    def test_contact_copy_rolls_back_with_invoice(self):
        self.contact.us_tax_resident = "yes"
        self.contact.save()
        with self.assertRaises(KeyError):
            create_invoice(self.org, self.contact, currency="USD", lines=[{"name": "x"}])
        from apps.contacts.models import Contact
        self.assertFalse(Contact.objects.filter(org=self.us).exists())

    def test_discount_amount_applies_before_tax(self):
        inv = create_invoice(self.org, self.contact, currency="EUR", lines=[
            {"quantity": 1, "unit_price_minor": 10000, "discount_amount_minor": 1000,
             "tax_rate_bps": 2000}])[0]
        self.assertEqual(totals(inv).total, 10800)


class RequestedByOrgRequiredTests(UnsealedVaultMixin, TestCase):
    def test_invoice_without_requested_by_org_is_refused(self):
        from django.db import IntegrityError, transaction

        org = Organization.objects.create(slug="eu")
        contact, _ = find_or_create_contact(org, "a@example.com")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Invoice.objects.create(
                org=org, contact=contact, currency="EUR", issue_date=datetime.date.today()
            )


class UsRuleAtSendTests(UnsealedVaultMixin, TestCase):
    """C2: a draft made before the contact became a US tax resident is never sent by the wrong issuer."""

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu")
        self.us = Organization.objects.create(slug="us", is_us_resident_issuer=True)
        self.contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="A")

    def test_send_refuses_when_the_issuer_would_switch(self):
        from apps.notify.models import OutboxMessage

        add_bank(self.org)
        draft = create_invoice(self.org, self.contact, currency="EUR", lines=[LINE])[0]
        self.contact.us_tax_resident = "yes"
        self.contact.save()
        with self.assertRaisesMessage(InvoiceError, "contact is a US tax resident: delete this draft and create it again"):
            send_invoice(draft)
        draft.refresh_from_db()
        self.assertEqual((draft.status, draft.number, draft.org), ("draft", None, self.org))
        self.assertEqual(OutboxMessage.objects.count(), 0)
        self.org.refresh_from_db()
        self.assertEqual(self.org.next_number, 1)  # no number was spent

    def test_invoice_already_issued_by_rule_still_sends(self):
        self.contact.us_tax_resident = "yes"
        self.contact.save()
        add_bank(self.us, "USD")
        inv = create_invoice(self.org, self.contact, currency="USD", lines=[LINE])[0]
        self.assertEqual(inv.org, self.us)
        inv, _, _ = send_invoice(inv, email_client=False)
        self.assertEqual(inv.status, "sent")


class DefaultMethodsTests(UnsealedVaultMixin, TestCase):
    """I3: what an invoice offers when the caller names nothing, and never offering nothing."""

    def setUp(self):
        super().setUp()
        from apps.orgs.models import BankAccount, FiatLink

        self.org = Organization.objects.create(slug="eu")
        self.us = Organization.objects.create(slug="us", is_us_resident_issuer=True)
        self.contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="A")
        self.eur = BankAccount.objects.create(org=self.org, currency="EUR", label="EUR")
        BankAccount.objects.create(org=self.org, currency="USD", label="USD")
        BankAccount.objects.create(org=self.org, currency="EUR", label="old", active=False)
        self.wise = FiatLink.objects.create(org=self.org, kind="wise", currencies=["EUR", "USD"])
        FiatLink.objects.create(org=self.org, kind="revolut", currencies=["USD"])
        FiatLink.objects.create(org=self.org, kind="n26", currencies=["EUR"], active=False)
        self.us_usd = BankAccount.objects.create(org=self.us, currency="USD", label="US")
        BankAccount.objects.create(org=self.us, currency="EUR", label="US EUR")

    def test_omitted_methods_default_to_the_issuers_matching_active_ones(self):
        inv = create_invoice(self.org, self.contact, currency="eur", lines=[LINE])[0]
        self.assertEqual(inv.offered_methods, [f"bank:{self.eur.pk}", f"fiat:{self.wise.pk}"])

    def test_defaults_come_from_the_issuer_after_the_us_rule(self):
        self.contact.us_tax_resident = "yes"
        self.contact.save()
        inv = create_invoice(self.org, self.contact, currency="USD", lines=[LINE])[0]
        self.assertEqual((inv.org, inv.offered_methods), (self.us, [f"bank:{self.us_usd.pk}"]))

    def test_an_invoice_that_offers_nothing_is_not_sent(self):
        for inv in (
            create_invoice(self.org, self.contact, currency="EUR", lines=[LINE], offered_methods=[])[0],
            create_invoice(self.org, self.contact, currency="CZK", lines=[LINE])[0],  # nothing in CZK
        ):
            with self.assertRaisesMessage(InvoiceError, "no payment method offered"):
                send_invoice(inv, email_client=False)
            inv.refresh_from_db()
            self.assertEqual((inv.status, inv.number), ("draft", None))

    def test_a_method_deactivated_since_creation_does_not_count(self):
        inv = create_invoice(self.org, self.contact, currency="EUR", lines=[LINE],
                             offered_methods=[f"bank:{self.eur.pk}"])[0]
        self.eur.active = False
        self.eur.save()
        with self.assertRaisesMessage(InvoiceError, "no payment method offered"):
            send_invoice(inv, email_client=False)

    def rail(self, org=None, **kw):
        from apps.rails.models import RailAccount

        fields = dict(org=org or self.org, kind="evm", label="EVM", address="0x" + "ab" * 20,
                      networks=["arbitrum"], assets=["USDC"])
        fields.update(kw)
        return RailAccount.objects.create(**fields)

    def test_active_rail_accounts_are_offered_in_any_currency(self):
        evm = self.rail()
        ln = self.rail(kind="lightning", label="LN", address="", networks=[], assets=["BTC"], config={"backend": "nwc"})
        self.rail(label="old", active=False)
        self.rail(org=self.us)
        inv = create_invoice(self.org, self.contact, currency="EUR", lines=[LINE])[0]
        self.assertEqual(inv.offered_methods,
                         [f"bank:{self.eur.pk}", f"fiat:{self.wise.pk}", f"rail:{evm.pk}", f"rail:{ln.pk}"])
        # a quote converts, so a crypto rail is offered whatever the invoice currency
        czk = create_invoice(self.org, self.contact, currency="CZK", lines=[LINE])[0]
        self.assertEqual(czk.offered_methods, [f"rail:{evm.pk}", f"rail:{ln.pk}"])

    def test_default_methods_include_active_rails(self):
        from apps.invoices.services import default_methods
        evm = self.rail()
        self.rail(label="old", active=False)
        self.rail(org=self.us)
        self.assertIn(f"rail:{evm.pk}", default_methods(self.org, "EUR"))
        self.assertEqual([m for m in default_methods(self.org, "CZK") if m.startswith("rail:")], [f"rail:{evm.pk}"])

    def test_an_invoice_offering_only_a_live_rail_can_be_sent(self):
        evm = self.rail()
        theirs = self.rail(org=self.us)
        for methods in ([f"rail:{theirs.pk}"], ["rail:999999"], ["rail:x"]):
            inv = create_invoice(self.org, self.contact, currency="CZK", lines=[LINE], offered_methods=methods)[0]
            with self.assertRaisesMessage(InvoiceError, "no payment method offered"):
                send_invoice(inv, email_client=False)
        inv = create_invoice(self.org, self.contact, currency="CZK", lines=[LINE], offered_methods=[f"rail:{evm.pk}"])[0]
        evm.active = False
        evm.save()
        with self.assertRaisesMessage(InvoiceError, "no payment method offered"):
            send_invoice(inv, email_client=False)
        evm.active = True
        evm.save()
        self.assertEqual(send_invoice(inv, email_client=False)[0].status, "sent")


class AuditTrailTests(UnsealedVaultMixin, TestCase):
    """I5: every state change leaves an audit entry naming who did it."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user("api:shop", is_active=False)
        self.org = Organization.objects.create(slug="eu")
        self.us = Organization.objects.create(slug="us", is_us_resident_issuer=True)
        add_bank(self.org)
        add_bank(self.us, "USD")

    def entry(self, action, obj):
        from apps.audit.models import AuditEntry

        return AuditEntry.objects.get(action=action, model_label=obj._meta.label_lower, object_pk=str(obj.pk))

    def test_contact_create_and_update_are_audited_with_the_flag_change(self):
        from apps.contacts.services import update_contact

        contact, _ = find_or_create_contact(self.org, "a@example.com", actor=self.user, legal_name="A")
        e = self.entry("contact.create", contact)
        self.assertEqual((e.actor, e.detail["us_tax_resident"]), (self.user, "unknown"))
        update_contact(contact, {"us_tax_resident": "yes", "display_name": "Ann", "legal_name": "A"}, actor=self.user)
        e = self.entry("contact.update", contact)
        self.assertEqual(e.detail["us_tax_resident"], ["unknown", "yes"])
        self.assertEqual(e.detail["fields"], ["display_name", "us_tax_resident"])  # legal_name did not change
        update_contact(contact, {"display_name": "Ann"}, actor=self.user)  # nothing changed: nothing recorded
        from apps.audit.models import AuditEntry

        self.assertEqual(AuditEntry.objects.filter(action="contact.update").count(), 1)

    def test_invoice_create_send_and_reissue_are_audited(self):
        contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="A")
        inv = create_invoice(self.org, contact, currency="EUR", lines=[LINE], actor=self.user)[0]
        e = self.entry("invoice.create", inv)
        self.assertEqual((e.actor, e.detail["issued_by_rule"], e.detail["requested_by_org"]), (self.user, False, "eu"))
        inv, _, _ = send_invoice(inv, email_client=False, actor=self.user)
        self.assertEqual(self.entry("invoice.send", inv).detail["number"], inv.number)
        reissue_links(inv, actor=self.user)
        e = self.entry("invoice.reissue_links", inv)
        self.assertEqual(e.actor, self.user)

    def test_issuer_switch_is_recorded_on_the_invoice_and_the_copied_contact(self):
        contact, _ = find_or_create_contact(self.org, "a@example.com", us_tax_resident="yes")
        inv = create_invoice(self.org, contact, currency="USD", lines=[LINE], actor=self.user)[0]
        e = self.entry("invoice.create", inv)
        self.assertEqual((e.detail["issued_by_rule"], e.detail["issuer_org"]), (True, "us"))
        copied = self.entry("contact.create", inv.contact)
        self.assertEqual(copied.detail["copied_from"], contact.pk)

    def test_client_declaration_is_audited_as_the_client(self):
        from apps.payments.services import declare_payment

        contact, _ = find_or_create_contact(self.org, "a@example.com")
        inv, _, _ = send_invoice(create_invoice(self.org, contact, currency="EUR", lines=[LINE])[0], email_client=False)
        d = declare_payment(inv, tx_hash="0xabc")
        e = self.entry("declaration.create", d)
        self.assertEqual((e.actor, e.actor_username, e.detail["invoice"]), (None, "client", inv.pk))


class TotalCapTests(UnsealedVaultMixin, TestCase):
    def test_service_refuses_a_total_above_ten_to_the_twelve(self):
        org = Organization.objects.create(slug="eu")
        contact, _ = find_or_create_contact(org, "a@example.com")
        with self.assertRaisesMessage(InvoiceError, "10^12"):
            create_invoice(org, contact, currency="EUR", lines=[
                {"name": "a", "quantity": 1, "unit_price_minor": 10**12},
                {"name": "b", "quantity": 1, "unit_price_minor": 1, "tax_rate_bps": 0}])
        self.assertFalse(Invoice.objects.exists())
        create_invoice(org, contact, currency="EUR", lines=[{"name": "a", "quantity": 1, "unit_price_minor": 10**12}])


class TokenHashConstraintTests(UnsealedVaultMixin, TestCase):
    """M9: a token hash names exactly one invoice; drafts have none."""

    def test_hashes_are_unique_indexed_and_null_on_drafts(self):
        from django.db import IntegrityError, transaction

        for name in ("view_token_hash", "pay_token_hash"):
            field = Invoice._meta.get_field(name)
            self.assertTrue(field.unique and field.null, name)
        org = Organization.objects.create(slug="eu")
        add_bank(org)
        contact, _ = find_or_create_contact(org, "a@example.com")
        a = create_invoice(org, contact, currency="EUR", lines=[LINE])[0]
        b = create_invoice(org, contact, currency="EUR", lines=[LINE])[0]
        self.assertIsNone(Invoice.objects.get(pk=a.pk).view_token_hash)  # two drafts, no clash
        send_invoice(a, email_client=False)
        sent = Invoice.objects.get(pk=a.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Invoice.objects.filter(pk=b.pk).update(view_token_hash=sent.view_token_hash)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Invoice.objects.filter(pk=b.pk).update(pay_token_hash=sent.pay_token_hash)
        self.assertIsNone(tokens.resolve("", "view"))
