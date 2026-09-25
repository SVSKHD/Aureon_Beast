"""Typed configuration from the environment (§84).

Env supplies defaults. For the execution gates, Firestore ``settings/execution``
overrides them at runtime (decision 11) -- so an executor asks
``ExecutionSettings`` what the limit is, not this object, and ``/trading disable``
takes effect without a restart. ``AureonConfig`` still carries those keys because
something has to seed the document and because a fresh deploy needs sane values
before anyone has written one.

Secrets are read here and never logged. ``__repr__`` is overridden so an
accidental print of the config in a traceback cannot leak the MT5 password or the
Discord token.
"""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from aureon.models.base import AureonModel
from aureon.models.enums import Timeframe

_SECRET_FIELDS = frozenset({"mt5_password", "discord_token"})


def _env_str(key: str, default: str) -> str:
    value = os.environ.get(key)
    return default if value is None or value == "" else value


def _env_opt(key: str) -> str | None:
    value = os.environ.get(key)
    return None if value is None or value == "" else value


def _env_float(key: str, default: float) -> float:
    raw = _env_opt(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a number, got {raw!r}") from exc


def _env_int(key: str, default: int) -> int:
    raw = _env_opt(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc


def _env_csv(key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = _env_opt(key)
    if raw is None:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _env_true(key: str) -> bool:
    """True only for the exact string ``"true"``, case-insensitively (11A, F-3).

    Strict on purpose, and only used for flags that unlock something dangerous. The usual
    permissive parsing -- "1", "yes", "on", any non-empty value -- means a stray
    ``AUREON_ALLOW_LIVE_EXECUTION=false`` or a leftover ``=0`` reads as permission. A flag
    whose whole job is to be hard to set by accident should be hard to set by accident.
    """
    raw = _env_opt(key)
    return raw is not None and raw.strip().lower() == "true"


def _env_map(key: str) -> dict[str, str]:
    """``KEY=A:x,B:y`` -> ``{"A": "x", "B": "y"}``.

    A malformed entry raises rather than being skipped: a pair without a colon is a
    typo, and silently dropping it would leave the symbol it was meant to configure
    looking unconfigured -- which for an evaluation rule means the wrong thresholds or
    a refusal to start, either way traced back to the wrong thing.
    """
    found: dict[str, str] = {}
    for part in _env_csv(key):
        symbol, separator, value = part.partition(":")
        if not separator or not symbol.strip() or not value.strip():
            raise ValueError(
                f"{key}: {part!r} is not SYMBOL:VALUE (e.g. XAUUSD:XAU_OUTCOME_V2)"
            )
        found[symbol.strip().upper()] = value.strip()
    return found


class AureonConfig(AureonModel):
    """Everything Aureon needs from its environment."""

    # ── Identity (§12) ────────────────────────────────────────────────────────
    # Decision 4: an alias, not the broker login, so a broker migration does not
    # re-key every detection in history. Changing it re-keys everything.
    account_scope: str = "primary"
    aureon_magic: int = 770177

    # ── Market clock (§8) ─────────────────────────────────────────────────────
    # Decision 6: the broker's server clock, DST-aware.
    market_tz: str = "Europe/Athens"

    # ── Observation ───────────────────────────────────────────────────────────
    symbols: tuple[str, ...] = ("XAUUSD",)
    timeframes: tuple[Timeframe, ...] = (Timeframe.M5,)

    # ── EMA cross periods (§13) ───────────────────────────────────────────────
    # Configuration, not a constructor default: EmaCrossAgent takes no defaults at
    # all, so there is exactly one place the live periods are decided and a replay
    # cannot quietly disagree with the observer about which pair produced a
    # detection. Changing these is an agent_version bump -- the periods are in
    # agent_params_snapshot, and under §12 the version is in the detection id.
    ema_fast: int = 20
    ema_slow: int = 50

    #: How many candles back a sweep or wick still counts as context for a cross
    #: (§23). On M5, 3 candles is fifteen minutes -- close enough that a human
    #: watching the chart would have seen both events together.
    context_window_candles: int = 3
    state_heartbeat_seconds: float = 5.0
    outbox_path: str = "outbox.db"
    observer_state_path: str = "observer_state.json"

    # ── Execution gates (§84); Firestore wins at runtime (decision 11) ────────
    confirmation_ttl_seconds: float = 60.0
    quote_ttl_seconds: float = 15.0
    status_stale_after_seconds: float = 45.0
    max_deviation_points: int = 20
    max_spread_points: float = 50.0
    max_lot: float = 1.0

    # Agent 19 — MT5 lead -> cTrader target blueprinting.
    ctrader_symbol_map: dict[str, str] = Field(default_factory=dict)
    ctrader_expected_delay_ms: float = Field(default=0.0, ge=0)
    ctrader_max_valid_delay_ms: float = Field(default=2500.0, gt=0)
    ctrader_max_price_drift: float = Field(default=2.0, ge=0)
    ctrader_min_capture_gap: float = Field(default=0.0, ge=0)
    ctrader_max_capture_gap: float | None = Field(default=None, ge=0)

    executor_lease_seconds: float = 60.0
    executor_poll_seconds: float = 2.0
    reconcile_grace_seconds: float = 120.0
    monitor_poll_seconds: float = 2.0

    # ── Sleeping through the close (11B) ──────────────────────────────────────
    #: How long CLOSED must hold on every symbol before a service parks its loops. A
    #: single closed poll is a broker hiccup far more often than it is the weekend.
    close_confirm_seconds: float = 300.0
    #: How far before the scheduled open a sleeping service wakes itself. Being up *at*
    #: the open is too late -- the observer still has a backfill to do.
    preopen_seconds: float = 900.0
    #: Heartbeat cadence while asleep. Slow, never absent.
    sleep_heartbeat_seconds: float = 300.0
    #: How often a sleeping service asks whether the market is open yet.
    sleep_poll_seconds: float = 60.0

    # ── Higher timeframes (11D) ───────────────────────────────────────────────
    #: How many M5 bars the engine keeps for aggregating M15…D1. SEPARATE from the analysis
    #: window, which is a correctness parameter fixed at the hungriest agent's requirement --
    #: widening that to reach H4 would silently rewrite every agent's history.
    #:
    #: 2880 is ten 24-hour days, which covers an H4 EMA(50) (200 hours) with room to spare. D1
    #: needs fifty trading days and will not be reachable from a live buffer at any sane size;
    #: that is what the ``market_day_frames`` cache is for. Zero disables the context entirely.
    mtf_m5_bars: int = 2880

    # ── Evaluation (§84) ──────────────────────────────────────────────────────
    #: The rule for a SINGLE-symbol deployment, and the historical name of this
    #: setting. With more than one symbol it is not enough -- see evaluation_rules.
    evaluation_rule_id: str = "XAU_OUTCOME_V2"

    #: ``AUREON_EVAL_RULES=XAUUSD:XAU_OUTCOME_V2,XAGUSD:XAG_OUTCOME_V1``.
    #:
    #: Required once more than one symbol is configured, because an outcome rule's
    #: thresholds are in the instrument's own money: XAU_OUTCOME_V2 measures $3-$20,
    #: which on silver at ~$30 is 10-65% of price and would never be reached. Sharing
    #: one rule across both symbols would fill the reached-N table with zeros that read
    #: as a finding about silver and are a unit error.
    evaluation_rules: dict[str, str] = Field(default_factory=dict)

    # ── Firestore ─────────────────────────────────────────────────────────────
    firebase_project_id: str | None = None
    google_application_credentials: str | None = None
    firestore_emulator_host: str | None = None

    # ── Discord ───────────────────────────────────────────────────────────────
    discord_token: str | None = Field(default=None, repr=False)
    discord_guild_id: int | None = None
    authorized_user_ids: tuple[str, ...] = ()
    link_window_minutes: int = 90
    #: Where detection embeds are posted (9C). Absent means Aureon announces nothing —
    #: which is the right default: a deployment that has not chosen a channel must not
    #: start posting into whichever one it can see.
    alert_channel_id: int | None = None
    #: How far back the notifier looks for detections it has not announced. Two minutes:
    #: long enough to survive a restart or a slow Firestore write, short enough that a bot
    #: which was down for an hour does not wake up and post an hour of history at once.
    notify_window_seconds: float = 120.0

    # ── 11A F-3: the live-account gate ────────────────────────────────────────
    #: Whether the executor may place orders on a REAL-money account.
    #:
    #: An environment variable on the box, NOT a Firestore setting, and that is the whole
    #: point. ``trading_enabled`` is a switch a human flips in Discord in a hurry; this is
    #: a line somebody had to write on the machine, while looking at which terminal is
    #: open. Making it flippable from the phone would collapse the two decisions into one.
    #:
    #: Fails closed, and ``UNKNOWN`` counts as real money (``AccountMode.is_real_money``):
    #: a terminal that will not say what it is logged into has not said it is a demo.
    allow_live_execution: bool = False

    # ── 11A F-15: ops event thresholds ────────────────────────────────────────
    #: How many closed-candle intervals may pass with nothing arriving, while the market is
    #: OPEN, before `observer_stale` fires. Two rather than one: a single missed interval is a
    #: slow broker or a poll landing a second early, and an alert on that would fire most days.
    ops_observer_stale_intervals: float = 2.0
    #: Seconds without a tick, while OPEN, before `symbol_feed_stale` fires for that symbol.
    #: Thirty because gold and silver tick several times a second in any session that is
    #: really open, so thirty seconds of silence is not quiet -- it is dark.
    ops_feed_stale_seconds: float = 30.0
    #: How long outbox delivery may keep failing before `firestore_unavailable` fires. A
    #: minute, because a few seconds of failure is a retry working as designed and the outbox
    #: exists precisely so that is survivable.
    ops_firestore_unavailable_seconds: float = 60.0
    #: Undelivered outbox rows before `outbox_backlog` fires. Fifty is roughly a busy hour of
    #: detections: below it the queue is draining, above it deliveries are slower than
    #: detections arrive and the gap will only grow.
    ops_outbox_backlog: int = 50

    # ── Reviews ───────────────────────────────────────────────────────────────
    infer_window_minutes: int = 30

    # ── MT5 ───────────────────────────────────────────────────────────────────
    mt5_login: int | None = None
    mt5_password: str | None = Field(default=None, repr=False)
    mt5_server: str | None = None
    mt5_terminal_path: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> AureonConfig:
        ZoneInfo(self.market_tz)  # fail fast on a typo'd zone
        # An UNSET or empty var falls back to the default, which is the ordinary
        # env convention. A whitespace-only value is different: it is a mistake
        # that would otherwise key every detection id in history on " ".
        if not self.account_scope.strip():
            raise ValueError(
                "account_scope must not be blank; it keys every detection id (decision 4)"
            )
        if self.account_scope != self.account_scope.strip():
            raise ValueError(
                f"account_scope must not have surrounding whitespace: {self.account_scope!r}"
            )
        if not self.symbols:
            raise ValueError("at least one symbol must be configured")
        if len(self.symbols) > 1:
            # One rule cannot serve two instruments priced two orders of magnitude
            # apart, so a multi-symbol deployment has to say which rule each uses.
            missing = [s for s in self.symbols if s.upper() not in self.evaluation_rules]
            if missing:
                raise ValueError(
                    f"AUREON_EVAL_RULES must name every configured symbol; missing "
                    f"{', '.join(missing)}. An outcome rule's thresholds are in the "
                    "instrument's own money, so one rule cannot serve two symbols "
                    "(e.g. AUREON_EVAL_RULES=XAUUSD:XAU_OUTCOME_V2,XAGUSD:XAG_OUTCOME_V1)."
                )
        if self.status_stale_after_seconds <= self.state_heartbeat_seconds:
            # Otherwise the observer is reported STALE while writing normally.
            raise ValueError(
                f"status_stale_after_seconds ({self.status_stale_after_seconds}) must "
                f"exceed state_heartbeat_seconds ({self.state_heartbeat_seconds})"
            )
        return self

    @classmethod
    def from_env(cls, *, env: dict[str, str] | None = None) -> AureonConfig:
        """Build config from ``os.environ`` (or an explicit mapping, for tests)."""
        if env is not None:
            saved = dict(os.environ)
            os.environ.clear()
            os.environ.update(env)
            try:
                return cls.from_env()
            finally:
                os.environ.clear()
                os.environ.update(saved)

        timeframes = tuple(
            Timeframe(tf) for tf in _env_csv("AUREON_TIMEFRAMES", ("M5",))
        )
        guild = _env_opt("AUREON_DISCORD_GUILD_ID")
        channel = _env_opt("AUREON_ALERT_CHANNEL_ID")
        login = _env_opt("AUREON_MT5_LOGIN")
        return cls(
            account_scope=_env_str("AUREON_ACCOUNT_SCOPE", "primary"),
            aureon_magic=_env_int("AUREON_MAGIC", 770177),
            market_tz=_env_str("AUREON_MARKET_TZ", "Europe/Athens"),
            symbols=_env_csv("AUREON_SYMBOLS", ("XAUUSD",)),
            timeframes=timeframes,
            ema_fast=_env_int("AUREON_EMA_FAST", 20),
            ema_slow=_env_int("AUREON_EMA_SLOW", 50),
            context_window_candles=_env_int("AUREON_CONTEXT_WINDOW_CANDLES", 3),
            state_heartbeat_seconds=_env_float("AUREON_STATE_HEARTBEAT_SECONDS", 5.0),
            outbox_path=_env_str("AUREON_OUTBOX_PATH", "outbox.db"),
            observer_state_path=_env_str("AUREON_OBSERVER_STATE_PATH", "observer_state.json"),
            confirmation_ttl_seconds=_env_float("AUREON_CONFIRMATION_TTL_SECONDS", 60.0),
            quote_ttl_seconds=_env_float("AUREON_QUOTE_TTL_SECONDS", 15.0),
            status_stale_after_seconds=_env_float("AUREON_STATUS_STALE_AFTER_SECONDS", 45.0),
            max_deviation_points=_env_int("AUREON_MAX_DEVIATION_POINTS", 20),
            max_spread_points=_env_float("AUREON_MAX_SPREAD_POINTS", 50.0),
            max_lot=_env_float("AUREON_MAX_LOT", 1.0),
            ctrader_symbol_map=_env_map("AUREON_CTRADER_SYMBOL_MAP"),
            ctrader_expected_delay_ms=_env_float("AUREON_CTRADER_EXPECTED_DELAY_MS", 0.0),
            ctrader_max_valid_delay_ms=_env_float("AUREON_CTRADER_MAX_VALID_DELAY_MS", 2500.0),
            ctrader_max_price_drift=_env_float("AUREON_CTRADER_MAX_PRICE_DRIFT", 2.0),
            ctrader_min_capture_gap=_env_float("AUREON_CTRADER_MIN_CAPTURE_GAP", 0.0),
            ctrader_max_capture_gap=(
                _env_float("AUREON_CTRADER_MAX_CAPTURE_GAP", 0.0)
                if os.getenv("AUREON_CTRADER_MAX_CAPTURE_GAP") else None
            ),
            executor_lease_seconds=_env_float("AUREON_EXECUTOR_LEASE_SECONDS", 60.0),
            executor_poll_seconds=_env_float("AUREON_EXECUTOR_POLL_SECONDS", 2.0),
            reconcile_grace_seconds=_env_float("AUREON_RECONCILE_GRACE_SECONDS", 120.0),
            monitor_poll_seconds=_env_float("AUREON_MONITOR_POLL_SECONDS", 2.0),
            close_confirm_seconds=_env_float("AUREON_CLOSE_CONFIRM_SECONDS", 300.0),
            preopen_seconds=_env_float("AUREON_PREOPEN_SECONDS", 900.0),
            sleep_heartbeat_seconds=_env_float("AUREON_SLEEP_HEARTBEAT_SECONDS", 300.0),
            sleep_poll_seconds=_env_float("AUREON_SLEEP_POLL_SECONDS", 60.0),
            mtf_m5_bars=_env_int("AUREON_MTF_M5_BARS", 2880),
            # AUREON_EVAL_RULE is the current name. The older
            # AUREON_EVALUATION_RULE_ID still wins when set, so an existing .env
            # pinning EMA_OUTCOME_V1 keeps getting V1 rather than silently switching
            # to a rule with different thresholds and different stored results.
            evaluation_rule_id=_env_str(
                "AUREON_EVAL_RULE",
                _env_str("AUREON_EVALUATION_RULE_ID", "XAU_OUTCOME_V2"),
            ),
            evaluation_rules=_env_map("AUREON_EVAL_RULES"),
            firebase_project_id=_env_opt("AUREON_FIREBASE_PROJECT_ID"),
            google_application_credentials=_env_opt("GOOGLE_APPLICATION_CREDENTIALS"),
            firestore_emulator_host=_env_opt("FIRESTORE_EMULATOR_HOST"),
            discord_token=_env_opt("AUREON_DISCORD_TOKEN"),
            discord_guild_id=int(guild) if guild else None,
            authorized_user_ids=_env_csv("AUREON_AUTHORIZED_USER_IDS"),
            link_window_minutes=_env_int("AUREON_LINK_WINDOW_MINUTES", 90),
            alert_channel_id=int(channel) if channel else None,
            notify_window_seconds=_env_float("AUREON_NOTIFY_WINDOW_SECONDS", 120.0),
            allow_live_execution=_env_true("AUREON_ALLOW_LIVE_EXECUTION"),
            ops_observer_stale_intervals=_env_float(
                "AUREON_OPS_OBSERVER_STALE_INTERVALS", 2.0
            ),
            ops_feed_stale_seconds=_env_float("AUREON_OPS_FEED_STALE_SECONDS", 30.0),
            ops_firestore_unavailable_seconds=_env_float(
                "AUREON_OPS_FIRESTORE_UNAVAILABLE_SECONDS", 60.0
            ),
            ops_outbox_backlog=_env_int("AUREON_OPS_OUTBOX_BACKLOG", 50),
            infer_window_minutes=_env_int("AUREON_INFER_WINDOW_MINUTES", 30),
            mt5_login=int(login) if login else None,
            mt5_password=_env_opt("AUREON_MT5_PASSWORD"),
            mt5_server=_env_opt("AUREON_MT5_SERVER"),
            mt5_terminal_path=_env_opt("AUREON_MT5_TERMINAL_PATH"),
        )

    def rule_id_for(self, symbol: str) -> str:
        """The outcome rule id for one symbol.

        Falls back to ``evaluation_rule_id`` ONLY for a single-symbol deployment, which
        is every existing one: the validator above refuses a multi-symbol config that
        does not name each symbol's rule, so this can never quietly measure silver
        against gold's thresholds.
        """
        named = self.evaluation_rules.get(symbol.upper())
        if named:
            return named
        if len(self.symbols) > 1:  # pragma: no cover - the validator refuses this first
            raise KeyError(f"no evaluation rule configured for {symbol}")
        return self.evaluation_rule_id

    @property
    def uses_emulator(self) -> bool:
        return bool(self.firestore_emulator_host)

    def is_authorized(self, discord_user_id: str) -> bool:
        """Whether a Discord user may command Aureon (§71).

        Fails closed: an empty allowlist authorises nobody. An empty allowlist
        meaning "everyone" would turn a missing env var into an open trading bot.
        """
        return discord_user_id in self.authorized_user_ids

    def __repr__(self) -> str:
        def render(name: str) -> str:
            value = getattr(self, name)
            if name in _SECRET_FIELDS and value:
                return f"{name}=***"
            return f"{name}={value!r}"

        shown = ", ".join(
            render(name)
            for name in ("account_scope", "market_tz", "symbols", "mt5_password", "discord_token")
        )
        return f"AureonConfig({shown}, ...)"

    __str__ = __repr__
