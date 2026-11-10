"""Passkeys (WebAuthn) as the staff second factor, beside the authenticator app (spec 2026-11-04 §3).

Sign-in stays password first; the second step takes a passkey or a code. Both end in the same
django-otp login, so `is_verified()` means "password + second factor" whichever was used.

Every rule here is py_webauthn's, called with ours:
- the relying party is the host of `SITE_URL` and the origin is `SITE_URL`'s, never the request's
  Host header (`relying_party`);
- user verification is required on registration and on every sign-in, discoverable credentials are
  preferred, attestation is `none`;
- the signature counter must grow when the authenticator keeps one (py_webauthn refuses one that
  stands still or goes back); one that reports 0 every time, as synced passkeys do, is accepted;
- a challenge is random, kept in the server-side session, good for one answer only (taken out
  before it is checked, so a refused answer spends it too) and for `CHALLENGE_TTL_S` seconds.

Browser responses are parsed and verified only by py_webauthn (`parse_*_credential_json`, then
`verify_registration_response` / `verify_authentication_response`). A response is something a
browser, or anyone, made up: whatever the library raises on it is a refusal, never a 500.
"""
import logging
import time
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac
from webauthn import (
    generate_authentication_options, generate_registration_options, options_to_json,
    verify_authentication_response, verify_registration_response,
)
from webauthn.helpers import (
    base64url_to_bytes, bytes_to_base64url, generate_challenge, parse_authentication_credential_json,
    parse_registration_credential_json,
)
from webauthn.helpers.structs import (
    AttestationConveyancePreference, AuthenticatorSelectionCriteria, AuthenticatorTransport,
    PublicKeyCredentialDescriptor, ResidentKeyRequirement, UserVerificationRequirement,
)

from apps.audit import services as audit

from .models import StaffPasskey, StaffTOTPDevice

logger = logging.getLogger(__name__)

CHALLENGE_TTL_S = 5 * 60
# Adding or removing a second factor needs a second factor this recent (a session left open is not
# enough to change how the account signs in).
RECENT_VERIFICATION_S = 10 * 60
SESSION_CHALLENGE = "staff_passkey_challenge"
SESSION_VERIFIED_AT = "staff_verified_at"
RP_NAME = "Invoicing"
MAX_RESPONSE_CHARS = 64 * 1024  # a real response is a few kilobytes
MAX_CREDENTIAL_ID_BYTES = 1023  # WebAuthn's limit; py_webauthn does not check it (final review F5)
_TRANSPORTS = {t.value for t in AuthenticatorTransport}


class PasskeyRefused(Exception):
    """A passkey ceremony that did not verify. `reason` is a short code, safe to log and to map to
    a sentence: never the library's message, which quotes what the browser sent."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class LastSecondFactor(Exception):
    """Removing this device would leave the account without a second factor."""


def _now() -> float:
    return time.time()


def relying_party() -> tuple[str, str]:
    """(RP ID, origin): the host name of `SITE_URL`, and `SITE_URL`'s scheme and host as the one
    origin a response may come from."""
    parts = urlsplit(settings.SITE_URL)
    return parts.hostname or "", f"{parts.scheme}://{parts.netloc}"


def user_handle(user) -> bytes:
    """The WebAuthn user handle: stable per account, so an authenticator replaces its own passkey for
    the account instead of keeping two, and naming nobody (neither the login name nor the id)."""
    return salted_hmac("apps.staff.passkeys.user-handle", str(user.pk), algorithm="sha256").digest()


# ---- the session: the pending challenge, and when the session last gave a second factor

def mark_verified(request) -> None:
    request.session[SESSION_VERIFIED_AT] = int(_now())


def recently_verified(request) -> bool:
    """True when this session gave a second factor within `RECENT_VERIFICATION_S`. A session
    verified before the timestamp existed has none, and counts as stale."""
    at = request.session.get(SESSION_VERIFIED_AT)
    return isinstance(at, int) and 0 <= _now() - at <= RECENT_VERIFICATION_S


def _issue(request, purpose: str) -> bytes:
    challenge = generate_challenge()
    request.session[SESSION_CHALLENGE] = {"purpose": purpose, "user": request.user.pk,
                                          "challenge": bytes_to_base64url(challenge),
                                          "expires": _now() + CHALLENGE_TTL_S}
    return challenge


def discard_challenge(request) -> None:
    request.session.pop(SESSION_CHALLENGE, None)


def _take(request, purpose: str) -> bytes:
    """The pending challenge for `purpose`, taken out of the session whatever happens next."""
    record = request.session.pop(SESSION_CHALLENGE, None)
    if not isinstance(record, dict) or record.get("purpose") != purpose or record.get("user") != request.user.pk:
        raise PasskeyRefused("no_challenge")
    if not isinstance(record.get("expires"), (int, float)) or _now() > record["expires"]:
        raise PasskeyRefused("expired")
    return base64url_to_bytes(record["challenge"])


# ---- options

def _descriptor(passkey: StaffPasskey) -> PublicKeyCredentialDescriptor:
    return PublicKeyCredentialDescriptor(
        id=base64url_to_bytes(passkey.credential_id),
        transports=[AuthenticatorTransport(t) for t in passkey.transports if t in _TRANSPORTS] or None)


def registration_options(request) -> str:
    """The options for navigator.credentials.create(), as JSON; a fresh challenge goes into the
    session. The account's passkeys are excluded, so one authenticator is not registered twice."""
    user = request.user
    rp_id, _origin = relying_party()
    options = generate_registration_options(
        rp_id=rp_id, rp_name=RP_NAME, user_id=user_handle(user), user_name=user.get_username(),
        challenge=_issue(request, "register"), timeout=CHALLENGE_TTL_S * 1000,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED, user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[_descriptor(p) for p in StaffPasskey.objects.filter(user=user).order_by("pk")])
    return options_to_json(options)


def authentication_options(request) -> str | None:
    """The options for navigator.credentials.get(), as JSON, naming this account's passkeys only;
    None when it has none (and then no challenge is issued)."""
    passkeys = list(StaffPasskey.objects.filter(user=request.user, confirmed=True).order_by("pk"))
    if not passkeys:
        return None
    rp_id, _origin = relying_party()
    options = generate_authentication_options(
        rp_id=rp_id, challenge=_issue(request, "login"), timeout=CHALLENGE_TTL_S * 1000,
        allow_credentials=[_descriptor(p) for p in passkeys],
        user_verification=UserVerificationRequirement.REQUIRED)
    return options_to_json(options)


# ---- verifying what the browser sent

def _reason(exc: Exception) -> str:
    """A short code for why py_webauthn refused a response (its messages quote the response)."""
    text = str(exc)
    for needle, code in (("origin", "origin"), ("RP ID", "rp_id"), ("challenge", "challenge"),
                         ("sign count", "sign_count"), ("signature", "signature"),
                         ("User verification", "user_verification"), ("not present", "user_presence"),
                         ("presence", "user_presence")):
        if needle in text:
            return code
    return "invalid"


def _checked_size(response: str) -> str:
    if not isinstance(response, str) or not response or len(response) > MAX_RESPONSE_CHARS:
        raise PasskeyRefused("malformed")
    return response


def register(request, name: str, response: str) -> StaffPasskey:
    """Verify a registration response against the pending challenge and store the passkey (audited).
    Raises PasskeyRefused."""
    challenge = _take(request, "register")
    response = _checked_size(response)
    rp_id, origin = relying_party()
    try:
        credential = parse_registration_credential_json(response)
        verified = verify_registration_response(
            credential=credential, expected_challenge=challenge, expected_rp_id=rp_id,
            expected_origin=origin, require_user_verification=True)
    except Exception as exc:  # noqa: BLE001 - a response anyone can make up: a refusal, never a 500
        reason = _reason(exc)
        logger.info("passkey registration refused for user #%s (%s, %s)", request.user.pk, reason, type(exc).__name__)
        raise PasskeyRefused(reason) from None
    if len(verified.credential_id) > MAX_CREDENTIAL_ID_BYTES:
        logger.info("passkey registration refused for user #%s (malformed, credential id of %s bytes)",
                    request.user.pk, len(verified.credential_id))
        raise PasskeyRefused("malformed")
    credential_id = bytes_to_base64url(verified.credential_id)
    transports = sorted({t.value for t in (credential.response.transports or [])} & _TRANSPORTS)
    if StaffPasskey.objects.filter(credential_id=credential_id).exists():
        raise PasskeyRefused("duplicate")
    try:
        with transaction.atomic():
            passkey = StaffPasskey.objects.create(
                user=request.user, name=name, confirmed=True, credential_id=credential_id,
                public_key=verified.credential_public_key, sign_count=verified.sign_count, transports=transports)
            audit.record(request.user, "passkey.add", passkey, {"name": name, "transports": transports})
    except IntegrityError:  # the same credential registered by a parallel request
        raise PasskeyRefused("duplicate") from None
    return passkey


def authenticate(request, response: str) -> StaffPasskey:
    """Verify an assertion against the pending challenge and one of this account's passkeys, and
    record its new counter. Raises PasskeyRefused."""
    challenge = _take(request, "login")
    response = _checked_size(response)
    rp_id, origin = relying_party()
    user = request.user
    try:
        credential = parse_authentication_credential_json(response)
    except Exception as exc:  # noqa: BLE001 - see register()
        raise PasskeyRefused("malformed") from None
    with transaction.atomic():  # locked: two answers at once cannot both pass one counter value
        passkey = (StaffPasskey.objects.select_for_update()
                   .filter(user=user, confirmed=True, credential_id=bytes_to_base64url(credential.raw_id)).first())
        if passkey is None:  # removed, never registered, or another account's
            logger.info("passkey sign-in refused for user #%s (unknown_credential)", user.pk)
            raise PasskeyRefused("unknown_credential")
        try:
            verified = verify_authentication_response(
                credential=credential, expected_challenge=challenge, expected_rp_id=rp_id, expected_origin=origin,
                credential_public_key=bytes(passkey.public_key), credential_current_sign_count=passkey.sign_count,
                require_user_verification=True)
        except Exception as exc:  # noqa: BLE001 - see register()
            reason = _reason(exc)
            if reason == "sign_count":
                # A counter that does not grow is what a cloned authenticator looks like.
                logger.warning("passkey #%s of user #%s: the sign count did not grow (stored %s); refused",
                               passkey.pk, user.pk, passkey.sign_count)
            else:
                logger.info("passkey sign-in refused for user #%s (%s, %s)", user.pk, reason, type(exc).__name__)
            raise PasskeyRefused(reason) from None
        passkey.sign_count = verified.new_sign_count
        passkey.last_used_at = timezone.now()
        passkey.save(update_fields=["sign_count", "last_used_at"])
    return passkey


# ---- removing a second factor

def second_factor_count(user) -> int:
    return (StaffTOTPDevice.objects.filter(user=user, confirmed=True).count()
            + StaffPasskey.objects.filter(user=user, confirmed=True).count())


def remove(actor, model, pk) -> bool:
    """Remove one of `actor`'s own devices (a StaffPasskey or a StaffTOTPDevice), audited. False when
    it is not theirs or already gone; LastSecondFactor when it is the last confirmed one."""
    action = "passkey.remove" if model is StaffPasskey else "totp_device.remove"
    with transaction.atomic():
        # One change to an account's second factors at a time: two removals at once must not both
        # see the other device still there.
        get_user_model()._default_manager.select_for_update().filter(pk=actor.pk).first()
        device = model.objects.filter(pk=pk, user=actor).first()
        if device is None:
            return False
        if device.confirmed and second_factor_count(actor) <= 1:
            raise LastSecondFactor
        audit.record(actor, action, device, {"name": device.name})
        device.delete()
    return True
