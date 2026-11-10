from django.db import migrations, models


def blank_to_null(apps, schema_editor):
    Invoice = apps.get_model("invoices", "Invoice")
    for name in ("view_token_hash", "pay_token_hash"):
        Invoice.objects.filter(**{name: ""}).update(**{name: None})


class Migration(migrations.Migration):
    """Drafts stored "" as their token hashes; "" would collide under the unique constraint added
    in 0005 (a separate migration: on PostgreSQL, ALTER TABLE after a data update in the same
    transaction can fail with pending trigger events)."""

    dependencies = [
        ("invoices", "0003_invoice_request_fingerprint"),
    ]

    operations = [
        migrations.AlterField(
            model_name="invoice", name="view_token_hash",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AlterField(
            model_name="invoice", name="pay_token_hash",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.RunPython(blank_to_null, migrations.RunPython.noop),
    ]
