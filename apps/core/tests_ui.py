"""The interface (Task 9): CSP-clean templates, the theme cookie and its switcher, the issuer's identity
on client pages, and the Needs a person page folding what is fine into one line."""
import io
import re
import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings

from apps.contacts.services import find_or_create_contact
from apps.core import theme
from apps.invoices.services import create_invoice, send_invoice
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.staff.tests import StaffBase
from apps.vault.testing import UnsealedVaultMixin
from apps.vault.vault import vault

TEMPLATES = Path(settings.BASE_DIR) / "templates"
# Pages a browser renders under the Content-Security-Policy. E-mail bodies (mail clients need inline
# styles) and the PDF templates (rendered offline by WeasyPrint) are not served as web pages.
WEB_TEMPLATE_DIRS = ("staff", "portal", "keys", "vault", "ui", "drf_spectacular")
INLINE_STYLE = re.compile(r"\sstyle\s*=", re.I)
INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=", re.I)
SCRIPT_TAG = re.compile(r"<script\b[^>]*>", re.I)


def html_tag(page: str) -> str:
    return re.search(r"<html\b[^>]*>", page).group(0)


def png_bytes() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (4, 2), (20, 40, 60)).save(out, format="PNG")
    return out.getvalue()


class TemplateHygieneTests(TestCase):
    def test_no_inline_style_or_handler_attributes_in_any_template(self):
        checked = 0
        for folder in WEB_TEMPLATE_DIRS:
            for path in sorted((TEMPLATES / folder).rglob("*.html")):
                text = path.read_text(encoding="utf-8")
                name = str(path.relative_to(TEMPLATES))
                self.assertIsNone(INLINE_STYLE.search(text), f"{name}: a style attribute")
                self.assertIsNone(INLINE_HANDLER.search(text), f"{name}: an on* attribute")
                for tag in SCRIPT_TAG.findall(text):
                    # An inline script runs only with the response's nonce; a script file comes from 'self'.
                    self.assertTrue("src=" in tag or 'nonce="{{ csp_nonce }}"' in tag, f"{name}: {tag}")
                checked += 1
        self.assertGreater(checked, 30)


class ThemeBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1")
        add_bank(self.org)
        contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="Ján", preferred_language="en")
        inv, _ = create_invoice(self.org, contact, currency="EUR",
                                lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 10000}])
        self.invoice, self.view, self.pay = send_invoice(inv)


class ThemeCookieTests(ThemeBase):
    def pages(self):
        return ("/login/", "/unlock/", f"/i/{self.view}/", f"/p/{self.pay}/", f"/i/{self.view}/key/")

    def test_theme_cookie_renders_data_theme(self):
        for value in ("black", "white", "dim"):
            client = Client()
            client.cookies[theme.COOKIE] = value
            for url in self.pages():
                page = client.get(url).content.decode()
                self.assertIn(f'data-theme="{value}"', html_tag(page), (value, url))

    def test_absent_or_bogus_cookie_renders_no_theme_attribute(self):
        for cookie in (None, "", "neon", "BLACK", '"><script>'):
            client = Client()
            if cookie is not None:
                client.cookies[theme.COOKIE] = cookie
            for url in self.pages():
                page = client.get(url).content.decode()
                self.assertNotIn("data-theme", html_tag(page), (cookie, url))

    def test_sealed_client_page_carries_the_theme_too(self):
        vault.seal()
        self.client.cookies[theme.COOKIE] = "dim"
        r = self.client.get(f"/p/{self.pay}/")
        self.assertEqual(r.status_code, 503)
        self.assertIn('data-theme="dim"', html_tag(r.content.decode()))


class ThemeSwitchTests(ThemeBase):
    def post(self, client=None, **data):
        return (client or self.client).post("/theme/", data)

    def test_theme_post_sets_cookie_and_redirects_same_origin_only(self):
        back = f"/p/{self.pay}/?lang=de"
        for value in ("black", "white", "dim"):
            r = self.post(theme=value, next=back)
            self.assertEqual(r.status_code, 302)
            self.assertEqual(r["Location"], back)
            cookie = r.cookies[theme.COOKIE]
            self.assertEqual(cookie.value, value)
            self.assertEqual(cookie["path"], "/")
            self.assertEqual(cookie["samesite"], "Lax")
            self.assertGreater(int(cookie["max-age"]), 30 * 24 * 3600)
        for elsewhere in ("https://evil.example/", "//evil.example/x", "/\\evil.example", "http:/evil.example",
                          "javascript:alert(1)", "evil.example", ""):
            r = self.post(theme="dim", next=elsewhere)
            self.assertEqual(r.status_code, 302, elsewhere)
            self.assertTrue(r["Location"].startswith("/"), (elsewhere, r["Location"]))
            self.assertFalse(r["Location"].startswith(("//", "/\\")), (elsewhere, r["Location"]))
            self.assertNotIn("evil", r["Location"])

    def test_auto_forgets_the_choice(self):
        r = self.post(theme="auto", next="/login/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.cookies[theme.COOKIE].value, "")
        self.assertEqual(int(r.cookies[theme.COOKIE]["max-age"]), 0)

    def test_unknown_theme_is_refused_and_sets_nothing(self):
        for value in ("neon", "", "BLACK"):
            r = self.post(theme=value, next="/login/")
            self.assertEqual(r.status_code, 400)
            self.assertNotIn(theme.COOKIE, r.cookies)

    def test_theme_post_needs_csrf_and_post(self):
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(self.post(strict, theme="dim", next="/login/").status_code, 403)
        self.assertEqual(self.client.get("/theme/").status_code, 405)

    def test_theme_switch_works_while_the_vault_is_sealed(self):
        vault.seal()
        r = self.post(theme="black", next="/unlock/")
        self.assertEqual((r.status_code, r["Location"]), (302, "/unlock/"))
        self.assertEqual(r.cookies[theme.COOKIE].value, "black")

    def test_switcher_is_a_plain_form_on_client_pages(self):
        for url in (f"/i/{self.view}/", f"/p/{self.pay}/", "/unlock/"):
            page = self.client.get(url).content.decode()
            form = re.search(r'<form[^>]*action="/theme/"[^>]*>.*?</form>', page, re.S)
            self.assertIsNotNone(form, url)
            self.assertIn('method="post"', form.group(0))
            self.assertIn("csrfmiddlewaretoken", form.group(0))
            self.assertIn(f'name="next" value="{url}"', form.group(0))
            for value in ("auto", "white", "dim", "black"):
                self.assertIn(f'name="theme" value="{value}"', form.group(0), url)

    def test_switcher_follows_the_page_language(self):
        page = self.client.get(f"/p/{self.pay}/?lang=sk").content.decode()
        form = re.search(r'<form[^>]*action="/theme/"[^>]*>.*?</form>', page, re.S).group(0)
        self.assertNotIn(">Dark<", form)
        self.assertIn("Tmavý", form)


class StaffThemeAndNavTests(StaffBase):
    def test_staff_navigation_carries_the_switcher_and_the_open_count(self):
        from apps.payments.models import Declaration

        Declaration.objects.create(invoice=self.invoice, network="arbitrum", tx_hash="0xabc")
        self.sign_in()
        page = self.client.get("/invoices/").content.decode()
        nav = re.search(r'<nav class="rail-nav".*?</nav>', page, re.S).group(0)
        self.assertRegex(nav, r'href="/"[^>]*>.*?Needs a person.*?<span class="count[^"]*"[^>]*>1<span class="sr-only"> open</span></span>')
        form = re.search(r'<form[^>]*action="/theme/"[^>]*>.*?</form>', page, re.S).group(0)
        self.assertIn('name="theme" value="dim"', form)
        self.assertIn(">Dark<", form)  # staff pages are English whatever the browser asks for

    def test_needs_person_folds_empty_sections_into_one_line(self):
        self.sign_in()
        page = self.client.get("/").content.decode()
        clear = re.search(r'<details class="all-clear".*?</details>', page, re.S).group(0)
        for title in ("Declared payments", "Refunds due", "E-mail not delivered", "Webhooks not delivered"):
            self.assertIn(title, clear)
        self.assertNotIn('<section class="queue"', page)  # nothing is open: no section stands alone

    def test_needs_person_header_counts_what_is_open(self):
        from apps.payments.models import Declaration

        Declaration.objects.create(invoice=self.invoice, network="arbitrum", tx_hash="0xabc")
        self.sign_in()
        page = self.client.get("/").content.decode()
        header = re.search(r'<header class="page-head".*?</header>', page, re.S).group(0)
        self.assertIn("1 open", header)
        queue = re.search(r'<section class="queue" id="declarations".*?</section>', page, re.S).group(0)
        self.assertIn("Declared payments", queue)
        clear = re.search(r'<details class="all-clear".*?</details>', page, re.S).group(0)
        self.assertNotIn("Declared payments", clear)


class ClientIdentityTests(ThemeBase):
    def setUp(self):
        super().setUp()
        self.media = tempfile.mkdtemp()
        o = override_settings(MEDIA_ROOT=self.media)
        o.enable()
        self.addCleanup(o.disable)
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)

    def head(self, url) -> str:
        page = self.client.get(url).content.decode()
        return re.search(r'<header class="client-head".*?</header>', page, re.S).group(0)

    def urls(self):
        return (f"/i/{self.view}/", f"/p/{self.pay}/", f"/i/{self.view}/key/")

    def test_client_pages_show_the_issuing_org_logo_and_name(self):
        self.org.logo.save("logo.png", ContentFile(png_bytes()))
        for url in self.urls():
            head = self.head(url)
            self.assertRegex(head, r'<img class="org-logo" src="data:image/png;base64,[A-Za-z0-9+/=]+"', url)
            self.assertIn('<span class="org-name">Seller s.r.o.</span>', head, url)

    def test_no_logo_element_when_the_org_has_none(self):
        for url in self.urls():
            head = self.head(url)
            self.assertNotIn("<img", head, url)
            self.assertIn('<span class="org-name">Seller s.r.o.</span>', head, url)


# ---------------------------------------------------------------------------------------------------
# Fix round 1 (finish review): phone rows, one action per row, quiet all-clear, cached rail counts,
# status tabs, human names, and the client pay details.

def section(page: str, queue_id: str) -> str:
    return re.search(rf'<section class="queue" id="{queue_id}".*?</section>', page, re.S).group(0)


class StaffFixRoundTests(StaffBase):
    def review_payment(self, **extra):
        from apps.payments.models import Payment

        return Payment.objects.create(invoice=self.invoice, rail="evm", network="arbitrum", asset="USDC",
                                      amount_minor=10000, received_amount="108.27", amount_base_units="108270000",
                                      state="needs_review", review_reason="after_expiry", final=True,
                                      tx_hash="0x" + "ab" * 32, **extra)

    def test_each_row_has_one_note_and_one_filled_primary_action(self):
        from apps.notify.models import OutboxMessage
        from apps.payments.models import Payment

        p = self.review_payment()
        xmr = Payment.objects.create(invoice=self.invoice, rail="manual", asset="XMR", amount_minor=100,
                                     received_amount="1", state="detected", needs_staff_confirmation=True)
        OutboxMessage.objects.create(kind="invoice", state="failed", attempts=12, last_error="SMTP", invoice=self.invoice)
        self.sign_in()
        page = self.client.get("/").content.decode()
        for queue, pay in (("reviews", p), ("awaiting", xmr)):
            row = section(page, queue)
            confirm = re.search(rf'<form method="post" action="/payments/{pay.pk}/confirm/".*?</form>', row, re.S).group(0)
            self.assertEqual(confirm.count('name="note"'), 1, queue)
            self.assertNotIn("formaction", confirm, queue)  # C1: Reject is a form of its own, never in Confirm's
            reject = re.search(rf'<details class="row-menu">.*?<form method="post" action="/payments/{pay.pk}/reject/"'
                               r'.*?</form>', row, re.S).group(0)
            self.assertEqual(reject.count('name="note"'), 1, queue)
            self.assertEqual(row.count("btn--primary"), 1, queue)
        outbox = section(page, "undelivered")
        self.assertEqual(outbox.count("btn--primary"), 1)
        self.assertNotIn("btn--danger\"", outbox.split("row-menu")[0])  # Dismiss lives in the row menu

    def test_reject_from_its_own_form_posts_what_the_view_expects(self):
        p = self.review_payment()
        self.sign_in()
        r = self.client.post(f"/payments/{p.pk}/reject/", {"note": "not ours"})
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.state, "rejected")

    def test_phone_rows_name_their_cells(self):
        self.review_payment()
        self.sign_in()
        row = section(self.client.get("/").content.decode(), "reviews")
        self.assertRegex(row, r'<tr>\s*<td><a class="strong-link"')  # the first cell heads the row on a phone
        self.assertIn('class="num cell-amount"', row)
        self.assertIn('data-label="Network"', row)
        self.assertIn('class="actions-cell"', row)

    def test_empty_needs_person_is_one_quiet_line(self):
        self.sign_in()
        page = self.client.get("/").content.decode()
        self.assertNotIn("clear-state", page)
        self.assertNotIn("Nothing open", page)
        details = re.search(r'<details class="all-clear"[^>]*>', page).group(0)
        self.assertNotIn("open", details.replace('class="all-clear"', ""))
        self.assertIn("Show 17 queues", page)  # sixteen, and the payment links queue

    def test_rail_counts_are_cached_for_twenty_seconds_and_fresh_on_needs_person(self):
        from apps.payments.models import Declaration
        from apps.staff import context

        self.assertEqual(context.NAV_COUNTS_TTL, 20)
        self.sign_in()
        self.client.get("/invoices/")  # caches 0 open
        Declaration.objects.create(invoice=self.invoice, network="arbitrum", tx_hash="0xabc")
        nav = re.search(r'<nav class="rail-nav".*?</nav>', self.client.get("/invoices/").content.decode(), re.S).group(0)
        self.assertNotIn('class="count count--attention"', nav)  # still the cached count
        page = self.client.get("/").content.decode()  # the page itself counts afresh, and says so in the rail
        nav = re.search(r'<nav class="rail-nav".*?</nav>', page, re.S).group(0)
        self.assertIn('>1<span class="sr-only"> open</span>', nav)
        nav = re.search(r'<nav class="rail-nav".*?</nav>', self.client.get("/invoices/").content.decode(), re.S).group(0)
        self.assertIn('>1<span class="sr-only"> open</span>', nav)

    def test_needs_person_never_extends_a_stale_invoice_count(self):
        from django.core.cache import cache

        from apps.staff import context

        cache.set(context.NAV_COUNTS_KEY, {"needs_person": 0, "invoices": 99}, context.NAV_COUNTS_TTL)
        self.sign_in()
        self.client.get("/")  # counts its own queues afresh and stores them for the next page
        nav = re.search(r'<nav class="rail-nav".*?</nav>', self.client.get("/invoices/").content.decode(), re.S).group(0)
        self.assertNotIn(">99<", nav)
        self.assertIn(">1 unpaid</span>", nav)

    def test_invoice_list_has_status_tabs_on_the_same_parameter(self):
        self.sign_in()
        page = self.client.get("/invoices/?status=sent").content.decode()
        self.assertNotIn('<select name="status"', page)
        tabs = re.search(r'<nav class="status-tabs".*?</nav>', page, re.S).group(0)
        self.assertRegex(tabs, r'href="\?status=sent"[^>]*aria-current="page"[^>]*>Sent <span class="count[^"]*">1</span>')
        self.assertIn('href="?status=draft"', tabs)
        self.assertIn('href="?"', tabs)  # All

    def test_staff_pages_name_organisations_and_drafts_in_words(self):
        draft, _ = create_invoice(self.org, self.contact, currency="EUR",
                                  lines=[{"name": "Later", "quantity": 1, "unit_price_minor": 100}])
        self.sign_in()
        listed = self.client.get("/invoices/").content.decode()
        self.assertIn("Seller s.r.o.", listed)
        self.assertNotIn('<td data-label="Organization">eu</td>', listed)
        self.assertIn(f"Draft #{draft.pk}", listed)
        detail = self.client.get(f"/invoices/{self.invoice.pk}/").content.decode()
        self.assertIn("Seller s.r.o.", re.search(r'<header class="page-head".*?</header>', detail, re.S).group(0))
        self.assertIn('title="invoice.send"', detail)  # the code stays for searching, the words are shown
        self.assertNotIn("invoices.invoice #", detail)

    def test_forms_use_human_labels(self):
        self.sign_in()
        new = self.client.get("/invoices/new/").content.decode()
        for label in ("Address line 1", "Postal code", "Tax IDs", ">Type<", "Payment request"):
            self.assertIn(label, new)
        detail = self.client.get(f"/invoices/{self.invoice.pk}/").content.decode()
        self.assertIn('<option value="bank">Bank transfer</option>', detail)


class ClientFixRoundTests(ThemeBase):
    def test_pay_page_names_the_invoice_in_the_header_and_copies_the_bank_amount(self):
        page = self.client.get(f"/p/{self.pay}/").content.decode()
        head = re.search(r'<header class="client-head".*?</header>', page, re.S).group(0)
        self.assertIn(f"Invoice {self.invoice.number}", head)
        self.assertNotIn("due-hero__doc", page)
        self.assertIn('data-copy="100.00"', page)  # the bank amount, as a plain number

    def test_declare_network_has_a_translated_empty_choice(self):
        page = self.client.get(f"/p/{self.pay}/?lang=sk").content.decode()
        self.assertIn('<option value="" selected>Vyberte sieť</option>', page)

    def test_key_upload_file_picker_speaks_the_page_language(self):
        page = self.client.get(f"/i/{self.view}/key/?lang=sk").content.decode()
        self.assertIn("Vybrať súbor", page)
        self.assertIn("Nie je vybraný žiadny súbor", page)


from apps.portal.tests_crypto import CryptoBase  # noqa: E402


class QuoteFixRoundTests(CryptoBase):
    def test_payment_uri_is_an_open_in_wallet_link_and_the_date_matches_the_page(self):
        from apps.quotes.models import Quote

        body = self.ask(HTTP_HX_REQUEST="true").content.decode()
        q = Quote.objects.get()
        import html as _html
        self.assertRegex(body, rf'<a class="[^"]*" href="{re.escape(_html.escape(q.payment_uri))}"[^>]*>[^<]*Open in wallet')
        self.assertNotIn('class="hint mono quote-uri"', body)
        from django.utils import dateformat, formats
        shown = dateformat.format(q.expires_at, formats.get_format("DATE_FORMAT"))
        self.assertIn(shown, body)


# ---------------------------------------------------------------------------------------------------
# Fix round 2: the wallet link's scheme, staff names for networks and watchers, empty states, the rail.

class WalletLinkTests(CryptoBase):
    def test_a_gateway_link_that_is_not_a_wallet_scheme_is_never_a_link(self):
        from apps.quotes.models import Quote

        self.ask(HTTP_HX_REQUEST="true")
        for bad in ("javascript:alert(document.cookie)", "JaVaScRiPt:alert(1)", " javascript:alert(1)",
                    "data:text/html,<script>alert(1)</script>", "vbscript:msgbox(1)", "https://evil.example/x"):
            Quote.objects.update(payment_uri=bad, checkout_url="javascript:alert(2)")
            body = self.ask(HTTP_HX_REQUEST="true").content.decode()
            self.assertNotIn("Open in wallet", body, bad)
            self.assertNotIn('href="javascript', body.lower(), bad)
            self.assertNotIn('href="data:', body.lower(), bad)
            self.assertNotIn('href=" javascript', body.lower(), bad)
            self.assertNotIn("Open the payment page", body, bad)  # a checkout link must be a web address

    def test_every_wallet_scheme_the_rails_emit_is_a_link(self):
        from apps.portal.views import wallet_link

        for uri in ("ethereum:0xab@42161/transfer?address=0xcd&uint256=1", "solana:Abc?amount=1&spl-token=X",
                    "ton://transfer/EQx?jetton=Y&amount=1", "bitcoin:bc1q?amount=0.1", "lightning:lnbc1",
                    "monero:44Ab?tx_amount=1", "zcash:t1Ab?amount=1", "BITCOIN:BC1Q?amount=0.1"):
            self.assertEqual(wallet_link(uri), uri, uri)
        for uri in ("", "javascript:alert(1)", "data:x", "file:///etc/passwd", "ethereum", "http://x"):
            self.assertEqual(wallet_link(uri), "", uri)


class StaffNamesRoundTwoTests(StaffBase):
    def test_queues_name_networks_rails_and_watchers_in_words(self):
        from apps.payments.models import Declaration, Payment
        from apps.rails import health
        from apps.rails.models import RailAccount

        Payment.objects.create(invoice=self.invoice, rail="evm", network="arbitrum", asset="USDC", amount_minor=100,
                               received_amount="1.08", amount_base_units="1080000", state="needs_review",
                               review_reason="after_expiry", final=True, tx_hash="0x" + "ab" * 32)
        Payment.objects.create(invoice=self.invoice, rail="manual", asset="EUR", amount_minor=100,
                               received_amount="1", state="detected")
        Declaration.objects.create(invoice=self.invoice, network="monero", tx_hash="")
        account = RailAccount.objects.create(org=self.org, kind="evm", label="Stablecoins", address="0x" + "cd" * 20,
                                             networks=["ethereum"], assets=["USDC"])
        health.failed(f"scan:evm:ethereum:{account.pk}", "timeout")
        self.sign_in()
        page = self.client.get("/").content.decode()
        self.assertIn("Arbitrum One", section(page, "reviews"))
        self.assertNotIn(">arbitrum<", section(page, "reviews"))
        self.assertIn("Recorded by hand", section(page, "awaiting"))
        self.assertIn(">Monero<", section(page, "declarations"))
        watchers = section(page, "watchers")
        self.assertIn("Ethereum, transfers to “Stablecoins”", watchers)
        self.assertIn(f'<span class="mono muted">scan:evm:ethereum:{account.pk}</span>', watchers)

    def test_invoice_detail_empty_sections_share_one_wording(self):
        self.sign_in()
        page = self.client.get(f"/invoices/{self.invoice.pk}/").content.decode()
        self.assertIn("No payments yet.", page)
        self.assertIn("No declarations yet.", page)
        self.assertNotIn(">None.<", page)

    def test_the_rail_says_what_the_invoice_count_counts(self):
        self.sign_in()
        nav = re.search(r'<nav class="rail-nav".*?</nav>', self.client.get("/invoices/").content.decode(), re.S).group(0)
        self.assertIn(">1 unpaid</span>", nav)

    def test_the_rail_counts_take_no_scope(self):
        # NAV_COUNTS_KEY is one global entry because staff see every organization. A count that ever
        # depends on the viewer must change the key too; this fails first if a count takes an argument.
        import inspect

        from apps.staff import context, views

        for fn in (context._counts, context._open_invoices, views.needs_person_count):
            self.assertEqual(list(inspect.signature(fn).parameters), [], fn.__name__)
        self.assertEqual(list(inspect.signature(context.fresh_counts).parameters), ["needs_person"])


class DeclaredNetworkTests(UnsealedVaultMixin, TestCase):
    """A network the client chose in "I have paid" is stored as chosen and shown by name to staff."""

    def test_a_declared_monero_payment_keeps_its_network(self):
        from apps.payments.models import Declaration
        from apps.rails.models import RailAccount

        org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1")
        btcpay = RailAccount.objects.create(org=org, kind="btcpay", label="BTCPay", assets=["BTC", "XMR"],
                                            config={"url": "https://btcpay.example", "store_id": "S1"},
                                            credentials={"api_key": "KEY"})
        contact, _ = find_or_create_contact(org, "a@example.com", legal_name="Ján", preferred_language="en")
        inv, _ = create_invoice(org, contact, currency="EUR", offered_methods=[f"rail:{btcpay.pk}"],
                                lines=[{"name": "S", "quantity": 1, "unit_price_minor": 10000}])
        inv, _view, pay = send_invoice(inv, email_client=False)
        page = self.client.get(f"/p/{pay}/?lang=en").content.decode()
        self.assertIn('<option value="monero">Monero</option>', page)
        r = self.client.post(f"/p/{pay}/declare?lang=en", {"network": "monero", "tx_hash": "", "note": "sent"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Declaration.objects.get().network, "monero")
