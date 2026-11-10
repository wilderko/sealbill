from apps.api.tests import ApiBase, LINE
from apps.invoices.models import Invoice
from apps.orgs.models import BankAccount
from apps.rails.models import RailAccount

EVM = dict(kind="evm", label="EVM", address="0x" + "ab" * 20, networks=["arbitrum"], assets=["USDC"])


class MethodKindsTests(ApiBase):
    def setUp(self):
        super().setUp()
        self.client, _, _ = self.client_for()
        self.bank = BankAccount.objects.create(org=self.org, currency="EUR", active=True)
        self.rail = RailAccount.objects.create(org=self.org, **EVM)
        self.off = RailAccount.objects.create(org=self.org, active=False, **EVM)
        self.us_rail = RailAccount.objects.create(org=self.us, **EVM)

    def post(self, us=False, **extra):
        contact = {"email": "x@example.com", **({"us_tax_resident": "yes"} if us else {})}
        return self.client.post("/api/v1/invoices", {
            "contact": contact, "currency": "EUR", "lines": [dict(LINE)], "external_reference": "ref-1", **extra,
        }, format="json")

    def test_rail_gives_active_rails_and_no_bank(self):
        r = self.post(method_kinds=["rail"])
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["offered_methods"], [f"rail:{self.rail.pk}"])

    def test_us_routed_resolves_against_the_us_issuer(self):
        r = self.post(us=True, method_kinds=["rail"])
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["offered_methods"], [f"rail:{self.us_rail.pk}"])
        self.assertEqual(Invoice.objects.get().org, self.us)

    def test_kind_resolving_to_nothing_is_fine_when_another_resolves(self):
        r = self.post(method_kinds=["bank", "fiat_link", "rail"])
        banks = BankAccount.objects.filter(org=self.org, active=True, currency="EUR").order_by("pk")
        self.assertEqual(r.json()["offered_methods"],
                         [f"bank:{b.pk}" for b in banks] + [f"rail:{self.rail.pk}"])

    def test_both_is_400(self):
        r = self.post(method_kinds=["rail"], offered_methods=[f"rail:{self.rail.pk}"])
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.json()["error"], "methods_conflict")

    def test_unknown_or_empty_kind_is_400(self):
        for kinds in (["card"], [], ["rail", "x"]):
            with self.subTest(kinds=kinds):
                self.assertEqual(self.post(method_kinds=kinds).status_code, 400)

    def test_nothing_resolved_is_409(self):
        r = self.post(method_kinds=["fiat_link"])
        self.assertEqual(r.status_code, 409, r.content)
        self.assertEqual(r.json()["error"], "no_methods")
        self.assertEqual(Invoice.objects.count(), 0)

    def test_replay_with_other_kinds_is_409(self):
        self.assertEqual(self.post(method_kinds=["rail"]).status_code, 201)
        self.assertEqual(self.post(method_kinds=["bank"]).status_code, 409)
        self.assertEqual(self.post(method_kinds=["rail"]).status_code, 200)

    def test_bank_on_a_usd_invoice_with_only_a_eur_account_is_409(self):
        r = self.post(currency="USD", method_kinds=["bank"])
        self.assertEqual(r.status_code, 409, r.content)
        self.assertEqual(r.json()["error"], "no_methods")

    def test_inline_us_resident_on_a_stored_contact_routes_to_the_us_issuer(self):
        from apps.contacts.services import find_or_create_contact

        find_or_create_contact(self.org, "x@example.com", legal_name="X")
        r = self.post(us=True, method_kinds=["rail"])
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["offered_methods"], [f"rail:{self.us_rail.pk}"])
        self.assertEqual(Invoice.objects.get().org, self.us)

    def test_created_with_rail_it_sends_with_issuer_rail_ids_stored(self):
        r = self.post(method_kinds=["rail"])
        self.assertEqual(r.status_code, 201, r.content)
        s = self.client.post(f"/api/v1/invoices/{r.json()['id']}/send", {"email_client": False}, format="json")
        self.assertEqual(s.status_code, 200, s.content)
        self.assertEqual(Invoice.objects.get().offered_methods, [f"rail:{self.rail.pk}"])
