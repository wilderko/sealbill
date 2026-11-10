"""Test helper: a staff member signed in with password and TOTP."""
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from .models import StaffTOTPDevice


def verified_staff_client(*, superuser: bool = False, username: str | None = None) -> Client:
    """A browser of its own, signed in as a new staff account (a superuser may also use the Django
    admin) with a confirmed TOTP device that has passed the second factor. `client.user` is the
    account."""
    from .tests import token_for

    User = get_user_model()
    user = User.objects.create_user(username or f"clerk{User.objects.count()}", password="pw-clerk-1",
                                    is_staff=True, is_superuser=superuser)
    device = StaffTOTPDevice.objects.create(user=user, name="t", confirmed=True)
    browser = Client()
    browser.force_login(user)
    r = browser.post(reverse("staff:verify"), {"token": token_for(device)})
    assert r.status_code == 302, r.content[:300]
    browser.user = user
    return browser
