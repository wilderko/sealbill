"""Carry the contacts and invoices of Zaprite and Request Finance over as paid history.

Reads the neutral NDJSON of the design (§5.1): `contact` and `invoice` records. Every invoice comes
in its final state (paid, numbered, snapshot frozen) in one transaction of its own, written directly
on the models: nothing here sends a letter, fires a webhook, makes a quote or a token, or moves
`next_number`. An invoice whose `external_reference` already exists is skipped, so a run that stopped
half-way is simply run again.

Nothing here puts personal data into a message: refusal reasons name the problem, never the value.
"""
import datetime
import fcntl
import json
import os
import re
import string
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import DataError, IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from apps.audit import services as audit
from apps.contacts.models import Contact, ContactEmail
from apps.contacts.services import find_contact, update_contact
from apps.invoices import snapshot
from apps.invoices.currencies import minor_units
from apps.invoices.errors import InvoiceError
from apps.invoices.migration import staff_note
from apps.invoices.models import Invoice, LineItem
from apps.invoices.money import totals_of
from apps.payments.models import Payment
from apps.payments.services import _decimal_major

SOURCES = ("zaprite", "rf")
KINDS = ("invoice", "payment_request")
# Request Finance invoices nobody owed: never sent, or withdrawn.
SKIPPED_RF_STATUSES = {"draft", "canceled", "cancelled", "rejected"}
_SUFFIX = {"zaprite": "Z", "rf": "RF"}
# The contact `address` is exactly the Contact's own shape (spec 5.1). Any other key is refused by name, so a
# field a source calls something else can never be dropped without anyone seeing it.
_ADDRESS_KEYS = ("line1", "line2", "city", "zip", "state", "country")
_CONTACT_FIELDS = ("legal_name", "display_name", "address", "tax_ids", "preferred_language")


BIGINT = 2**63 - 1
MAX_QUANTITY = Decimal(10) ** 10  # LineItem.quantity is max_digits=12, decimal_places=2
MAX_ID = 100
MAX_DUE_DAYS = 3650
FIRST_DATE, LAST_DATE = datetime.date(1970, 1, 1), datetime.date(2100, 12, 31)


class ImportBusy(Exception):
    """Another import holds the lock."""


class UsIssuerRefused(Exception):
    """A Zaprite import into the US-resident issuer's organization, without `allow_us_issuer`."""


class Refused(Exception):
    """A record that cannot be imported; the message says why and never quotes a value."""


@dataclass
class Report:
    contacts_new: int = 0
    contacts_merged: int = 0
    contacts_without_email: int = 0
    contacts_skipped_existing: int = 0
    invoices_by_kind_currency: dict = field(default_factory=dict)
    totals_by_currency: dict = field(default_factory=dict)
    collisions: list = field(default_factory=list)
    skipped_existing: int = 0
    refused: list = field(default_factory=list)
    source_status_counts: dict = field(default_factory=dict)
    skipped_by_status: dict = field(default_factory=dict)
    skipped_refs: list = field(default_factory=list)  # (ref, why) for every record skipped, not refused


# ---- reading -------------------------------------------------------------------------------------

def _text(rec, key, *, required=False) -> str | None:
    value = rec.get(key)
    if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int))):
        raise Refused(f"{key} is not text")
    text = "" if value is None else str(value).strip()
    if not text and required:
        raise Refused(f"missing {key}")
    return text or None


def _date(rec, key, *, required=False):
    value = rec.get(key)
    if value in (None, ""):
        if required:
            raise Refused(f"missing {key}")
        return None
    try:
        day = datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        raise Refused(f"{key} is not a date")
    if not FIRST_DATE <= day <= LAST_DATE:
        raise Refused(f"{key} is out of range")
    return day


def _int(rec, key, *, required=False, minimum=0, maximum=BIGINT):
    value = rec.get(key)
    if value is None:
        if required:
            raise Refused(f"missing {key}")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise Refused(f"{key} is not a whole number between {minimum} and {maximum}")
    return value


def _address(raw) -> dict | None:
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise Refused("address is not an object")
    out = {}
    for key, value in raw.items():
        if key not in _ADDRESS_KEYS:
            raise Refused(f"address has an unknown key {str(key)[:40]!r}")
        if value is None or isinstance(value, bool) or not isinstance(value, (str, int)):
            if value is None:
                continue
            raise Refused(f"address {key} is not text")
        text = str(value).strip()
        if not text:
            continue
        if key == "country":
            if not (len(text) == 2 and text.isascii() and text.isalpha()):
                raise Refused("address country is not an ISO alpha-2 code")
            text = text.upper()
        out[key] = text
    return out or None


def _parse_contact(rec) -> dict:
    sid = _text(rec, "source_id", required=True)
    if len(sid) > MAX_ID:
        raise Refused("source_id is too long")
    email = rec.get("email")
    if email is not None and not isinstance(email, str):
        raise Refused("email is not text")
    email = (email or "").strip()
    language = (_text(rec, "language") or "").lower()
    return {
        "source_id": sid,
        "email": email if "@" in email else "",
        "legal_name": _text(rec, "legal_name"), "display_name": _text(rec, "display_name"),
        "address": _address(rec.get("address")), "tax_ids": _text(rec, "tax_id"),
        "preferred_language": language if language in {c for c, _ in settings.LANGUAGES} else None,  # stated only
    }


def _parse_invoice(rec) -> dict:
    sid = _text(rec, "source_id", required=True)
    if len(sid) > MAX_ID:
        raise Refused("source_id is too long")
    kind = rec.get("kind")
    if kind not in KINDS:
        raise Refused("unknown kind")
    currency = (_text(rec, "currency", required=True) or "").upper()
    try:
        minor_units(currency)
    except ValueError:
        raise Refused("unknown currency")
    total = _int(rec, "total_minor", required=True)
    raw_lines = rec.get("lines")
    if not isinstance(raw_lines, list) or (not raw_lines and total):
        raise Refused("missing lines")
    lines = []
    for i, raw in enumerate(raw_lines):
        if not isinstance(raw, dict):
            raise Refused(f"line {i + 1} is not an object")
        try:
            quantity = Decimal(str(raw.get("quantity")))
        except InvalidOperation:
            raise Refused(f"line {i + 1} has no valid quantity")
        if not quantity.is_finite() or quantity < 0 or quantity >= MAX_QUANTITY \
                or quantity != quantity.quantize(Decimal("0.01")):
            raise Refused(f"line {i + 1} has an unusable quantity")
        price = raw.get("unit_price_minor")
        if isinstance(price, bool) or not isinstance(price, int) or abs(price) > BIGINT:
            raise Refused(f"line {i + 1} has no valid unit_price_minor")
        name = raw.get("name")
        if not isinstance(name, str) or len(name) > 500:
            raise Refused(f"line {i + 1} has no valid name")
        description = raw.get("description")
        if description is not None and not isinstance(description, str):
            raise Refused(f"line {i + 1} has a description that is not text")
        extra = {}
        for key, ceiling in (("discount_amount_minor", BIGINT), ("discount_rate_bps", 10000), ("tax_rate_bps", 10000)):
            value = raw.get(key, 0)
            if value is None:
                value = 0
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or (ceiling and value > ceiling):
                raise Refused(f"line {i + 1} has an invalid {key}")
            extra[key] = value
        lines.append({"name": name, "description": description or None, "quantity": quantity,
                      "unit_price_minor": price, **extra})
    number = _text(rec, "number")
    if number and len(number) > 50:
        raise Refused("number is too long")
    return {
        "source_id": sid, "number": number, "kind": kind, "contact_source_id": _text(rec, "contact_source_id", required=True),
        "currency": currency, "issue_date": _date(rec, "issue_date", required=True), "paid_date": _date(rec, "paid_date"),
        "due_days": _int(rec, "due_days", maximum=MAX_DUE_DAYS), "title": _text(rec, "title"), "footer": _text(rec, "footer"),
        "lines": lines, "total_minor": total, "source_status": _text(rec, "source_status", required=True),
        "source_url": _text(rec, "source_url") or "",
    }


def _read(lines: Iterable[str], report: Report, source: str):
    """(contacts, invoices): each a list of (line_no, parsed record), the bad lines refused. A refusal is
    `(line_no, "<source>:<source_id>" or None, reason)`: the id is not personal data and is what lets a
    refused record be traced back after a pipeline."""
    contacts, invoices = {}, []
    for no, raw in enumerate(lines, start=1):
        raw = raw.strip()
        if not raw:
            continue
        ref = None
        try:
            try:
                rec = json.loads(raw)
            except ValueError:
                raise Refused("not valid JSON")
            if not isinstance(rec, dict):
                raise Refused("not a JSON object")
            kind = rec.get("type")
            sid = rec.get("source_id")
            if isinstance(sid, (str, int)) and not isinstance(sid, bool) and str(sid).strip():
                ref = f"{source}:{str(sid).strip()[:MAX_ID]}"
            if kind == "contact":
                parsed = _parse_contact(rec)
                if parsed["source_id"] in contacts:
                    raise Refused("contact source_id is repeated; the first record stands")
                contacts[parsed["source_id"]] = (no, parsed)
            elif kind == "invoice":
                invoices.append((no, _parse_invoice(rec)))
            else:
                raise Refused("unknown record type")
        except Refused as exc:
            report.refused.append((no, ref, str(exc)))
    return contacts, invoices


# ---- contacts ------------------------------------------------------------------------------------

class _Contacts:
    """Resolves source contacts to contacts of `org`, merging by e-mail, the newest record winning.

    Every contact the import touches carries a line `migration-source:<source>:<id>` in its notes, which
    is how a re-run knows it has been here and leaves it alone."""

    def __init__(self, org, source, report):
        self.org, self.source, self.report = org, source, report
        self.recency = {}  # contact pk -> date of its newest invoice we know of
        self.by_source = {}
        self._markerless = None

    def _marker(self, sid):
        return f"migration-source:{self.source}:{sid}"

    @staticmethod
    def _notes(contact) -> list[str]:
        from apps.vault.fields import UndecryptableValue
        try:
            return (Contact.objects.only("notes").get(pk=contact.pk).notes or "").splitlines()
        except UndecryptableValue:
            return []

    def _no_email_index(self):
        if self._markerless is None:
            self._markerless = {}
            for pk in Contact.objects.filter(org=self.org, copy_for_org__isnull=True, emails__isnull=True) \
                    .values_list("pk", flat=True):
                for line in self._notes(Contact(pk=pk)):
                    if line.startswith("migration-source:"):
                        self._markerless[line] = pk
        return self._markerless

    def _known_recency(self, contact):
        if contact.pk not in self.recency:
            newest = Invoice.objects.filter(contact=contact).aggregate(m=Max("issue_date"))["m"]
            self.recency[contact.pk] = newest or datetime.date.min
        return self.recency[contact.pk]

    def _create(self, email, fields, us, marker):
        """A new contact of `org` (with its e-mail, if any) and its audit entry; None if the e-mail was
        taken by a concurrent writer."""
        try:
            with transaction.atomic():
                contact = Contact.objects.create(org=self.org, notes=marker,
                                                 **({"us_tax_resident": us} if us else {}), **fields)
                if email:
                    ContactEmail.objects.create(contact=contact, address=email, primary=True)
                audit.record(None, "contact.create", contact,
                             {"source": self.source, "us_tax_resident": contact.us_tax_resident},
                             actor_name="importer")
            return contact
        except IntegrityError:
            if email and find_contact(self.org, email) is not None:
                return None
            raise

    def resolve(self, rec, newest: datetime.date):
        fields = {k: rec[k] for k in _CONTACT_FIELDS if rec[k]}
        stated_language = bool(fields.get("preferred_language"))
        us = "yes" if self.org.is_us_resident_issuer else None
        marker = self._marker(rec["source_id"])
        if rec["email"]:
            contact = find_contact(self.org, rec["email"])
        else:
            pk = self._no_email_index().get(marker)
            contact = Contact.objects.get(pk=pk) if pk else None
        if contact is None:
            create_fields = {**fields} if stated_language else {**fields, "preferred_language": self.org.default_language}
            contact = self._create(rec["email"], create_fields, us, marker)
            if contact is not None:
                self.report.contacts_new += 1
                if not rec["email"]:
                    self.report.contacts_without_email += 1
                    self._no_email_index()[marker] = contact.pk
                self.recency[contact.pk] = newest
                return contact
            contact = find_contact(self.org, rec["email"])
        notes = self._notes(contact)
        if marker in notes:  # imported before: a re-run leaves it as staff may have left it
            self.report.contacts_skipped_existing += 1
            return contact
        self.report.contacts_merged += 1
        self._merge(contact, fields, us, newest, notes + [marker])
        return contact

    def _merge(self, contact, fields, us, newest, notes):
        newer = newest >= self._known_recency(contact)
        changes = {}
        for name, value in fields.items():
            if newer or not getattr(contact, name):
                changes[name] = value
        if us and contact.us_tax_resident != us:
            changes["us_tax_resident"] = us
        changes["notes"] = "\n".join(notes)
        update_contact(contact, changes)
        self.recency[contact.pk] = max(self._known_recency(contact), newest)


# ---- invoices ------------------------------------------------------------------------------------

def _noon(day: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(12, 0), tzinfo=datetime.timezone.utc)


def _native_seq(org, number):
    """The sequence number `number` carries if it has the shape of the organization's own numbering."""
    pattern = ""
    for literal, name, _spec, _conv in string.Formatter().parse(org.number_format):
        pattern += re.escape(literal)
        if name == "year":
            pattern += r"\d{4}"
        elif name == "seq":
            pattern += r"(?P<seq>\d+)"
    match = re.fullmatch(pattern, number)
    return int(match.group("seq")) if match else None


def _assign_number(org, number, source):
    """(assigned, collided): the source's own number, or the first of `n-RF`, `n-RF2`, … (`-Z2`, … for
    Zaprite) that the organization does not hold yet."""
    if not number:
        return None, False
    seq = _native_seq(org, number)
    if seq is not None and seq >= org.next_number:
        raise Refused("the number has the shape of this organization's own numbering and is not below its "
                      "next number; decide by hand")
    tag = _SUFFIX[source]
    candidates = [number] + ([f"{number}-{tag}"] if source == "rf" else []) + \
        [f"{number}-{tag}{k}" for k in range(2, 10000)]
    for candidate in candidates:
        if not Invoice.objects.filter(org=org, number=candidate).exists():
            return candidate, candidate != number
    raise Refused("no free number")


def _write_payment(invoice, total, paid_at, note, external_id):
    payment = Payment.objects.create(
        invoice=invoice, rail="manual", asset=invoice.currency, amount_minor=total,
        received_amount=_decimal_major(total, invoice.currency), state="confirmed", confirmed_at=paid_at,
        final=True, applied=True, evidence_note=note, external_id=external_id,
    )
    # detected_at is auto_now_add; the books (payments CSV) date the payment by the source's paid date.
    Payment.objects.filter(pk=payment.pk).update(detected_at=paid_at)
    return payment


def _write_invoice(rec, *, org, source, contact, buyer, report):
    prefix = "zaprite" if source == "zaprite" else "rf"
    ref = f"{prefix}:{rec['source_id']}"
    items = [LineItem(position=i, **line) for i, line in enumerate(rec["lines"])]
    if totals_of(items).total != rec["total_minor"]:
        raise Refused("the lines do not add up to total_minor")
    external_id = f"migration:{source}:{org.slug}:{rec['source_id']}"
    if len(external_id) > 128 or len(ref) > 200:
        raise Refused("source_id is too long")
    number, collided = _assign_number(org, rec["number"], source)
    issue = rec["issue_date"]
    paid_at = _noon(rec["paid_date"] or issue)
    info = {"source": source, "source_id": rec["source_id"], "source_number": rec["number"],
            "source_status": rec["source_status"], "source_url": rec["source_url"],
            "migrated_at": timezone.now().isoformat()}
    with transaction.atomic():
        invoice = Invoice.objects.create(
            org=org, requested_by_org=org, contact=contact, kind=rec["kind"], number=number,
            currency=rec["currency"], issue_date=issue,
            due_date=issue + datetime.timedelta(days=rec["due_days"]) if rec["due_days"] is not None else None,
            status="draft", external_reference=ref, title=rec["title"], notes=rec["footer"],
            offered_methods=[], client_letters=False,
        )
        for item in items:
            item.invoice = invoice
            item.save()
        snap = snapshot.build(invoice, buyer={"name": buyer["legal_name"] or buyer["display_name"] or "",
                                              "address": buyer["address"]})
        snap["buyer"]["tax_ids"] = buyer["tax_ids"]
        invoice.snapshot = snap
        invoice.status, invoice.sent_at, invoice.paid_at = "paid", _noon(issue), paid_at
        invoice.metadata = {"migration": info}  # last: this is what locks the invoice to the views
        invoice.save()
        if rec["total_minor"] > 0:
            _write_payment(invoice, rec["total_minor"], paid_at, staff_note(info), external_id)
        audit.record(None, "invoice.migrate", invoice, {"source": source, "source_id": rec["source_id"]},
                     actor_name="importer")
    key = (rec["kind"], rec["currency"])
    report.invoices_by_kind_currency[key] = report.invoices_by_kind_currency.get(key, 0) + 1
    report.totals_by_currency[rec["currency"]] = report.totals_by_currency.get(rec["currency"], 0) + rec["total_minor"]
    report.source_status_counts[rec["source_status"]] = report.source_status_counts.get(rec["source_status"], 0) + 1
    if collided:
        report.collisions.append((rec["source_id"], rec["number"], number))


def _lock_path() -> str:
    return getattr(settings, "INVOICE_IMPORT_LOCK_FILE", None) or os.path.join(
        tempfile.gettempdir(), "payinv-import-invoices.lock")


def run(lines: Iterable[str], *, source: str, org, dry_run: bool, allow_us_issuer: bool = False) -> Report:
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    if source == "zaprite" and org.is_us_resident_issuer and not allow_us_issuer:
        raise UsIssuerRefused("Zaprite records are not imported into the US-resident issuer's organization "
                              "unless --allow-us-issuer is given")
    report = Report()
    # One run at a time, for the whole run (a dry run too): the loser writes nothing.
    with open(_lock_path(), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ImportBusy("another import is running") from None
        if dry_run:
            with transaction.atomic():
                _run(lines, source, org, report)
                transaction.set_rollback(True)
        else:
            # No outer transaction: each invoice commits on its own, so a run that dies half-way keeps
            # what it wrote and the re-run skips it.
            _run(lines, source, org, report)
            audit.record(None, "migration.run", org, {
                "source": source, "contacts_new": report.contacts_new, "contacts_merged": report.contacts_merged,
                "contacts_without_email": report.contacts_without_email,
                "contacts_skipped_existing": report.contacts_skipped_existing,
                "invoices": sum(report.invoices_by_kind_currency.values()), "collisions": len(report.collisions),
                "skipped_existing": report.skipped_existing, "refused": len(report.refused),
                "skipped_by_status": sum(report.skipped_by_status.values()),
            }, actor_name="importer")
    return report


def _run(lines, source, org, report):
    contacts, invoices = _read(lines, report, source)
    newest = {}
    for _no, rec in invoices:
        sid = rec["contact_source_id"]
        newest[sid] = max(newest.get(sid, datetime.date.min), rec["issue_date"])
    resolver = _Contacts(org, source, report)
    for sid, (_no, rec) in sorted(contacts.items(), key=lambda kv: (newest.get(kv[0], datetime.date.min), kv[1][0])):
        try:
            resolver.by_source[sid] = resolver.resolve(rec, newest.get(sid, datetime.date.min))
        except (DataError, OverflowError, ValueError):
            report.refused.append((_no, f"{source}:{sid}", "the contact could not be written"))
    for no, rec in invoices:
        status = rec["source_status"]
        if source == "rf" and status.lower() in SKIPPED_RF_STATUSES:
            report.skipped_by_status[status] = report.skipped_by_status.get(status, 0) + 1
            report.skipped_refs.append((f"{source}:{rec['source_id']}", f"source status {status}"))
            continue
        ref = f"{'zaprite' if source == 'zaprite' else 'rf'}:{rec['source_id']}"
        if Invoice.objects.filter(requested_by_org=org, external_reference=ref).exists():
            report.skipped_existing += 1
            report.skipped_refs.append((ref, "already imported"))
            continue
        contact = resolver.by_source.get(rec["contact_source_id"])
        if contact is None:
            report.refused.append((no, ref, "contact_source_id names no contact record"))
            continue
        try:
            _write_invoice(rec, org=org, source=source, contact=contact,
                           buyer=contacts[rec["contact_source_id"]][1], report=report)
        except Refused as exc:
            report.refused.append((no, ref, str(exc)))
        except (InvoiceError, IntegrityError, DataError, OverflowError):
            report.refused.append((no, ref, "the database refused the invoice"))
    report.refused.sort(key=lambda r: r[0])


def btc_lines(lines: Iterable[str], ids: list[str]) -> list[tuple]:
    """(source_id, satoshis or None, source_url) for the invoice records named; satoshis only for a
    BTC invoice, whose minor unit is the satoshi."""
    wanted, found = set(ids), {}
    for raw in lines:
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("type") == "invoice" and str(rec.get("source_id")) in wanted:
            total = rec.get("total_minor")
            sats = total if (str(rec.get("currency", "")).upper() == "BTC" and isinstance(total, int)) else None
            found[str(rec["source_id"])] = (str(rec["source_id"]), sats, rec.get("source_url") or "")
    return [found.get(i, (i, None, "")) for i in ids]
