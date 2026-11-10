"""Parallel attempts against one rate-limit window (red-team report 2026-11-10, A1).

The counter used to be read and written back through the cache, so attempts arriving together all
read the same count: fifty parallel guesses at /unlock/ moved its 5-per-5-minutes counter by one
to three. It is now one UPDATE in the database. Only PostgreSQL shows the difference, since SQLite
serialises every writer."""
import threading
from unittest import skipUnless

from django.db import connection, connections
from django.test import TransactionTestCase

from apps.vault.models import RateCounter
from apps.vault.ratelimit import attempts_so_far, count_attempt, rate_limited

THREADS, EACH = 8, 25


def _in_parallel(work):
    start = threading.Barrier(THREADS)
    results, errors = [], []

    def run():
        try:
            start.wait()
            for _ in range(EACH):
                results.append(work())
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


@skipUnless(connection.vendor == "postgresql", "PostgreSQL only: SQLite serialises writers")
class ParallelAttemptsTests(TransactionTestCase):
    def test_every_parallel_attempt_is_counted(self):
        results, errors = _in_parallel(lambda: count_attempt("unlock", "203.0.113.9", 300))
        self.assertEqual(errors, [])
        self.assertEqual(RateCounter.objects.get(key="rl:unlock:203.0.113.9").count, THREADS * EACH)
        self.assertEqual(attempts_so_far("unlock", "203.0.113.9"), THREADS * EACH)
        self.assertEqual(max(results), THREADS * EACH)

    def test_no_more_than_the_limit_get_through_together(self):
        results, errors = _in_parallel(lambda: rate_limited("unlock", "203.0.113.10", 5, 300))
        self.assertEqual(errors, [])
        self.assertLessEqual(results.count(False), 5)  # a read-back may only err towards refusing
        self.assertGreaterEqual(results.count(False), 1)
