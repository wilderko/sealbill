from django.db import migrations, models


def mark_mirrors(apps, schema_editor):
    """Rows copied before the marker existed carry their original's source: they are mirrors."""
    EncryptionKey = apps.get_model("keys", "EncryptionKey")
    EncryptionKey.objects.filter(mirrors__isnull=False).update(source="mirror")


class Migration(migrations.Migration):

    dependencies = [
        ('keys', '0003_encryptionkey_mirrors'),
    ]

    operations = [
        migrations.AlterField(
            model_name='encryptionkey',
            name='source',
            field=models.CharField(choices=[('staff', 'staff'), ('api', 'api'), ('client', 'client'), ('mirror', 'mirror')], max_length=6),
        ),
        migrations.RunPython(mark_mirrors, migrations.RunPython.noop),
    ]
