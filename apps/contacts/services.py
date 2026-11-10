import re

from django.db import IntegrityError, transaction

from apps.audit import services as audit
from apps.contacts.models import Contact, ContactEmail
from apps.vault.fields import blind_index


def find_contact(org, email: str, *, copy_for=None) -> Contact | None:
    """`org`'s own contact with this address; with `copy_for` (an organization or its id), the copy
    `org` keeps of that requesting organization's contact (the US-resident issuer's copies). The
    one never stands in for the other. Clients of record only: a contact an unpaid payment-link
    checkout made is never found (`Contact.of_record`, audit SEC-1)."""
    if not email or not email.strip():
        return None
    rows = ContactEmail.objects.filter(org=org, address_index=blind_index(email), of_record=True)
    rows = rows.filter(copy_for_org__isnull=True) if copy_for is None else rows.filter(copy_for_org=copy_for)
    row = rows.select_related("contact").first()
    return row.contact if row else None


def find_or_create_contact(org, email: str, *, copy_for=None, actor=None, audit_detail=None,
                           **fields) -> tuple[Contact, bool]:
    """The contact `find_contact` finds, or a new one: `org`'s own, or with `copy_for` its copy for
    that requesting organization."""
    if not email or not email.strip():
        raise ValueError("e-mail address is required")
    existing = find_contact(org, email, copy_for=copy_for)
    if existing:
        return existing, False
    copy_for_id = getattr(copy_for, "pk", copy_for)
    try:
        with transaction.atomic():
            contact = Contact.objects.create(org=org, copy_for_org_id=copy_for_id, **fields)
            ContactEmail.objects.create(contact=contact, address=email.strip(), primary=True)
            audit.record(actor, "contact.create", contact,
                         {"us_tax_resident": contact.us_tax_resident, **(audit_detail or {})})
        return contact, True
    except IntegrityError:
        existing = find_contact(org, email, copy_for=copy_for)
        if existing is None:
            raise
        return existing, False


def create_unrecorded_contact(org, email: str, *, copy_for=None, actor=None, audit_detail=None,
                              **fields) -> Contact:
    """A link buyer: a contact for one payment-link checkout alone, made from what the visitor typed and not
    a client of record (`Contact.of_record` False). No lookup finds it and nothing reuses it, another
    checkout included. Paying its order does not change that (review S1, F2): only a person makes it a
    client of record (`promote_link_buyer`). With `copy_for`, the US-resident issuer's copy of such a
    contact, made for that checkout alone too. Never looks anything up, so it never lands on, or changes, a
    contact somebody else holds."""
    if not email or not email.strip():
        raise ValueError("e-mail address is required")
    with transaction.atomic():
        contact = Contact.objects.create(org=org, copy_for_org_id=getattr(copy_for, "pk", copy_for), of_record=False,
                                         **fields)
        ContactEmail.objects.create(contact=contact, address=email.strip(), primary=True)
        audit.record(actor, "contact.create", contact,
                     {"us_tax_resident": contact.us_tax_resident, "of_record": False, **(audit_detail or {})})
    return contact


class NotPromotable(ValueError):
    """The contact cannot be made a client of record; `holder` is the client of record holding its address."""

    def __init__(self, message, holder=None):
        super().__init__(message)
        self.holder = holder


def promote_link_buyer(contact: Contact, *, actor) -> None:
    """A person checked a link buyer and makes it a client of record (review S1, F2). Refused for a contact
    that already is one, for the US-resident issuer's copy (once its original is a client, the next invoice
    makes a copy of that), and while a client of record of its organization holds one of its addresses:
    that one is the client. Locks the contact; a racing claim on the address is refused by the unique
    index. Audited under the person's name."""
    with transaction.atomic():
        row = (Contact.objects.select_for_update().filter(pk=contact.pk)
               .values_list("org_id", "copy_for_org_id", "of_record").first())
        if row is None:
            raise NotPromotable("This contact no longer exists.")
        org_id, copy_for_id, of_record = row
        if of_record:
            raise NotPromotable("This contact is already a client of record.")
        if copy_for_id is not None:
            raise NotPromotable("The US-resident issuer's copy of a link buyer is not promoted: promote the "
                                "original, and its next invoice makes a copy of it.")
        indexes = list(ContactEmail.objects.filter(contact_id=contact.pk).values_list("address_index", flat=True))
        holder = (ContactEmail.objects.filter(org_id=org_id, copy_for_org__isnull=True, of_record=True,
                                              address_index__in=indexes)
                  .values_list("contact_id", flat=True).first())
        if holder is not None:
            raise NotPromotable("A client of record already holds this address.", holder=holder)
        try:
            with transaction.atomic():
                Contact.objects.filter(pk=contact.pk).update(of_record=True)
                ContactEmail.objects.filter(contact_id=contact.pk).update(of_record=True)
        except IntegrityError:  # another contact took the address meanwhile: it is the client
            raise NotPromotable("A client of record took this address meanwhile.") from None
        audit.record(actor, "contact.promote", contact, {"of_record": [False, True]})
    contact.of_record = True


def has_paid_invoice(contact: Contact) -> bool:
    """An invoice that staff or the API issued to the contact, or to the US-resident issuer's copy of it
    (`mirroring.copies_of`), is paid or overpaid. A payment-link order never counts, whatever it cost: anyone
    can pay one under any name (review S1, F2). Computed when asked, so a payment reversed later counts no
    more. Plaintext only."""
    from apps.invoices.models import Invoice
    from apps.invoices.services import SETTLED
    from apps.keys.mirroring import copies_of

    return Invoice.objects.filter(contact_id__in={contact.pk} | copies_of(contact.pk), status__in=SETTLED,
                                  payment_link__isnull=True).exists()


def update_contact(contact: Contact, changes: dict, *, actor=None) -> list[str]:
    """Apply `changes` and audit them; returns the names of the fields that really changed.

    The audit names the changed fields and, for the US tax resident flag, old and new value."""
    changed = sorted(name for name, value in changes.items() if getattr(contact, name) != value)
    if not changed:
        return []
    old_flag = contact.us_tax_resident
    with transaction.atomic():
        for name in changed:
            setattr(contact, name, changes[name])
        contact.save(update_fields=changed)
        detail = {"fields": changed}
        if "us_tax_resident" in changed:
            detail["us_tax_resident"] = [old_flag, contact.us_tax_resident]
        audit.record(actor, "contact.update", contact, detail)
    return changed


def effective_us_flag(stored: str, typed: str) -> str:
    """The US tax resident answer that counts: "unknown" is the blank of the tri-state and never
    overrides a stored yes or no."""
    if typed == "unknown" and stored in ("yes", "no"):
        return stored
    return typed


def apply_typed_fields(contact: Contact, typed: dict, *, actor=None) -> list[str]:
    """Apply what staff typed about a contact: the address parts merge over the stored address
    (a part left blank keeps its stored value) and an "unknown" US answer changes nothing."""
    changes = dict(typed)
    if isinstance(changes.get("address"), dict):  # a cleared address (None) replaces, as typed
        stored = contact.address if isinstance(contact.address, dict) else {}
        changes["address"] = {**stored, **changes["address"]}
    if "us_tax_resident" in changes:
        changes["us_tax_resident"] = effective_us_flag(contact.us_tax_resident, changes["us_tax_resident"])
    return update_contact(contact, changes, actor=actor)


def update_contact_and_copies(contact: Contact, changes: dict, *, actor=None) -> list[str]:
    """`update_contact` (PATCH semantics on the contact itself), and the same fields carried to the
    US-resident issuer's copy made for this contact's organization (`mirroring.copies_of`: never
    the issuer's own client, never a copy made for another organization) the way the staff form
    carries them: address parts merge, and an "unknown" US answer never downgrades a copy. Returns
    the contact's own changed fields. Locks the contact, then each copy, as key changes do."""
    from apps.keys.mirroring import copies_of

    with transaction.atomic():
        Contact.objects.select_for_update().filter(pk=contact.pk).only("pk").first()
        changed = update_contact(contact, changes, actor=actor)
        for copy_id in sorted(copies_of(contact.pk)):
            copy = Contact.objects.select_for_update().get(pk=copy_id)
            apply_typed_fields(copy, changes, actor=actor)
    return changed


# Letters NFKD leaves whole, written the way a keyboard without them would (the other side of
# `someone_else` in liberation.services, which the spec asks this to match).
_UNACCENTED = str.maketrans({"ł": "l", "ø": "o", "đ": "d", "ß": "ss", "æ": "ae", "œ": "oe", "’": "'", "‘": "'"})
_HYPHENS = re.compile(r"[\s\-\u2010\u2011\u2012\u2013\u2014]+")


def fold_words(text) -> list[str]:
    """Words of `text` with case, accents and spacing folded away. A hyphen separates words
    (`Marie-Claire` is `marie` and `claire`); an apostrophe does not, and a typographic one is the
    plain one. `ł ø đ ß æ œ` become `l o d ss ae oe`, which NFKD alone would leave."""
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text or "")
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold().translate(_UNACCENTED)
    return [w for w in _HYPHENS.split(plain) if w]


# The folded words of a stored name, by its Fernet token, kept per process (audit SEC-6). A token changes
# whenever its name is written (every encryption has a fresh IV), so it stands for the row's update marker:
# a renamed contact is a miss, never a stale hit. Bounded, and simply emptied when full. Only folded words
# are kept, in this process's memory, which already holds the vault key.
NAME_CACHE_MAX = 20_000
_NAME_CACHE: dict[str, frozenset] = {}


def clear_name_cache() -> None:
    _NAME_CACHE.clear()


def _folded_name(field, token) -> frozenset:
    """The folded words of one stored name (its token, or None). UndecryptableValue when it does not open,
    raised and logged by the field itself, as a loaded row would; a failure is never cached."""
    if token is None:
        return frozenset()
    words = _NAME_CACHE.get(token)
    if words is None:
        words = frozenset(fold_words(field.from_db_value(token, None, None)))
        if len(_NAME_CACHE) >= NAME_CACHE_MAX:
            _NAME_CACHE.clear()
        _NAME_CACHE[token] = words
    return words


def search_by_name(org, text: str, limit: int = 20) -> list[Contact]:
    """`org`'s own clients of record where every word of `text` is a word of `legal_name` or of
    `display_name` (case, accents and spacing folded). Names are encrypted, so this is a linear
    scan; a row whose name cannot be decrypted is skipped (and logged), never fatal. A contact an
    unpaid payment-link checkout made is never a match: anyone can type any name there (audit SEC-1).

    One query reads every candidate's two name tokens, as stored (audit SEC-6: it used to be one query
    per row): loading the rows as models would decrypt them on the way, and one unreadable row would then
    end the whole read. Each token is decrypted and folded once per process (`_folded_name`). Three
    queries in all, however large the table: the tokens, the matches, their e-mail addresses."""
    import logging

    from django.db.models import ExpressionWrapper, F, TextField

    from apps.vault.fields import UndecryptableValue
    from apps.vault.vault import vault

    wanted = set(fold_words(text))
    if not wanted:
        return []
    as_stored = {name: ExpressionWrapper(F(name), output_field=TextField()) for name in ("legal_name", "display_name")}
    rows = list(Contact.objects.filter(org=org, copy_for_org__isnull=True, of_record=True)
                .annotate(legal_token=as_stored["legal_name"], display_token=as_stored["display_name"])
                .order_by("-pk").values_list("pk", "legal_token", "display_token"))
    if rows:
        vault.fernet()  # VaultSealed while sealed, as reading the names did: never answered from the cache
    legal_field, display_field = Contact._meta.get_field("legal_name"), Contact._meta.get_field("display_name")
    found = []
    for pk, legal, display in rows:
        try:
            words = _folded_name(legal_field, legal) | _folded_name(display_field, display)
        except UndecryptableValue:
            logging.getLogger(__name__).error("Contact #%s has a name that cannot be decrypted; skipped in search", pk)
            continue
        if wanted <= words:
            found.append(pk)
            if len(found) >= limit:
                break
    by_pk = {c.pk: c for c in Contact.objects.filter(pk__in=found).prefetch_related("emails")}
    return [by_pk[pk] for pk in found if pk in by_pk]  # a row deleted since the names were read is left out
