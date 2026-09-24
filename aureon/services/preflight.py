"""Everything that must be true before a session starts (P-2).

A live session is expensive to repeat: it needs a market to be open, a terminal to be
running and a human to be watching. Most of the ways it fails are knowable in the first
two seconds -- a terminal logged into the wrong server, a collection prefix still
pointing at production, a data directory that is not writable, a clock two minutes off.
This finds those before the market opens rather than during the review afterwards.

## Every check reports one of five things, and they are not interchangeable

PASS, FAIL, WARN, SKIP, INFO -- defined once in ``aureon.services.checks``, which says
why two statuses would be a lie. The ones that matter here: ``--skip-mt5`` produces
SKIPs, never passes, and ``trading_enabled`` is INFO because whether it should be on
depends on which session this is.

## What it does not do

It never places an order, never touches ``settings.trading_enabled``, and never writes a
detection. It writes exactly two documents: ``heartbeats/preflight``, which is the
round-trip it is testing, and ``symbol_specs/{symbol}``, which is the same publish the
observer performs seconds later.
"""

from __future__ import annotations

import inspect
import os
import shutil
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from aureon.config.config import AureonConfig
from aureon.models.base import utc_now
from aureon.models.enums import AccountMode, Timeframe
from aureon.services.checks import CheckReport, CheckResult, Status
from aureon.storage import paths

#: The drift a broker clock may have from this machine's before it matters (P-2).
#: Two seconds because a candle boundary is decided on this clock: at M5, a clock two
#: seconds fast reads a bar as closed while the terminal is still writing it.
DEFAULT_DRIFT_TOLERANCE_SECONDS = 2.0

#: Beyond this, a stale last tick says "the market is quiet", not "the clock is wrong",
#: and the two cannot be told apart from one sample. See ``check_clock_drift``.
MARKET_IDLE_SECONDS = 120.0

#: Below this, the archive directory is reported as short of room. A broker day of M5
#: parquet is well under a megabyte, so this is generous by two orders of magnitude --
#: it is meant to catch a full disk, not to budget.
MIN_FREE_MEGABYTES = 50.0

#: The heartbeat document preflight writes. Deliberately NOT one of ``paths.SERVICES``:
#: writing ``heartbeats/observer`` would make a dead observer look alive, which is the
#: exact lie the heartbeat exists to prevent.
PREFLIGHT_SERVICE = "preflight"


def _probe_writable(directory: Path) -> None:
    """Prove a directory is writable by writing to it. Raises OSError if not.

    An ``os.access`` check would answer a different question -- what the permission bits
    say -- and would pass on a full disk, a read-only mount and a stale NFS handle.
    """
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".preflight-", delete=True):
        pass


class Preflight:
    """Runs the checks. Every dependency is injectable, so every check has a unit test.

    ``provider_factory`` and ``client_factory`` are called at most once each, lazily, so
    a run with ``--skip-mt5`` never constructs a terminal connection and a run on a
    machine with no Firestore credentials still reports the local checks.
    """

    def __init__(
        self,
        config: AureonConfig,
        *,
        provider_factory: Callable[[], Any] | None = None,
        client_factory: Callable[[], Any] | None = None,
        now: Callable[[], datetime] = utc_now,
        symbol: str | None = None,
        timeframe: Timeframe | None = None,
        skip_mt5: bool = False,
        drift_tolerance_seconds: float = DEFAULT_DRIFT_TOLERANCE_SECONDS,
        archive_dir: Path | None = None,
        outbox_path: Path | None = None,
        database_factory: Any | None = None,
    ) -> None:
        self.config = config
        self.now = now
        self.symbol = symbol or config.symbols[0]
        self.timeframe = timeframe or config.timeframes[0]
        self.skip_mt5 = skip_mt5
        self.drift_tolerance_seconds = drift_tolerance_seconds
        self.archive_dir = archive_dir
        self.outbox_path = outbox_path or Path(config.outbox_path)
        self._provider_factory = provider_factory or self._default_provider
        self._client_factory = client_factory or self._default_client
        self._database_factory = database_factory or self._default_database
        self._provider: Any | None = None
        self._client: Any | None = None

    # ── Default wiring ────────────────────────────────────────────────────────

    def _default_provider(self) -> Any:
        from aureon.data.mt5_provider import MT5DataProvider

        return MT5DataProvider(
            market_tz=self.config.market_tz,
            login=self.config.mt5_login,
            password=self.config.mt5_password,
            server=self.config.mt5_server,
            terminal_path=self.config.mt5_terminal_path,
        )

    def _default_database(self) -> Any:
        from aureon.storage.local_database import get_database

        return get_database()

    def _default_client(self) -> Any:
        # Kept under the historical attribute name so test injection remains compatible.
        # Production receives a local repository bundle, not a cloud client.
        from aureon.storage.runtime import build_storage

        return build_storage(
            account_scope=self.config.account_scope,
            state_heartbeat_seconds=self.config.state_heartbeat_seconds,
        )

    # ── The run ───────────────────────────────────────────────────────────────

    def run(self) -> CheckReport:
        """Every check, in the order a human would want them: local, stored, broker."""
        report = CheckReport()
        for check in (
            self.check_config,
            self.check_code_contract,
            self.check_outbox,
            self.check_archive_dir,
            self.check_local_storage,
            self.check_trading_enabled,
            self.check_mt5_init,
            self.check_mt5_account,
            self.check_symbol_tradable,
            self.check_symbol_specs,
            self.check_clock_drift,
        ):
            report.results.append(check())
        return report

    # ── Local ─────────────────────────────────────────────────────────────────

    def _rule_id(self) -> str:
        """THIS symbol's outcome rule (9A).

        Printed on the config line, so a two-symbol deployment's preflight for silver says
        `XAG_OUTCOME_V1` rather than whichever rule the environment's single-symbol setting
        happens to name.
        """
        try:
            return self.config.rule_id_for(self.symbol)
        except KeyError:
            # The config validator refuses this, but preflight exists to report a broken
            # configuration rather than to crash on one.
            return f"none configured for {self.symbol}"

    def check_config(self) -> CheckResult:
        """The identity and clock this session will be recorded under.

        Reported rather than judged, with one exception: the market timezone's CURRENT
        offset, because that is the number an operator compares against the terminal's
        clock, and "Europe/Athens" alone does not tell them whether to expect +2 or +3.
        """
        from zoneinfo import ZoneInfo

        offset = self.now().astimezone(ZoneInfo(self.config.market_tz)).utcoffset()
        hours = (offset.total_seconds() / 3600) if offset else 0.0
        detail = (
            f"env_file={self._env_file()} "
            f"account_scope={self.config.account_scope} "
            f"symbols={','.join(self.config.symbols)} "
            f"tf={','.join(t.value for t in self.config.timeframes)} "
            f"ema={self.config.ema_fast}/{self.config.ema_slow} "
            f"tz={self.config.market_tz} (UTC{hours:+.0f} now) "
            f"rule={self._rule_id()}"
        )
        if self.symbol not in self.config.symbols:
            return CheckResult(
                "config",
                Status.FAIL,
                f"{detail} — but preflight was asked about {self.symbol}",
                remedy=(
                    f"{self.symbol} is not in AUREON_SYMBOLS, so the observer will not "
                    "watch it. Fix one or the other."
                ),
            )
        return CheckResult("config", Status.PASS, detail)

    def check_code_contract(self) -> CheckResult:
        """Refuse a mixed checkout whose setup engine and repository APIs disagree.

        A partial merge can leave setup_engine.py calling repository methods that the local
        setup_repository.py does not provide. Python imports both successfully, so without
        this check the observer appears healthy until the first live setup candle, then silently
        stops persisting setup state.
        """
        try:
            from aureon.storage.postgres.repositories.setups import SetupRepository

            missing = [
                name
                for name in ("open_for_symbol", "record", "refresh_live_context")
                if not callable(getattr(SetupRepository, name, None))
            ]
            record_params = inspect.signature(SetupRepository.record).parameters
            if "agent_confluence" not in record_params:
                missing.append("record(agent_confluence=...)")
        except Exception as exc:  # noqa: BLE001 - preflight must report, not crash
            return CheckResult(
                "code_contract",
                Status.FAIL,
                f"{type(exc).__name__}: {exc}",
                remedy="Sync a clean current main/feature branch before starting Aureon.",
            )

        if missing:
            return CheckResult(
                "code_contract",
                Status.FAIL,
                "incompatible runtime SetupRepository API: " + ", ".join(missing),
                remedy=(
                    "The runtime SQL setup repository does not match the setup engine. Stop Aureon, "
                    "sync the complete branch, and let startup upgrade the local SQLite schema."
                ),
            )
        return CheckResult(
            "code_contract",
            Status.PASS,
            "setup engine/repository API compatible",
        )

    def _env_file(self) -> str:
        """Which ``.env`` the launcher loaded, or ``none`` (12, T-3).

        Printed because the commonest configuration surprise is not a wrong value but a file that
        was never read -- a `--env-file` typo, or a service started from a different directory.
        Every other row on the table describes a value; this one describes where the values came
        from, and without it "symbols=XAUUSD" is indistinguishable from "symbols defaulted
        because nothing was loaded".

        ``none`` rather than ``.env`` when unset: the launcher sets the variable to the empty
        string when it found no file, and printing a plausible filename for a file nobody read is
        the failure this line exists to prevent.
        """
        from aureon.services.supervisor import ENV_FILE_VAR

        return os.environ.get(ENV_FILE_VAR, "").strip() or "none"

    def check_collection_prefix(self) -> CheckResult:
        """Which database this session will write into.

        A WARN rather than a FAIL on the production prefix: writing to production is a
        legitimate thing to do eventually, and refusing it would make preflight
        something to be bypassed. But a first validation session writing into the same
        collections as everything else is almost never what was meant, and the mistake
        is invisible afterwards -- the documents look identical.
        """
        detail = f"prefix={paths.PREFIX} (e.g. {paths.DETECTIONS})"
        if paths.PREFIX == paths.DEFAULT_COLLECTION_PREFIX:
            return CheckResult(
                "collection_prefix",
                Status.WARN,
                f"{detail} — the PRODUCTION default",
                remedy=(
                    "Set AUREON_COLLECTION_PREFIX to something else for a validation "
                    "session; the data is worth keeping separate from production."
                ),
            )
        return CheckResult("collection_prefix", Status.PASS, detail)

    def check_outbox(self) -> CheckResult:
        """The durable queue is writable, and what it is still holding.

        The pending count is the point. An outbox with rows left from a previous run
        means detections were produced and never reached Firestore, and starting a new
        session on top of that makes the two runs' failures impossible to separate.
        """
        from aureon.outbox.local_outbox import LocalOutbox

        path = self.outbox_path
        try:
            _probe_writable(path.parent if path.parent.as_posix() else Path())
            with LocalOutbox(path) as outbox:
                pending = outbox.pending_count()
                total = len(outbox)
        except Exception as exc:  # sqlite3.Error, OSError -- both mean "unusable"
            return CheckResult(
                "outbox",
                Status.FAIL,
                f"{path}: {exc}",
                remedy="Point AUREON_OUTBOX_PATH somewhere writable.",
            )
        detail = f"{path} ({total} rows, {pending} pending)"
        if pending:
            return CheckResult(
                "outbox",
                Status.WARN,
                detail,
                remedy=(
                    f"{pending} detection(s) from a previous run never reached "
                    "Firestore. Drain them before this session, or their failure and "
                    "this session's become one indistinguishable story."
                ),
            )
        return CheckResult("outbox", Status.PASS, detail)

    def check_archive_dir(self) -> CheckResult:
        """Where the live candles go, and whether there is room for them (§82).

        Without the archive there is no way to tell a live/replay difference caused by
        the engine from one caused by the broker serving different bars, so an
        unwritable directory costs the whole session's evidence, not a log line.
        """
        from aureon.data.live_candle_archive import DEFAULT_ARCHIVE_DIR

        directory = self.archive_dir or DEFAULT_ARCHIVE_DIR
        try:
            _probe_writable(directory)
            free_mb = shutil.disk_usage(directory).free / (1024 * 1024)
        except OSError as exc:
            return CheckResult(
                "archive_dir",
                Status.FAIL,
                f"{directory}: {exc}",
                remedy="Create it, or pass --archive-dir somewhere writable.",
            )
        detail = f"{directory} ({free_mb:,.0f} MB free)"
        if free_mb < MIN_FREE_MEGABYTES:
            return CheckResult(
                "archive_dir",
                Status.WARN,
                detail,
                remedy=f"Under {MIN_FREE_MEGABYTES:.0f} MB free.",
            )
        return CheckResult("archive_dir", Status.PASS, detail)

    def check_local_storage(self) -> CheckResult:
        """Local SQLite is required; no cloud credentials are involved."""
        try:
            self._client = self._client_factory()
            database = self._database_factory()
            database.probe()
            database.transaction_probe()
            path = getattr(database, "path", getattr(database, "name", "local"))
            return CheckResult("local_storage", Status.PASS, f"sqlite WAL · {path}")
        except Exception as exc:
            self._client = None
            return CheckResult(
                "local_storage",
                Status.FAIL,
                f"{type(exc).__name__}: {exc}",
                remedy="Check AUREON_LOCAL_DB_PATH and local file permissions.",
            )

    # ── Firestore ─────────────────────────────────────────────────────────────

    def _client_or_none(self) -> Any | None:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def check_credentials(self) -> CheckResult:
        """Whether Firestore credentials are configured, and which way (11A, F-2).

        Ahead of the round trip so a missing key file is reported as a missing key file. The
        round trip behind this is still the proof; this is the diagnosis, because "permission
        denied", "no credentials found" and "that is an OAuth client secret, not a
        service-account key" all arrive as one opaque exception otherwise.
        """
        from aureon.services import credentials

        verdict = credentials.inspect(
            emulator_host=self.config.firestore_emulator_host,
            project_id=self.config.firebase_project_id,
            collection_prefix=paths.PREFIX,
        )
        if verdict.not_applicable:
            # SKIP, not PASS. A green row for a check that never ran is how an
            # emulator-only run comes to look like evidence about production.
            return CheckResult("credentials", Status.SKIP, verdict.detail)
        if not verdict.ok:
            return CheckResult(
                "credentials", Status.FAIL, verdict.detail, remedy=verdict.remedy
            )
        if verdict.remedy is not None:
            # Configured, but by the path that only works on a developer machine. Worth
            # saying out loud on a box that is about to run unattended for a week.
            return CheckResult(
                "credentials", Status.WARN, verdict.detail, remedy=verdict.remedy
            )
        return CheckResult("credentials", Status.PASS, verdict.detail)

    def check_firestore(self) -> CheckResult:
        """A real write and a real read-back, against the prefix this session will use.

        Round trip rather than "the client constructed": credentials that resolve, a
        project that exists and rules that permit a write are three different things,
        and only the last one is what the observer needs in ten minutes.
        """
        from aureon.storage.system_state_repository import HeartbeatRepository

        started = self.now()
        try:
            client = self._client_or_none()
            # min_interval 0 and force: the throttle exists to protect a service loop,
            # and a throttled no-op here would report a round trip that never happened.
            repository = HeartbeatRepository(client, min_interval_seconds=0.0)
            written = repository.beat(
                PREFLIGHT_SERVICE,
                detail={"symbol": self.symbol, "prefix": paths.PREFIX},
                force=True,
                now=started,
            )
            read_back = repository.read(PREFLIGHT_SERVICE)
        except Exception as exc:
            self._client = None
            from aureon.services import credentials

            if credentials.is_permission_error(exc):
                # The credentials resolved and the identity they name cannot write. A different
                # action from a missing key, and the generic remedy would send an operator
                # looking for a better key file (12, T-3).
                return CheckResult(
                    "firestore",
                    Status.FAIL,
                    f"{paths.HEARTBEATS}: permission denied writing to this prefix "
                    f"({type(exc).__name__})",
                    remedy=credentials.PERMISSION_REMEDY,
                )
            if credentials.is_default_credentials_error(exc):
                # The one failure mode worth naming separately: the library could not find
                # credentials at all, which is a different action from a permission denial
                # and from a wrong project (11A, F-2).
                return CheckResult(
                    "firestore",
                    Status.FAIL,
                    f"{paths.HEARTBEATS}: no usable credentials "
                    f"({type(exc).__name__})",
                    remedy=credentials.REMEDY,
                )
            return CheckResult(
                "firestore",
                Status.FAIL,
                f"{paths.HEARTBEATS}: {type(exc).__name__}: {exc}",
                remedy=(
                    f"Firestore, not MT5: check {credentials.CREDENTIALS_ENV}, "
                    f"{credentials.PROJECT_ENV}, and that the rules permit a write to this "
                    "prefix."
                ),
            )
        if not written or read_back is None:
            return CheckResult(
                "firestore",
                Status.FAIL,
                f"wrote {paths.heartbeat_path(PREFLIGHT_SERVICE)} but read back nothing",
                remedy="The write reported success and the document is not there.",
            )
        if read_back.updated_at != started:
            return CheckResult(
                "firestore",
                Status.FAIL,
                f"read back {read_back.updated_at.isoformat()}, wrote "
                f"{started.isoformat()}",
                remedy="The round trip returned a different document than it wrote.",
            )
        elapsed_ms = (self.now() - started).total_seconds() * 1000
        return CheckResult(
            "firestore",
            Status.PASS,
            f"{paths.heartbeat_path(PREFLIGHT_SERVICE)} round trip "
            f"({elapsed_ms:.0f} ms)",
        )

    def check_migrations(self) -> CheckResult:
        """Is this database's schema the one THIS build was written against? (plan §6, §35)

        SKIPs while the storage backend is still Firestore, because there is no PostgreSQL
        to have a schema yet -- S-4 is the commit that switches over, and a row that FAILED
        before then would be a preflight that fails on a correctly-configured system.

        When it does run it FAILS in BOTH directions, which is C-7 and is the whole point:

        * **behind** -- somebody deployed without migrating. A query will hit a missing
          column. Remedy: run the migration.
        * **ahead** -- an OLDER build has started against a schema a NEWER one wrote. This
          is the dangerous one, and the one a ``>=`` check misses. The build reads the
          tables it knows, writes the columns it knows, and leaves every column added since
          silently NULL. On ``trade_requests`` that is a broker ticket or a failure code
          that never gets recorded, in a run that reports success. Remedy: run the right
          build -- migrating again would do nothing and look like a fix.
        * **drifted** -- the revision matches and the TABLES do not, because a model was
          changed without generating a migration or a migration was hand-edited. Everything
          looks healthy until a query mentions the column that is not there.
        """
        from aureon.storage.backend import is_postgres
        from aureon.storage.postgres import schema

        if not is_postgres():
            return CheckResult(
                "migrations",
                Status.SKIP,
                "storage backend is not postgres; no schema to check yet",
            )

        try:
            database = self._database_factory()
        except Exception as exc:  # noqa: BLE001 - a bad URL is a report, not a crash
            return CheckResult(
                "migrations",
                Status.FAIL,
                str(exc),
                remedy="set AUREON_DATABASE_URL (see .env.example)",
            )

        try:
            state = schema.compare(database)
            if state.status is schema.SchemaStatus.UNKNOWN:
                return CheckResult(
                    "migrations",
                    Status.FAIL,
                    state.render(),
                    remedy="start the PostgreSQL service, then re-run preflight",
                )
            if state.status is schema.SchemaStatus.AHEAD:
                return CheckResult(
                    "migrations",
                    Status.FAIL,
                    state.render(),
                    remedy="run the build that matches the schema; do NOT migrate again",
                )
            if not state.ok:
                return CheckResult(
                    "migrations",
                    Status.FAIL,
                    state.render(),
                    remedy="python scripts/migrate.py upgrade",
                )

            differences = schema.drift(database)
            if differences:
                return CheckResult(
                    "migrations",
                    Status.FAIL,
                    f"{state.render()}, but the tables do not match the models: "
                    + "; ".join(differences),
                    remedy="python -m alembic revision --autogenerate, then migrate",
                )
            return CheckResult("migrations", Status.PASS, state.render())
        finally:
            database.dispose()

    def check_trading_enabled(self) -> CheckResult:
        """Printed, never judged (§56).

        Whether trading should be on depends on which session this is -- an observation
        run wants it false, a demo execution drill wants it true -- so preflight refuses
        to have an opinion and refuses to let it go unseen.
        """
        if self._client is None:
            return CheckResult(
                "trading_enabled",
                Status.SKIP,
                "not read: local storage unavailable",
                remedy="Fix the local_storage check first.",
            )
        try:
            if hasattr(self._client, "settings"):
                settings = self._client.settings.read()
            else:
                # Compatibility for the in-memory repository double used by legacy unit tests.
                from aureon.storage.settings_repository import ExecutionSettingsRepository
                settings = ExecutionSettingsRepository(self._client).read()
        except Exception as exc:
            return CheckResult(
                "trading_enabled", Status.FAIL, f"{type(exc).__name__}: {exc}"
            )
        if settings is None:
            return CheckResult(
                "trading_enabled",
                Status.INFO,
                f"false (no {paths.execution_settings_path()} document; the default is "
                "fail-closed)",
            )
        stamp = (
            settings.updated_at.isoformat() if settings.updated_at else "never written"
        )
        return CheckResult(
            "trading_enabled",
            Status.INFO,
            f"{str(settings.trading_enabled).lower()} "
            f"(v{settings.settings_version}, by {settings.updated_by or '—'} at "
            f"{stamp}, max_lot={settings.max_lot})",
        )

    # ── The terminal ──────────────────────────────────────────────────────────

    def _skipped_mt5(self, name: str) -> CheckResult:
        if self.skip_mt5:
            return CheckResult(name, Status.SKIP, "not run: --skip-mt5")
        return CheckResult(
            name,
            Status.SKIP,
            "not run: the terminal is not connected",
            remedy="Fix mt5_init first.",
        )

    def check_mt5_init(self) -> CheckResult:
        if self.skip_mt5:
            return CheckResult(
                "mt5_init",
                Status.SKIP,
                "not run: --skip-mt5",
                remedy="A session cannot start on a skipped terminal check.",
            )
        try:
            provider = self._provider_factory()
            provider.connect()
        except Exception as exc:
            return CheckResult(
                "mt5_init",
                Status.FAIL,
                f"{type(exc).__name__}: {exc}",
                remedy=(
                    "Start the MT5 terminal and log in, then re-run. MetaTrader5 is "
                    "Windows-only; on any other OS this check cannot pass."
                ),
            )
        self._provider = provider
        try:
            terminal = provider.terminal_info()
        except Exception:
            terminal = {}
        build = terminal.get("build", "?")
        name = terminal.get("name", "?")
        return CheckResult(
            "mt5_init", Status.PASS, f"connected to {name} build {build}"
        )

    def check_mt5_account(self) -> CheckResult:
        """Which account the terminal is on, and whether it is the one the config names.

        The failure this prevents is the expensive one: a terminal left logged into a different
        server produces a perfectly plausible session whose candles came from somewhere else, and
        nothing downstream records which server they came from.

        ## Attaching is the DEFAULT, not a fallback (12, T-3)

        ``mt5.initialize()`` with no credentials attaches to whatever terminal is running and
        already logged in, and that is how Aureon is meant to run: the operator logs the terminal
        in once, and the services attach. ``AUREON_MT5_LOGIN``/``PASSWORD``/``SERVER``/
        ``TERMINAL_PATH`` are **optional overrides**, for a box with more than one terminal or a
        login that must be pinned.

        Until T-3 this row returned WARN when no login was configured, with a remedy telling the
        operator to set the variables. That was the wrong way round: it made the normal
        configuration look like an incomplete one, and the remedy asked for a password in a
        `.env` file that did not need one. It now PASSES and names the attached account, which is
        the information the row exists to carry. What it must never do is pass *quietly* -- the
        account is printed either way, because "which account is this terminal on" is the question
        nobody thinks to ask until the answer is the expensive one.
        """
        if self._provider is None:
            return self._skipped_mt5("mt5_account")
        try:
            account = self._provider.account_info()
        except Exception as exc:
            return CheckResult(
                "mt5_account", Status.FAIL, f"{type(exc).__name__}: {exc}"
            )

        login, server = account.get("login"), account.get("server")
        # 11A F-3. Printed on every preflight, because "which account is this terminal on"
        # is the question nobody thinks to ask until the answer is the expensive one.
        mode = AccountMode.from_trade_mode(account.get("trade_mode"))
        detail = (
            f"login={login} server={server} mode={mode.value.upper()} "
            f"currency={account.get('currency', '?')} "
            f"trade_allowed={str(account.get('trade_allowed', False)).lower()}"
        )
        wanted_login, wanted_server = self.config.mt5_login, self.config.mt5_server
        mismatches = []
        if wanted_login is not None and int(login or 0) != int(wanted_login):
            mismatches.append(f"login is {login}, config says {wanted_login}")
        if wanted_server is not None and str(server) != str(wanted_server):
            mismatches.append(f"server is {server!r}, config says {wanted_server!r}")
        if mismatches:
            return CheckResult(
                "mt5_account",
                Status.FAIL,
                f"{detail} — " + "; ".join(mismatches),
                remedy=(
                    "The terminal is logged into a different account than the config "
                    "names. Every candle this session records would come from there."
                ),
            )
        attached = wanted_login is None and wanted_server is None
        if attached and not mode.is_real_money:
            # The normal configuration: no MT5 variables set, attached to the terminal the
            # operator logged in. PASS, and say which account it is.
            return CheckResult(
                "mt5_account",
                Status.PASS,
                f"using attached terminal account: {detail}",
            )
        if mode.is_real_money:
            # WARN rather than FAIL: observation on a live account is legitimate and
            # read-only, and the executor refuses execution there on its own (F-3). But it
            # never passes quietly -- an operator scanning a green table would not notice.
            #
            # This outranks the attach case above deliberately: "no login configured" must not
            # turn a real-money terminal into a PASS.
            prefix = "using attached terminal account: " if attached else ""
            return CheckResult(
                "mt5_account",
                Status.WARN,
                f"{prefix}{detail} — REAL MONEY"
                if mode is AccountMode.REAL
                else f"{prefix}{detail} — the terminal did not report its account mode",
                remedy=(
                    "Observation is read-only and safe here. The executor will refuse "
                    "execution unless AUREON_ALLOW_LIVE_EXECUTION=true, and the demo "
                    "drills refuse to run at all."
                ),
            )
        return CheckResult("mt5_account", Status.PASS, f"verified against the config: {detail}")

    def check_symbol_tradable(self) -> CheckResult:
        """The symbol exists, is visible, and the broker will accept an order on it."""
        if self._provider is None:
            return self._skipped_mt5("symbol_tradable")
        try:
            info = self._provider.symbol_info(self.symbol)
        except Exception as exc:
            return CheckResult(
                "symbol_tradable",
                Status.FAIL,
                f"{self.symbol}: {type(exc).__name__}: {exc}",
                remedy=f"Add {self.symbol} to the terminal's Market Watch.",
            )
        detail = (
            f"{info.symbol} point={info.point} digits={info.digits} "
            f"vol={info.volume_min}/{info.volume_step}/{info.volume_max} "
            f"stops={info.stops_level} fill={','.join(m.value for m in info.filling_modes) or '—'} "
            f"trade_mode={info.trade_mode}"
        )
        if info.trade_mode == "disabled":
            return CheckResult(
                "symbol_tradable",
                Status.FAIL,
                detail,
                remedy=f"The broker has {self.symbol} closed to trading.",
            )
        if not info.filling_modes:
            return CheckResult(
                "symbol_tradable",
                Status.FAIL,
                detail,
                remedy=(
                    "The symbol reports no supported filling mode, so no order can be "
                    "built for it (§39)."
                ),
            )
        if info.trade_mode != "full":
            return CheckResult(
                "symbol_tradable",
                Status.WARN,
                detail,
                remedy=(
                    f"trade_mode is {info.trade_mode}: some order directions will be "
                    "rejected by the broker."
                ),
            )
        return CheckResult("symbol_tradable", Status.PASS, detail)

    def check_symbol_specs(self) -> CheckResult:
        """Publish the broker's metadata and read it back (decision 79).

        The check is named ``symbol_specs_published`` rather than after the collection:
        a string constant equal to a bare collection name is exactly what the §83
        boundary guard forbids, and a check label is not worth an exemption to a rule
        that exists because a bare literal fails silently.

        This is the path Discord depends on: it may never call the broker, so a lot it
        validates against stale or missing specs is validated against nothing.
        """
        if self._provider is None:
            return self._skipped_mt5("symbol_specs_published")
        if self._client is None:
            return CheckResult(
                "symbol_specs_published",
                Status.SKIP,
                "not run: local storage unavailable",
                remedy="Fix the local_storage check first.",
            )
        try:
            info = self._provider.symbol_info(self.symbol)
            if hasattr(self._client, "symbols"):
                repository = self._client.symbols
            else:
                # Compatibility for the in-memory repository double used by unit tests.
                from aureon.storage.symbol_repository import SymbolRepository
                repository = SymbolRepository(self._client)
            path = repository.publish(info, now=self.now())
            stored = repository.get(self.symbol)
        except Exception as exc:
            return CheckResult(
                "symbol_specs_published", Status.FAIL, f"{type(exc).__name__}: {exc}"
            )
        if stored != info:
            return CheckResult(
                "symbol_specs_published",
                Status.FAIL,
                f"{path} read back different metadata than was published",
                remedy="Discord would validate lots against something else entirely.",
            )
        return CheckResult("symbol_specs_published", Status.PASS, f"{path} published and verified")

    def check_clock_drift(self) -> CheckResult:
        """This machine's clock against the broker's, to the extent one sample can.

        It matters because candle boundaries are decided on the LOCAL clock: a clock
        running fast reads a bar as closed while the terminal is still writing it, and
        the detection that follows is computed from a candle that later changed.

        The measurement is a last tick's timestamp, and it is asymmetric on purpose:

        * a tick **in the future** is unambiguous -- no tick can have happened after
          now, so a clock behind the broker's is a FAIL;
        * a tick **in the past** by more than the tolerance is *inconclusive*, because a
          quiet market and a clock running fast produce exactly the same number. It is
          reported as a WARN naming the lag, not as a pass and not as a failure.

        The honest resolution is to re-run it while the symbol is actually ticking.
        """
        if self._provider is None:
            return self._skipped_mt5("clock_drift")
        try:
            tick_at = self._provider.last_tick_time(self.symbol)
        except Exception as exc:
            return CheckResult(
                "clock_drift", Status.FAIL, f"{type(exc).__name__}: {exc}"
            )
        if tick_at is None:
            return CheckResult(
                "clock_drift",
                Status.SKIP,
                f"the terminal reports no tick for {self.symbol}",
                remedy="Nothing was measured. Re-run while the symbol is ticking.",
            )

        lag = (self.now() - tick_at).total_seconds()
        stamp = f"last tick {tick_at.isoformat()}, {lag:+.1f}s from local now"
        if lag < -self.drift_tolerance_seconds:
            return CheckResult(
                "clock_drift",
                Status.FAIL,
                f"{stamp} — the broker's clock is AHEAD of this machine's",
                remedy=(
                    "A tick cannot happen in the future, so this is real drift. Sync "
                    "this machine's clock (w32tm /resync) before the session."
                ),
            )
        if lag <= self.drift_tolerance_seconds:
            return CheckResult("clock_drift", Status.PASS, stamp)
        if lag > MARKET_IDLE_SECONDS:
            return CheckResult(
                "clock_drift",
                Status.SKIP,
                f"{stamp} — the market looks closed, so drift was not measured",
                remedy="Re-run within a few minutes of the open.",
            )
        return CheckResult(
            "clock_drift",
            Status.WARN,
            f"{stamp} — either a quiet market or a fast local clock",
            remedy=(
                "One sample cannot tell those apart. Re-run while the symbol is "
                "ticking; if the lag persists, sync this machine's clock."
            ),
        )
