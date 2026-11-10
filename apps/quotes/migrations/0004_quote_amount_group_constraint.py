"""Amount groups (audit finding MONEY-6), the constraint: a held amount is unique per amount group,
no longer per (network, asset, address). Its own migration, after the backfill in 0003, so the
index is built in a transaction that has written no rows."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quotes", "0003_quote_amount_group"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="quote",
            name="unique_held_quote_amount",
        ),
        migrations.AddConstraint(
            model_name="quote",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    models.Q(("state", "released"), _negated=True),
                    models.Q(("address", ""), _negated=True),
                    ("shares_amount", False),
                ),
                fields=("amount_group", "group_amount"),
                name="unique_held_quote_amount",
            ),
        ),
    ]
