#!/usr/bin/env python3
"""A stand-in for one Aureon service process, for the supervisor's tests (12, T-1).

Real subprocesses, not mocks. The things T-1 promises -- that a console Ctrl+C reaches the
children, that the observer gets time to flush before anything is terminated, that one child
dying takes the stack down -- are all properties of process groups, signal delivery and exit
codes. A mocked ``Popen`` would agree with whatever the test asserted, including when the
production code had the signal wrong, which is exactly the class of bug that only shows up on the
VPS at 03:00.

Behaviours:

* ``exit``    -- exit with ``--code`` after ``--delay`` seconds. ``--delay 0`` dies inside the
                 supervisor's startup grace (the "exited during startup" case); a delay past it
                 dies while the supervisor is watching (the "exited during running" case), and
                 the two take different paths through ``run``.
* ``hang``    -- ignore every stop signal and loop forever. The straggler that must be terminated.
* ``flush``   -- on a stop signal, sleep ``--flush-seconds``, write ``--marker``, then exit 0.
                 Stands in for the observer draining its outbox. The marker records WHICH signal
                 arrived, which is the part that matters: a marker alone cannot tell "the
                 launcher interrupted me and waited" from "the launcher terminated me and I
                 happened to handle SIGTERM too", and two plants survived a test that only
                 looked for the file.
* ``serve``   -- exit 0 on a stop signal, immediately. The well-behaved service.

Every behaviour appends its name to ``--order-file`` on startup, under a lock-free append (one
``open(..., "a")`` write of one short line is atomic enough on both platforms for this), which is
how the start ORDER is checked without the test having to watch pids.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

STOP = False
STOP_SIGNAL: int | None = None


def _stop_signals() -> list[int]:
    return [
        getattr(signal, name)
        for name in ("SIGINT", "SIGTERM", "SIGBREAK")
        if hasattr(signal, name)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument(
        "--behaviour", choices=("exit", "hang", "flush", "serve"), default="serve"
    )
    parser.add_argument("--code", type=int, default=0)
    parser.add_argument("--order-file")
    parser.add_argument("--marker")
    parser.add_argument("--flush-seconds", type=float, default=0.3)
    parser.add_argument("--delay", type=float, default=0.0)
    args = parser.parse_args(argv)

    if args.order_file:
        with open(args.order_file, "a", encoding="utf-8") as handle:
            handle.write(f"{args.name}\n")
            handle.flush()

    if args.behaviour == "exit":
        if args.delay:
            time.sleep(args.delay)
        return args.code

    def handle(signum: int, _frame: object) -> None:
        global STOP, STOP_SIGNAL
        STOP = True
        STOP_SIGNAL = signum

    for sig in _stop_signals():
        if args.behaviour == "hang":
            # SIG_IGN, not a handler that does nothing: a handler would still interrupt the
            # sleep, and the point of this behaviour is a child that genuinely will not leave.
            signal.signal(sig, signal.SIG_IGN)
        else:
            signal.signal(sig, handle)

    while not STOP:
        time.sleep(0.05)

    if args.behaviour == "flush":
        # Deliberately AFTER the signal: a supervisor that terminated instead of waiting would
        # kill this process here, and the marker would never appear.
        time.sleep(args.flush_seconds)
        if args.marker:
            name = (
                signal.Signals(STOP_SIGNAL).name if STOP_SIGNAL is not None else "none"
            )
            Path(args.marker).write_text(
                f"{args.name} flushed pid={os.getpid()} signal={name}\n", encoding="utf-8"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
