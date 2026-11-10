"""BTCPay's webhook. It only makes us look sooner: nothing in the body decides a payment."""

import hashlib
import hmac
import json
import logging

from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.quotes.models import Quote
from apps.quotes.services import gateway_money_may_be_coming
from apps.rails.gateways.btcpay import confirmed_recently
from apps.rails.models import RailAccount
from apps.vault.ratelimit import address_bucket, client_ip, rate_limited
from apps.vault.vault import VaultSealed

logger = logging.getLogger(__name__)
MAX_BODY = 1024 * 1024
RATE_PER_MINUTE = 120


def _signed(account, request, body: bytes) -> bool:
    secret = str((account.credentials or {}).get("webhook_secret") or "")
    header = request.headers.get("BTCPay-Sig", "")
    if not secret or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256="):].lower().encode(), expected.encode())


@csrf_exempt
@require_POST
def btcpay_webhook(request, account_pk: int):
    if rate_limited("btcpay-hook", address_bucket(client_ip(request)), RATE_PER_MINUTE, 60):
        return JsonResponse({"error": "rate_limited"}, status=429)
    try:
        if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_BODY:
            return JsonResponse({"error": "too_large"}, status=413)
    except ValueError:
        return JsonResponse({"error": "bad_length"}, status=400)
    body = request.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        return JsonResponse({"error": "too_large"}, status=413)
    account = RailAccount.objects.filter(pk=account_pk, kind="btcpay").first()
    if account is None:
        return HttpResponse(status=200)  # reveals nothing
    try:
        if not _signed(account, request, body):
            logger.warning("btcpay webhook: bad signature (account %s)", account_pk)
            return JsonResponse({"error": "bad_signature"}, status=401)
    except VaultSealed:
        return JsonResponse({"error": "vault_sealed"}, status=503)
    try:
        invoice_id = json.loads(body).get("invoiceId")
    except (ValueError, AttributeError):
        return HttpResponse(status=200)
    if isinstance(invoice_id, str) and invoice_id:
        # A cancelled quote's checkout stays payable at BTCPay for a while: look at it too. So is
        # any quote whose money may still be coming, whatever its state (a detected payment), and
        # one whose payment was confirmed lately, which BTCPay may now call Invalid (MONEY-4).
        Quote.objects.filter(rail_account_id=account.pk, external_id=invoice_id).filter(
            ~Q(state__in=("released", "paid")) | gateway_money_may_be_coming()
            | confirmed_recently(timezone.now())).update(check_soon=True)
    return HttpResponse(status=200)
