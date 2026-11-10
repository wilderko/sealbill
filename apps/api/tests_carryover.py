"""Plan C: what an admin token sees among payments with no invoice is decided in SQL."""
from apps.api.tests import ApiBase
from apps.api.views import visible_payments
from apps.payments.models import Payment
from apps.payments.services import payment_org_ids
from apps.rails.models import RailAccount

OWN, SHARED, THEIRS, STRANGER = ("0x" + c * 40 for c in "abcd")


class AdminVisibilityTests(ApiBase):
    def setUp(self):
        super().setUp()
        mine = dict(kind="evm", networks=["arbitrum", "base"], assets=["USDC"])
        self.own = RailAccount.objects.create(org=self.org, label="own", address=OWN, **mine)
        self.shared_mine = RailAccount.objects.create(org=self.org, label="shared", address=SHARED, **mine)
        self.shared_theirs = RailAccount.objects.create(org=self.other, label="shared", address=SHARED, **mine)
        self.theirs = RailAccount.objects.create(org=self.other, label="theirs", address=THEIRS, **mine)
        self.inactive = RailAccount.objects.create(org=self.org, label="off", address=STRANGER, active=False, **mine)
        self.payments = {
            "own": self.pay(1, self.own, OWN),
            "shared_via_other_account": self.pay(2, self.shared_theirs, SHARED),
            "other": self.pay(3, self.theirs, THEIRS),
            "no_account_shared_address": self.pay(4, None, SHARED),
            "no_account_other_address": self.pay(5, None, THEIRS),
            "no_org_at_all": self.pay(6, None, "0x" + "e" * 40),
            "no_account_wrong_network": self.pay(7, None, OWN, network="polygon"),
            "inactive_account_address": self.pay(8, None, STRANGER),
            "no_address": self.pay(9, None, ""),
            "uppercase_address": self.pay(10, None, "0x" + "A" * 40),
        }
        self.pay(11, self.own, OWN, invoice=self.invoice(send=True), state="confirmed")

    def pay(self, n, account, to, network="arbitrum", **kw):
        fields = dict(rail="evm", network=network, asset="USDC", amount_minor=0, received_amount="1",
                      tx_hash="0x" + f"{n:02x}" * 32, state="needs_review", review_reason="no_matching_invoice",
                      amount_base_units="1000000", to_address=to, rail_account=account, final=True)
        fields.update(kw)
        return Payment.objects.create(**fields)

    def expected(self, token):
        return {p.pk for p in Payment.objects.filter(invoice__isnull=True)
                if (ids := payment_org_ids(p)) == set() or token.org_id in ids}

    def test_admin_visibility_matches_payment_org_ids(self):
        for org in (self.org, self.other, self.us):
            _, token, _ = self.client_for(org, scopes=("read", "admin"))
            got = set(visible_payments(token).filter(invoice__isnull=True).values_list("pk", flat=True))
            self.assertEqual(got, self.expected(token), org.slug)
        _, token, _ = self.client_for(self.org, scopes=("read", "admin"))
        got = set(visible_payments(token).values_list("pk", flat=True))
        by_name = {name: p.pk in got for name, p in self.payments.items()}
        self.assertEqual(by_name, {
            "own": True, "shared_via_other_account": True, "other": False,
            "no_account_shared_address": True, "no_account_other_address": False, "no_org_at_all": True,
            "no_account_wrong_network": True, "inactive_account_address": True, "no_address": True,
            "uppercase_address": True,
        })

    def test_non_admin_token_sees_no_unattributed_payment(self):
        _, token, _ = self.client_for(self.org, scopes=("read",))
        self.assertFalse(visible_payments(token).filter(invoice__isnull=True).exists())

    def test_admin_visibility_is_one_query(self):
        _, token, _ = self.client_for(self.org, scopes=("read", "admin"))
        with self.assertNumQueries(1):
            list(visible_payments(token).values_list("pk", flat=True))
