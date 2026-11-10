"""The staff second factors: django-otp's TOTP device, with its secret encrypted under the vault key,
and a passkey (WebAuthn), either of which completes a sign-in after the password.

django-otp keeps the secret in a plaintext column, so a database dump (every backup holds one)
yielded working codes with the vault sealed. This model has the same fields, the secret in an
encrypted column, and django-otp's own verification, unchanged: `verify_token` (replay protection
through `last_t`, drift learning, throttling through `ThrottlingMixin`), `bin_key`, `config_url`
and the throttle factor are taken from `TOTPDevice` as they are, not rewritten.

Its manager leaves the secret out of every load, so the session check that loads the device on each
staff request never decrypts it; it is read when a code is verified, and only then.
"""
from django.db import models
from django_otp.models import Device, DeviceManager, ThrottlingMixin, TimestampMixin
from django_otp.plugins.otp_totp.models import TOTPDevice
from django_otp.util import hex_validator, random_hex

from apps.vault.fields import EncryptedCharField


def key_validator(value):
    return hex_validator()(value)


class StaffDeviceManager(DeviceManager):
    def get_queryset(self):
        return super().get_queryset().defer("key")


class StaffTOTPDevice(TimestampMixin, ThrottlingMixin, Device):
    # null=True only because encrypted fields take no default (vault.E002): save() fills it in.
    key = EncryptedCharField(max_length=80, null=True, validators=[key_validator],
                             help_text="A hex-encoded secret key of up to 40 bytes, encrypted with the vault key.")
    step = models.PositiveSmallIntegerField(default=30, help_text="The time step in seconds.")
    t0 = models.BigIntegerField(default=0, help_text="The Unix time at which to begin counting steps.")
    digits = models.PositiveSmallIntegerField(choices=[(6, 6), (8, 8)], default=6,
                                              help_text="The number of digits to expect in a token.")
    tolerance = models.PositiveSmallIntegerField(default=1,
                                                 help_text="The number of time steps in the past or future to allow.")
    drift = models.SmallIntegerField(default=0,
                                     help_text="The number of time steps the prover is known to deviate from our clock.")
    last_t = models.BigIntegerField(
        default=-1,
        help_text="The t value of the latest verified token. The next token must be at a higher time step.")

    objects = StaffDeviceManager()

    class Meta(Device.Meta):
        verbose_name = "staff TOTP device"

    bin_key = TOTPDevice.bin_key
    verify_token = TOTPDevice.verify_token
    get_throttle_factor = TOTPDevice.get_throttle_factor  # OTP_TOTP_THROTTLE_FACTOR, as for TOTPDevice
    config_url = TOTPDevice.config_url
    _read_str_from_settings = TOTPDevice._read_str_from_settings

    def save(self, *args, **kwargs):
        if self._state.adding and not self.key:
            self.key = random_hex(20)  # django-otp's default: 20 random bytes
        super().save(*args, **kwargs)


class StaffPasskey(TimestampMixin, Device):
    """A passkey (a WebAuthn credential) as a django-otp device, so `is_verified()` and every
    `@staff_required` keep meaning "password + second factor" whichever factor was used.

    It holds what verifying an assertion needs and nothing else: the credential id (base64url, as
    browsers send it), the COSE public key, the signature counter and the transports the
    authenticator named, besides django-otp's name and timestamps. The public key is not a secret,
    so nothing here is encrypted, and a sealed vault cannot keep anyone from signing in with one.
    A passkey has no code: `verify_token` refuses every token, and only `apps.staff.passkeys`
    verifies it, through py_webauthn.
    """

    # base64url of at most 1023 bytes (WebAuthn's limit), unpadded
    credential_id = models.CharField(max_length=1366, unique=True,
                                     help_text="The credential id, base64url-encoded, as the browser sends it.")
    public_key = models.BinaryField(help_text="The credential's COSE public key (not secret).")
    sign_count = models.PositiveBigIntegerField(
        default=0, help_text="The authenticator's signature counter at the last verified use; 0 if it keeps none.")
    transports = models.JSONField(default=list, blank=True,
                                  help_text="How the authenticator said it can be reached (usb, nfc, ble, internal, hybrid).")

    class Meta(Device.Meta):
        verbose_name = "staff passkey"

    def verify_token(self, token):
        return False  # a passkey is verified by its signature (apps.staff.passkeys), never by a code
