"""The staff navigation: which item is current, and the counts beside two of them.

Only on staff pages of a signed-in, verified staff account, and only when the page shows the
navigation: the counts are lazy, so the sign-in pages never run their queries. They are about
seventeen COUNT queries, so they are kept in the shared cache for NAV_COUNTS_TTL seconds. They are
the same for every staff account (staff see every organization), so there is one entry. Needs a
person counts its queues afresh anyway and writes what it found, so that page and its rail agree.
"""
from django.core.cache import cache
from django.utils.functional import SimpleLazyObject

# url_name -> navigation item
CURRENT = {
    "needs_person": "needs_person",
    "invoice_list": "invoices", "invoice": "invoices", "invoice_edit": "invoices", "invoice_links": "invoices",
    "contact_keys": "contacts", "contact_list": "contacts", "contact_detail": "contacts", "contact_edit": "contacts", "record_payment": "invoices", "void": "invoices",
    "invoice_new": "invoice_new",
    "link_list": "links", "link_new": "links", "link_detail": "links", "link_edit": "links", "coupon_add": "links",
    "order_fulfil": "links",
    "exports": "exports",
    "security": "security",
    "settings": "settings", "org_list": "settings", "org_new": "settings", "org_edit": "settings",
    "bank_new": "settings", "bank_edit": "settings", "bank_toggle": "settings",
    "fiat_new": "settings", "fiat_edit": "settings", "fiat_toggle": "settings",
    "rail_list": "settings", "rail_new": "settings", "rail_edit": "settings", "rail_credentials": "settings",
    "rail_btcpay_webhook": "settings", "rail_toggle": "settings",
    "token_list": "settings", "token_new": "settings", "token_scopes": "settings", "token_revoke": "settings",
    "webhook_list": "settings", "webhook_new": "settings", "webhook_detail": "settings", "webhook_delete": "settings",
    "webhook_ping": "settings", "webhook_retry": "settings", "team": "settings", "team_factor_remove": "settings",
    "audit_list": "settings", "audit_detail": "settings", "outbox_list": "settings", "delivery_list": "settings",
}
OPEN_INVOICES = ("sent", "overdue", "declared", "partially_paid")
# One entry for every staff account: staff see every organization, so the counts depend on nobody.
# Counts that ever take a scope (a user, an organization) must put that scope into this key;
# tests_ui.test_the_rail_counts_take_no_scope fails first if a count function gains an argument.
NAV_COUNTS_KEY = "staff-nav-counts:v1"
NAV_COUNTS_TTL = 20


def _open_invoices() -> int:
    from apps.invoices.models import Invoice

    return Invoice.objects.filter(status__in=OPEN_INVOICES).count()


def _counts() -> dict:
    counts = cache.get(NAV_COUNTS_KEY)
    if counts is None:
        from .views import needs_person_count

        counts = {"needs_person": needs_person_count(), "invoices": _open_invoices()}
        cache.set(NAV_COUNTS_KEY, counts, NAV_COUNTS_TTL)
    return counts


def fresh_counts(needs_person: int) -> dict:
    """The counts with Needs a person's own fresh total, stored for the pages that follow. The other
    count is read afresh too: storing a cached one again would restart its clock, and a page visited
    every few seconds would then keep it stale for good."""
    counts = {"needs_person": needs_person, "invoices": _open_invoices()}
    cache.set(NAV_COUNTS_KEY, counts, NAV_COUNTS_TTL)
    return counts


def navigation(request) -> dict:
    user = getattr(request, "user", None)
    match = getattr(request, "resolver_match", None)
    if match is None or match.namespace != "staff" or user is None or not user.is_authenticated:
        return {}
    verified = getattr(user, "is_verified", None)
    if not (user.is_active and user.is_staff and callable(verified) and verified()):
        return {}
    return {"nav_current": CURRENT.get(match.url_name if match else "", ""), "nav_counts": SimpleLazyObject(_counts),
            "stepup": SimpleLazyObject(lambda: _stepup_state(request))}


def _stepup_state(request) -> dict:
    """Whether this account may make the step-up changes now (audit SEC-3): the pages that hold them say so."""
    from .stepup import page_state

    return page_state(request)
