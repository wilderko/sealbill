from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0004_token_hashes_nullable"),
    ]

    operations = [
        migrations.AlterField(
            model_name="invoice", name="view_token_hash",
            field=models.CharField(blank=True, max_length=64, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name="invoice", name="pay_token_hash",
            field=models.CharField(blank=True, max_length=64, null=True, unique=True),
        ),
    ]
