"""Amount groups (audit finding MONEY-6): every quote gets its amount group and its amount at the
group's common number of decimals, so that 0004 can make amounts unique per group instead of per
network and asset.

Quotes issued before this could hold the same amount on two networks of one address. Such a pair
is kept exactly as it is, since a client may be paying it right now: the first quote of each such
amount (by issue time) stays under the new constraint, and the others are marked `shares_amount`,
which keeps them outside it until they are released. Their amounts still count as held, so no new
quote is given them. The number of quotes marked is logged, never which ones or for what.
"""
import logging

from django.db import migrations, models

from apps.quotes import groups

logger = logging.getLogger(__name__)
BATCH = 500


def _group_amount(network, asset, amount_base_units):
    if not amount_base_units.isdigit():
        return amount_base_units  # never fail on a row: an amount like this was never written by the app
    return str(groups.group_amount(network, asset, int(amount_base_units)))


def backfill(apps, schema_editor):
    Quote = apps.get_model("quotes", "Quote")
    rows = Quote.objects.using(schema_editor.connection.alias).only(
        "pk", "network", "asset", "address", "amount_base_units", "state").order_by("issued_at", "pk")
    first, changed, shared, amounts = {}, [], 0, set()
    for quote in rows.iterator(chunk_size=BATCH):
        quote.amount_group = groups.amount_group(quote.network, quote.asset, quote.address)
        quote.group_amount = (_group_amount(quote.network, quote.asset, quote.amount_base_units)
                              if quote.amount_group else "")
        quote.shares_amount = False
        if quote.amount_group and quote.state != "released":
            key = (quote.amount_group, quote.group_amount)
            if key in first:
                quote.shares_amount = True
                shared += 1
                amounts.add(key)
            else:
                first[key] = quote.pk
        changed.append(quote)
    Quote.objects.using(schema_editor.connection.alias).bulk_update(
        changed, ["amount_group", "group_amount", "shares_amount"], batch_size=BATCH)
    if shared:
        logger.warning("%d held quote(s) share an amount with an earlier quote of their amount group "
                       "(%d amount(s)); kept as they were, outside the unique constraint until released",
                       shared, len(amounts))


class Migration(migrations.Migration):

    dependencies = [
        ("quotes", "0002_quote_read_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="quote",
            name="amount_group",
            field=models.CharField(blank=True, default="", max_length=160),
        ),
        migrations.AddField(
            model_name="quote",
            name="group_amount",
            field=models.CharField(blank=True, default="", max_length=96),
        ),
        migrations.AddField(
            model_name="quote",
            name="shares_amount",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
