"""Starting the whole backend with one command, and stopping it without losing the queue (12, T-1).

Five processes, not five threads. Each existing entrypoint stays exactly what it was: a
separate OS process with its own MT5 connection, its own Firestore client and its own crash.
The supervisor starts them in order, watches them, and takes the stack down if one dies.

## Why separate processes, and not one event loop

The observer holds a blocking MT5 terminal handle, the executor holds an exclusive lease, and
Discord holds a gateway websocket. Sharing one loop would mean one unhandled exception can stop
all three, and one MetaTrader5 call that blocks for four seconds stalls the gateway heartbeat.
Worse, it would put the broker connection in the same process as the observer, and the rule that
a detection can never create a trade would become a matter of discipline rather than of address
space. Five processes keep it structural: the observer has no broker handle to place an order
with, and a boundary test greps the observation side for the broker's own vocabulary -- which is
why this paragraph describes that vocabulary instead of quoting it.

## Stop-on-crash, not restart

A child exiting unexpectedly takes the whole stack down. That is the contract, and it is the
conservative choice: a half-running stack is the dangerous state. An executor running without
the monitor means positions nobody reconciles; an observer running without the executor means
confirmed requests that sit; Discord running alone means a human confirming a trade that will
never be claimed. Each of those *looks* healthy from the one screen the operator has. So the
supervisor refuses to be the thing that hides it -- it logs ``child_exited``, records the
condition in ``ops_events`` so it survives the terminal scrollback, stops the rest and exits
non-zero for whatever restarted it (systemd, a scheduled task, a human).

Automatic restart with backoff was in an earlier design and is deliberately not here. Restarting
a child that crashed on a poisoned Firestore document produces a loop that writes the same error
forever; restarting an executor mid-reconciliation is how an unknown broker outcome becomes two.

## Shutdown is cooperative first, because the observer has a queue

The observer's shutdown flushes the outbox, flushes the parquet archive and saves its cursor
(§75). A terminated observer loses the queue's in-flight tail and re-does work on the next boot;
a killed one can leave a partially written parquet file. So Ctrl+C sends the children a real
interrupt and then *waits* ``AUREON_SHUTDOWN_GRACE_SECONDS`` for them to finish, in reverse start
order, and only terminates what is left.

The children are started in their own process group (``start_new_session`` on POSIX,
``CREATE_NEW_PROCESS_GROUP`` on Windows) so that a console Ctrl+C reaches the supervisor ONLY.
Without that, the terminal delivers SIGINT to the whole foreground group, every child begins
shutting down at once while the supervisor is also shutting them down, and the ordering the
paragraph above describes is not the ordering that happens. One signal, sent by one owner, in a
known order.
"""

from __future__ import annotations

import io
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("aureon.supervisor")
REPO_ROOT = Path(__file__).resolve().parents[2]

#: How long the children get to flush after the interrupt, before anything is terminated.
#: Twenty seconds because the observer's shutdown does three network-bound things -- drain the
#: outbox, write the archive, save the cursor -- and a Firestore write that is retrying can take
#: several seconds on its own. Overridable for tests and for an operator who knows their box.
DEFAULT_SHUTDOWN_GRACE_SECONDS = 20.0

#: The environment variable the launcher sets to the ``.env`` it loaded, so that the preflight's
#: ``config`` row can name the file rather than guessing at one (T-3). Empty means none.
ENV_FILE_VAR = "AUREON_ENV_FILE"


@dataclass(frozen=True)
class ServiceSpec:
    """One child process: the name it is logged under, and how to start it."""

    name: str
    argv: tuple[str, ...]


DEFAULT_SERVICES: tuple[ServiceSpec, ...] = (
    # The order is §75's and is not cosmetic. The observer first, so that the archive and the
    # outbox are being drained before anything else can produce work. The monitor before the
    # executor, so that a position opened by the executor's first poll is already being watched.
    # The executor before Discord, so that a human cannot confirm a request into a process that
    # has not claimed its lease yet. Discord before the review watcher only because the watcher
    # is the one service nothing else waits on.
    ServiceSpec("observer", ("main_observer.py",)),
    ServiceSpec("monitor", ("main_monitor.py",)),
    ServiceSpec("executor", ("main_executor.py",)),
    ServiceSpec("discord", ("main_discord.py",)),
    ServiceSpec("review", ("main_review.py", "watch")),
)

SERVICE_NAMES: tuple[str, ...] = tuple(spec.name for spec in DEFAULT_SERVICES)


@dataclass(frozen=True)
class EnvFile:
    """What happened when the launcher tried to load a ``.env``.

    ``found`` is separate from ``applied`` because zero applied values has two very different
    causes -- there was no file, or every key in it was already set in the environment -- and an
    operator debugging "why is it using the wrong project" needs to know which.
    """

    path: Path
    found: bool
    applied: int
    #: Keys present in the file that the process environment already had, so the file's value was
    #: NOT used. Named rather than counted: this is the single most confusing thing about dotenv
    #: precedence, and "AUREON_FIREBASE_PROJECT_ID came from the environment, not from .env" is
    #: the sentence that ends the debugging session.
    overridden_by_environment: tuple[str, ...] = ()


class EnvFileError(ValueError):
    """A line in the ``.env`` that is not ``KEY=value``."""


def load_env_file(path: Path, *, override: bool = False) -> EnvFile:
    """Load a dotenv file into ``os.environ``, process environment winning by default.

    ``override=False`` is the contract, not a default worth changing: the launcher passes its
    environment to every child, and a child that re-read the file with ``override=True`` would
    silently disagree with its parent about the collection prefix or the account. That is the
    failure mode where half the stack writes to ``aureon_test_`` and half to ``aureon_beast_``.

    Parsing is python-dotenv's, so quoting, ``export`` and interpolation behave the way every
    other tool in the ecosystem reads the same file. What is NOT python-dotenv's is the
    strictness: a malformed line raises here, where a bare ``load_dotenv`` would skip it. A
    silently dropped ``AUREON_ALLOW_LIVE_EXECUTION=true`` is a safety-relevant surprise, and the
    operator who typed it would see a successful start and a guard that refuses everything.
    """
    from dotenv.parser import parse_stream

    resolved = path.expanduser()
    if not resolved.exists():
        return EnvFile(resolved, found=False, applied=0)

    text = resolved.read_text(encoding="utf-8")
    values: dict[str, str] = {}
    for line_no, binding in enumerate(parse_stream(io.StringIO(text)), 1):
        if binding.error:
            raise EnvFileError(
                f"{resolved}:{line_no}: expected KEY=value, got "
                f"{binding.original.string.strip()!r}"
            )
        if binding.key is None:
            continue
        values[binding.key] = binding.value or ""

    applied = 0
    skipped: list[str] = []
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
            applied += 1
        else:
            skipped.append(key)
    return EnvFile(
        resolved, found=True, applied=applied, overridden_by_environment=tuple(skipped)
    )


@dataclass(frozen=True)
class Exited:
    """A child that is no longer running, and what it exited with."""

    name: str
    code: int


def build_ops_register(service: str = "supervisor") -> Any | None:
    """An ``OpsRegister`` writing to Firestore, or ``None`` if one cannot be built.

    ``None`` rather than raising: the supervisor's job is to start the stack, and a launcher that
    refused to run because it could not reach Firestore would turn a reporting outage into a
    trading outage. The preflight is the thing that refuses. Every call site here logs as well as
    records, so a missing register loses the durable copy and nothing else.
    """
    try:
        from aureon.config import AureonConfig
        from aureon.services.ops_events import OpsRegister
        from aureon.storage.firebase_service import get_client
        from aureon.storage.ops_repository import OpsEventRepository

        config = AureonConfig.from_env()
        client = get_client(
            project_id=config.firebase_project_id,
            emulator_host=config.firestore_emulator_host,
        )
        return OpsRegister(OpsEventRepository(client), service=service)
    except Exception:  # noqa: BLE001 - see the docstring
        log.warning("could not reach the ops register; failures will only be logged", exc_info=True)
        return None


class AureonSupervisor:
    """Start, watch and stop Aureon's long-lived service processes."""

    def __init__(
        self,
        *,
        services: Iterable[ServiceSpec] = DEFAULT_SERVICES,
        python: str | None = None,
        cwd: Path = REPO_ROOT,
        preflight_argv: tuple[str, ...] = ("scripts/preflight.py",),
        startup_grace_seconds: float = 1.0,
        shutdown_grace_seconds: float | None = None,
        terminate_grace_seconds: float = 5.0,
        ops: Any | None = None,
    ) -> None:
        self.services = tuple(services)
        self.python = python or sys.executable
        self.cwd = cwd
        self.preflight_argv = preflight_argv
        self.startup_grace_seconds = startup_grace_seconds
        self.shutdown_grace_seconds = (
            shutdown_grace_seconds
            if shutdown_grace_seconds is not None
            else _grace_from_env()
        )
        self.terminate_grace_seconds = terminate_grace_seconds
        self.ops = ops
        #: Insertion-ordered, which is start order; ``reversed`` on it is stop order.
        self.processes: dict[str, subprocess.Popen] = {}

    # ── starting ──────────────────────────────────────────────────────────────

    def run_preflight(self) -> int:
        cmd = [self.python, *self.preflight_argv]
        log.info("preflight: %s", " ".join(cmd))
        return subprocess.run(cmd, cwd=self.cwd, env=os.environ.copy()).returncode

    def start(self) -> None:
        """Start every service in order, refusing to continue past one that dies at once.

        The per-service pause is a smoke check and is documented as one: a child that takes
        longer than the grace to fail is caught by :meth:`wait` instead, with the same outcome.
        What the pause buys is the error message -- "discord exited during startup with code 2"
        names the cause where a later exit only says a child died.
        """
        for spec in self.services:
            cmd = [self.python, *spec.argv]
            log.info("starting %-8s %s", spec.name, " ".join(cmd))
            process = subprocess.Popen(
                cmd, cwd=self.cwd, env=os.environ.copy(), **_isolation_kwargs()
            )
            self.processes[spec.name] = process
            try:
                code = process.wait(timeout=self.startup_grace_seconds)
            except subprocess.TimeoutExpired:
                continue
            raise StartupFailed(spec.name, code)

    # ── watching ──────────────────────────────────────────────────────────────

    def wait(self, *, poll_seconds: float = 0.5) -> Exited:
        """Block until one child exits, and say which. ``KeyboardInterrupt`` propagates."""
        while True:
            for name, process in self.processes.items():
                code = process.poll()
                if code is not None:
                    return Exited(name, code)
            time.sleep(poll_seconds)

    # ── stopping ──────────────────────────────────────────────────────────────

    def stop(self) -> None:
        """Interrupt every live child, wait out the grace, then terminate and kill stragglers.

        One shared deadline rather than a grace per child: five children at twenty seconds each
        would be a hundred-second Ctrl+C, and they flush in parallel anyway. Reverse start order
        for the signalling, so the observer -- the one with a queue to drain -- is asked last and
        therefore has the longest to finish.
        """
        alive = [(n, p) for n, p in self.processes.items() if p.poll() is None]
        if not alive:
            return

        for name, process in reversed(alive):
            log.info("interrupting %s (pid %s)", name, process.pid)
            _interrupt(process)

        deadline = time.monotonic() + self.shutdown_grace_seconds
        for name, process in reversed(alive):
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
                log.info("%s stopped cleanly", name)
            except subprocess.TimeoutExpired:
                pass

        stubborn = [(n, p) for n, p in alive if p.poll() is None]
        for name, process in reversed(stubborn):
            log.warning("%s did not stop within the grace; terminating it", name)
            _quietly(process.terminate)

        deadline = time.monotonic() + self.terminate_grace_seconds
        for name, process in reversed(stubborn):
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
                continue
            except subprocess.TimeoutExpired:
                log.error("%s refused termination; killing it", name)
                _quietly(process.kill)
            # Reaped, not just signalled. SIGKILL cannot be caught, so this returns at once --
            # but WITHOUT it the launcher returns from `stop` while the child is still a live
            # (then zombie) process, having reported the stack stopped when it had not. A test
            # planting the missing wait is what found this.
            try:
                process.wait(timeout=self.terminate_grace_seconds)
            except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL was ignored
                log.error("%s survived SIGKILL; it is now the operating system's problem", name)

    # ── the whole run ─────────────────────────────────────────────────────────

    def run(self, *, preflight: bool = True) -> int:
        if preflight:
            code = self.run_preflight()
            if code != 0:
                log.error("preflight failed; no Aureon services were started")
                return code
        else:
            log.warning(
                "--no-preflight: starting without checking credentials, the terminal or "
                "Firestore. A misconfigured stack will fail later and less clearly."
            )

        try:
            self.start()
        except StartupFailed as failure:
            log.error(
                "child_exited service=%s code=%s during=startup", failure.name, failure.code
            )
            self._record_stack_down(failure.name, failure.code, during="startup")
            self.stop()
            return failure.code or 1
        except KeyboardInterrupt:
            log.info("interrupted during startup; stopping what was started")
            self.stop()
            return 130
        except Exception:
            log.exception("supervisor could not start the stack")
            self.stop()
            return 1

        log.info("Aureon is running (%s)", ", ".join(self.processes))
        self._record_stack_up()

        try:
            exited = self.wait()
        except KeyboardInterrupt:
            log.info(
                "interrupt received; giving the children %.0fs to flush",
                self.shutdown_grace_seconds,
            )
            self.stop()
            return 0

        log.error("child_exited service=%s code=%s during=running", exited.name, exited.code)
        self._record_stack_down(exited.name, exited.code, during="running")
        self.stop()
        # A child that exits 0 on its own is still a stack that is no longer whole, so the
        # launcher's own exit code must not say success -- whatever restarted it would take a
        # zero as "asked to stop" and leave Aureon down.
        return exited.code or 1

    # ── ops reporting ─────────────────────────────────────────────────────────

    def _record_stack_up(self) -> None:
        self._observe(active=False, detail=f"running: {', '.join(self.processes)}")

    def _record_stack_down(self, name: str, code: int, *, during: str) -> None:
        self._observe(active=True, detail=f"{name} exited with code {code} during {during}")

    def _observe(self, *, active: bool, detail: str) -> None:
        if self.ops is None:
            return
        try:
            self.ops.observe("supervisor_stack_down", active=active, detail=detail)
        except Exception:  # noqa: BLE001 - reporting must never be the thing that fails
            log.warning("could not record the supervisor's ops event", exc_info=True)


class StartupFailed(RuntimeError):
    """A child exited within the startup grace."""

    def __init__(self, name: str, code: int) -> None:
        super().__init__(f"{name} exited during startup with code {code}")
        self.name = name
        self.code = code


def _grace_from_env() -> float:
    raw = os.environ.get("AUREON_SHUTDOWN_GRACE_SECONDS", "").strip()
    if not raw:
        return DEFAULT_SHUTDOWN_GRACE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "AUREON_SHUTDOWN_GRACE_SECONDS=%r is not a number; using %.0fs",
            raw,
            DEFAULT_SHUTDOWN_GRACE_SECONDS,
        )
        return DEFAULT_SHUTDOWN_GRACE_SECONDS
    if value <= 0:
        # Zero would mean "terminate immediately", which is the one thing the grace exists to
        # prevent. Refusing it loudly beats an operator discovering it by losing a queue.
        log.warning("AUREON_SHUTDOWN_GRACE_SECONDS=%s is not positive; using %.0fs", value,
                    DEFAULT_SHUTDOWN_GRACE_SECONDS)
        return DEFAULT_SHUTDOWN_GRACE_SECONDS
    return value


def _isolation_kwargs() -> dict[str, Any]:
    """Put each child in its own process group -- see the module docstring."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _interrupt(process: subprocess.Popen) -> None:
    """Ask one child to shut down cooperatively.

    CTRL_BREAK on Windows because CTRL_C_EVENT cannot be delivered to a single process group --
    the API ignores the group argument for it -- and the children register ``SIGBREAK`` for
    exactly this reason. ``killpg`` on POSIX rather than ``send_signal`` so that a child which
    spawned helpers of its own takes them with it.
    """
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        _quietly(lambda: process.send_signal(signal.CTRL_BREAK_EVENT))
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGINT)
    except (OSError, ProcessLookupError):
        _quietly(lambda: process.send_signal(signal.SIGINT))


def _quietly(action: Any) -> None:
    try:
        action()
    except (OSError, ProcessLookupError, ValueError):
        pass
