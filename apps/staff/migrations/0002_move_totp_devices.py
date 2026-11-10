"""Move every django-otp TOTP device into the encrypted StaffTOTPDevice, secret unchanged.

Production holds one confirmed device, the owner's: its secret is copied as it is, with its drift,
its last verified step (replay protection), its throttling state and its timestamps, so the owner's
authenticator app keeps working. Each copy is read back and compared before the plaintext rows are
deleted, all in the migration's one transaction.

Encrypting needs the vault. With devices to move and the vault sealed, the migration stops with a
clear error and changes nothing: no row is copied, none deleted, and the migration is not recorded,
so `migrate` simply runs it again once the vault is open. Nothing to move: it runs sealed.
"""
from django.core.management.base import CommandError
from django.db import migrations

# The password is read without echo and never typed into a command line, where the shell history
# would keep it (final review F3; the README unseals the same way).
SEALED = ("Staff TOTP secrets are stored encrypted with the vault key, and the vault is sealed: unseal the "
          "vault first, then migrate. To unseal without the password reaching the shell history: "
          "read -rsp 'Vault password: ' p; echo; printf '%s\\n' \"$p\" | payinv-manage unseal_vault; unset p")
# Every column but the user and the secret, which are set apart, and created_at (auto_now_add).
COPIED = ("name", "confirmed", "step", "t0", "digits", "tolerance", "drift", "last_t",
          "throttling_failure_timestamp", "throttling_failure_count", "last_used_at")


def _move(source, target, using):
    if not source.objects.using(using).exists():
        return
    from apps.vault.vault import vault

    if not vault.is_unsealed():  # before any row is read: going back, reading one decrypts it
        raise CommandError(SEALED)
    rows = list(source.objects.using(using).order_by("pk"))
    for row in rows:
        copy = target.objects.using(using).create(user_id=row.user_id, key=row.key,
                                                  **{name: getattr(row, name) for name in COPIED})
        target.objects.using(using).filter(pk=copy.pk).update(created_at=row.created_at)
        if target.objects.using(using).get(pk=copy.pk).key != row.key:  # read back through the vault
            raise RuntimeError(f"TOTP device #{row.pk}: the copied secret does not read back; nothing was moved")
    source.objects.using(using).filter(pk__in=[row.pk for row in rows]).delete()


def forwards(apps, schema_editor):
    _move(apps.get_model("otp_totp", "TOTPDevice"), apps.get_model("staff", "StaffTOTPDevice"),
          schema_editor.connection.alias)


def backwards(apps, schema_editor):
    _move(apps.get_model("staff", "StaffTOTPDevice"), apps.get_model("otp_totp", "TOTPDevice"),
          schema_editor.connection.alias)


class Migration(migrations.Migration):
    dependencies = [
        ("staff", "0001_initial"),
        ("otp_totp", "0003_add_timestamps"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
