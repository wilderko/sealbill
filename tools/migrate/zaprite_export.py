#!/usr/bin/env python3
"""Export every Zaprite contact and order as the NDJSON that `manage.py import_invoices` reads.

    ZAPRITE_API_KEY=... python3 tools/migrate/zaprite_export.py zaprite.ndjson
    ZAPRITE_API_KEY=... python3 tools/migrate/zaprite_export.py > zaprite.ndjson

Standalone: Python 3.11 or newer and `requests`, nothing from Sealbill. It only reads from Zaprite
(`GET /v1/contacts`, `GET /v1/orders`, 100 records a page). The records go to the file named on the
command line, created with mode 0600, or to stdout. Only counts and order ids go to stderr, never a
name, an e-mail address or an amount, so the terminal and its scrollback hold no client data.

Environment:
    ZAPRITE_API_KEY  required; an API key of the Zaprite organization (Settings > API in Zaprite)
    ZAPRITE_API_URL  optional; default https://api.zaprite.com

What goes into the file (the record shapes are documented in apps/invoices/importer.py and
docs/migration.md):

- one `contact` record per Zaprite contact, then one for every buyer an order names that the contact
  list did not hold, then the `invoice` records, so every contact comes before its invoices;
- an order with an invoice becomes an `invoice` record with the invoice's number, lines, discounts,
  tax rates, title, footer and dates. The total is the order's own `totalAmount`, never the sum of the
  lines, so the importer can refuse an invoice whose lines do not add up;
- any other order (payment link, API order, point of sale) becomes a `payment_request` with one line
  at the order's total, because Zaprite's order items carry no price;
- an order with no contact takes its buyer from the order itself (`invoice.customer`, then
  `customerData`); an order with neither an invoice nor any buyer data (an anonymous payment-link
  payment) is filed under one placeholder contact, `anonymous-payment-link`.

Left out, each one named by id on stderr: PENDING orders (abandoned checkouts, drafts; not money
received), an invoice that names no buyer at all, an order without a total and an order without any
date. The importer imports every record it is given as paid, so read the stderr summary before
importing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Iterator

DEFAULT_API_URL = "https://api.zaprite.com"


class ZapriteClient:
    """The two attributes `iter_pages` needs: a requests session carrying the key, and the base URL."""

    def __init__(self, api_key: str, api_url: str = DEFAULT_API_URL):
        import requests

        self.api_url = api_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}", "Accept": "application/json"})


def iter_pages(client, path, params=None) -> Iterator[dict]:
    """Yield every item of a paged Zaprite list, page=1..meta.pagesCount.
    The page size is fixed by Zaprite (100); there is no limit parameter."""
    page = 1
    while True:
        resp = client.session.get(
            f"{client.api_url}{path}", params={**(params or {}), "page": page}, timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("items") or []:
            yield item
        pages = ((data.get("meta") or {}).get("pagesCount")) or 1
        if page >= pages:
            return
        page += 1


# Zaprite's billingAddress (and customerData.address) -> the importer's contact address shape
# {line1, line2, city, zip, state, country}. Country is an ISO alpha-2 code in upper case.
_ADDRESS_KEYS = {"line1": "line1", "line2": "line2", "city": "city", "zipCode": "zip",
                 "state": "state", "countryCode": "country"}


def address_of(raw) -> dict:
    """The importer's address object. A key Zaprite documents nowhere is refused, never dropped: a
    field dropped silently takes every postcode or country with it."""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("address is not an object")
    out = {}
    for key, value in raw.items():
        if key not in _ADDRESS_KEYS:
            raise ValueError(f"address key {key!r} is not documented")
        text = str(value).strip() if value is not None else ""
        if text:
            out[_ADDRESS_KEYS[key]] = text.upper() if key == "countryCode" else text
    return out


def contact_line(c: dict) -> dict:
    email = (c.get("email") or "").strip().lower()
    rec = {
        "type": "contact",
        "source_id": c["id"],
        "email": email or None,
        "legal_name": c.get("legalName") or "",
        "display_name": c.get("displayName") or c.get("contactName") or "",
        "address": address_of(c.get("billingAddress")),
        "tax_id": c.get("taxId") or "",
    }
    # The documented Zaprite contact has no language/locale field, so none is invented; it is sent
    # only if a record states one.
    lang = c.get("language") or c.get("locale")
    if lang:
        rec["language"] = lang
    return rec


# An order with no invoice and no buyer data at all (an anonymous payment-link payment) is imported
# under this one placeholder contact; an invoice must still name its buyer.
ANONYMOUS_CONTACT_ID = "anonymous-payment-link"


def anonymous_contact() -> dict:
    return contact_line({"id": ANONYMOUS_CONTACT_ID, "displayName": "Anonymous payer (Zaprite payment link)"})


def derived_contact_id(o: dict) -> str:
    return f"order:{o['id']}"


def derived_contact(o: dict):
    """A contact record for an order whose `contact` is null, from the buyer data the order itself
    carries: `invoice.customer`, then `customerData`. Its source id is derived from the order id."""
    customer = (o.get("invoice") or {}).get("customer")
    if customer and any(customer.get(k) for k in ("legalName", "contactName", "displayName", "email")):
        return contact_line({**customer, "id": derived_contact_id(o)})
    data = o.get("customerData") or {}
    if any(data.get(k) for k in ("name", "email", "company")):
        return contact_line({
            "id": derived_contact_id(o), "email": data.get("email"), "legalName": data.get("company") or "",
            "contactName": data.get("name"), "billingAddress": data.get("address"),
        })
    return None


class OrderLeftOut(ValueError):
    """An order that cannot be exported as a complete record; `reason` names why."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _day(value):
    return value[:10] if isinstance(value, str) and value else None


def _line(i):
    line = {
        "name": i.get("name") or "",
        "description": i.get("description") or "",
        "quantity": i.get("quantity") if i.get("quantity") is not None else 1,
        "unit_price_minor": int(i.get("unitPrice") or 0),
    }
    # lineItems[].discountRateBips / discountAmount. Never both: a rate wins, so the importer does not
    # apply a discount twice.
    if i.get("discountRateBips"):
        line["discount_rate_bps"] = int(i["discountRateBips"])
    elif i.get("discountAmount"):
        line["discount_amount_minor"] = int(i["discountAmount"])
    # The importer has no tax-amount field: the rate only (taxAmount is not sent).
    if i.get("taxRateBips"):
        line["tax_rate_bps"] = int(i["taxRateBips"])
    return line


def _confirmed_dates(o):
    return sorted(
        d for d in (_day(t.get("date")) for t in o.get("transactions") or []
                    if t.get("status") == "CONFIRMED") if d
    )


def _paid_date(o):
    # transactions[].date of CONFIRMED transactions only; order.paidAt is the fallback when Zaprite
    # lists no confirmed transaction.
    dates = _confirmed_dates(o)
    return dates[-1] if dates else _day(o.get("paidAt"))


def carries_tax_or_promo(o: dict) -> tuple:
    """(has line tax, has an order-level promo): orders worth a second look in the dry run. An order
    promo has no field in the importer, so such an invoice's lines do not add up to its total and the
    importer refuses it by id."""
    tax = any(
        (line.get("taxRateBips") or line.get("taxAmount"))
        for line in (o.get("invoice") or {}).get("lineItems") or []
    )
    return bool(tax), bool(o.get("promo"))


def order_line(o: dict) -> dict:
    """Map one order. Raises OrderLeftOut (reason pending, no_contact, no_total, no_date) for an order
    that cannot be exported as a complete record."""
    if o.get("status") == "PENDING":
        raise OrderLeftOut("pending")
    if (o.get("contact") or {}).get("id"):
        contact_id = o["contact"]["id"]
    elif derived_contact(o):
        contact_id = derived_contact_id(o)
    elif not o.get("invoice"):
        contact_id = ANONYMOUS_CONTACT_ID
    else:
        raise OrderLeftOut("no_contact")
    if o.get("totalAmount") is None:
        raise OrderLeftOut("no_total")
    invoice = o.get("invoice")
    paid = _paid_date(o)
    if invoice:
        lines = [_line(i) for i in invoice.get("lineItems") or []]
        kind, number = "invoice", (str(invoice["number"]) if invoice.get("number") else None)
        title = invoice.get("title") or ""
        footer = invoice.get("footerNote") or ""
        issue = _day(invoice.get("date")) or paid  # invoice.date, else the paid date
        due_days = invoice.get("daysToPay")
        url = invoice.get("publicUrl") or o.get("checkoutUrl") or ""
    else:
        # Every other order type (payment link, apiOrder, pos, ...) is a payment request at the order's
        # own total. orderItems carry label/quantity/paymentLinkItem{id,title} and NO price, so it is
        # one line, named from the items, the link's title, the order's label, else its id.
        total = o["totalAmount"]
        labels = [
            (i.get("label") or (i.get("paymentLinkItem") or {}).get("title") or "").strip()
            for i in o.get("orderItems") or []
        ]
        name = (" + ".join(label for label in labels if label) or (o.get("paymentLink") or {}).get("title")
                or o.get("label") or f"Zaprite order {o['id']}")
        lines = [{"name": name, "description": "", "quantity": 1, "unit_price_minor": int(total)}]
        kind, number = "payment_request", None
        title = (o.get("paymentLink") or {}).get("title") or o.get("label") or ""
        footer = ""
        # No date on such an order itself: it takes its paid date (first CONFIRMED transaction, else paidAt).
        confirmed = _confirmed_dates(o)
        issue = confirmed[0] if confirmed else _day(o.get("paidAt"))
        due_days = None
        url = o.get("checkoutUrl") or ""
    if not issue:
        raise OrderLeftOut("no_date")
    return {
        "type": "invoice",
        "source_id": o["id"],
        "number": number,
        "kind": kind,
        "contact_source_id": contact_id,
        "currency": o.get("currency") or "",
        "issue_date": issue,
        "paid_date": paid,
        "due_days": due_days,
        "title": title,
        "footer": footer,
        "lines": lines,
        "total_minor": int(o["totalAmount"]),  # the order's own total, never the line sum
        "source_status": o.get("status") or "",
        "source_url": url,
    }


LEFT_OUT_REASONS = ("no_contact", "no_total", "no_date", "pending")


def export(client, out, err) -> dict:
    """Write every record to `out` (one JSON object per line) and the summary to `err`. Returns the
    counts, for tests."""
    contacts = orders = 0
    left_out: dict[str, list] = {}
    tax, promo = [], []
    seen = set()
    order_status: dict[str, int] = {}
    invoice_status: dict[str, int] = {}
    anonymous = []

    def emit(record):
        out.write(json.dumps(record, ensure_ascii=False) + "\n")

    for c in iter_pages(client, "/v1/contacts"):
        emit(contact_line(c))
        seen.add(c["id"])
        contacts += 1
    lines, embedded = [], []
    for o in iter_pages(client, "/v1/orders"):
        status = o.get("status") or "none"
        order_status[status] = order_status.get(status, 0) + 1
        inv_status = (o.get("invoice") or {}).get("status") or "none"
        invoice_status[inv_status] = invoice_status.get(inv_status, 0) + 1
        try:
            line = order_line(o)
        except OrderLeftOut as e:
            left_out.setdefault(e.reason, []).append(str(o.get("id")))
            continue
        c = o.get("contact") or {}
        if line["contact_source_id"] == ANONYMOUS_CONTACT_ID:
            if not anonymous:
                embedded.append(anonymous_contact())
            anonymous.append(o["id"])
        elif c.get("id"):
            if c["id"] not in seen:
                seen.add(c["id"])
                embedded.append(contact_line(c))
        else:
            embedded.append(derived_contact(o))
        t, p = carries_tax_or_promo(o)
        if t:
            tax.append(o["id"])
        if p:
            promo.append(o["id"])
        lines.append(line)
    for c in embedded:
        emit(c)
        contacts += 1
    for line in lines:
        emit(line)
        orders += 1

    def say(text):
        err.write(text + "\n")

    say(f"contacts={contacts} (embedded_only={len(embedded)}) orders={orders}")
    say(f"anonymous_payment_links={len(anonymous)}")
    for reason in LEFT_OUT_REASONS:
        ids = left_out.get(reason, [])
        say(f"{reason}={len(ids)}" + (": " + ", ".join(ids) if ids else ""))
    for name, ids in (("tax_orders", tax), ("promo_orders", promo)):
        say(f"{name}={len(ids)}" + (": " + ", ".join(ids) if ids else ""))
    for label, counts in (("order_status", order_status), ("invoice_status", invoice_status)):
        for status, n in sorted(counts.items()):
            say(f"{label} {status}={n}")
    return {"contacts": contacts, "orders": orders, "left_out": left_out}


def main(argv=None, *, client=None, stdout=None, stderr=None) -> int:
    parser = argparse.ArgumentParser(
        description="Export Zaprite contacts and orders as NDJSON for Sealbill's import_invoices.")
    parser.add_argument("output", nargs="?", help="file to write (created with mode 0600); default stdout")
    args = parser.parse_args(argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    if client is None:
        key = os.environ.get("ZAPRITE_API_KEY", "").strip()
        if not key:
            stderr.write("ZAPRITE_API_KEY is not set. Nothing was read.\n")
            return 2
        client = ZapriteClient(key, os.environ.get("ZAPRITE_API_URL", "").strip() or DEFAULT_API_URL)
    try:
        if args.output:
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.chmod(args.output, 0o600)  # an existing file keeps its mode through O_CREAT
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    export(client, out, stderr)
            except BaseException:
                os.unlink(args.output)  # half an export imported as paid history is worse than none
                raise
        else:
            export(client, stdout, stderr)
    except Exception as exc:  # requests' errors name the URL, never the key
        stderr.write(f"export failed: {type(exc).__name__}: {exc}\n")
        stderr.write("Nothing usable was written." if args.output else
                     "The output above is incomplete: do not import it.")
        stderr.write("\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
