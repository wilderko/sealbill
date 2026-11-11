"""When a client's "I have paid" e-mails staff (the owner, 2026-11-11: only when they have to act).

Staff are told at once when nobody else can check the claim (no transaction named, a network the
system does not read) and when the named transaction's money reached another invoice. A named
transaction the system can read is given `DECLARATION_GRACE_S` to be found; staff are told only if
it is not. A claim whose money is this invoice's is never mailed: it settles, or its payment goes
to review, which has a letter of its own."""
import secrets

from apps.notify.models import OutboxMessage
from apps.payments.models import Declaration, Payment
from apps.payments.e2e_helpers import Clock
from apps.payments.services import DECLARATION_GRACE_S
from apps.payments.tests_e2e_evm import K0, MINE, PAYER, USDC_ARB, EvmBase


class DeclarationLetterTests(EvmBase):
    def setUp(self):
        super().setUp()
        self.clock = Clock(self)

    def letters(self, pk=None):
        rows = OutboxMessage.objects.filter(kind="declared_staff")
        return rows.filter(invoice_id=pk).count() if pk else rows.count()

    def test_a_declared_transfer_that_settles_on_its_own_mails_nobody(self):
        pk, pay = self.sent_invoice()
        self.quote(pay, self.method, "arbitrum", "USDC")
        tx = self.nodes["arbitrum"].send_token(USDC_ARB, MINE, K0, sender=PAYER)
        self.nodes["arbitrum"].finalize()
        self.declare(pay, "arbitrum", tx)
        self.assertEqual(Declaration.objects.get(invoice_id=pk).state, "matched")
        self.clock.advance(DECLARATION_GRACE_S + 3600)
        self.work("declarations_verify")
        self.assertEqual(self.letters(), 0)

    def test_a_declared_transfer_still_short_of_finality_mails_nobody_and_settles(self):
        pk, pay = self.sent_invoice()
        self.quote(pay, self.method, "arbitrum", "USDC")
        tx = self.nodes["arbitrum"].send_token(USDC_ARB, MINE, K0, sender=PAYER)
        self.declare(pay, "arbitrum", tx)  # found, not final yet: the declaration stays open
        self.assertEqual(Payment.objects.get(tx_hash=tx).state, "detected")
        self.clock.advance(DECLARATION_GRACE_S + 60)
        self.work("declarations_verify")
        self.assertEqual(self.letters(), 0)
        self.nodes["arbitrum"].finalize()
        self.scan()
        self.assertEqual(Declaration.objects.get(invoice_id=pk).state, "matched")
        self.assertEqual(self.letters(), 0)

    def test_a_hash_nothing_is_found_for_is_mailed_once_the_grace_runs_out(self):
        pk, pay = self.sent_invoice()
        self.declare(pay, "arbitrum", "0x" + secrets.token_hex(32))  # a typo, or another chain
        self.assertEqual(self.letters(), 0)
        self.clock.advance(DECLARATION_GRACE_S - 60)
        self.work("declarations_verify")
        self.assertEqual(self.letters(), 0)
        self.clock.advance(120)
        self.work("declarations_verify")
        self.assertEqual(self.letters(pk), 1)
        self.clock.advance(3600)
        self.work("declarations_verify")
        self.assertEqual(self.letters(pk), 1)  # once

    def test_a_declaration_nobody_but_a_person_can_check_is_mailed_at_once(self):
        pk, pay = self.sent_invoice()
        self.declare(pay, "", "", note="paid by bank transfer")
        self.assertEqual(self.letters(pk), 1)
        self.declare(pay, "", "", note="and again")  # another while the first is open: no second letter
        self.work("declarations_verify")
        self.assertEqual(self.letters(pk), 1)

    def test_money_that_settled_another_invoice_is_mailed_at_once(self):
        mine, my_pay = self.sent_invoice()
        other, other_pay = self.sent_invoice()
        self.quote(my_pay, self.method, "arbitrum", "USDC")
        theirs = self.quote(other_pay, self.method, "arbitrum", "USDC")
        tx = self.nodes["arbitrum"].send_token(USDC_ARB, MINE, int(theirs.amount_base_units), sender=PAYER)
        self.nodes["arbitrum"].finalize()
        self.declare(my_pay, "arbitrum", tx)  # names a transfer whose amount is the other invoice's
        self.assertEqual(Payment.objects.get(tx_hash=tx).invoice_id, other)
        self.assertEqual(Declaration.objects.get(invoice_id=mine).state, "open")
        self.assertEqual((self.letters(mine), self.letters(other)), (1, 0))


class OldDeclarationsAreNotMailedAgainTests(EvmBase):
    def test_the_migration_marks_every_earlier_declaration_as_told(self):
        import importlib

        from django.apps import apps as registry

        pk, pay = self.sent_invoice()
        self.declare(pay, "arbitrum", "0x" + secrets.token_hex(32))
        d = Declaration.objects.get(invoice_id=pk)
        Declaration.objects.filter(pk=d.pk).update(staff_told_at=None)  # as every row was before 0009
        migration = importlib.import_module("apps.payments.migrations.0009_declaration_staff_told_at")
        migration.told_the_old_way(registry, None)
        d.refresh_from_db()
        self.assertEqual(d.staff_told_at, d.created_at)
        self.work("declarations_verify")
        self.assertEqual(OutboxMessage.objects.filter(kind="declared_staff").count(), 0)
