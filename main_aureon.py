#!/usr/bin/env python3
"""Run the complete Aureon backend from one command.

    python main_aureon.py

Loads .env once, runs the normal preflight, then starts the existing observer,
monitor, executor, Discord bot and review watcher as separate child processes.
Their existing safety boundaries remain unchanged.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from aureon.services.supervisor import (
    DEFAULT_SERVICES,
    AureonSupervisor,
    ServiceSpec,
    load_env_file,
)


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    env_path = Path(args.env_file)
    try:
        loaded = load_env_file(env_path)
    except (OSError, ValueError) as exc:
        logging.getLogger("aureon").error("cannot load %s: %s", env_path, exc)
        return 2

    if env_path.exists():
        logging.getLogger("aureon").info(
            "loaded %d value(s) from %s", loaded, env_path
        )
    else:
        logging.getLogger("aureon").warning(
            "%s not found; using the current process environment", env_path
        )

    disabled = {
        "observer": args.no_observer,
        "monitor": args.no_monitor,
        "executor": args.no_executor,
        "discord": args.no_discord,
        "review": args.no_review,
    }
    services: tuple[ServiceSpec, ...] = tuple(
        spec for spec in DEFAULT_SERVICES if not disabled[spec.name]
    )
    if not services:
        parser.error("all services are disabled")

    supervisor = AureonSupervisor(services=services)
    return supervisor.run(preflight=not args.no_preflight)


if __name__ == "__main__":
    sys.exit(main())
