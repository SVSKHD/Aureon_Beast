#!/usr/bin/env python3
"""Run the whole Aureon backend from one command (12, T-1).

    python main_aureon.py

Loads ``.env`` once, runs the normal preflight, then starts the observer, the position monitor,
the executor, the Discord bot and the review watcher as five separate child processes. Every
safety boundary they already had is unchanged: starting this does not enable trading, and the
executor still refuses a real-money account unless ``AUREON_ALLOW_LIVE_EXECUTION`` is exactly
``true`` and ``settings/execution.trading_enabled`` is on.

Why a launcher at all, when five terminals worked: because five terminals meant five chances to
start the stack half-configured, and no single place that noticed a service had died. See
``aureon/services/supervisor.py`` for the stop-on-crash contract.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from aureon.services.supervisor import (
    DEFAULT_SERVICES,
    ENV_FILE_VAR,
    AureonSupervisor,
    EnvFileError,
    PLANNED_RESTART_EXIT_CODE,
    ServiceSpec,
    build_ops_register,
    load_env_file,
)
from aureon.services.runtime_guardian import build_runtime_guardian

log = logging.getLogger("aureon.launcher")


def build_parser() -> argparse.ArgumentParser:
    """The command line, in one place so the tests use the real one.

    A test that rebuilt an equivalent parser would keep passing after a flag was renamed here,
    which is the drift the selection logic below is most exposed to.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env", help="default: .env")
    parser.add_argument("--no-observer", action="store_true")
    parser.add_argument("--no-monitor", action="store_true")
    parser.add_argument("--no-executor", action="store_true")
    parser.add_argument("--no-discord", action="store_true")
    parser.add_argument("--no-review", action="store_true")
    parser.add_argument(
        "--no-preflight",
        action="store_true",
        help="development only: start without scripts/preflight.py",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        env = load_env_file(Path(args.env_file))
    except (OSError, EnvFileError) as exc:
        log.error("cannot load %s: %s", args.env_file, exc)
        return 2

    if env.found:
        log.info("loaded %d value(s) from %s", env.applied, env.path)
        if env.overridden_by_environment:
            # Named, not counted. "Why is it writing to the wrong prefix" is answered here or
            # not at all: the file says one thing and the environment already said another.
            log.info(
                "already set in the environment, so %s was NOT used for: %s",
                env.path,
                ", ".join(env.overridden_by_environment),
            )
        # Handed to the children and to the preflight, so its `config` row can name the file
        # rather than assume `.env` (T-3).
        os.environ[ENV_FILE_VAR] = str(env.path)
    else:
        log.warning("%s not found; using the current process environment", env.path)
        os.environ.setdefault(ENV_FILE_VAR, "")

    services = _selected(args, parser)
    ops = build_ops_register()
    guardian = build_runtime_guardian(
        repo_root=Path(__file__).resolve().parent,
        ops=ops,
        run_preflight=not args.no_preflight,
    )
    supervisor = AureonSupervisor(services=services, ops=ops, guardian=guardian)
    code = supervisor.run(preflight=not args.no_preflight)
    if code == PLANNED_RESTART_EXIT_CODE:
        restart_args = [sys.executable, str(Path(__file__).resolve()), *(argv or sys.argv[1:])]
        log.warning("re-executing Aureon after planned runtime-guardian restart")
        os.execv(sys.executable, restart_args)
    return code


def _selected(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[ServiceSpec, ...]:
    """The services left after the ``--no-*`` flags, in the canonical start order.

    Order comes from ``DEFAULT_SERVICES`` and not from the flags, so that disabling the monitor
    cannot silently move the executor ahead of the observer.
    """
    disabled = {
        "observer": args.no_observer,
        "monitor": args.no_monitor,
        "executor": args.no_executor,
        "discord": args.no_discord,
        "review": args.no_review,
    }
    services = tuple(spec for spec in DEFAULT_SERVICES if not disabled[spec.name])
    if not services:
        parser.error("all services are disabled; there would be nothing to run")
    return services


if __name__ == "__main__":
    sys.exit(main())
