from django.db import migrations


def forget(apps, schema_editor):
    """Letters sent or cancelled before audit DATA-4 still held their recipients and contents, the
    client's working /i/ and /p/ links among them. Clearing needs no key, so this runs sealed too."""
    OutboxMessage = apps.get_model("notify", "OutboxMessage")
    OutboxMessage.objects.filter(state__in=("sent", "cancelled")).update(
        to=None, cc=None, subject_context=None, body_context=None)


class Migration(migrations.Migration):

    dependencies = [
        ('notify', '0008_staff_alert_per_channel'),
    ]

    operations = [
        migrations.RunPython(forget, migrations.RunPython.noop),
    ]
