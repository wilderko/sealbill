"""Test helpers for organizations."""
from apps.orgs.models import BankAccount


def add_bank(org, currency="EUR", **fields) -> BankAccount:
    """An active bank account, so invoices in `currency` have a payment method to offer."""
    defaults = {"label": f"{currency} account", "beneficiary_name": "Seller", "account_number": "SK0000000000009"}
    return BankAccount.objects.create(org=org, currency=currency, **{**defaults, **fields})
