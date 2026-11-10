import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Exists, OuterRef, Q

from apps.orgs.models import Organization
from apps.rails import addresses, networks
from apps.vault.fields import EncryptedJSONField

CHAIN_KINDS = ("evm", "tron", "solana", "ton")
BTCPAY_ASSETS = ("BTC", "XMR", "ZEC")
# BTCPay settles after 1, 2 or 6 confirmations under these; HighSpeed settles at 0 and is refused,
# as is anything else (audit MONEY-4). Absent means MediumSpeed.
BTCPAY_SPEED_POLICIES = ("MediumSpeed", "LowMediumSpeed", "LowSpeed")
LN_BACKENDS = ("nwc", "lnd", "cln", "lnbits")
TON_DISABLED = ("TON is off (TON_ENABLED=false) until the credit to our own jetton wallet can be verified: "
                "save this account inactive.")


def ton_enabled() -> bool:
    """Whether TON may be scanned, quoted and offered (settings.TON_ENABLED, off by default)."""
    return bool(getattr(settings, "TON_ENABLED", False))


def ton_blocked(kind: str) -> bool:
    """A rail of `kind` that must not be used now: TON while `TON_ENABLED` is off."""
    return kind == "ton" and not ton_enabled()


# What a watcher reads off an account. While a quote of the account is unreleased a client may
# still pay it, so none of these may change (audit MONEY-10a): a new address, network or asset
# list would stop the watching of the one quoted, and a new organization would re-home its money.
LOCKED_WHILE_QUOTED = ("org", "kind", "address", "networks", "assets")
LOCKED_MESSAGE = ("This account has quotes a client may still pay: its {field} cannot change until they are "
                  "released (a day after they close). Create a new account instead.")


def _unreleased_quotes():
    from django.apps import apps

    quote = apps.get_model("quotes", "Quote")
    return quote.objects.filter(rail_account=OuterRef("pk")).exclude(state="released")


class RailAccountQuerySet(models.QuerySet):
    def watched(self):
        """The accounts a watcher still reads: active ones, and inactive ones while a quote of
        theirs is unreleased (audit E2E-4: a client may be paying the quote they were shown)."""
        return self.filter(Q(active=True) | Q(Exists(_unreleased_quotes())))


class RailAccount(models.Model):
    class Kind(models.TextChoices):
        BTCPAY = "btcpay"
        LIGHTNING = "lightning"
        EVM = "evm"
        TRON = "tron"
        SOLANA = "solana"
        TON = "ton"

    org = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="rail_accounts")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    label = models.CharField(max_length=100)
    active = models.BooleanField(default=True)
    address = models.CharField(max_length=128, blank=True)  # normalized; "" for btcpay/lightning
    networks = models.JSONField(default=list, blank=True)
    assets = models.JSONField(default=list, blank=True)
    config = models.JSONField(default=dict, blank=True)
    credentials = EncryptedJSONField(null=True, blank=True)

    objects = RailAccountQuerySet.as_manager()

    def __str__(self):
        return f"{self.label} ({self.kind})"

    def has_unreleased_quotes(self) -> bool:
        return self.pk is not None and self.quotes.exclude(state="released").exists()

    @property
    def method_key(self) -> str:
        return f"rail:{self.pk}"

    def _problems(self) -> dict[str, str]:
        kind, errs = self.kind, {}
        nets, assets = list(self.networks or []), list(self.assets or [])
        cfg = self.config or {}
        if kind in CHAIN_KINDS:
            if self.active and ton_blocked(kind):
                errs["active"] = TON_DISABLED
            try:
                self.address = addresses.normalize(kind, self.address)
            except ValueError as exc:
                errs["address"] = str(exc)
            allowed = [k for k, n in networks.NETWORKS.items() if n.family == kind]
            if not nets or any(n not in allowed for n in nets):
                errs["networks"] = f"choose from: {', '.join(allowed)}"
            elif kind != "evm" and nets != allowed:
                errs["networks"] = f"must be exactly {allowed}"
            if not assets:
                errs["assets"] = "choose at least one asset"
            elif "networks" not in errs:
                enabled = {s for n in nets for s in networks.NETWORKS[n].assets}
                if any(a not in enabled for a in assets):
                    errs["assets"] = "every asset must be available on at least one chosen network"
            return errs
        self.address = ""
        if nets:
            errs["networks"] = "not used for this kind"
        if kind == "btcpay":
            if not assets or any(a not in BTCPAY_ASSETS for a in assets):
                errs["assets"] = f"choose from: {', '.join(BTCPAY_ASSETS)}"
            problems = []
            if not cfg.get("url") or not cfg.get("store_id"):
                problems.append("url and store_id are required")
            if "speed_policy" in cfg and cfg["speed_policy"] not in BTCPAY_SPEED_POLICIES:
                problems.append(f"speed_policy must be one of {', '.join(BTCPAY_SPEED_POLICIES)} (HighSpeed "
                                "settles with no confirmation at all)")
            if problems:
                errs["config"] = "; ".join(problems)
        elif kind == "lightning":
            if assets != ["BTC"]:
                errs["assets"] = 'must be ["BTC"]'
            if cfg.get("backend") not in LN_BACKENDS:
                errs["config"] = f"backend must be one of: {', '.join(LN_BACKENDS)}"
        else:
            errs["kind"] = "unknown kind"
        return errs

    def _locked_changes(self) -> dict[str, str]:
        """The fields in `LOCKED_WHILE_QUOTED` this edit changes, while a quote is unreleased."""
        if self.pk is None:
            return {}
        stored = RailAccount.objects.filter(pk=self.pk).values("org_id", "kind", "address", "networks",
                                                                 "assets", "config").first()
        if stored is None:
            return {}
        now = {"org": self.org_id, "kind": self.kind, "address": self.address,
               "networks": sorted(self.networks or []), "assets": sorted(self.assets or [])}
        was = {"org": stored["org_id"], "kind": stored["kind"], "address": stored["address"],
               "networks": sorted(stored["networks"] or []), "assets": sorted(stored["assets"] or [])}
        changed = [f for f in LOCKED_WHILE_QUOTED if now[f] != was[f]]
        gateway = self._gateway_changes(stored["config"]) if self.kind in ("btcpay", "lightning") else []
        if not (changed or gateway) or not self.has_unreleased_quotes():
            return {}
        errs = {f: LOCKED_MESSAGE.format(field="organization" if f == "org" else f) for f in changed}
        if gateway:
            # Where a watcher polls for a quote already shown: another host, store or backend would leave
            # that invoice unread for good. speed_policy is not in this list.
            errs["config"] = " ".join(LOCKED_MESSAGE.format(field=f) for f in gateway)
        return errs

    def _gateway_changes(self, stored_config) -> list[str]:
        was = stored_config if isinstance(stored_config, dict) else {}
        now = self.config if isinstance(self.config, dict) else {}

        def norm(key, cfg):
            value = str(cfg.get(key) or "").strip()
            return value.rstrip("/") if key == "url" else value

        return [k for k in ("url", "store_id", "backend") if norm(k, was) != norm(k, now)]

    def clean(self):
        errs = self._problems()  # normalizes the address first, so a re-spelling is no change
        for field, message in self._locked_changes().items():
            errs.setdefault(field, message)
        if errs:
            raise ValidationError(errs)

    def save(self, *args, **kwargs):
        if self.active and ton_blocked(self.kind):
            raise ValidationError({"active": TON_DISABLED})
        if self.kind in CHAIN_KINDS:
            try:
                self.address = addresses.normalize(self.kind, self.address)
            except ValueError as exc:
                raise ValidationError({"address": str(exc)}) from None
        else:
            self.address = ""
        super().save(*args, **kwargs)


class FxLegRate(models.Model):
    """The last fiat cross accepted from Frankfurter (ECB) for a pair, e.g. EUR to CZK. A new answer
    more than `fx.FX_MAX_LEG_JUMP_BPS` away from it is refused (audit MONEY-9): that leg has no
    second source, so a wrong value would price every crypto quote in that currency.

    The newest refused answer waits in `pending_*`; it is never the reference until a person who
    checked the ECB's rate accepts it on Needs a person (`fx.accept_leg`, audited). An answer within
    the bound replaces the reference and clears what was waiting. Rates are decimal strings, as
    Frankfurter gave them. Plaintext: it is written while the vault is sealed."""

    base = models.CharField(max_length=3)
    quote = models.CharField(max_length=3)
    rate = models.CharField(max_length=40)
    source_date = models.DateField()
    updated_at = models.DateTimeField(auto_now=True)
    pending_rate = models.CharField(max_length=40, blank=True, default="")
    pending_source_date = models.DateField(null=True, blank=True)
    pending_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["base", "quote"], name="uniq_fx_leg_rate")]

    def __str__(self):
        return f"{self.base}/{self.quote} {self.rate} ({self.source_date})"


class WatchCursor(models.Model):
    network = models.CharField(max_length=32)
    address = models.CharField(max_length=128)
    stream = models.CharField(max_length=32)
    position = models.CharField(max_length=128, blank=True)
    extra = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["network", "address", "stream"], name="uniq_watch_cursor")]


class WatcherHealth(models.Model):
    name = models.CharField(max_length=128, unique=True)
    last_ok_at = models.DateTimeField(null=True, blank=True)
    failing_since = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    last_error_kind = models.CharField(max_length=64, blank=True)
    # The failing episode was announced: by e-mail (`alerted_at`) and as a staff.alert event
    # (`webhook_alerted_at`), each on its own; `health.ok` clears both.
    alerted_at = models.DateTimeField(null=True, blank=True)
    webhook_alerted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.name


_DIGITS = re.compile(r"^[0-9]{1,78}$")


class PendingTransfer(models.Model):
    """A transfer a watcher reported whose recording (`matching.ingest`) failed.

    The scan that found it has already moved its cursor past it, so this row is the only
    record of it. `chain_scan` tries it again at the start of every pass until it is recorded,
    and only then deletes the row. Every column is plaintext (the amount a digit string), so the
    retry runs while the vault is sealed. Unique like a payment: one row per transfer."""

    account = models.ForeignKey(RailAccount, on_delete=models.PROTECT, related_name="pending_transfers")
    network = models.CharField(max_length=32)
    contract = models.CharField(max_length=128, blank=True, default="")
    to_address = models.CharField(max_length=128)
    from_address = models.CharField(max_length=128, blank=True, default="")
    amount_base_units = models.CharField(max_length=78)
    tx_hash = models.CharField(max_length=128)
    log_index = models.IntegerField()
    block_number = models.BigIntegerField(null=True, blank=True)
    block_time = models.DateTimeField()
    final = models.BooleanField(default=False)
    comment = models.TextField(blank=True, default="")
    attempts = models.PositiveIntegerField(default=0)
    last_error_kind = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [models.UniqueConstraint(fields=["network", "tx_hash", "log_index"],
                                               name="uniq_pending_transfer")]

    def save(self, *args, **kwargs):
        if not (isinstance(self.amount_base_units, str) and _DIGITS.match(self.amount_base_units)):
            raise ValidationError({"amount_base_units": "must be digits only (1 to 78)"})
        super().save(*args, **kwargs)

    @classmethod
    def fields_of(cls, transfer) -> dict:
        """The columns that hold `transfer` (an `apps.rails.chains.base.Transfer`)."""
        return {
            "network": transfer.network, "contract": transfer.contract, "to_address": transfer.to_address,
            "from_address": transfer.from_address, "amount_base_units": str(transfer.amount_base_units),
            "tx_hash": transfer.tx_hash, "log_index": transfer.log_index, "block_number": transfer.block_number,
            "block_time": transfer.block_time, "final": transfer.final, "comment": transfer.comment,
        }

    def transfer(self):
        """The transfer as the watcher reported it."""
        from apps.rails.chains.base import Transfer

        return Transfer(
            network=self.network, contract=self.contract, to_address=self.to_address,
            from_address=self.from_address, amount_base_units=int(self.amount_base_units), tx_hash=self.tx_hash,
            log_index=self.log_index, block_number=self.block_number, block_time=self.block_time,
            final=self.final, comment=self.comment,
        )


class ScanGap(models.Model):
    """Blocks a person must check (`from_block` to `to_block`, both included), because a transfer
    in them to `address` may be missing. `reason` says why:

    - `skipped`: the watcher moved past them without reading them (until final review F7 the native
      ETH walk skipped what lay beyond one run's blocks; it is now carried across runs, so only older
      rows say this);
    - `unreadable`: every node refused to list them, even one block at a time and through the
      block's receipts (audit MONEY-5: spam can do this);
    - `unexplained_balance`: they were read, the native balance rose across them, and no
      transaction they hold explains it (an internal transfer from a contract, say; audit MONEY-8).
      Nothing is guessed: a person looks.

    Durable until a person has checked the wallet's history and acknowledged it; staff are told
    once by e-mail (`alerted_at`) and once as a staff.alert event (`webhook_alerted_at`).
    Plaintext only: the watcher writes it while the vault is sealed."""

    REASONS = [("skipped", "skipped"), ("unreadable", "unreadable"),
               ("unexplained_balance", "balance rose with no visible transaction")]

    network = models.CharField(max_length=32)
    address = models.CharField(max_length=128)
    from_block = models.BigIntegerField()
    to_block = models.BigIntegerField()
    reason = models.CharField(max_length=32, choices=REASONS, default="skipped")
    created_at = models.DateTimeField(auto_now_add=True)
    alerted_at = models.DateTimeField(null=True, blank=True)
    webhook_alerted_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [models.CheckConstraint(condition=models.Q(from_block__lte=models.F("to_block")),
                                              name="scan_gap_range_ordered")]

    def __str__(self):
        return f"{self.network} blocks {self.from_block}-{self.to_block}"
