"""SEC-1 (audit 2026-11): a payment-link checkout creates no client of record until it pays.

A checkout whose e-mail matches a client of record books the order to it and changes nothing on it. One
that matches none makes a contact of its own from what the visitor typed (`Contact.of_record` False):
no lookup finds it, nothing reuses it (another checkout, the API, the staff form, the US-resident
issuer's copies), until its order is paid. The API says which contacts have paid (`has_paid_invoice`)."""
import datetime

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.models import ApiToken
from apps.contacts.models import Contact, ContactEmail
from apps.contacts.services import find_contact, find_or_create_contact, search_by_name
from apps.invoices.errors import InvoiceError
from apps.invoices.models import Invoice
from apps.invoices.services import create_invoice
from apps.keys.mirroring import copies_of
from apps.links import services as links
from apps.links.models import LinkOrder
from apps.links.tests import LinkBase, order
from apps.vault.testing import UnsealedVaultMixin

TYPED = dict(email="alice@victim.example", name="Mallory Imposter", phone="+10000000000",
             billing={"line1": "1 Attacker Road", "city": "Nowhere"}, us_tax_resident="no")


class Base(LinkBase):
    def api(self, scopes=("read", "invoices:write")):
        _token, plain = ApiToken.issue(self.org, f"t{ApiToken.objects.count()}", list(scopes))
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")
        return client

    def stranger(self, **kw):
        """A visitor who checks out with somebody else's e-mail and never pays."""
        return links.checkout(self.link, order(**{**TYPED, **kw}))[0]

    def api_invoice(self, client, contact, key="order-1"):
        r = client.post("/api/v1/invoices", {
            "external_reference": key, "currency": "EUR", "contact": contact,
            "lines": [{"name": "Bank account", "quantity": "1", "unit_price_minor": 133700}],
        }, format="json", HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(r.status_code, 201, r.content)
        return Invoice.objects.get(pk=r.json()["id"])


class AuditPocTests(Base):
    """The audit's two PoCs (poc_sec_1), kept as regression tests."""

    def test_link_checkout_decides_the_name_on_a_later_api_invoice(self):
        self.stranger()
        client = self.api()
        inv = self.api_invoice(client, {"email": "alice@victim.example", "legal_name": "Alice Real",
                                        "us_tax_resident": "no"})
        s = client.post(f"/api/v1/invoices/{inv.pk}/send", {"email_client": False, "client_letters": False},
                        format="json")
        self.assertEqual(s.status_code, 200, s.content)
        inv.refresh_from_db()
        self.assertEqual(inv.snapshot["buyer"]["name"], "Alice Real")
        self.assertNotIn("1 Attacker Road", inv.snapshot["buyer"]["address_lines"])

    def test_unpaid_link_checkout_answers_the_name_search(self):
        links.checkout(self.link, order(email="attacker@evil.example", name="Jan Novák", us_tax_resident="no"))
        r = self.api(("read",)).get("/api/v1/contacts", {"name": "Novak"})
        self.assertEqual(r.json()["results"], [])


class ExistingClientTests(Base):
    """Requirement 1: a checkout never attaches what was typed to an existing contact, nor shows it."""

    def test_typed_fields_never_reach_an_existing_contact(self):
        stored, _ = find_or_create_contact(self.org, "alice@victim.example", legal_name="Alice Real",
                                           phone="+421 1", address={"line1": "Real Street 1"},
                                           preferred_language="sk", us_tax_resident="no")
        before = {f: getattr(stored, f) for f in ("legal_name", "phone", "address", "preferred_language",
                                                   "us_tax_resident", "of_record")}
        invoice = links.checkout(self.link, order(**{**TYPED, "us_tax_resident": "yes"}), language="de")[0]
        fresh = Contact.objects.get(pk=stored.pk)
        self.assertEqual({f: getattr(fresh, f) for f in before}, before)
        self.assertEqual(Contact.objects.filter(org=self.org, copy_for_org__isnull=True).count(), 1)
        self.assertEqual(LinkOrder.objects.get(invoice=invoice).buyer_contact_id, stored.pk)
        self.assertEqual(invoice.snapshot["buyer"]["name"], "Mallory Imposter")  # what was typed, never what is stored
        self.assertNotIn("Real Street 1", str(invoice.snapshot["buyer"]))

    def test_a_paid_order_on_an_existing_contact_changes_nothing_on_it(self):
        stored, _ = find_or_create_contact(self.org, "alice@victim.example", legal_name="Alice Real")
        self.pay_fully(self.stranger())
        fresh = Contact.objects.get(pk=stored.pk)
        self.assertEqual((fresh.legal_name, fresh.phone, fresh.of_record), ("Alice Real", None, True))


class UnpaidCheckoutContactTests(Base):
    """Requirement 2: an unpaid checkout's contact is found by nothing."""

    def test_it_is_not_a_client_of_record(self):
        invoice = self.stranger()
        contact = invoice.contact
        self.assertFalse(contact.of_record)
        self.assertFalse(ContactEmail.objects.get(contact=contact).of_record)
        self.assertEqual(LinkOrder.objects.get(invoice=invoice).buyer_contact_id, contact.pk)
        self.assertEqual((contact.legal_name, contact.phone), ("Mallory Imposter", "+10000000000"))

    def test_service_lookups_skip_it(self):
        made = self.stranger().contact
        self.assertIsNone(find_contact(self.org, "alice@victim.example"))
        self.assertEqual(search_by_name(self.org, "Mallory"), [])
        contact, created = find_or_create_contact(self.org, "alice@victim.example", legal_name="Alice Real")
        self.assertTrue(created)
        self.assertNotEqual(contact.pk, made.pk)
        self.assertEqual((contact.legal_name, contact.of_record), ("Alice Real", True))

    def test_api_lookups_skip_it(self):
        made = self.stranger().contact
        client = self.api()
        self.assertEqual(client.get("/api/v1/contacts").json()["count"], 0)
        self.assertEqual(client.get("/api/v1/contacts", {"email": "alice@victim.example"}).json()["count"], 0)
        self.assertEqual(client.get("/api/v1/contacts", {"name": "Mallory Imposter"}).json()["count"], 0)
        self.assertEqual(client.get(f"/api/v1/contacts/{made.pk}").status_code, 404)
        self.assertEqual(client.patch(f"/api/v1/contacts/{made.pk}", {"legal_name": "X"}, format="json").status_code, 404)
        self.assertEqual(client.get(f"/api/v1/contacts/{made.pk}/keys").status_code, 404)
        r = client.post("/api/v1/contacts", {"email": "alice@victim.example", "legal_name": "Alice Real"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["legal_name"], "Alice Real")

    def test_the_payment_methods_lookup_ignores_its_us_answer(self):
        self.stranger(us_tax_resident="yes")
        r = self.api(("read",)).get("/api/v1/payment-methods", {"currency": "EUR",
                                                                 "contact_email": "alice@victim.example"})
        self.assertEqual((r.json()["issuer_org"], r.json()["issued_by_rule"]), ("lt", False))

    def test_another_checkout_never_reuses_it(self):
        first = self.stranger().contact
        second = links.checkout(self.link, order(email="alice@victim.example", name="Alice Real",
                                                 us_tax_resident="no"))[0]
        self.assertNotEqual(second.contact_id, first.pk)
        self.assertEqual(second.contact.legal_name, "Alice Real")
        self.assertEqual(second.snapshot["buyer"]["name"], "Alice Real")

    def test_nothing_else_invoices_it(self):
        made = self.stranger().contact
        with self.assertRaises(InvoiceError):
            create_invoice(self.org, made, currency="EUR", lines=[{"name": "x", "unit_price_minor": 100}])


class InlineContactTests(Base):
    """Requirement 3: the API's inline contact never lands on an unpaid checkout's contact."""

    def test_inline_data_wins(self):
        made = self.stranger().contact
        inv = self.api_invoice(self.api(), {"email": "alice@victim.example", "legal_name": "Alice Real",
                                            "us_tax_resident": "no"})
        self.assertNotEqual(inv.contact_id, made.pk)
        self.assertEqual((inv.contact.legal_name, inv.contact.phone, inv.contact.of_record), ("Alice Real", None, True))
        self.assertEqual(Contact.objects.get(pk=made.pk).legal_name, "Mallory Imposter")  # untouched, still its order's

    def test_a_contact_id_naming_it_is_refused(self):
        made = self.stranger().contact
        r = self.api().post("/api/v1/invoices", {
            "contact_id": made.pk, "currency": "EUR",
            "lines": [{"name": "x", "quantity": "1", "unit_price_minor": 100}]}, format="json")
        self.assertEqual(r.status_code, 400, r.content)

    def test_a_us_checkout_never_shapes_the_issuers_copy(self):
        """The audit: when the invoice goes to the US-resident issuer, its copy inherited the stranger's data."""
        made = self.stranger(us_tax_resident="yes")
        self.assertEqual(made.org_id, self.us.pk)
        self.assertEqual((made.contact.copy_for_org_id, made.contact.of_record), (self.org.pk, False))
        self.assertIsNone(find_contact(self.us, "alice@victim.example", copy_for=self.org))
        inv = self.api_invoice(self.api(), {"email": "alice@victim.example", "legal_name": "Alice Real",
                                            "us_tax_resident": "yes"})
        self.assertEqual(inv.org_id, self.us.pk)
        self.assertNotEqual(inv.contact_id, made.contact_id)
        self.assertEqual((inv.contact.legal_name, inv.contact.of_record), ("Alice Real", True))
        native = find_contact(self.org, "alice@victim.example")
        self.assertEqual(copies_of(native.pk), {inv.contact_id})  # the checkout's copy is linked to nothing


class PaymentDoesNotPromoteTests(Base):
    """Review S1, F2 (ruling): a link payment does not buy a client of record. A contact born in a checkout
    stays a link buyer, whatever was paid, until a person promotes it."""

    def test_a_paid_checkout_stays_a_link_buyer(self):
        invoice = self.pay_fully(self.stranger(name="Jan Novák"))
        self.assertEqual(invoice.status, "paid")
        contact = Contact.objects.get(pk=invoice.contact_id)
        self.assertFalse(contact.of_record)
        self.assertIsNone(find_contact(self.org, "alice@victim.example"))
        self.assertEqual(search_by_name(self.org, "Novak"), [])
        client = self.api(("read",))
        self.assertEqual(client.get("/api/v1/contacts", {"name": "Novak"}).json()["results"], [])
        self.assertEqual(client.get("/api/v1/contacts", {"email": "alice@victim.example"}).json()["results"], [])

    def test_the_one_cent_attack(self):
        """The verdict: a `customer_chooses` link with no floor sells "a client of record" for one cent."""
        link, _token = self.make_link(customer_chooses=True, price_minor=None)
        invoice = links.checkout(link, order(email="attacker@evil.example", name="Jan Novák", us_tax_resident="no",
                                             amount_minor=1))[0]
        self.pay_fully(invoice)
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, "paid")
        client = self.api()
        self.assertEqual(client.get("/api/v1/contacts", {"name": "Novak"}).json()["results"], [])
        inv = self.api_invoice(client, {"email": "attacker@evil.example", "legal_name": "Jan Novák Real",
                                        "us_tax_resident": "no"})
        self.assertNotEqual(inv.contact_id, invoice.contact_id)  # the inline data wins
        self.assertEqual(client.get(f"/api/v1/contacts/{inv.contact_id}").json()["has_paid_invoice"], False)

    def test_a_paid_us_order_leaves_the_original_and_the_copy_link_buyers(self):
        invoice = self.pay_fully(self.stranger(us_tax_resident="yes"))
        original = Contact.objects.get(pk=LinkOrder.objects.get(invoice=invoice).buyer_contact_id)
        copy = Contact.objects.get(pk=invoice.contact_id)
        self.assertEqual((original.org_id, original.of_record), (self.org.pk, False))
        self.assertEqual((copy.org_id, copy.copy_for_org_id, copy.of_record), (self.us.pk, self.org.pk, False))
        self.assertIsNone(find_contact(self.us, "alice@victim.example", copy_for=self.org))

    def test_settling_a_link_order_while_sealed_changes_no_contact(self):
        """A chain watcher settles orders while the vault is sealed: through `apply_payments` and `order_paid`."""
        from apps.invoices.money import totals
        from apps.payments.models import Payment
        from apps.payments.services import apply_payments
        from apps.vault.vault import vault

        invoice = self.stranger()
        Payment.objects.create(invoice=invoice, rail="bank", asset="EUR", amount_minor=totals(invoice).total,
                               received_amount=100, state="confirmed", confirmed_at=timezone.now())
        vault.seal()
        self.assertEqual(apply_payments(invoice), "paid")
        self.assertEqual(LinkOrder.objects.filter(invoice=invoice).values_list("hold", flat=True).get(), "used")
        self.assertEqual(Contact.objects.filter(pk=invoice.contact_id).values_list("of_record", flat=True).get(), False)


class PublicFormTests(Base):
    """Review S1, F7: the whole way through the public form, `POST /l/<token>/`."""

    def test_the_form_makes_a_link_buyer_from_what_was_typed(self):
        r = self.post(self.form(email="alice@victim.example", name="Mallory Imposter"))
        self.assertEqual(r.status_code, 302, r.content[:300])
        invoice = Invoice.objects.get(payment_link=self.link)
        self.assertEqual((invoice.contact.of_record, invoice.contact.legal_name), (False, "Mallory Imposter"))
        self.assertEqual(invoice.snapshot["buyer"]["name"], "Mallory Imposter")
        self.assertIsNone(find_contact(self.org, "alice@victim.example"))

    def test_the_form_changes_nothing_on_an_existing_client(self):
        stored, _ = find_or_create_contact(self.org, "alice@victim.example", legal_name="Alice Real", phone="+421 1")
        r = self.post(self.form(email="alice@victim.example", name="Mallory Imposter", us_tax_resident="yes"))
        self.assertEqual(r.status_code, 302, r.content[:300])
        invoice = Invoice.objects.get(payment_link=self.link)
        self.assertEqual(LinkOrder.objects.get(invoice=invoice).buyer_contact_id, stored.pk)
        fresh = Contact.objects.get(pk=stored.pk)
        self.assertEqual((fresh.legal_name, fresh.phone, fresh.us_tax_resident, fresh.of_record),
                         ("Alice Real", "+421 1", "unknown", True))
        self.assertEqual(invoice.snapshot["buyer"]["name"], "Mallory Imposter")
        page = self.client.get(r["Location"]).content.decode()
        self.assertNotIn("Alice Real", page)
        self.assertNotIn("+421 1", page)


class HasPaidInvoiceTests(Base):
    """Requirement 4, as ruled in F2: only invoices staff or the API issued count, never a link order."""

    def test_has_paid_invoice(self):
        client = self.api()
        unpaid = self.api_invoice(client, {"email": "a@example.com", "legal_name": "Unpaid"}, key="u")
        paid = self.api_invoice(client, {"email": "b@example.com", "legal_name": "Paid"}, key="p")
        client.post(f"/api/v1/invoices/{paid.pk}/send", {"email_client": False}, format="json")
        self.pay_fully(paid)
        rows = {c["legal_name"]: c["has_paid_invoice"] for c in client.get("/api/v1/contacts").json()["results"]}
        self.assertEqual(rows, {"Unpaid": False, "Paid": True})
        self.assertIs(client.get(f"/api/v1/contacts/{paid.contact_id}").json()["has_paid_invoice"], True)
        self.assertIs(client.get(f"/api/v1/contacts/{unpaid.contact_id}").json()["has_paid_invoice"], False)

    def test_a_paid_link_order_never_counts(self):
        stored, _ = find_or_create_contact(self.org, "alice@victim.example", legal_name="Alice Real")
        self.pay_fully(self.stranger())  # booked to the existing client, and paid
        self.assertIs(self.api(("read",)).get(f"/api/v1/contacts/{stored.pk}").json()["has_paid_invoice"], False)

    def test_an_invoice_paid_to_the_us_issuers_copy_counts(self):
        client = self.api()
        inv = self.api_invoice(client, {"email": "us@example.com", "legal_name": "Us Person",
                                        "us_tax_resident": "yes"})
        client.post(f"/api/v1/invoices/{inv.pk}/send", {"email_client": False}, format="json")
        self.pay_fully(inv)
        native = find_contact(self.org, "us@example.com")
        self.assertIs(client.get(f"/api/v1/contacts/{native.pk}").json()["has_paid_invoice"], True)

    def test_llms_txt_documents_it(self):
        text = " ".join(APIClient().get("/llms.txt").content.decode().split())
        for needle in ('"has_paid_invoice"', "never a payment-link order", "paying does not change that"):
            self.assertTrue(needle in text, f"llms.txt does not say {needle!r}")


class StaffBase(Base):
    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model

        from apps.staff.models import StaffTOTPDevice
        from apps.staff.tests import token_for

        self.staff = get_user_model().objects.create_user("clerk", password="pw-clerk-1", is_staff=True)
        device = StaffTOTPDevice.objects.create(user=self.staff, name="t", confirmed=True)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post("/verify/", {"token": token_for(device)}).status_code, 302)


class StaffPagesTests(StaffBase):
    """F2: a paying customer is never invisible to staff. The list shows link buyers, marked, with a filter."""

    def test_the_list_shows_link_buyers_marked(self):
        made = self.stranger().contact
        client, _ = find_or_create_contact(self.org, "client@example.com", legal_name="Real Client")
        page = self.client.get("/contacts/").content.decode()
        self.assertIn(f"/contacts/{made.pk}/", page)
        self.assertIn(f"/contacts/{client.pk}/", page)
        self.assertEqual(page.count("Link buyer</span>"), 1)

    def test_the_filter(self):
        made = self.stranger().contact
        client, _ = find_or_create_contact(self.org, "client@example.com", legal_name="Real Client")
        buyers = self.client.get("/contacts/", {"kind": "link"}).content.decode()
        self.assertIn(f"/contacts/{made.pk}/", buyers)
        self.assertNotIn(f"/contacts/{client.pk}/", buyers)
        clients = self.client.get("/contacts/", {"kind": "record"}).content.decode()
        self.assertNotIn(f"/contacts/{made.pk}/", clients)
        self.assertIn(f"/contacts/{client.pk}/", clients)
        found = self.client.get("/contacts/", {"q": "alice@victim.example"}).content.decode()
        self.assertIn(f"/contacts/{made.pk}/", found)
        self.assertIn("Link buyer</span>", found)

    def test_its_page_says_it_is_a_link_buyer(self):
        made = self.stranger().contact
        page = self.client.get(f"/contacts/{made.pk}/").content.decode()
        self.assertIn("not a client of record", page)
        self.assertIn(f"/contacts/{made.pk}/promote/", page)


class StaffPromotionTests(StaffBase):
    """F2: a person makes a link buyer a client of record, after checking it. Audited."""

    def test_staff_promote_a_link_buyer(self):
        from apps.audit.models import AuditEntry

        made = self.pay_fully(self.stranger(name="Alice Paid")).contact
        r = self.client.post(f"/contacts/{made.pk}/promote/")
        self.assertEqual(r.status_code, 302)
        fresh = Contact.objects.get(pk=made.pk)
        self.assertTrue(fresh.of_record)
        self.assertTrue(ContactEmail.objects.get(contact=fresh).of_record)
        self.assertEqual(find_contact(self.org, "alice@victim.example"), fresh)
        entry = AuditEntry.objects.get(action="contact.promote", object_pk=str(made.pk))
        self.assertEqual(entry.actor, self.staff)
        self.assertNotIn(f"/contacts/{made.pk}/promote/", self.client.get(f"/contacts/{made.pk}/").content.decode())

    def test_refused_when_a_client_already_holds_the_address(self):
        made = self.stranger().contact
        real, _ = find_or_create_contact(self.org, "alice@victim.example", legal_name="Alice Real")
        r = self.client.post(f"/contacts/{made.pk}/promote/", follow=True)
        self.assertContains(r, f"contact #{real.pk}")
        self.assertFalse(Contact.objects.get(pk=made.pk).of_record)

    def test_refused_for_an_issuer_copy(self):
        """A copy is promoted by nobody: once its original is a client, the next invoice makes a copy of that."""
        copy = self.stranger(us_tax_resident="yes").contact
        self.client.post(f"/contacts/{copy.pk}/promote/")
        self.assertFalse(Contact.objects.get(pk=copy.pk).of_record)

    def test_only_a_post_from_staff(self):
        made = self.stranger().contact
        self.assertEqual(self.client.get(f"/contacts/{made.pk}/promote/").status_code, 405)
        self.client.logout()
        self.client.post(f"/contacts/{made.pk}/promote/")
        self.assertFalse(Contact.objects.get(pk=made.pk).of_record)

class ClientOfRecordMigrationTests(UnsealedVaultMixin, TransactionTestCase):
    """Requirement 5: contacts that link checkouts made on production before SEC-1 are classified by plaintext
    columns alone, so a sealed deploy does not stop at migrate."""

    TARGET = [("contacts", "0002_contact_copy_for_org"), ("links", "0002_one_open_issuerless_link_problem")]

    def setUp(self):
        super().setUp()
        self.addCleanup(self.migrate_to_latest)

    def migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_unpaid_checkout_contacts_stop_being_clients_of_record(self):
        from apps.vault.fields import blind_index
        from apps.vault.vault import vault

        executor = MigrationExecutor(connection)
        executor.migrate(self.TARGET)
        old = MigrationExecutor(connection)._create_project_state(with_applied_migrations=True).apps
        Org, Contact = old.get_model("orgs", "Organization"), old.get_model("contacts", "Contact")
        Email, Invoice = old.get_model("contacts", "ContactEmail"), old.get_model("invoices", "Invoice")
        Link, Order = old.get_model("links", "PaymentLink"), old.get_model("links", "LinkOrder")
        Audit, User = old.get_model("audit", "AuditEntry"), old.get_model("auth", "User")
        org, us = Org.objects.create(slug="lt"), Org.objects.create(slug="us", is_us_resident_issuer=True)
        link = Link.objects.create(org=org, token_hash="c" * 64, currency="EUR")
        api_user = User.objects.create(username="api:t", is_active=False)
        n = iter(range(1, 100))

        def contact(address, *, of=org, copy_for=None, made_by="checkout"):
            c = Contact.objects.create(org=of, copy_for_org=copy_for)
            Email.objects.create(contact=c, org=of, copy_for_org=copy_for, address=address,
                                 address_index=blind_index(address), primary=True)
            if made_by == "checkout":
                Audit.objects.create(action="contact.create", model_label="contacts.contact", object_pk=str(c.pk))
            elif made_by == "api":
                Audit.objects.create(actor=api_user, actor_username="api:t", action="contact.create",
                                     model_label="contacts.contact", object_pk=str(c.pk))
            return c

        def invoice(c, *, status="sent", by_link=True, issuer=org):
            i = next(n)
            inv = Invoice.objects.create(org=issuer, requested_by_org=org, contact=c, currency="EUR",
                                         issue_date=datetime.date(2026, 11, 6), status=status, number=f"2026-{i:04}",
                                         view_token_hash=f"{i:064x}", pay_token_hash=f"{i + 500:064x}",
                                         payment_link=link if by_link else None)
            if by_link:
                Order.objects.create(invoice=inv, language="en", us_answer="no",
                                     reserved_until=timezone.now())
            return inv

        unpaid = contact("unpaid@example.com")
        unpaid_inv = invoice(unpaid)
        paid = contact("paid@example.com")
        invoice(paid, status="paid")
        used = contact("used@example.com")
        invoice(used)
        invoice(used, by_link=False)  # later invoiced through the API: kept, for a person to check
        by_api = contact("api@example.com", made_by="api")
        invoice(by_api)
        unaudited = contact("old@example.com", made_by=None)
        invoice(unaudited)
        us_original = contact("us@example.com")
        us_copy = contact("us@example.com", of=us, copy_for=org)
        us_inv = invoice(us_copy, issuer=us)
        no_invoice = contact("none@example.com")  # F3: holds no invoice, so no checkout made it

        vault.seal()  # every column the migration reads or writes is plaintext
        with self.assertLogs("apps.links.migrations", level="INFO") as logs:
            self.migrate_to_latest()
        from apps.contacts.models import Contact as Now, ContactEmail as NowEmail
        from apps.links.models import LinkOrder as NowOrder

        flags = dict(Now.objects.values_list("pk", "of_record"))
        self.assertEqual({k for k, v in flags.items() if not v}, {unpaid.pk, us_original.pk, us_copy.pk})
        self.assertEqual(set(NowEmail.objects.filter(of_record=False).values_list("contact_id", flat=True)),
                         {unpaid.pk, us_original.pk, us_copy.pk})
        buyers = dict(NowOrder.objects.values_list("invoice_id", "buyer_contact_id"))
        self.assertEqual(buyers[unpaid_inv.pk], unpaid.pk)
        self.assertEqual(buyers[us_inv.pk], us_original.pk)  # the original, though the invoice names the copy
        self.assertTrue(all(buyers.values()))
        text = "\n".join(logs.output)
        self.assertIn("3 contact(s)", text)
        self.assertIn(f"[{used.pk}]", text)  # made by a checkout and invoiced since: a person checks it
        self.assertIn(f"[{paid.pk}]", text)  # paid: a stranger may have seeded it before the real buyer paid
        self.assertTrue(flags[no_invoice.pk])
        self.assertNotIn("used@example.com", text)

    def test_the_schema_change_reverses_with_link_buyers_sharing_an_address(self):
        """F4: undoing contacts.0003 must not fail once a link buyer shares an address with a client. It leaves the
        column and the conditional constraints (a client of record is still unique), and migrating forward again
        works."""
        from apps.contacts.models import Contact as Now, ContactEmail as NowEmail
        from apps.contacts.services import create_unrecorded_contact, find_or_create_contact as make
        from apps.orgs.models import Organization

        org = Organization.objects.create(slug="rv")
        client, _ = make(org, "shared@example.com", legal_name="Client")
        buyer = create_unrecorded_contact(org, "shared@example.com", legal_name="Buyer")
        executor = MigrationExecutor(connection)
        executor.migrate([("contacts", "0002_contact_copy_for_org")])
        self.assertEqual(NowEmail.objects.filter(address_index=NowEmail.objects.get(contact_id=client.pk).address_index)
                         .count(), 2)
        self.migrate_to_latest()
        self.assertEqual(dict(Now.objects.filter(pk__in=[client.pk, buyer.pk]).values_list("pk", "of_record")),
                         {client.pk: True, buyer.pk: False})
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            make(org, "other@example.com")[0].emails.update(address_index=NowEmail.objects.get(contact_id=client.pk)
                                                            .address_index)
