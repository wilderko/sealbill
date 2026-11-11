"""MCP tools over the invoicing API: read, create invoices and payment links, send invoices. Nothing that moves money state."""
import os
import sys
from functools import wraps
from typing import Any

from mcp.server.mcpserver import MCPServer

from mcp_server.client import Api, ApiError, check_base_url

PRIVILEGED = ("payments:confirm", "admin")
# Capability links: whoever holds one sees (or pays) the invoice. Kept out of the agent's transcript
# unless the agent itself must deliver them.
CLIENT_LINKS = ("view_url", "pay_url")


def _reported(fn):
    """A failure is an answer the agent can read, not an exception."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ApiError as exc:
            return {"error": exc.error, "status": exc.status}

    return wrapper


def build(api: Api) -> MCPServer:
    mcp = MCPServer("pay-invoicing")

    def tool(fn):
        mcp.add_tool(_reported(fn))
        return fn

    @tool
    def list_invoices(status: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List invoices, newest first, optionally only those in one status."""
        params = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        return api.get("/api/v1/invoices", params)

    @tool
    def get_invoice(invoice_id: int) -> dict[str, Any]:
        """One invoice with its lines and state."""
        return api.get(f"/api/v1/invoices/{invoice_id}")

    @tool
    def create_invoice(contact_email: str, currency: str, lines: list[dict], *, idempotency_key: str,
                       kind: str = "invoice", due_days: int | None = None, title: str = "", notes: str = "",
                       contact: dict | None = None, external_reference: str | None = None) -> dict[str, Any]:
        """Create a draft invoice. Each line: name, unit_price_minor (integer cents), optional quantity
        (decimal string), tax_rate_bps, discount_rate_bps. `contact` adds details such as legal_name.
        Repeating the same idempotency_key returns the same invoice instead of a second one."""
        body = {"contact": {**(contact or {}), "email": contact_email}, "currency": currency, "lines": lines,
                "kind": kind, "title": title, "notes": notes}
        if due_days is not None:
            body["due_days"] = due_days
        if external_reference:
            body["external_reference"] = external_reference
        return api.post("/api/v1/invoices", body, idempotency_key=idempotency_key)

    @tool
    def send_invoice(invoice_id: int, email_client: bool = True) -> dict[str, Any]:
        """Issue a draft invoice (it gets its number and can no longer change) and optionally e-mail the client.
        The client's view and pay links are returned only with email_client=False, for you to pass on;
        otherwise they reach the client by e-mail and are left out of this answer."""
        answer = api.post(f"/api/v1/invoices/{invoice_id}/send", {"email_client": email_client})
        if email_client and isinstance(answer, dict):
            answer = {k: v for k, v in answer.items() if k not in CLIENT_LINKS}
        return answer

    @tool
    def list_payments(limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List recorded payments, newest first."""
        return api.get("/api/v1/payments", {"limit": limit, "offset": offset})

    @tool
    def get_payment_methods(currency: str, contact_email: str | None = None) -> dict[str, Any]:
        """What an invoice in this currency would offer, and which organization would issue it."""
        params = {"currency": currency}
        if contact_email:
            params["contact_email"] = contact_email
        return api.get("/api/v1/payment-methods", params)

    @tool
    def list_contacts(email: str | None = None) -> dict[str, Any]:
        """List contacts; with `email`, the contact holding exactly that address (zero or one)."""
        params = {"email": email} if email else None
        return api.get("/api/v1/contacts", params)

    @tool
    def list_quotes(invoice_id: int) -> dict[str, Any]:
        """Crypto payment quotes already asked for on an invoice."""
        return api.get(f"/api/v1/invoices/{invoice_id}/quotes")

    @tool
    def list_payment_links(limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List payment links, newest first. The listing does not carry a link's URL (that is a capability:
        anyone who has it can buy); create_payment_link hands one out when it is made."""
        answer = api.get("/api/v1/payment-links", {"limit": limit, "offset": offset})
        if isinstance(answer, dict):
            answer = {**answer, "results": [{k: v for k, v in row.items() if k != "url"}
                                            for row in answer.get("results", []) if isinstance(row, dict)]}
        return answer

    @tool
    def create_payment_link(title: str, currency: str, price_minor: int | None = None, *, description: str | None = None,
                            customer_chooses: bool | None = None, min_minor: int | None = None,
                            max_minor: int | None = None, tax_rate_bps: int | None = None,
                            max_quantity: int | None = None, kind: str | None = None,
                            max_uses: int | None = None, valid_until: str | None = None) -> dict[str, Any]:
        """Create a payment link: one public page that many buyers can pay through. Price in integer minor
        units (cents), or customer_chooses=true with optional min_minor/max_minor. kind is "invoice" or
        "payment_request". The answer includes the link's URL, because sharing it is the point: pass it
        on to whoever should have it and nowhere else."""
        body: dict[str, Any] = {"title": title, "currency": currency}
        for name, value in (("price_minor", price_minor), ("description", description),
                            ("customer_chooses", customer_chooses), ("min_minor", min_minor),
                            ("max_minor", max_minor), ("tax_rate_bps", tax_rate_bps),
                            ("max_quantity", max_quantity), ("kind", kind), ("max_uses", max_uses),
                            ("valid_until", valid_until)):
            if value is not None:
                body[name] = value
        return api.post("/api/v1/payment-links", body)

    @tool
    def deactivate_payment_link(link_id: int) -> dict[str, Any]:
        """Turn a payment link off: its page then says it is no longer available. Orders already made are
        untouched. The answer leaves the link's URL out."""
        answer = api.patch(f"/api/v1/payment-links/{link_id}", {"active": False})
        if isinstance(answer, dict):
            answer = {k: v for k, v in answer.items() if k != "url"}
        return answer

    return mcp


def _refuse(message: str):
    print(f"mcp_server: {message}", file=sys.stderr)
    raise SystemExit(2)


def main():
    url = os.environ.get("PAY_API_URL", "").strip()
    token = os.environ.get("PAY_API_TOKEN", "").strip()
    if not url or not token:
        _refuse("PAY_API_URL and PAY_API_TOKEN must both be set.")
    try:
        check_base_url(url)
    except ValueError as exc:
        _refuse(f"{exc}; the token is never sent in the clear.")
    api = Api(url, token)
    try:
        me = api.get("/api/v1/me")
    except ApiError as exc:
        _refuse(f"cannot read /api/v1/me ({exc.status} {exc.error}); check PAY_API_URL and the token.")
    if not isinstance(me, dict) or not isinstance(me.get("scopes"), list):
        _refuse("unexpected /api/v1/me response; refusing to start without knowing the token's scopes.")
    scopes = me["scopes"]
    bad = [s for s in PRIVILEGED if s in scopes]
    if bad:
        _refuse(f"this token holds {', '.join(bad)}; issue one with only read and invoices:write.")
    build(api).run("stdio")
