from django.db import migrations, models


def carry_over(apps, schema_editor):
    """Until now one mark covered both channels: an episode already announced is announced on both."""
    for name in ("ScanGap", "WatcherHealth"):
        model = apps.get_model("rails", name)
        model.objects.filter(alerted_at__isnull=False).update(webhook_alerted_at=models.F("alerted_at"))


class Migration(migrations.Migration):

    dependencies = [
        ('rails', '0002_pending_transfer_scan_gap'),
    ]

    operations = [
        migrations.AddField(
            model_name='scangap',
            name='webhook_alerted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='watcherhealth',
            name='webhook_alerted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(carry_over, migrations.RunPython.noop),
    ]
