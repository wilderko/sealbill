from unittest import mock

import requests
from django.test import SimpleTestCase, override_settings

from apps.rails import http


def resp(status=200, payload=None, bad_json=False):
    r = mock.Mock(status_code=status)
    if bad_json:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = payload
    return r


class HttpTests(SimpleTestCase):
    def test_http_disabled_under_test_runner_raises(self):
        with mock.patch("apps.rails.http.requests.get") as g:
            with self.assertRaises(RuntimeError):
                http.get_json("https://x.example/a", service="x")
            with self.assertRaises(RuntimeError):
                http.post_json("https://x.example/a", {}, service="x")
        g.assert_not_called()

    def test_redirect_is_not_followed(self):
        with override_settings(RAILS_HTTP_ENABLED=True):
            with mock.patch("apps.rails.http.requests.get", return_value=resp(302)) as g:
                with self.assertRaises(http.Unavailable) as cm:
                    http.get_json("https://x.example/a", headers={"X-API-Key": "k"}, service="x")
            self.assertEqual(cm.exception.kind, "redirect")
            self.assertIs(g.call_args.kwargs["allow_redirects"], False)
            with mock.patch("apps.rails.http.requests.post", return_value=resp(301)) as p:
                with self.assertRaises(http.Unavailable) as cm:
                    http.post_json("https://x.example/a", {}, service="x")
            self.assertEqual(cm.exception.kind, "redirect")
            self.assertIs(p.call_args.kwargs["allow_redirects"], False)

    def test_first_answer_falls_over_to_second_url(self):
        calls = []

        def call(url):
            calls.append(url)
            if url == "a":
                raise http.Unavailable("svc", "timeout")
            return url

        self.assertEqual(http.first_answer(["a", "b"], call), "b")
        self.assertEqual(calls, ["a", "b"])

    def test_first_answer_reraises_last_unavailable(self):
        def call(url):
            raise http.Unavailable("svc", url)

        with self.assertRaises(http.Unavailable) as cm:
            http.first_answer(["a", "b"], call)
        self.assertEqual(cm.exception.kind, "b")

    def test_first_answer_does_not_swallow_refused(self):
        def call(url):
            raise http.Refused("svc", 400)

        with self.assertRaises(http.Refused):
            http.first_answer(["a", "b"], call)

    def setUp(self):
        super().setUp()
        # the helper logs every failure (service and kind only); keep that out of the test output
        patcher = mock.patch("apps.rails.http.logger")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    @override_settings(RAILS_HTTP_ENABLED=True)
    def test_5xx_and_429_are_unavailable_4xx_is_refused(self):
        for status in (429, 500, 503):
            with mock.patch("apps.rails.http.requests.get", return_value=resp(status)):
                with self.assertRaises(http.Unavailable):
                    http.get_json("https://x.example/a", service="x")
        for status in (400, 403, 404):
            with mock.patch("apps.rails.http.requests.get", return_value=resp(status)):
                with self.assertRaises(http.Refused) as cm:
                    http.get_json("https://x.example/a", service="x")
            self.assertEqual(cm.exception.status, status)

    @override_settings(RAILS_HTTP_ENABLED=True)
    def test_ok_timeouts_connection_errors_and_bad_json(self):
        with mock.patch("apps.rails.http.requests.get", return_value=resp(200, {"a": 1})):
            self.assertEqual(http.get_json("https://x.example/a", service="x"), {"a": 1})
        with mock.patch("apps.rails.http.requests.post", return_value=resp(200, [1])) as p:
            self.assertEqual(http.post_json("https://x.example/a", {"q": 1}, service="x"), [1])
        self.assertEqual(p.call_args.kwargs["json"], {"q": 1})
        for exc, kind in ((requests.Timeout(), "timeout"), (requests.ConnectionError(), "connection")):
            with mock.patch("apps.rails.http.requests.get", side_effect=exc):
                with self.assertRaises(http.Unavailable) as cm:
                    http.get_json("https://x.example/a", service="x")
            self.assertEqual(cm.exception.kind, kind)
        with mock.patch("apps.rails.http.requests.get", return_value=resp(200, bad_json=True)):
            with self.assertRaises(http.Unavailable) as cm:
                http.get_json("https://x.example/a", service="x")
        self.assertEqual(cm.exception.kind, "invalid_json")

    @override_settings(RAILS_HTTP_ENABLED=True)
    def test_log_never_carries_url_or_headers(self):
        url = "https://x.example/a?apikey=SECRET"
        with mock.patch("apps.rails.http.requests.get", return_value=resp(500)):
            with self.assertRaises(http.Unavailable) as exc:
                http.get_json(url, headers={"X-Key": "SECRET"}, service="svc")
        text = repr(self.log.mock_calls) + str(exc.exception)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("x.example", text)
        self.assertIn("svc", text)
