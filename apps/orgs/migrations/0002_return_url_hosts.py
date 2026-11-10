from django.db import migrations

SHOP = "liberation.services"


def allow_the_shop(apps, schema_editor):
    """The shop's own pages may be a Back button's target for the two organizations it invoices through.
    Written as plain JSON on `settings` (not encrypted). Only `pk` and `settings` are loaded: the other
    columns are encrypted, and a sealed deploy must not stop here. Organizations that do not exist are skipped, and
    hosts already listed are kept."""
    Organization = apps.get_model("orgs", "Organization")
    for org in Organization.objects.filter(slug__in=["liberation", "seychelles"]).only("pk", "settings"):
        settings = dict(org.settings) if isinstance(org.settings, dict) else {}
        hosts = settings.get("return_url_hosts")
        hosts = list(hosts) if isinstance(hosts, list) else []
        if SHOP in hosts:
            continue
        settings["return_url_hosts"] = [*hosts, SHOP]
        org.settings = settings
        org.save(update_fields=["settings"])


class Migration(migrations.Migration):
    dependencies = [("orgs", "0001_initial")]
    operations = [migrations.RunPython(allow_the_shop, migrations.RunPython.noop)]
