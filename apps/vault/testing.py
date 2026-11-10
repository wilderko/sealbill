import shutil
import tempfile
from pathlib import Path

from django.core.cache import cache, caches
from django.test import override_settings

from apps.vault.vault import vault

TEST_VAULT_PASSWORD = "correct horse battery"


class UnsealedVaultMixin:
    """Use before TestCase: gives each test a fresh, unsealed vault on temp paths."""

    def setUp(self):
        super().setUp()
        self.vault_dir = Path(tempfile.mkdtemp())
        o = override_settings(
            VAULT_KEY_PATH=str(self.vault_dir / "run" / "key"),
            VAULT_CONFIG_PATH=str(self.vault_dir / "cfg" / "vault.json"),
        )
        o.enable()
        self.addCleanup(o.disable)
        vault._key = None
        vault._seen = None
        caches["ratelimit"].clear(); cache.clear()
        vault.init(TEST_VAULT_PASSWORD)
        vault.unseal(TEST_VAULT_PASSWORD)

    def tearDown(self):
        vault.seal()
        shutil.rmtree(self.vault_dir, ignore_errors=True)
        super().tearDown()
