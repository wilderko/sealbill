from django.core.management.base import BaseCommand, CommandError

from apps.api.models import ApiToken


class Command(BaseCommand):
    help = "Revoke an API token by name; its next request gets 401."

    def add_arguments(self, parser):
        parser.add_argument("name")
        parser.add_argument("--org", help="organization slug, when several tokens share the name")

    def handle(self, *args, name, org=None, **options):
        tokens = ApiToken.objects.filter(name=name, revoked_at__isnull=True)
        if org:
            tokens = tokens.filter(org__slug=org)
        tokens = list(tokens)
        if not tokens:
            raise CommandError(f"no active token named {name!r}")
        if len(tokens) > 1:
            raise CommandError(f"{len(tokens)} active tokens are named {name!r}; add --org")
        token = tokens[0]
        token.revoke(channel="cli")
        self.stdout.write(f"Revoked {token}")
