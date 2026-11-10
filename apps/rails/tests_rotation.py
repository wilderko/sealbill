import datetime

from django.test import SimpleTestCase

from apps.rails.gateways.rotation import rotated

START = datetime.datetime(2026, 11, 7, 12, 0, tzinfo=datetime.timezone.utc)


class RotationTests(SimpleTestCase):
    def at(self, slot, period=30):
        return START + datetime.timedelta(seconds=period * slot)

    def test_every_item_is_there_once_whatever_the_offset(self):
        for slot in range(10):
            self.assertEqual(sorted(rotated([1, 2, 3, 4], 30, self.at(slot))), [1, 2, 3, 4])

    def test_it_moves_one_step_per_period_and_wraps_around(self):
        firsts = [rotated([1, 2, 3, 4], 30, self.at(slot))[0] for slot in range(4)]
        self.assertEqual(sorted(firsts), [1, 2, 3, 4])
        self.assertEqual(rotated([1, 2, 3, 4], 30, self.at(4))[0], firsts[0])

    def test_the_period_is_the_jobs(self):
        self.assertEqual(rotated([1, 2, 3], 60, START), rotated([1, 2, 3], 60, START + datetime.timedelta(seconds=59)))

    def test_short_lists(self):
        self.assertEqual(rotated([], 30, START), [])
        self.assertEqual(rotated([7], 30, START), [7])
