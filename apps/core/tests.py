from django.test import TestCase


class HealthTest(TestCase):
    def test_healthz_reports_status(self):
        # No worker has ever beaten and the test vault is sealed: reachable, but not "ok".
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "degraded")
        self.assertIsNone(data["worker_age_s"])
        self.assertIn("vault", data)
