from django.db import migrations, models


def carry_over(apps, schema_editor):
    """Until now one mark covered both channels (the event went out with the e-mail): a message
    already announced is announced on both, so no old failure is replayed as a new event."""
    OutboxMessage = apps.get_model("notify", "OutboxMessage")
    OutboxMessage.objects.filter(staff_alerted_at__isnull=False).update(webhook_alerted_at=models.F("staff_alerted_at"))


class Migration(migrations.Migration):

    dependencies = [
        ('notify', '0007_outboxmessage_last_error'),
    ]

    operations = [
        migrations.AddField(
            model_name='outboxmessage',
            name='webhook_alerted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(carry_over, migrations.RunPython.noop),
    ]
