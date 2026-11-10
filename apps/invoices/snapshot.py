"""What a sent invoice says about its seller, its buyer and where to pay, frozen when it is sent.

A sent invoice is never edited, only voided and replaced, so the document a client holds never
changes under them (audit API-4). The invoice's own columns and lines are guarded by the model.
The parties and the payment details live in other rows that keep changing: the organization,
the contact, its bank accounts and payment links. So `send_invoice` copies them into
`Invoice.snapshot` (encrypted, like the rows they come from), and the PDF, the receipt and the
client's pages of anything that is no longer a draft read them from there. A draft renders from
the live rows.

What is not frozen: the logo (a file; the seller's name and address are), the language of the
rendering, and the crypto rails, which are quoted live: a quote fixes its own address and amount.
"""
import logging

from apps.orgs.models import BankAccount, FiatLink

logger = logging.getLogger(__name__)

VERSION = 1


def address_lines(address) -> list[str]:
    if not isinstance(address, dict):
        return [str(address)] if address else []
    city = " ".join(p for p in (address.get("zip"), address.get("city")) if p)
    parts = [address.get("line1"), address.get("line2"), city, address.get("state"), address.get("country")]
    return [p for p in parts if p]


def _instructions(raw) -> dict:
    return dict(raw) if isinstance(raw, dict) else {}


def method_details(invoice) -> list[dict]:
    """The bank accounts and payment links the invoice offers, as printed. The instructions keep
    every language, so the document can still be rendered in any of them."""
    out = []
    for key in invoice.offered_methods or []:
        kind, _, raw = str(key).partition(":")
        if not raw.isdigit():
            continue
        if kind == "bank":
            acct = BankAccount.objects.filter(pk=int(raw), org_id=invoice.org_id).first()
            if acct is None:
                continue
            out.append({
                "kind": "bank", "label": acct.label or acct.bank_name,
                "beneficiary_name": acct.beneficiary_name, "beneficiary_address": acct.beneficiary_address,
                "account_number": acct.account_number,
                "bank_name": acct.bank_name, "bank_swift": acct.bank_swift, "bank_address": acct.bank_address,
                "intermediary_bank_name": acct.intermediary_bank_name,
                "intermediary_swift": acct.intermediary_swift,
                "intermediary_account": acct.intermediary_account,
                "instructions": _instructions(acct.instructions),
            })
        elif kind == "fiat":
            link = FiatLink.objects.filter(pk=int(raw), org_id=invoice.org_id).first()
            if link is None:
                continue
            out.append({"kind": "fiat", "label": link.label or link.kind, "url": link.url,
                        "instructions": _instructions(link.instructions)})
    return out


def build(invoice, buyer: dict | None = None) -> dict:
    """The seller, buyer and payment blocks of `invoice`, read from the live rows now.

    `buyer` (a payment link's order): the buyer block as the buyer typed it, `name` and `address`
    (the parts of a postal address), instead of the contact's stored fields, which a public form never
    shows (spec 2.3)."""
    org = invoice.org
    if buyer is None:
        contact = invoice.contact
        buyer_block = {"name": contact.legal_name or contact.display_name,
                       "address_lines": address_lines(contact.address), "tax_ids": contact.tax_ids}
    else:
        buyer_block = {"name": buyer.get("name") or "", "address_lines": address_lines(buyer.get("address")),
                       "tax_ids": None}
    return {
        "version": VERSION,
        "seller": {"legal_name": org.legal_name or org.slug, "address": org.address, "tax_ids": org.tax_ids,
                   "footer_note": org.footer_note},
        "buyer": buyer_block,
        "methods": method_details(invoice),
    }


def for_render(invoice) -> dict:
    """What to render the parties and payment details from: the live rows for a draft, the
    frozen snapshot for anything else. Reads the vault: VaultSealed propagates."""
    if invoice.status == "draft":
        return build(invoice)
    stored = invoice.snapshot
    return stored if stored is not None else _freeze(invoice)


def _freeze(invoice) -> dict:
    """A sent invoice with no snapshot (sent before there was one, while the migration that
    backfills them could not open the vault): frozen from its current rows now, once."""
    from apps.invoices.models import Invoice

    snap = build(invoice)
    if Invoice.objects.filter(pk=invoice.pk, snapshot__isnull=True).update(snapshot=snap):
        logger.info("Invoice #%s had no snapshot; frozen from its current rows", invoice.pk)
    else:  # another request froze it first
        snap = Invoice.objects.filter(pk=invoice.pk).values_list("snapshot", flat=True).get()
    invoice.snapshot = snap
    return snap


def methods_in(snap: dict, language: str) -> list[dict]:
    """The payment blocks for rendering in `language`: instructions in it, else in English."""
    out = []
    for method in snap.get("methods") or []:
        instr = method.get("instructions") or {}
        out.append({**method, "instructions": instr.get(language) or instr.get("en")})
    return out
