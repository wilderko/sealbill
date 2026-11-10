from django.db import connection

from apps.api.tests import ApiBase
from apps.contacts.services import find_or_create_contact


class ContactNameSearchTests(ApiBase):
    def names(self, c, q):
        r = c.get("/api/v1/contacts", {"name": q})
        self.assertEqual(r.status_code, 200, r.content)
        return sorted(x["legal_name"] for x in r.json()["results"])

    def setUp(self):
        super().setUp()
        find_or_create_contact(self.org, "a@example.com", legal_name="Ján Novák")
        find_or_create_contact(self.org, "b@example.com", legal_name="Eva Novak", display_name="Evička")
        find_or_create_contact(self.org, "c@example.com", legal_name="Peter Kováč")
        self.c, _, _ = self.client_for(scopes=("read",))

    def test_folds_accents_and_case(self):
        self.assertEqual(self.names(self.c, "jan novak"), ["Ján Novák"])
        self.assertEqual(self.names(self.c, "  NOVAK "), ["Eva Novak", "Ján Novák"])

    def test_every_word_must_match_and_display_name_counts(self):
        self.assertEqual(self.names(self.c, "jan kovac"), [])
        self.assertEqual(self.names(self.c, "evicka"), ["Eva Novak"])
        self.assertEqual(self.names(self.c, "nov"), [])  # whole words only

    def test_short_or_long_text_or_with_email_is_400(self):
        for params in ({"name": "x"}, {"name": " x "}, {"name": "a" * 101}, {"name": "novak", "email": "a@example.com"}):
            self.assertEqual(self.c.get("/api/v1/contacts", params).status_code, 400, params)

    def test_other_orgs_contacts_never_returned(self):
        find_or_create_contact(self.other, "z@example.com", legal_name="Zoltan Novak")
        self.assertEqual(self.names(self.c, "novak"), ["Eva Novak", "Ján Novák"])

    def test_at_most_20(self):
        for i in range(25):
            find_or_create_contact(self.org, f"m{i}@example.com", legal_name="Marta Horvath")
        r = self.c.get("/api/v1/contacts", {"name": "horvath", "limit": 200})
        self.assertEqual(r.json()["count"], 20)

    def test_undecryptable_row_is_skipped(self):
        bad, _ = find_or_create_contact(self.org, "bad@example.com", legal_name="Novak Broken")
        with connection.cursor() as cur:
            cur.execute("UPDATE contacts_contact SET legal_name = %s WHERE id = %s", ["gAAAAAnotvalid", bad.pk])
        self.assertEqual(self.names(self.c, "novak"), ["Eva Novak", "Ján Novák"])

    def test_rate_limited_per_token(self):
        from apps.api import views

        for _ in range(views.NAME_SEARCH_PER_MINUTE):
            self.assertEqual(self.c.get("/api/v1/contacts", {"name": "novak"}).status_code, 200)
        r = self.c.get("/api/v1/contacts", {"name": "novak"})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["error"], "throttled")
        self.assertEqual(self.c.get("/api/v1/contacts").status_code, 200)

    def test_needs_read_scope(self):
        c, _, _ = self.client_for(scopes=("invoices:write",))
        self.assertEqual(c.get("/api/v1/contacts", {"name": "novak"}).status_code, 403)


class ContactNameSearchEdgeTests(ApiBase):
    def setUp(self):
        super().setUp()
        self.c, _, _ = self.client_for(scopes=("read",))

    def names(self, q):
        r = self.c.get("/api/v1/contacts", {"name": q})
        self.assertEqual(r.status_code, 200, r.content)
        return sorted(x["legal_name"] for x in r.json()["results"])

    def test_copy_contact_is_excluded(self):
        find_or_create_contact(self.org, "a@example.com", legal_name="Copy Person")
        find_or_create_contact(self.org, "a@example.com", legal_name="Copy Person", copy_for=self.other)
        self.assertEqual(self.names("copy person"), ["Copy Person"])
        from apps.contacts.models import Contact
        self.assertEqual(Contact.objects.filter(org=self.org).count(), 2)

    def test_hyphen_separates_words_and_apostrophe_does_not(self):
        find_or_create_contact(self.org, "j@example.com", legal_name="Jean-Luc Picard")
        find_or_create_contact(self.org, "o@example.com", legal_name="Sean O'Brien")
        self.assertEqual(self.names("jean-luc"), ["Jean-Luc Picard"])
        self.assertEqual(self.names("jean"), ["Jean-Luc Picard"])
        self.assertEqual(self.names("o'brien"), ["Sean O'Brien"])
        self.assertEqual(self.names("o\u2019brien"), ["Sean O'Brien"])  # a typographic apostrophe
        self.assertEqual(self.names("brien"), [])

    def test_a_spaced_query_finds_a_hyphenated_name(self):
        find_or_create_contact(self.org, "m@example.com", legal_name="Marie-Claire Dupont")
        self.assertEqual(self.names("MARIE CLAIRE DUPONT"), ["Marie-Claire Dupont"])

    def test_letters_nfkd_leaves_whole_are_folded(self):
        find_or_create_contact(self.org, "l@example.com", legal_name="\u0141ukasz Wa\u0142\u0119sa")
        find_or_create_contact(self.org, "s@example.com", legal_name="S\u00f8ren \u0110or\u0111evi\u0107")
        find_or_create_contact(self.org, "g@example.com", legal_name="Gro\u00dfmann \u00c6gir Cur\u0153")
        self.assertEqual(self.names("LUKASZ WALESA"), ["\u0141ukasz Wa\u0142\u0119sa"])
        self.assertEqual(self.names("soren dordevic"), ["S\u00f8ren \u0110or\u0111evi\u0107"])
        self.assertEqual(self.names("grossmann aegir curoe"), ["Gro\u00dfmann \u00c6gir Cur\u0153"])

    def test_sealed_vault_is_503_when_there_are_contacts(self):
        from apps.vault.vault import vault

        find_or_create_contact(self.org, "a@example.com", legal_name="Eva Novak")
        vault.seal()
        r = self.c.get("/api/v1/contacts", {"name": "novak"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json(), {"error": "vault_sealed"})

    def test_row_deleted_mid_scan_is_skipped(self):
        from apps.contacts.models import Contact
        from apps.contacts.services import search_by_name

        from apps.contacts import services

        gone, _ = find_or_create_contact(self.org, "g@example.com", legal_name="Novak Gone")
        find_or_create_contact(self.org, "k@example.com", legal_name="Novak Kept")
        real = services._folded_name
        state = {"done": False}

        def fold_then_delete(*a, **k):  # the names are read; the row goes before the matches are loaded
            if not state["done"]:
                state["done"] = True
                Contact.objects.filter(pk=gone.pk).delete()
            return real(*a, **k)

        from unittest import mock
        with mock.patch.object(services, "_folded_name", side_effect=fold_then_delete):
            found = search_by_name(self.org, "novak")
        self.assertEqual([c.legal_name for c in found], ["Novak Kept"])
