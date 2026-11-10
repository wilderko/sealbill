"""Room for the sent-invoice snapshot (API-4) and for a versioned, keyed request fingerprint (DATA-1)."""
import apps.vault.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0005_unique_token_hashes'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoice',
            name='snapshot',
            field=apps.vault.fields.EncryptedJSONField(blank=True, editable=False, null=True),
        ),
        migrations.AlterField(
            model_name='invoice',
            name='request_fingerprint',
            field=models.CharField(blank=True, default='', max_length=80),
        ),
    ]
