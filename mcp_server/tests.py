"""No Django, no network: `client.Api` is replaced or `requests` is patched.

The `mcp` package is an optional dependency group (`uv sync --group mcp`; the dev group includes it).
Without it the tests of the server itself are skipped and the HTTP client's still run."""
import asyncio
import importlib.util
import io
import os
import unittest
from unittest import mock

from mcp_server import client

HAVE_MCP = importlib.util.find_spec("mcp") is not None
if HAVE_MCP:
    from mcp_server import server
needs_mcp = unittest.skipUnless(HAVE_MCP, "the mcp package is not installed (uv sync --group mcp)")

ALLOWED = {"list_invoices", "get_invoice", "create_invoice", "send_invoice", "list_payments",
           "get_payment_methods", "list_contacts", "list_quotes", "list_payment_links", "create_payment_link",
           "deactivate_payment_link"}
FORBIDDEN = ("confirm", "reject", "reverse", "assign", "refund", "void", "webhook")


NOTSET = object()


class FakeApi:
    def __init__(self, get_result=NOTSET, post_result=None, error=None):
        self.calls = []
        self.get_result = {"results": []} if get_result is NOTSET else get_result
        self.post_result = post_result if post_result is not None else {"id": 1}
        self.error = error

    def get(self, path, params=None):
        self.calls.append(("get", path, params))
        if self.error:
            raise self.error
        return self.get_result

    def post(self, path, json, idempotency_key=None):
        self.calls.append(("post", path, json, idempotency_key))
        if self.error:
            raise self.error
        return self.post_result

    def patch(self, path, json):
        self.calls.append(("patch", path, json))
        if self.error:
            raise self.error
        return self.post_result


def call(srv, name, args):
    result = asyncio.run(srv.call_tool(name, args))
    return result.structured_content


@needs_mcp
class ToolTests(unittest.TestCase):
    def test_tool_names_are_exactly_the_allowed_set(self):
        tools = asyncio.run(server.build(FakeApi()).list_tools())
        self.assertEqual({t.name for t in tools}, ALLOWED)

    def test_no_money_moving_tool_exists(self):
        for t in asyncio.run(server.build(FakeApi()).list_tools()):
            text = f"{t.name} {t.description or ''}".lower()
            for word in FORBIDDEN:
                self.assertNotIn(word, text, t.name)

    def test_create_invoice_sends_idempotency_key(self):
        api = FakeApi(post_result={"id": 7, "status": "draft"})
        out = call(server.build(api), "create_invoice", {
            "contact_email": "a@example.com", "currency": "EUR", "idempotency_key": "k-1",
            "lines": [{"name": "x", "unit_price_minor": 100}], "due_days": 14})
        self.assertEqual(out["id"], 7)
        _, path, body, key = api.calls[0]
        self.assertEqual(path, "/api/v1/invoices")
        self.assertEqual(key, "k-1")
        self.assertEqual(body["contact"], {"email": "a@example.com"})
        self.assertEqual(body["due_days"], 14)
        self.assertNotIn("idempotency_key", body)

    def test_create_invoice_requires_idempotency_key(self):
        with self.assertRaises(Exception):
            call(server.build(FakeApi()), "create_invoice", {
                "contact_email": "a@example.com", "currency": "EUR", "lines": [{"name": "x", "unit_price_minor": 1}]})

    def test_api_error_is_reported_not_raised(self):
        api = FakeApi(error=client.ApiError(409, "invoice_error"))
        out = call(server.build(api), "send_invoice", {"invoice_id": 3})
        self.assertEqual(out, {"error": "invoice_error", "status": 409})

    def test_send_and_reads_use_the_right_paths(self):
        api = FakeApi()
        srv = server.build(api)
        call(srv, "send_invoice", {"invoice_id": 3, "email_client": False})
        call(srv, "list_quotes", {"invoice_id": 3})
        call(srv, "get_payment_methods", {"currency": "EUR"})
        self.assertEqual(api.calls[0][:3], ("post", "/api/v1/invoices/3/send", {"email_client": False}))
        self.assertEqual(api.calls[1][1], "/api/v1/invoices/3/quotes")
        self.assertEqual(api.calls[2][1:], ("/api/v1/payment-methods", {"currency": "EUR"}))


    def test_send_invoice_keeps_the_client_links_out_of_the_transcript(self):
        # The client gets them by e-mail; anyone who reads the agent's transcript must not.
        sent = {"id": 3, "status": "sent", "number": "2026-0001",
                "view_url": "https://pay.example/i/VIEW-SECRET/", "pay_url": "https://pay.example/p/PAY-SECRET/"}
        api = FakeApi(post_result=dict(sent))
        out = call(server.build(api), "send_invoice", {"invoice_id": 3})
        self.assertEqual(out, {"id": 3, "status": "sent", "number": "2026-0001"})
        self.assertNotIn("SECRET", repr(out))
        self.assertEqual(api.calls[0][2], {"email_client": True})
        # Not e-mailed: the agent has to pass the links on, so they are the point of the answer.
        out = call(server.build(FakeApi(post_result=dict(sent))), "send_invoice",
                   {"invoice_id": 3, "email_client": False})
        self.assertEqual(out, sent)

    def test_payment_link_tools(self):
        api = FakeApi(get_result={"count": 1, "results": [{"id": 4, "title": "T", "url": "https://pay.example/l/SECRET/"}]},
                      post_result={"id": 4, "url": "https://pay.example/l/SECRET/", "active": True})
        srv = server.build(api)
        out = call(srv, "list_payment_links", {"limit": 10})
        self.assertEqual(api.calls[0], ("get", "/api/v1/payment-links", {"limit": 10, "offset": 0}))
        self.assertNotIn("SECRET", repr(out))  # a listing never puts a link into the transcript
        out = call(srv, "create_payment_link", {"title": "T", "currency": "EUR", "price_minor": 500, "max_uses": 5})
        self.assertEqual(api.calls[1][:2], ("post", "/api/v1/payment-links"))
        self.assertEqual(api.calls[1][2], {"title": "T", "currency": "EUR", "price_minor": 500, "max_uses": 5})
        self.assertIn("SECRET", repr(out))  # sharing is its purpose: the URL comes back
        post_result = {"id": 4, "url": "https://pay.example/l/SECRET/", "active": False}
        api.post_result = post_result
        out = call(srv, "deactivate_payment_link", {"link_id": 4})
        self.assertEqual(api.calls[2], ("patch", "/api/v1/payment-links/4", {"active": False}))
        self.assertNotIn("SECRET", repr(out))
        self.assertEqual(out["active"], False)

    def test_only_the_create_tool_says_it_returns_the_url(self):
        tools = {t.name: (t.description or "").lower() for t in asyncio.run(server.build(FakeApi()).list_tools())}
        self.assertIn("url", tools["create_payment_link"])
        self.assertIn("does not carry", tools["list_payment_links"])
        self.assertIn("leaves the link's url out", tools["deactivate_payment_link"])

    def test_list_contacts_sends_email_as_a_query_parameter(self):
        api = FakeApi()
        call(server.build(api), "list_contacts", {"email": "a@example.com"})
        self.assertEqual(api.calls, [("get", "/api/v1/contacts", {"email": "a@example.com"})])


class ClientTests(unittest.TestCase):

    def test_the_client_refuses_plain_http_off_loopback(self):
        for url in ("http://pay.example", "ftp://pay.example", "pay.example"):
            with self.subTest(url), self.assertRaises(ValueError):
                client.Api(url, "t")
        for url in ("https://pay.example/", "http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"):
            client.Api(url, "t")
    def response(self, status=200, body=None, reason="OK"):
        r = mock.Mock(status_code=status, reason=reason)
        r.json.return_value = body if body is not None else {}
        return r

    def test_request_shape_and_token_only_in_header(self):
        with mock.patch("mcp_server.client.requests.request", return_value=self.response(body={"ok": 1})) as req:
            api = client.Api("https://pay.example/", "SECRET")
            self.assertEqual(api.post("/api/v1/invoices", {"a": 1}, idempotency_key="k"), {"ok": 1})
        args, kw = req.call_args
        self.assertEqual(args, ("POST", "https://pay.example/api/v1/invoices"))
        self.assertEqual(kw["timeout"], 15)
        self.assertIs(kw["allow_redirects"], False)
        self.assertEqual(kw["headers"]["Authorization"], "Bearer SECRET")
        self.assertEqual(kw["headers"]["Idempotency-Key"], "k")
        self.assertNotIn("SECRET", repr({k: v for k, v in kw.items() if k != "headers"}))

    def test_non_2xx_raises_api_error_with_api_error_field(self):
        with mock.patch("mcp_server.client.requests.request", return_value=self.response(403, {"error": "forbidden"})):
            with self.assertRaises(client.ApiError) as cm:
                client.Api("https://x", "t").get("/api/v1/me")
        self.assertEqual((cm.exception.status, cm.exception.error), (403, "forbidden"))

    def test_patch_sends_json(self):
        with mock.patch("mcp_server.client.requests.request", return_value=self.response(body={"ok": 1})) as req:
            self.assertEqual(client.Api("https://pay.example/", "S").patch("/api/v1/payment-links/1", {"active": False}),
                             {"ok": 1})
        args, kw = req.call_args
        self.assertEqual(args, ("PATCH", "https://pay.example/api/v1/payment-links/1"))
        self.assertEqual(kw["json"], {"active": False})

    def test_redirect_is_an_error(self):
        with mock.patch("mcp_server.client.requests.request", return_value=self.response(302, None, "Found")):
            with self.assertRaises(client.ApiError) as cm:
                client.Api("https://x", "t").get("/api/v1/me")
        self.assertEqual(cm.exception.status, 302)


@needs_mcp
class MainTests(unittest.TestCase):
    def run_main(self, env, api=None):
        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stderr", err), \
                mock.patch("mcp_server.server.Api", return_value=api) as ctor, \
                mock.patch("mcp_server.server.build") as build:
            with self.assertRaises(SystemExit) as cm:
                server.main()
        return cm.exception.code, err.getvalue(), build, ctor

    def test_refuses_token_with_payments_confirm_or_admin(self):
        for scope in ("payments:confirm", "admin"):
            api = FakeApi(get_result={"organization": "eu", "token": "t", "scopes": ["read", scope]})
            code, err, build, _ = self.run_main({"PAY_API_URL": "https://x", "PAY_API_TOKEN": "SECRET-TOKEN"}, api)
            self.assertEqual(code, 2)
            self.assertIn(scope, err)
            self.assertNotIn("SECRET-TOKEN", err)
            build.assert_not_called()

    def test_refuses_a_malformed_me_answer(self):
        for body in ([], {}, None, {"scopes": "admin"}):
            api = FakeApi(get_result=body)
            code, err, build, _ = self.run_main({"PAY_API_URL": "https://x", "PAY_API_TOKEN": "SECRET-TOKEN"}, api)
            self.assertEqual(code, 2, body)
            self.assertIn("unexpected /api/v1/me response", err)
            self.assertNotIn("SECRET-TOKEN", err)
            build.assert_not_called()

    def test_refuses_without_configuration_or_when_me_unreachable(self):
        code, err, _, _ = self.run_main({"PAY_API_URL": "https://x"})
        self.assertEqual(code, 2)
        code, err, _, _ = self.run_main({"PAY_API_TOKEN": "SECRET-TOKEN"})
        self.assertEqual(code, 2)
        api = FakeApi(error=client.ApiError(0, "unreachable"))
        code, err, build, _ = self.run_main({"PAY_API_URL": "https://x", "PAY_API_TOKEN": "SECRET-TOKEN"}, api)
        self.assertEqual(code, 2)
        self.assertNotIn("SECRET-TOKEN", err)
        build.assert_not_called()

    def test_refuses_to_send_the_token_over_plain_http(self):
        # Audit 2026-11-11: the token went to any PAY_API_URL, http:// included.
        code, err, build, ctor = self.run_main({"PAY_API_URL": "http://pay.example", "PAY_API_TOKEN": "SECRET-TOKEN"})
        self.assertEqual(code, 2)
        self.assertIn("https", err)
        self.assertNotIn("SECRET-TOKEN", err)
        ctor.assert_not_called()
        build.assert_not_called()

    def test_starts_with_safe_scopes(self):
        api = FakeApi(get_result={"organization": "eu", "token": "t", "scopes": ["read", "invoices:write"]})
        with mock.patch.dict(os.environ, {"PAY_API_URL": "https://x", "PAY_API_TOKEN": "t"}, clear=True), \
                mock.patch("mcp_server.server.Api", return_value=api), \
                mock.patch("mcp_server.server.build") as build:
            server.main()
        build.return_value.run.assert_called_once_with("stdio")


if __name__ == "__main__":
    unittest.main()
