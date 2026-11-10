"""The client's side of encrypted e-mail: /i/<view token>/key/ to upload a key, /k/<token>/ to
confirm it, /k/<token>/cancel/ for the holder of the contact's active key to refuse it.

An upload through an invoice link only ever makes a `pending` key and sends a letter encrypted
to it; the key is used once a person opens that letter's link and presses Confirm. A GET of the
confirmation link reads and shows, never changes anything: mail scanners and link previews
fetch every link they see. Every response carries the portal's headers (no-store, noindex,
Referrer-Policy same-origin, never no-referrer, which would break the CSRF check on Confirm).
"""

from django.conf import settings
from django.shortcuts import redirect, render
from django.utils import translation
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods

from apps.invoices import tokens
from apps.keys import services
from apps.keys.models import EncryptionKey
from apps.keys.validation import MAX_BYTES, KeyRejected
from apps.portal.views import MISS_LIMIT, RATE_LIMIT, RATE_WINDOW_S, _harden, identity, invoice_identity, portal
from apps.vault.ratelimit import address_bucket, client_ip, rate_limited

# Uploads through one invoice link: each one can put a letter in the contact's mailbox.
UPLOAD_LIMIT = 5
UPLOAD_WINDOW_S = 3600
# Which key this browser just uploaded, for the result page after the redirect. Kept in the
# session, not the URL: the result address shows nothing to anyone else who opens it.
SENT_SESSION_KEY = "keys.upload.sent"

# What a refused key is told, by KeyRejected code. Staff read the same sentences in English.
REASONS = {
    "too_large": gettext_lazy("The file is too large to be a public key."),
    "pkcs12_refused": gettext_lazy("This is a .p12/.pfx file, which holds a private key. Upload only the certificate."),
    "secret_key": gettext_lazy("This contains a private key. Keep it to yourself and upload only the public key."),
    "not_a_key": gettext_lazy("This is not a PGP public key or an S/MIME certificate."),
    "several_keys": gettext_lazy("This contains more than one key. Upload one key only."),
    "cannot_encrypt": gettext_lazy("This key cannot be used to encrypt e-mail."),
    "expired": gettext_lazy("This key has expired."),
    "not_yet_valid": gettext_lazy("This key is not valid yet."),
    "revoked": gettext_lazy("This key has been revoked."),
    "email_mismatch": gettext_lazy("This key does not name the e-mail address we write to you at."),
    "unsupported_key": gettext_lazy("This type or size of key is not supported."),
}
# The confirmation link does not work, by confirm_key's KeyError code: (HTTP status).
LINK_STATUS = {"unknown": 404, "expired": 410, "used": 410, "withdrawn": 410}


def reason_text(code: str) -> str:
    return str(REASONS.get(code, REASONS["not_a_key"]))


def read_upload(request) -> bytes | str | None:
    """The key a form sent: the chosen file, else the pasted text; None when neither.

    A file is read only a little past MAX_BYTES, which is enough for `inspect` to call it too
    large, so a huge upload is never held in memory."""
    upload = request.FILES.get("key_file")
    if upload is not None and upload.size:
        return upload.read(MAX_BYTES + 1)
    text = request.POST.get("key") or ""
    return text if text.strip() else None


def _lang_query(request, lang) -> str:
    return f"?lang={lang}" if "lang" in request.GET else ""


@require_http_methods(["GET", "POST"])
@portal("view")
def upload(request, invoice, token, lang):
    ctx = {"token": token, "lang_query": _lang_query(request, lang), **invoice_identity(invoice)}
    if request.method == "GET":
        key = _just_uploaded(request, invoice, token) if request.GET.get("sent") else None
        if key is not None:
            ctx.update({"uploaded": True, "fingerprint": services.format_fingerprint(key.fingerprint)})
        return render(request, "keys/upload.html", ctx)
    # Counted per link, whoever sends it and whatever it holds: gpg is not free, and a
    # forwarded link must not become a way to fill the contact's mailbox.
    if rate_limited("key-upload", tokens.hash_token(token)[:16], UPLOAD_LIMIT, UPLOAD_WINDOW_S):
        ctx["error"] = _("Too many keys were uploaded through this link. Please try again in an hour.")
        return render(request, "keys/upload.html", ctx, status=429)
    data = read_upload(request)
    if data is None:
        ctx["error"] = _("Paste the key or choose a file.")
        return render(request, "keys/upload.html", ctx, status=400)
    try:
        key = services.add_key(invoice.contact, data, source="client")
    except KeyRejected as exc:
        ctx["error"] = reason_text(exc.code)
        return render(request, "keys/upload.html", ctx, status=400)
    except services.KeyCheckUnavailable:
        ctx["error"] = _("We could not check the key right now. Please try again in a few minutes.")
        return render(request, "keys/upload.html", ctx, status=503)
    # Post/redirect/get: refreshing the result page must not upload (or mail) anything again.
    request.session[SENT_SESSION_KEY] = {"link": tokens.hash_token(token)[:16], "key": key.pk}
    suffix = f"&lang={lang}" if "lang" in request.GET else ""
    return redirect(f"/i/{token}/key/?sent=1{suffix}")


def _just_uploaded(request, invoice, token):
    """The key this browser uploaded through this link, for the result page; None otherwise."""
    sent = request.session.get(SENT_SESSION_KEY) or {}
    if sent.get("link") != tokens.hash_token(token)[:16]:
        return None
    return EncryptionKey.objects.filter(pk=sent.get("key"), contact_id=invoice.contact_id).first()


def _show_issuer(request, key) -> None:
    """The head of a /k/ page names the organization that writes to this contact (read by the
    `apps.portal.context.issuer` context processor); an unknown link names nobody."""
    if key is not None:
        request.client_identity = identity(key.contact.org)


def _confirm_language(request, key) -> str:
    codes = {code for code, _name in settings.LANGUAGES}
    lang = request.GET.get("lang")
    if lang in codes:
        return lang
    if key is not None:
        from apps.core.i18n import activate_for

        return activate_for(key.contact)
    lang = translation.get_language_from_request(request)
    return lang if lang in codes else "en"


@require_http_methods(["GET", "POST"])
def confirm(request, token):
    """GET shows the key and a Confirm button and changes nothing; POST confirms."""
    key = services.find_by_token(token)
    _show_issuer(request, key)
    ip = address_bucket(client_ip(request))  # an IPv6 address counts by its /64 (final review I4)
    if key is None:
        # Unknown tokens share one bucket per address, as on the portal: guessing costs the guesser.
        limited = rate_limited("key-confirm-miss", ip, MISS_LIMIT, RATE_WINDOW_S)
    else:
        limited = rate_limited("key-confirm", f"{ip}:{tokens.hash_token(token)[:16]}", RATE_LIMIT, RATE_WINDOW_S)
    with translation.override(_confirm_language(request, key)):
        if limited:
            response = render(request, "portal/too_many.html", status=429)
        else:
            response = _confirm(request, token, key)
    return _harden(response)


def _confirm(request, token, key):
    problem = services.confirmation_problem(key)
    if problem == "used" and key.state == "active":
        return _confirmed(request, key)  # a second click: this link already made the key active
    if problem:
        return render(request, "keys/expired.html", {"reason": problem}, status=LINK_STATUS[problem])
    ctx = {"token": token, "fingerprint": services.format_fingerprint(key.fingerprint),
           "lang_query": _lang_query(request, translation.get_language())}
    if request.method == "GET":
        return render(request, "keys/confirm.html", ctx)
    try:
        services.confirm_key(token)
    except KeyError as exc:
        reason = exc.args[0]
        key.refresh_from_db(fields=["state"])
        if reason == "used" and key.state == "active":
            return _confirmed(request, key)  # the other click of a double click won the race
        return render(request, "keys/expired.html", {"reason": reason}, status=LINK_STATUS[reason])
    except KeyRejected as exc:
        return render(request, "keys/expired.html", {"rejected": reason_text(exc.code)}, status=409)
    except services.KeyCheckUnavailable:
        ctx["error"] = _("We could not check the key right now. Please try again in a few minutes.")
        return render(request, "keys/confirm.html", ctx, status=503)
    return _confirmed(request, key)


def _confirmed(request, key):
    return render(request, "keys/confirmed.html", {"fingerprint": services.format_fingerprint(key.fingerprint)})


@require_http_methods(["GET", "POST"])
def cancel(request, token):
    """The link in the replacement notice (encrypted to the key that was active). GET shows the new
    key and a button and changes nothing; POST refuses the new key and keeps the old one."""
    key = services.find_by_cancel_token(token)
    _show_issuer(request, key)
    ip = address_bucket(client_ip(request))  # an IPv6 address counts by its /64 (final review I4)
    if key is None:
        # The confirmation page's miss bucket: an unknown token costs the guesser, wherever it is tried.
        limited = rate_limited("key-confirm-miss", ip, MISS_LIMIT, RATE_WINDOW_S)
    else:
        limited = rate_limited("key-cancel", f"{ip}:{tokens.hash_token(token)[:16]}", RATE_LIMIT, RATE_WINDOW_S)
    with translation.override(_confirm_language(request, key)):
        if limited:
            response = render(request, "portal/too_many.html", status=429)
        else:
            response = _cancel(request, token, key)
    return _harden(response)


def _cancel(request, token, key):
    if request.method == "GET":
        problem = services.cancel_problem(key)
        if problem:
            return _unusable(request, problem)
        return render(request, "keys/cancel.html", {
            "token": token, "fingerprint": services.format_fingerprint(key.fingerprint),
            "lang_query": _lang_query(request, translation.get_language())})
    # A POST is always put to the service: it checks the link again and tells staff of a refusal.
    try:
        services.cancel_replacement(token)
    except KeyError as exc:
        return _unusable(request, exc.args[0])
    except services.CancelRefused as exc:
        if exc.code == "replaced_key_withdrawn":
            return _unusable(request, "withdrawn")
        return render(request, "keys/cancel_refused.html", status=409)
    still_encrypted = EncryptionKey.objects.filter(contact_id=key.contact_id, state="active").exists()
    return render(request, "keys/cancelled.html", {"still_encrypted": still_encrypted})


def _unusable(request, reason):
    return render(request, "keys/cancel_unusable.html", {"reason": reason}, status=LINK_STATUS[reason])
