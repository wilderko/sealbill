from django.core.exceptions import ValidationError
from django.db import NotSupportedError, connection, models
from django.test import TestCase
from django.test.utils import isolate_apps

from apps.vault.fields import EncryptedCharField, EncryptedTextField, UndecryptableValue, blind_index
from apps.vault.registry import encrypted_field_names
from apps.vault.testing import UnsealedVaultMixin
from apps.vault.tests_app.models import Sample
from apps.vault.vault import VaultSealed, vault


def raw(pk, col):
    with connection.cursor() as c:
        c.execute(f"SELECT {col} FROM vault_tests_sample WHERE id = %s", [pk])
        return c.fetchone()[0]


class EncryptedFieldTest(UnsealedVaultMixin, TestCase):
    def test_roundtrip(self):
        s = Sample.objects.create(name="Žaneta", note="", data={"a": [1, "é"]})
        s = Sample.objects.get(pk=s.pk)
        self.assertEqual((s.name, s.note, s.data), ("Žaneta", "", {"a": [1, "é"]}))

    def test_none_stays_none(self):
        s = Sample.objects.create()
        self.assertIsNone(Sample.objects.get(pk=s.pk).name)
        self.assertIsNone(raw(s.pk, "name"))

    def test_stored_value_is_ciphertext(self):
        s = Sample.objects.create(name="Alice", note="secret", data={"k": "valuexyz"})
        for col, plain in (("name", "Alice"), ("note", "secret"), ("data", "valuexyz")):
            stored = raw(s.pk, col)
            self.assertTrue(stored.startswith("gAAAAA"))
            self.assertNotIn(plain, stored)

    def test_read_while_sealed_raises(self):
        s = Sample.objects.create(name="Alice")
        vault.seal()
        with self.assertRaises(VaultSealed):
            Sample.objects.get(pk=s.pk)

    def test_undecryptable_raises_and_logs(self):
        s = Sample.objects.create(name="Alice")
        other = vault.fernet().__class__(vault.fernet().__class__.generate_key())
        with connection.cursor() as c:
            c.execute("UPDATE vault_tests_sample SET name = %s WHERE id = %s", [other.encrypt(b'"x"').decode(), s.pk])
        with self.assertLogs("apps.vault.fields", "ERROR") as logs:
            with self.assertRaises(UndecryptableValue) as cm:
                Sample.objects.get(pk=s.pk)
        self.assertIn("name", "".join(logs.output))
        self.assertNotIn("gAAAAA", str(cm.exception))

    def test_char_max_length_validates_plaintext(self):
        with self.assertRaises(ValidationError):
            Sample(name="x" * 21).full_clean()
        Sample(name="x" * 20).full_clean()

    def test_blind_index_case_insensitive_and_stable(self):
        a = blind_index("  Foo@Example.COM ")
        self.assertEqual(a, blind_index("foo@example.com"))
        self.assertEqual(len(a), 64)
        self.assertNotEqual(a, blind_index("bar@example.com"))

    def test_blind_index_field_lookup(self):
        s = Sample.objects.create(email_idx=blind_index("a@b.c"))
        self.assertEqual(Sample.objects.get(email_idx=blind_index("A@B.C")).pk, s.pk)

    def test_encrypted_field_names_reads_meta(self):
        self.assertEqual(encrypted_field_names(Sample), ["name", "note", "data"])
        self.assertEqual(encrypted_field_names(Sample()), ["name", "note", "data"])


class EncryptedLookupAndCheckTest(UnsealedVaultMixin, TestCase):
    def test_filter_on_encrypted_field_raises(self):
        for kw in ({"name": "x"}, {"name__in": ["x"]}, {"note__icontains": "x"}):
            with self.assertRaisesMessage(NotSupportedError, "blind index"):
                list(Sample.objects.filter(**kw))

    def test_isnull_works(self):
        s = Sample.objects.create(name="a")
        Sample.objects.create()
        self.assertEqual(list(Sample.objects.filter(name__isnull=False)), [s])
        self.assertEqual(Sample.objects.filter(name__isnull=True).count(), 1)

    @isolate_apps("apps.vault.tests_app")
    def test_default_is_error_e002(self):
        class M(models.Model):
            a = EncryptedTextField(default="")
            b = EncryptedCharField(max_length=5, default="x")
            c = EncryptedTextField(null=True, default=None)

            class Meta:
                app_label = "vault_tests"

        ids = {f.name: [e.id for e in f.check()] for f in M._meta.get_fields()}
        self.assertIn("vault.E002", ids["a"])
        self.assertIn("vault.E002", ids["b"])
        self.assertNotIn("vault.E002", ids["c"])

    @isolate_apps("apps.vault.tests_app")
    def test_unique_or_indexed_is_error_e003(self):
        class M(models.Model):
            a = EncryptedTextField(unique=True)
            b = EncryptedTextField(db_index=True)

            class Meta:
                app_label = "vault_tests"

        for f in M._meta.get_fields():
            if f.name != "id":
                self.assertIn("vault.E003", [e.id for e in f.check()])
