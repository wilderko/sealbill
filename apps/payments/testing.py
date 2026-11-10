"""Test helpers for what only a person may do (`services.is_a_person`): record a manual payment, confirm or
assign a payment waiting for one."""
from django.contrib.auth import get_user_model


def person(username: str = "cashier"):
    """An active staff user, made once per test database."""
    user, _ = get_user_model().objects.get_or_create(username=username,
                                                     defaults={"is_staff": True, "is_active": True})
    return user
