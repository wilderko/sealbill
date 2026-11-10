from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.staff.models import StaffTOTPDevice
from apps.vault.vault import vault


class Command(BaseCommand):
    help = "Create a TOTP device for a staff user and print its otpauth:// setup URL. Needs the vault unsealed."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--replace", action="store_true", help="delete the user's existing TOTP devices first")

    def handle(self, *args, username, replace=False, **options):
        User = get_user_model()
        try:
            user = User.objects.get(**{User.USERNAME_FIELD: username})
        except User.DoesNotExist:
            raise CommandError(f"no such user: {username}")
        if not user.is_staff:
            raise CommandError("only staff users get an authenticator")
        if not vault.is_unsealed():
            raise CommandError("The secret is stored encrypted with the vault key: unseal the vault first "
                               "(unseal_vault), then run this again.")
        if replace:
            StaffTOTPDevice.objects.filter(user=user).delete()
            TOTPDevice.objects.filter(user=user).delete()  # a plaintext one left over from before
        device = StaffTOTPDevice.objects.create(user=user, name="authenticator", confirmed=True)
        self.stdout.write("Scan or paste this into an authenticator app. It is shown once:")
        self.stdout.write(device.config_url)
