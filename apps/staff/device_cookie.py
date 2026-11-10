"""The staff device cookie (audit WEB-2, OWASP's device-cookie pattern).

The login keeps a bucket per username: it caps password guessing spread over many addresses. Charged
on every attempt, it also let anyone keep a named account, and the admin, locked out. A browser that
has completed password AND TOTP before therefore gets this cookie, and a login that carries a valid
one for the username it names skips the username bucket. Its attempts are counted against the
cookie instead, so a stolen cookie buys no more guesses than the account bucket gave. The cookie
never skips TOTP, nor the per-address bucket.

The value is signed (Django's signed cookies, under their own salt) and names the user by id, with a
random device id and a binding that changes with the password: an HMAC of the password hash under a
salt of its own (the construction of `get_session_auth_hash`, never that hash itself). A password
change, deactivation or the loss of staff status makes every cookie of the account worthless.
"""
import secrets

from django.contrib.auth import get_user_model
from django.utils.crypto import constant_time_compare, salted_hmac

COOKIE = "pay_staff_device"
SALT = "apps.staff.device-cookie"
MAX_AGE_S = 90 * 24 * 60 * 60
PATH = "/"


def _binding(user) -> str:
    return salted_hmac(SALT, user.password, algorithm="sha256").hexdigest()[:32]


def issue(response, user) -> None:
    """Set the cookie on `response`: after a successful password + TOTP only."""
    value = f"{user.pk}:{secrets.token_urlsafe(16)}:{_binding(user)}"
    response.set_signed_cookie(COOKIE, value, salt=SALT, max_age=MAX_AGE_S, path=PATH,
                               secure=True, httponly=True, samesite="Lax")


def device_for(request, username: str) -> str | None:
    """The device id of a valid cookie for `username` (lower-cased, as the login buckets key it),
    or None: no cookie, a bad signature, expired, another user, or an account that changed."""
    value = request.get_signed_cookie(COOKIE, default=None, salt=SALT, max_age=MAX_AGE_S)
    if not value:
        return None
    try:
        pk, device_id, binding = value.split(":")
        pk = int(pk)
    except ValueError:
        return None
    user = get_user_model()._default_manager.filter(pk=pk).first()
    if user is None or not (user.is_active and user.is_staff):
        return None
    if user.get_username().strip().lower() != username or not constant_time_compare(binding, _binding(user)):
        return None
    return device_id
