from django.core.management.base import BaseCommand, CommandError

from apps.api.models import SCOPES, ApiToken
from apps.orgs.models import Organization


class Command(BaseCommand):
    help = "Create an API token for an organization. The token is printed once and never stored."

    def add_arguments(self, parser):
        parser.add_argument("--org", required=True, help="organization slug")
        parser.add_argument("--name", required=True)
        parser.add_argument("--scopes", default="read", help=f"comma separated, from: {', '.join(SCOPES)}")

    def handle(self, *args, org, name, scopes, **options):
        try:
            organization = Organization.objects.get(slug=org)
        except Organization.DoesNotExist:
            raise CommandError(f"no such organization: {org}")
        try:
            token, plain = ApiToken.issue(organization, name, [s.strip() for s in scopes.split(",") if s.strip()],
                                          channel="cli")
        except ValueError as exc:
            raise CommandError(str(exc))
        self.stdout.write(f"Token for {organization.slug} / {token.name} (scopes: {', '.join(token.scopes)}). Shown once:")
        self.stdout.write(plain)
