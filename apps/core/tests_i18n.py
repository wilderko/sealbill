"""Nine languages: catalogue completeness and template-syntax leak guards.

The completeness check parses the .po files itself (no polib) and compares them
with a regex scan of the sources. Limits of the scan: it finds only string
*literals* passed to `{% translate "..." %}`, `{% blocktranslate %}...` (variables
written as `{{ name }}` become `%(name)s`; `trimmed`/`count` plurals are not
handled) and `_("...")`/`gettext("...")`/`gettext_lazy("...")` in apps/. A msgid
built at run time is invisible to it; `makemessages` stays the authority, this
test only fails when a task forgets to re-run it or leaves an entry empty/fuzzy.
"""
import ast
import io
import re
from pathlib import Path

from django.conf import settings
from django.core.cache import cache, caches
from django.test import TestCase
from django.utils import translation

from apps.contacts.services import find_or_create_contact
from apps.core.i18n import activate_for
from apps.invoices.services import create_invoice
from apps.orgs.models import BankAccount, Organization
from apps.vault.testing import UnsealedVaultMixin
from apps.vault.vault import vault

BASE = Path(settings.BASE_DIR)
LANGS = [code for code, _ in settings.LANGUAGES]
NON_EN = [c for c in LANGS if c != "en"]
LEAKS = ("{%", "{{", "{#")


def parse_po(path):
    """Return {msgid: (msgid_plural|None, [msgstr...], fuzzy)} for non-header entries."""
    entries, cur, field, flags = {}, None, None, set()

    def flush():
        nonlocal cur, flags
        if cur is not None and cur.get("msgid"):
            entries[(cur.get("ctxt", ""), cur["msgid"])] = (cur.get("plural"), cur["str"], "fuzzy" in flags)
        cur, flags = None, set()

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            flush()
        elif line.startswith("#~"):
            continue
        elif line.startswith("#,"):
            if cur is not None and cur.get("str"):
                flush()
            flags |= {f.strip() for f in line[2:].split(",")}
        elif line.startswith("#"):
            if cur is not None and cur.get("str"):
                flush()
        elif line.startswith("msgctxt"):
            if cur is not None:
                flush()
            cur = {"ctxt": ast.literal_eval(line[7:].strip()), "msgid": None, "str": []}
            field = "ctxt"
        elif line.startswith("msgid_plural"):
            cur["plural"] = ast.literal_eval(line[len("msgid_plural"):].strip())
            field = "plural"
        elif line.startswith("msgid"):
            if cur is not None and cur.get("msgid") is not None:
                flush()
            if cur is None:
                cur = {"str": []}
            cur["msgid"] = ast.literal_eval(line[5:].strip())
            field = "msgid"
        elif line.startswith("msgstr["):
            cur["str"].append(ast.literal_eval(line.split("]", 1)[1].strip()))
            field = "str"
        elif line.startswith("msgstr"):
            cur["str"].append(ast.literal_eval(line[6:].strip()))
            field = "str"
        elif line.startswith('"'):
            piece = ast.literal_eval(line)
            if field == "str":
                cur["str"][-1] += piece
            elif field == "plural":
                cur["plural"] += piece
            else:
                cur["msgid"] += piece
    flush()
    return entries


def po_path(lang):
    return BASE / "locale" / lang / "LC_MESSAGES" / "django.po"


TEMPLATE_TRANSLATE = re.compile(r"""\{%\s*trans(?:late)?\s+("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""")
TEMPLATE_BLOCK = re.compile(r"\{%\s*blocktrans(?:late)?\b[^%]*%\}(.*?)\{%\s*endblocktrans(?:late)?\s*%\}", re.S)
_LIT = r"""("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')"""
PY_CALL = re.compile(r"\b(?:_|gettext|ngettext|gettext_noop)(?:_lazy)?\(\s*" + _LIT)
PY_CTX_CALL = re.compile(r"\b(?:pgettext|npgettext)(?:_lazy)?\(\s*" + _LIT + r"\s*,\s*" + _LIT)  # (context, msgid)


def template_files(root):
    for pattern in ("*.html", "*.txt"):
        yield from (root / "templates").rglob(pattern)
        yield from (root / "apps").glob(f"*/templates/**/{pattern}")


def source_msgids(root=BASE):
    found = {}
    for path in template_files(root):
        text = path.read_text(encoding="utf-8")
        for m in TEMPLATE_TRANSLATE.finditer(text):
            found[ast.literal_eval(m.group(1))] = path
        for m in TEMPLATE_BLOCK.finditer(text):
            body = re.sub(r"\{\{\s*(\w+)\s*\}\}", r"%(\1)s", m.group(1)).strip()
            found[body] = path
    for path in (root / "apps").rglob("*.py"):
        if "migrations" in path.parts or path.name.startswith("tests"):
            continue
        for m in PY_CALL.finditer(path.read_text(encoding="utf-8")):
            found[ast.literal_eval(m.group(1))] = path
        for m in PY_CTX_CALL.finditer(path.read_text(encoding="utf-8")):
            found[ast.literal_eval(m.group(2))] = path
    return found


class ScannerSelfTest(TestCase):
    def test_scanner_finds_txt_templates_and_lazy_calls(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "templates" / "email").mkdir(parents=True)
            (root / "templates" / "email" / "a.txt").write_text('{% load i18n %}{% trans "Hello txt" %}')
            (root / "apps" / "x" / "templates" / "x").mkdir(parents=True)
            (root / "apps" / "x" / "templates" / "x" / "b.html").write_text(
                "{% blocktrans %}Block {{ n }}{% endblocktrans %}")
            (root / "apps" / "x" / "m.py").write_text(
                'a = gettext_lazy("Lazy one")\nb = pgettext("ctx", "Ctx msg")\nc = ngettext("One", "Many", 2)\n')
            self.assertEqual(set(source_msgids(root)), {"Hello txt", "Block %(n)s", "Lazy one", "Ctx msg", "One"})


GETTEXT_NAMES = {"_", "gettext", "gettext_lazy", "ngettext", "ngettext_lazy", "pgettext", "pgettext_lazy",
                 "npgettext", "npgettext_lazy", "gettext_noop"}


def gettext_in_fstrings(root=BASE):
    """(path, line) of every gettext call inside an f-string: xgettext does not look there, so
    makemessages drops the string from the catalogues (final review m1) while the regex scanner above
    still finds it."""
    found = []
    for path in sorted((root / "apps").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.JoinedStr):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id in GETTEXT_NAMES:
                    found.append((str(path.relative_to(root)), inner.lineno))
    return found


class FstringGettextTests(TestCase):
    def test_no_translated_string_hides_in_an_f_string(self):
        self.assertEqual(gettext_in_fstrings(), [])

    def test_the_check_sees_one(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "apps").mkdir()
            (Path(d) / "apps" / "m.py").write_text('x = f\'{_("A.")} {_("B.")}\'\n')
            self.assertEqual(gettext_in_fstrings(Path(d)), [("apps/m.py", 1), ("apps/m.py", 1)])


class CatalogueCompletenessTests(TestCase):
    def test_every_catalogue_complete(self):
        catalogues = {}
        for lang in NON_EN:
            self.assertTrue(po_path(lang).exists(), f"missing catalogue for {lang}")
            catalogues[lang] = parse_po(po_path(lang))
            self.assertTrue((po_path(lang).with_suffix(".mo")).exists(), f"{lang}: .mo not compiled")
        reference = set(catalogues["sk"])
        self.assertGreater(len(reference), 20)
        for lang, entries in catalogues.items():
            self.assertEqual(set(entries), reference, f"{lang}: msgid set differs from sk")
            m = re.search(r"nplurals=(\d+)", po_path(lang).read_text(encoding="utf-8"))
            self.assertTrue(m, f"{lang}: no Plural-Forms header")
            nplurals = int(m.group(1))
            for msgid, (plural, strs, fuzzy) in entries.items():
                if plural is not None:
                    self.assertEqual(len(strs), nplurals, f"{lang}: plural forms of {msgid!r}")
                self.assertFalse(fuzzy, f"{lang}: fuzzy entry {msgid!r}")
                self.assertTrue(strs and all(s.strip() for s in strs), f"{lang}: empty msgstr for {msgid!r}")

    def test_every_source_string_is_in_every_catalogue(self):
        wanted = source_msgids()
        self.assertIn("Unlock the vault", wanted)
        self.assertIn("Receipt", wanted)
        for lang in NON_EN:
            have = {mid for (_ctx, mid) in parse_po(po_path(lang))}
            for msgid, path in wanted.items():
                self.assertIn(msgid, have, f"{lang}: {msgid!r} ({path.relative_to(BASE)}) not in catalogue")

    def test_translations_differ_from_english(self):
        # A hand-written catalogue has real words in it; an English copy is the failure.
        keep = {"Number", "Total", "Description", "Unlock", "Notes", "Bank", "Account", "Beneficiary",
                "Subtotal", "Tax", "Password", "Intermediary bank", "Amount", "Qty", "Rabatt"}
        for lang in NON_EN:
            same = [k for (_c, k), (_p, s, _f) in parse_po(po_path(lang)).items() if s[0] == k and k not in keep]
            self.assertEqual(same, [], f"{lang}: msgstr equals msgid")


class FallbackTests(TestCase):
    def test_unknown_language_falls_back_to_english(self):
        org = Organization(slug="x", default_language="xx")

        from apps.contacts.models import Contact

        contact = Contact(org=org, preferred_language="zz")
        self.assertEqual(activate_for(contact), "en")
        contact.preferred_language = "ko"
        self.assertEqual(activate_for(contact), "ko")
        contact.preferred_language = ""
        org.default_language = "pl"
        self.assertEqual(activate_for(contact), "pl")

    def test_unknown_language_renders_english(self):
        with translation.override("xx"):
            self.assertEqual(translation.gettext("Unlock the vault"), "Unlock the vault")


class LeakGuardTests(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1")
        self.contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="Ján Žilinský")
        self.bank = BankAccount.objects.create(
            org=self.org, currency="EUR", label="Main EUR", beneficiary_name="Seller s.r.o.",
            account_number="SK0000000000001", bank_name="Bank A", bank_swift="BANKSKBX",
            intermediary_bank_name="Inter Bank", intermediary_swift="INTERXXX9", intermediary_account="C-1",
        )

    def assertClean(self, text, what):
        for token in LEAKS:
            self.assertNotIn(token, text, f"{what} leaks {token!r}")

    def test_rendered_pdfs_have_no_template_syntax(self):
        from apps.invoices.pdf import render_invoice_pdf, render_receipt_pdf

        try:
            from weasyprint import HTML  # noqa: F401
        except Exception:
            self.skipTest("WeasyPrint system libraries are missing")
        from pypdf import PdfReader

        inv = create_invoice(
            self.org, self.contact, currency="EUR", offered_methods=[f"bank:{self.bank.pk}"],
            lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 133700}],
        )[0]
        for lang in LANGS:
            for render in (render_invoice_pdf, render_receipt_pdf):
                text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(render(inv, lang))).pages)
                self.assertClean(text, f"{render.__name__}/{lang}")

    def test_rendered_emails_have_no_template_syntax(self):
        from django.template.loader import render_to_string

        from apps.notify import services as notify
        from apps.notify.models import OutboxMessage

        contexts = {
            "invoice": {"ref": "2026-0001", "org": "Seller", "name": "Ján", "is_request": False, "message": "Hi",
                        "total": "1 EUR", "due": "1. 1. 2027", "view_url": "https://x/i/t/", "pay_url": "https://x/p/t/",
                        "count": 2, "paid": "1 EUR", "remaining": "2 EUR", "admin_url": "https://x/a/",
                        "unlock_url": "https://x/unlock/"},
        }
        base = contexts["invoice"]
        for lang in LANGS:
            for kind in OutboxMessage.KINDS:
                for flag in (True, False):
                    ctx = {**base, "language": lang, "pdf_attached": flag, "is_request": not flag}
                    with translation.override(lang):
                        for ext in ("txt", "html"):
                            self.assertClean(render_to_string(f"email/{kind}.{ext}", ctx), f"{kind}.{ext}/{lang}")
                    msg = OutboxMessage(kind=kind, language=lang, subject_context=ctx, body_context=ctx)
                    with translation.override(lang):
                        self.assertClean(notify._subject(msg), f"subject {kind}/{lang}")
                        if lang != "en":
                            self.assertNotEqual(notify._subject(msg), notify.SUBJECTS[kind if kind != "invoice" else "invoice"] % ctx)

    def test_rendered_pages_have_no_template_syntax(self):
        for lang in LANGS:
            r = self.client.get("/unlock/", HTTP_ACCEPT_LANGUAGE=lang)
            self.assertEqual(r.status_code, 200)
            self.assertClean(r.content.decode(), f"unlock/{lang}")
            caches["ratelimit"].clear(); cache.clear()  # five wrong passwords from one IP would otherwise trip the rate limit
            vault.seal()  # an unsealed vault short-circuits the POST
            with self.assertLogs("apps.vault.unlock_attempts", "WARNING"):
                r = self.client.post("/unlock/", {"password": "wrong"}, HTTP_ACCEPT_LANGUAGE=lang)
            self.assertEqual(r.status_code, 401)
            self.assertClean(r.content.decode(), f"unlock-error/{lang}")
        vault.seal()
        for lang in LANGS:
            for path in ("/i/abc", "/p/abc"):
                r = self.client.get(f"{path}?lang={lang}")
                self.assertEqual(r.status_code, 503)
                self.assertClean(r.content.decode(), f"sealed 503 {path}/{lang}")

    def test_portal_pages_have_no_template_syntax_and_are_translated(self):
        from apps.invoices.services import send_invoice, void_invoice
        from apps.payments.services import declare_payment

        from apps.orgs.models import FiatLink

        link = FiatLink.objects.create(org=self.org, kind="revolut", label="Rev", currencies=["EUR"], url="https://r.example/x")
        offered = [f"bank:{self.bank.pk}", f"fiat:{link.pk}"]
        line = [{"name": "Service", "quantity": 1, "unit_price_minor": 133700}]
        inv, view, pay = send_invoice(create_invoice(
            self.org, self.contact, currency="EUR", offered_methods=offered, lines=line)[0])
        voided, vview, vpay = send_invoice(create_invoice(
            self.org, self.contact, currency="EUR", offered_methods=offered, lines=line, idempotency_key="v")[0])
        void_invoice(voided, "x", None)
        english = {}
        for lang in LANGS:
            caches["ratelimit"].clear(); cache.clear()
            pages = {
                "invoice": self.client.get(f"/i/{view}/?lang={lang}"),
                "pay": self.client.get(f"/p/{pay}/?lang={lang}"),
                "status": self.client.get(f"/p/{pay}/status?lang={lang}"),
                "void-invoice": self.client.get(f"/i/{vview}/?lang={lang}"),
                "void-pay": self.client.get(f"/p/{vpay}/?lang={lang}"),
                "bad-declare": self.client.post(f"/p/{pay}/declare?lang={lang}", {"tx_hash": "x" * 200}),
                "too-many": None,
            }
            for _ in range(61):
                pages["too-many"] = self.client.get(f"/p/{pay}/status?lang={lang}")
            self.assertEqual(pages["too-many"].status_code, 429)
            for name, r in pages.items():
                text = r.content.decode()
                self.assertClean(text, f"portal {name}/{lang}")
                self.assertContains(r, f'lang="{lang}"', status_code=r.status_code) if name != "status" else None
                if lang == "en":
                    english[name] = text
                elif name != "status":
                    self.assertNotEqual(text.split("<main>")[1][:400], english[name].split("<main>")[1][:400], f"{name}/{lang}")
        declare_payment(inv)
        for lang in LANGS:
            caches["ratelimit"].clear(); cache.clear()
            r = self.client.get(f"/p/{pay}/status?lang={lang}")
            self.assertClean(r.content.decode(), f"declared status/{lang}")
            if lang != "en":
                self.assertNotContains(r, "We have been told")

    def test_unlock_page_is_translated(self):
        r = self.client.get("/unlock/", HTTP_ACCEPT_LANGUAGE="sk")
        self.assertContains(r, 'lang="sk"')
        self.assertContains(r, "Odomknúť")
        self.assertNotContains(r, "Unlock the vault")

    def test_sealed_503_is_translated_by_query_then_header(self):
        vault.seal()
        r = self.client.get("/i/abc?lang=de")
        self.assertContains(r, "Vorübergehend nicht verfügbar", status_code=503)
        r = self.client.get("/p/abc", HTTP_ACCEPT_LANGUAGE="ko")
        self.assertContains(r, 'lang="ko"', status_code=503)
        self.assertNotContains(r, "Temporarily unavailable", status_code=503)
        r = self.client.get("/p/abc?lang=zz", HTTP_ACCEPT_LANGUAGE="fr")  # unknown ?lang -> request language
        self.assertContains(r, 'lang="fr"', status_code=503)
        r = self.client.get("/p/abc?lang=zz")
        self.assertContains(r, "Temporarily unavailable", status_code=503)

    def test_pdf_headings_in_every_language_differ_from_english(self):
        from apps.invoices.pdf import render_invoice_pdf

        try:
            from weasyprint import HTML  # noqa: F401
        except Exception:
            self.skipTest("WeasyPrint system libraries are missing")
        from pypdf import PdfReader

        inv = create_invoice(
            self.org, self.contact, currency="EUR", offered_methods=[],
            lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 100}],
        )[0]
        text = lambda lang: "\n".join(
            p.extract_text() for p in PdfReader(io.BytesIO(render_invoice_pdf(inv, lang))).pages)
        english = text("en")
        self.assertIn("bill to", english.lower())
        for lang in NON_EN:
            self.assertNotIn("bill to", text(lang).lower(), lang)
