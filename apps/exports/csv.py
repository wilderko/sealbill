"""The CSV of invoices and payments, with the accounting currency beside the invoice's own.

Money is a decimal string made from integers; nothing here touches a float. Every cell goes
through `safe_cell`. A value the vault cannot open is a dash, never a failed export.
"""
import csv as stdcsv
import datetime
from collections.abc import Iterator
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from django.utils import timezone

from apps.exports import rates
from apps.invoices.currencies import CURRENCIES, minor_units
from apps.invoices.migration import migration_info
from apps.invoices.money import AMOUNT_FIELDS, totals_of
from apps.payments.models import Payment
from apps.portal.crypto import decimals_for
from apps.rails.uris import decimal_amount
from apps.vault.fields import UndecryptableValue

NO_VALUE = "—"
DANGEROUS = ("=", "+", "-", "@", "\t", "\r")

INVOICE_HEADER = [
    "organization", "number", "kind", "status", "issue_date", "due_date", "contact", "contact_email", "currency",
    "subtotal", "discount", "tax", "total", "paid", "accounting_currency", "accounting_rate", "accounting_total",
    "rate_source", "external_reference", "payment_link", "migrated_from",
]
PAYMENT_HEADER = [
    "organization", "invoice_number", "payment_id", "state", "rail", "network", "asset", "amount",
    "invoice_currency", "amount_invoice_currency", "quote_rate", "accounting_currency", "accounting_rate",
    "accounting_amount", "tx_hash", "log_index", "detected_at", "confirmed_at", "confirmed_by", "review_reason",
]


def safe_cell(value) -> str:
    """Text a spreadsheet will not run as a formula: a leading quote makes it inert. Applied to
    every cell, numbers included, so a negative amount reads `'-5.00` rather than slipping through."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(DANGEROUS) else text


def _money(minor: int, currency: str) -> str:
    sign = "-" if minor < 0 else ""
    return sign + decimal_amount(abs(minor), minor_units(currency))


def _converted(minor: int, currency: str, rate: Decimal, accounting: str) -> str:
    major = Decimal(minor).scaleb(-minor_units(currency)) * rate
    exp = minor_units(accounting) if accounting in CURRENCIES else 2
    return str(major.quantize(Decimal(1).scaleb(-exp), rounding=ROUND_HALF_UP))


def _utc_day(moment) -> datetime.date:
    return timezone.localtime(moment, datetime.timezone.utc).date()


def _guarded(read) -> str:
    try:
        value = read()
    except UndecryptableValue:
        return NO_VALUE
    return value or ""


def _contact_cells(contact, invoice=None) -> tuple[str, str]:
    """(name, e-mail). A sent invoice's buyer is named as on the document: its frozen snapshot, which for
    a payment link's order is what the buyer typed (final review F1); a draft, or an invoice sent before
    snapshots, by the live contact."""
    from apps.invoices.services import primary_address

    def name():
        snapshot = invoice.snapshot if invoice is not None else None
        buyer = (snapshot or {}).get("buyer") if isinstance(snapshot, dict) else None
        if buyer is not None:
            return buyer.get("name") or ""
        return contact.display_name or contact.legal_name

    email = _guarded(lambda: primary_address(contact))
    return _guarded(name), email


def _accounting(minor: int, currency: str, accounting: str, day) -> tuple[str, str, str, str]:
    """(accounting_currency, rate, converted amount, rate_source); the last three are empty/missing
    when the cache has no such rate."""
    rate = rates.daily_rate(currency, accounting, day) if day else None
    if rate is None:
        return accounting, "", "", "missing"
    return accounting, format(rate, "f"), _converted(minor, currency, rate, accounting), rates.rate_source(
        currency, accounting, day)


def _payment_link_cell(inv) -> str:
    """The title of the payment link an order came from (staff-written, so as readable as the contact's name);
    its id when the link has no title. Empty for an ordinary invoice."""
    if inv.payment_link_id is None:
        return ""
    title = _guarded(lambda: inv.payment_link.title)
    return title if title and title != NO_VALUE else (title or f"#{inv.payment_link_id}")


def _migrated_from_cell(inv) -> str:
    try:
        info = migration_info(inv)
    except UndecryptableValue:
        return NO_VALUE
    return f"{info.get('source') or ''}:{info.get('source_status') or ''}" if info else ""


def invoices_rows(org_ids, date_from: datetime.date, date_to: datetime.date, queryset=None) -> Iterator[list[str]]:
    """Header first. `queryset` (the API's visibility) replaces the organization filter."""
    from apps.invoices.models import Invoice
    from apps.payments.services import confirmed_minor

    rows = queryset if queryset is not None else Invoice.objects.filter(org_id__in=org_ids)
    rows = (rows.exclude(status="draft").filter(issue_date__gte=date_from, issue_date__lte=date_to)
            .prefetch_related(None).select_related("org", "contact", "payment_link").order_by("issue_date", "pk"))
    yield INVOICE_HEADER
    for inv in rows.iterator(chunk_size=200):
        t = totals_of(inv.lines.only("pk", "invoice_id", *AMOUNT_FIELDS))
        name, email = _contact_cells(inv.contact, inv)
        acc, rate, acc_total, source = _accounting(
            t.total, inv.currency, rates.accounting_currency(inv.org), inv.issue_date)
        yield [
            inv.org.slug, inv.number or "", inv.kind, inv.status, inv.issue_date.isoformat(),
            inv.due_date.isoformat() if inv.due_date else "", name, email, inv.currency,
            _money(t.subtotal, inv.currency), _money(t.discount, inv.currency), _money(t.tax, inv.currency),
            _money(t.total, inv.currency), _money(confirmed_minor(inv), inv.currency),
            acc, rate, acc_total, source, inv.external_reference or "", _payment_link_cell(inv), _migrated_from_cell(inv),
        ]


def _native_amount(p: Payment) -> str:
    """The amount in the asset it arrived in, with that asset's decimals."""
    if p.rail in ("bank", "fiat_link", "manual") and p.invoice_id:
        return _money(p.amount_minor, p.invoice.currency)
    decimals = decimals_for(p.network, p.asset) if p.asset else None
    if p.amount_base_units and decimals is not None:
        return decimal_amount(int(p.amount_base_units), decimals)
    if decimals is not None:
        # A gateway (BTCPay, Lightning) reports whole units, stored at the column's 18 decimals:
        # written like a chain payment, at the asset's own (BTC 8, XMR 12, ZEC 8). Lightning's
        # millisatoshis below a satoshi are not counted or refunded, so they are not shown either.
        units = p.received_amount.scaleb(decimals).to_integral_value(rounding=ROUND_DOWN)
        if units >= 0:
            return decimal_amount(int(units), decimals)
    return format(p.received_amount, "f")


def payments_rows(org_ids, date_from: datetime.date, date_to: datetime.date, queryset=None) -> Iterator[list[str]]:
    """Payments detected in the range (UTC days). `queryset` is the API's visibility."""
    rows = queryset if queryset is not None else Payment.objects.filter(invoice__org_id__in=org_ids)
    start = datetime.datetime.combine(date_from, datetime.time(), tzinfo=datetime.timezone.utc)
    end = datetime.datetime.combine(date_to + datetime.timedelta(days=1), datetime.time(), tzinfo=datetime.timezone.utc)
    rows = (rows.filter(detected_at__gte=start, detected_at__lt=end)
            .select_related("invoice", "invoice__org", "quote", "confirmed_by", "rail_account__org")
            .order_by("detected_at", "pk"))
    yield PAYMENT_HEADER
    for p in rows.iterator(chunk_size=200):
        inv = p.invoice
        org = inv.org if inv else (p.rail_account.org if p.rail_account else None)
        if inv is not None and p.confirmed_at is not None and p.state == "confirmed":
            acc, rate, acc_amount, _source = _accounting(
                p.amount_minor, inv.currency, rates.accounting_currency(inv.org), _utc_day(p.confirmed_at))
            acc_amount_cells = (acc, rate, acc_amount)
        else:
            acc_amount_cells = (rates.accounting_currency(org) if org else rates.DEFAULT_ACCOUNTING_CURRENCY, "", "")
        yield [
            org.slug if org else "", (inv.number or "") if inv else "", str(p.pk), p.state, p.rail, p.network, p.asset,
            _native_amount(p), inv.currency if inv else "",
            _money(p.amount_minor, inv.currency) if inv else "",
            p.quote.rate if p.quote else "", *acc_amount_cells,
            p.tx_hash, str(p.log_index) if p.tx_hash else "",
            p.detected_at.isoformat(), p.confirmed_at.isoformat() if p.confirmed_at else "",
            p.confirmed_by.get_username() if p.confirmed_by else "", p.review_reason,
        ]


class _Echo:
    def write(self, value):
        return value


def stream(rows) -> Iterator[str]:
    """The rows as CSV text, one line at a time, every cell neutralised."""
    writer = stdcsv.writer(_Echo(), lineterminator="\r\n")
    for row in rows:
        yield writer.writerow([safe_cell(c) for c in row])


def parse_range(raw_from, raw_to, max_days=400):
    """(from, to) or None when missing, malformed, backwards or longer than `max_days`."""
    try:
        a = datetime.date.fromisoformat(raw_from or "")
        b = datetime.date.fromisoformat(raw_to or "")
    except ValueError:
        return None
    if b < a or (b - a).days > max_days:
        return None
    return a, b
