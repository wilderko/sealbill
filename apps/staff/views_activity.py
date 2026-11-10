"""The audit log, the outbox and the webhook deliveries as pages of their own (Task 9).

The audit list never reads `detail`: it is encrypted, and a list of fifty would decrypt fifty values.
The outbox shows plaintext columns only (recipients and contents are encrypted and cleared once a
letter is finished). A delivery's endpoint is shown by its host, never its URL: receivers carry
tokens in paths."""
import datetime
from urllib.parse import urlsplit

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.utils.http import urlencode

from apps.audit.models import AuditEntry
from apps.notify.models import OutboxMessage
from apps.vault.fields import UndecryptableValue
from apps.vault.registry import plaintext_columns
from apps.vault.vault import VaultSealed
from apps.webhooks.models import WebhookDelivery

from .views import staff_required

PAGE_SIZE = 50
STATES = ["queued", "sent", "failed", "cancelled"]


def _date(raw: str):
    try:
        return datetime.date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _pager(request, rows, params: dict):
    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page"))
    return page, urlencode([(k, v) for k, v in params.items() if v])


@staff_required
def audit_list(request):
    get = request.GET
    action, model, actor = get.get("action", "").strip()[:64], get.get("model", "").strip()[:64], get.get("actor", "").strip()[:150]
    start, end = _date(get.get("from", "")), _date(get.get("to", ""))
    rows = AuditEntry.objects.defer("detail").order_by("-created_at", "-pk")
    if action:
        rows = rows.filter(action=action)
    if model:
        rows = rows.filter(model_label=model)
    if actor:
        rows = rows.filter(actor_username=actor)
    if start:
        rows = rows.filter(created_at__date__gte=start)
    if end:
        rows = rows.filter(created_at__date__lte=end)
    page, query = _pager(request, rows, {"action": action, "model": model, "actor": actor,
                                         "from": start.isoformat() if start else "", "to": end.isoformat() if end else ""})
    return render(request, "staff/activity/audit_list.html", {
        "page": page, "query": query, "action": action, "model": model, "actor": actor,
        "start": start.isoformat() if start else "", "end": end.isoformat() if end else "",
        "actions": list(AuditEntry.objects.order_by().values_list("action", flat=True).distinct().order_by("action")[:200]),
        "models": list(AuditEntry.objects.order_by().values_list("model_label", flat=True).distinct().order_by("model_label")[:200]),
    })


@staff_required
def audit_detail(request, pk):
    entry = get_object_or_404(AuditEntry.objects.defer("detail"), pk=pk)
    sealed, detail = False, None
    try:
        detail = AuditEntry.objects.only("detail").get(pk=pk).detail
    except (UndecryptableValue, VaultSealed):
        sealed = True
    if detail is None and entry.system_detail is not None:
        detail, sealed = entry.system_detail, False
    return render(request, "staff/activity/audit_detail.html", {
        "entry": entry, "detail": detail, "sealed": sealed,
        "detail_items": sorted(detail.items()) if isinstance(detail, dict) else None})


@staff_required
def outbox_list(request):
    state = request.GET.get("state", "")
    if state not in STATES:
        state = ""
    rows = OutboxMessage.objects.only(*plaintext_columns(OutboxMessage), "invoice__number").select_related("invoice").order_by("-created_at", "-pk")
    if state:
        rows = rows.filter(state=state)
    page, query = _pager(request, rows, {"state": state})
    return render(request, "staff/activity/outbox_list.html", {"page": page, "query": query, "state": state, "states": STATES})


def _host(endpoint) -> str:
    try:
        return urlsplit(endpoint.url).hostname or "(unreadable)"
    except (ValueError, VaultSealed):
        return "(unreadable)"


@staff_required
def delivery_list(request):
    from .orgnames import org_names

    state = request.GET.get("state", "")
    if state not in STATES:
        state = ""
    rows = (WebhookDelivery.objects.select_related("event", "endpoint").defer("event__data", "endpoint__secret")
            .order_by("-created_at", "-pk"))
    if state:
        rows = rows.filter(state=state)
    page, query = _pager(request, rows, {"state": state})
    names = org_names()
    for d in page.object_list:
        d.host = _host(d.endpoint)
        d.org_name = names.get(d.endpoint.org_id, str(d.endpoint.org_id))
    return render(request, "staff/activity/delivery_list.html", {"page": page, "query": query, "state": state, "states": STATES})
