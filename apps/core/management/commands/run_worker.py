import time

from django.core.management.base import BaseCommand

from apps.core import jobs, worker


class Command(BaseCommand):
    help = "Run the background jobs (outbox, overdue, sealed-vault alert) until SIGTERM."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run every job once and exit.")

    def handle(self, *args, **options):
        worker.reset()
        jobs.register_all()
        if options["once"]:
            self.stdout.write(" ".join(worker.run_due(time.monotonic())))
            return
        worker.serve()
