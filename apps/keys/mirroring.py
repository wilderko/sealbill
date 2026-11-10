"""Keys mirrored onto the US-resident issuer's copy of a contact.

An invoice to a US tax resident is issued by the US-resident issuer, to that organization's copy
of the contact (`invoices.services._contact_in`). The issuer keeps one copy per address and
requesting organization (`Contact.copy_for_org`), so a copy has one original: the requesting
organization's contact with that address. The issuer's own clients and the copies made for other
organizations are never linked to it. The issuer's letters must be as protected as the
original's, so the original's key is mirrored onto the copy: a row with `source="mirror"` whose
`mirrors` names the key it follows. A mirror follows that key, and only that key:

- the key is active: the mirror is usable, unless it failed at send time on the copy or staff
  revoked it there; it then stays so while the key it mirrors is unchanged (only a different
  key of the original replaces it);
- the key failed while active: the mirror is failed too, and keeps plaintext from the copy;
- the key is pending, replaced or revoked, or is gone (an orphan, `mirrors` NULL): revoked.

Of the usable mirrors, the one whose key became active last is the copy's active key and the
others are `replaced`, ready to be promoted again when it goes away. A key added to the copy
itself (staff, the API, a client through an invoice link) is never touched here, and while one
is active it wins over every mirror.

Copies made before `copy_for_org` existed were shared by every requesting organization with that
address, and cannot be told from the issuer's own clients, so they count as the issuer's own
now: nothing is linked to them and no new key is mirrored onto them. The mirrors they already
hold keep following their keys, so a withdrawn key is never left active there.

Every state change of a key calls `key_changed` in the transaction that made it, so a letter
queued earlier is never encrypted to a withdrawn key, and one organization's invoice never
strips another's key. `reconcile` also runs whenever an invoice is issued by the rule: the link
between an original and its copy may be new, and a key may have reached the table another way.

Locks: the original's contact row, then the copy's, then key rows. State changes are audited
with `record_system` (ids, states and codes only), so following a key works from the outbox;
only making a new mirror reads key material, and `create=False` never does.
"""

from apps.audit import services as audit
from apps.contacts.models import Contact, ContactEmail
from apps.keys.models import EncryptionKey
from apps.vault.registry import plaintext_columns

MIRROR = "mirror"
_USABLE = "usable"  # active or replaced: the ranking decides which


def _plain():
    return EncryptionKey.objects.only(*plaintext_columns(EncryptionKey))


def key_changed(contact_id: int, *, actor=None, create: bool = True) -> None:
    """A key of `contact_id` changed state: the copy made for this contact's organization
    follows now, every other contact holding a mirror of its keys brings that mirror in step
    (and is given no new one), and the contact itself does when it is a copy."""
    copies = copies_of(contact_id)
    for copy_id in sorted(copies):
        reconcile(copy_id, sources=(contact_id,), actor=actor, create=create)
    for holder_id in sorted(_mirror_holders(contact_id) - copies - {contact_id}):
        reconcile(holder_id, actor=actor, create=create)
    if EncryptionKey.objects.filter(contact_id=contact_id, source=MIRROR).exists():
        reconcile(contact_id, actor=actor, create=create)


def _mirror_holders(contact_id: int) -> set[int]:
    """Contacts holding a mirror of one of this contact's keys, whatever links them now."""
    return set(EncryptionKey.objects.filter(source=MIRROR, mirrors__contact_id=contact_id)
               .values_list("contact_id", flat=True))


def copies_of(contact_id: int) -> set[int]:
    """The US-resident issuer's copies of this contact: contacts made for its organization
    (`copy_for_org`) that share one of its addresses. Never a native contact, and never a copy
    made for another organization. Clients of record only, on both sides: a payment-link checkout's
    own contact and copy (`Contact.of_record` False) are linked to nothing (audit SEC-1)."""
    row = Contact.objects.filter(pk=contact_id).values_list("org_id", "of_record").first()
    if row is None or not row[1]:
        return set()
    org_id = row[0]
    addresses = ContactEmail.objects.filter(contact_id=contact_id).values("address_index")
    linked = set(ContactEmail.objects.filter(copy_for_org_id=org_id, address_index__in=addresses, of_record=True)
                 .exclude(org_id=org_id).values_list("contact_id", flat=True))
    linked.discard(contact_id)
    return linked


def sources_of(copy_id: int) -> set[int]:
    """The original a copy follows: the contact of the organization it was made for
    (`copy_for_org`) that shares one of its addresses. Nothing for a native contact."""
    row = Contact.objects.filter(pk=copy_id).values_list("org_id", "copy_for_org_id", "of_record").first()
    if row is None or row[1] is None or not row[2]:
        return set()
    addresses = ContactEmail.objects.filter(contact_id=copy_id).values("address_index")
    linked = set(ContactEmail.objects.filter(org_id=row[1], copy_for_org__isnull=True, address_index__in=addresses,
                                             of_record=True)
                 .values_list("contact_id", flat=True))
    linked.discard(copy_id)
    return linked


def decisive_key(contact_id: int) -> EncryptionKey | None:
    """The key the contact's letters depend on: its active key, else the newest that failed while
    active (it keeps plaintext from the contact), else None. Plaintext columns only."""
    keys = _plain().filter(contact_id=contact_id)
    return (keys.filter(state="active").first()
            or keys.filter(state="failed").exclude(failed_while="pending").order_by("-pk").first())


def _follow(mirror_state: str, source: EncryptionKey | None) -> str:
    """The state a mirror must have, judged by the key it mirrors alone."""
    if source is None:
        return "revoked"
    if source.state == "active":
        return mirror_state if mirror_state in ("failed", "revoked") else _USABLE
    if source.state == "failed" and source.failed_while != "pending":
        return "revoked" if mirror_state == "revoked" else "failed"
    return "revoked"


def _activated(key: EncryptionKey):
    """When the key became active: a client's when it was confirmed, anyone else's when added."""
    return (key.confirmed_at or key.created_at, key.pk)


def reconcile(copy_id: int, *, sources=(), actor=None, create: bool = True) -> None:
    """Bring every mirror on the copy in step with the key it mirrors; with `create`, mirror the
    decisive key of each original (`sources` and `sources_of`) that has no mirror yet; then
    choose the copy's active key. Call inside a transaction."""
    Contact.objects.select_for_update().filter(pk=copy_id).only("pk").first()
    actor = actor if getattr(actor, "pk", None) else None
    keys = list(_plain().filter(contact_id=copy_id))
    mirrors = [k for k in keys if k.source == MIRROR]
    followed = {k.pk: k for k in _plain().filter(pk__in={m.mirrors_id for m in mirrors if m.mirrors_id})}
    fresh = []
    if create:
        have = {m.mirrors_id for m in mirrors}
        for contact_id in sorted(set(sources) | sources_of(copy_id)):
            key = decisive_key(contact_id)
            if key is not None and key.pk not in have:
                fresh.append(key)
                followed[key.pk] = key
                have.add(key.pk)

    verdict = {m.pk: _follow(m.state, followed.get(m.mirrors_id)) for m in mirrors}
    candidates = [(followed[m.mirrors_id], m) for m in mirrors if verdict[m.pk] == _USABLE]
    candidates += [(key, None) for key in fresh if key.state == "active"]
    own_active = any(k.state == "active" and k.source != MIRROR for k in keys)
    winner = None if own_active or not candidates else max(candidates, key=lambda pair: _activated(pair[0]))

    target = {}
    for m in mirrors:
        state = verdict[m.pk]
        if state == _USABLE:
            state = "active" if winner is not None and winner[1] is m else "replaced"
        target[m.pk] = state
    # Whatever stops being active first: never two active keys on one contact.
    for m in mirrors:
        if target[m.pk] != m.state and target[m.pk] != "active":
            _move(m, target[m.pk], followed.get(m.mirrors_id), copy_id)
    for key in fresh:
        if winner is None or winner[0] is not key:
            _mirror(key, copy_id, "replaced" if key.state == "active" else "failed", actor)
    if winner is not None:
        source, mirror = winner
        if mirror is None:
            _mirror(source, copy_id, "active", actor)
        elif mirror.state != "active":
            _move(mirror, "active", source, copy_id)


def _move(mirror: EncryptionKey, state: str, source: EncryptionKey | None, copy_id: int) -> None:
    fields = {"state": state}
    if state == "failed":
        fields.update(failed_while="active", last_error_kind=source.last_error_kind if source else "")
    if EncryptionKey.objects.filter(pk=mirror.pk, state=mirror.state).update(**fields):
        audit.record_system("key.mirror", mirror, {"was": mirror.state, "state": state, "contact": copy_id,
                                                   "follows": mirror.mirrors_id})
        mirror.state = state


def _mirror(source: EncryptionKey, copy_id: int, state: str, actor) -> EncryptionKey:
    """A new mirror of `source` on the copy. Reads the key material: needs the vault."""
    full = EncryptionKey.objects.get(pk=source.pk)
    failed = state == "failed"
    copy = EncryptionKey.objects.create(
        contact_id=copy_id, kind=full.kind, material=full.material, fingerprint=full.fingerprint,
        expires_at=full.expires_at, source=MIRROR, state=state, confirmed_at=full.confirmed_at,
        last_error_kind=full.last_error_kind if failed else "", failed_while="active" if failed else "",
        mirrors=full, created_by=actor,
    )
    audit.record(actor, "key.copy", copy, {"kind": copy.kind, "state": state, "contact": copy_id,
                                           "copied_from": full.pk})
    return copy
