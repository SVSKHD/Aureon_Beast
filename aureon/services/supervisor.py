"""Process supervisor for the complete Aureon runtime.

Each existing Aureon entrypoint remains a separate process. The supervisor only
starts them in the intended order, watches them, and shuts the whole stack down
if one service exits unexpectedly.

Default runtime:
    observer -> monitor -> executor -> discord -> review watcher

A preflight runs before any long-lived process starts. Starting the executor
does not enable trading; its existing live-account and Firestore gates remain
authoritative.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

log = logging.getLogger("aureon.supervisor")
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    argv: tuple[str, ...]


DEFAULT_SERVICES: tuple[ServiceSpec, ...] = (
    ServiceSpec("observer", ("main_observer.py",)),
    ServiceSpec("monitor", ("main_monitor.py",)),
    ServiceSpec("executor", ("main_executor.py",)),
    ServiceSpec("discord", ("main_discord.py",)),
    ServiceSpec("review", ("main_review.py", "watch")),
)


def load_env_file(path: Path, *, override: bool = False) -> int:
    """Load a simple dotenv file into os.environ.

    Supports KEY=value, optional export, comments, blank lines, and quoted
    values. Existing process environment wins unless override=True.
    """
    if not path.exists():
        return 0

    applied = 0
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            raise ValueError(f"{path}:{line_no}: expected KEY=value")
        key = key.strip()
        if not key:
            raise ValueError(f"{path}:{line_no}: empty environment key")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
            applied += 1
    return applied


class AureonSupervisor:
    """Start, monitor and stop Aureon's long-lived service processes."""

    def __init__(
        self,
        *,
        services: Iterable[ServiceSpec] = DEFAULT_SERVICES,
        python: str | None = None,
        cwd: Path = REPO_ROOT,
        startup_grace_seconds: float = 1.0,
    ) -> None:
        self.services = tuple(services)
        self.python = python or sys.executable
        self.cwd = cwd
        self.startup_grace_seconds = startup_grace_seconds
        self.processes: dict[str, subprocess.Popen] = {}

    def run_preflight(self) -> int:
        cmd = [self.python, "scripts/preflight.py"]
        log.info("preflight: %s", " ".join(cmd))
        return subprocess.run(cmd, cwd=self.cwd, env=os.environ.copy()).returncode

    def start(self) -> None:
        for spec in self.services:
            cmd = [self.python, *spec.argv]
            log.info("starting %-8s %s", spec.name, " ".join(cmd))
            process = subprocess.Popen(cmd, cwd=self.cwd, env=os.environ.copy())
            self.processes[spec.name] = process
            time.sleep(self.startup_grace_seconds)
            code = process.poll()
            if code is not None:
                raise RuntimeError(
                    f"{spec.name} exited during startup with code {code}"
                )

    def wait(self, *, poll_seconds: float = 1.0) -> int:
        while True:
            for name, process in self.processes.items():
                code = process.poll()
                if code is not None:
                    log.error("%s exited with code %s", name, code)
                    return code if code != 0 else 1
            time.sleep(poll_seconds)

    def stop(self, *, timeout_seconds: float = 10.0) -> None:
        alive = [(name, p) for name, p in self.processes.items() if p.poll() is None]
        for name, process in reversed(alive):
            log.info("stopping %s", name)
            try:
                process.terminate()
            except OSError:
                pass

        deadline = time.monotonic() + timeout_seconds
        for name, process in reversed(alive):
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                log.warning("%s did not stop cleanly; killing it", name)
                try:
                    process.kill()
                except OSError:
                    pass

    def run(self, *, preflight: bool = True) -> int:
        if preflight:
            code = self.run_preflight()
            if code != 0:
                log.error("preflight failed; no Aureon services were started")
                return code

        try:
            self.start()
            log.info("Aureon is running (%s)", ", ".join(self.processes))
            return self.wait()
        except KeyboardInterrupt:
            log.info("interrupted")
            return 0
        except Exception:
            log.exception("supervisor startup/runtime failure")
            return 1
        finally:
            self.stop()
