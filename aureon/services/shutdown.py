"""One way for every service process to be asked to stop (12, T-1).

Three signals, not one, and the third is the reason this module exists rather than four copies of
the same three lines in the entrypoints.

* ``SIGINT`` -- a console Ctrl+C, and what the launcher sends on POSIX.
* ``SIGTERM`` -- what systemd, Docker and ``kill`` send.
* ``SIGBREAK`` -- Windows only, raised by ``CTRL_BREAK_EVENT``. The launcher has to use
  CTRL_BREAK rather than CTRL_C there, because CTRL_C_EVENT cannot be aimed at a single process
  group, and a child that registered only SIGINT would be killed outright by it. Aureon runs on
  a Windows VPS, so "only on Windows" means "on the box that matters".

Every handler does the same small thing: log which signal arrived and set the loop's stop flag.
It never flushes, because a signal handler runs between bytecodes and the flush wants Firestore;
the loop's own ``finally`` does that work on the way out.

A second signal is deliberately NOT escalated to a hard exit. An operator pressing Ctrl+C twice
usually means "I am impatient", and the thing they would interrupt is the outbox drain -- the one
part of shutdown that loses data if abandoned. The launcher's grace period, and then its
terminate, is what puts a bound on it.
"""

from __future__ import annotations

import logging
import signal
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


def stop_signals() -> tuple[Any, ...]:
    """The signals a service listens for, skipping any this platform does not have."""
    names = ("SIGINT", "SIGTERM", "SIGBREAK")
    return tuple(getattr(signal, name) for name in names if hasattr(signal, name))


def install_handlers(stop: Callable[[], None], *, service: str) -> tuple[Any, ...]:
    """Register ``stop`` for every stop signal; return the ones registered.

    Returning them is for the tests: "the handler is installed for SIGBREAK on Windows" is a
    claim worth being able to check without sending a signal to the test runner.
    """

    def handle(signum: int, _frame: Any) -> None:
        log.info("%s received signal %s; shutting down", service, signum)
        stop()

    registered: list[Any] = []
    for sig in stop_signals():
        try:
            signal.signal(sig, handle)
        except (ValueError, OSError):  # pragma: no cover - a non-main thread, or no such signal
            log.warning("%s could not register handler for %s", service, sig)
            continue
        registered.append(sig)
    return tuple(registered)


def flushed(service: str, **detail: Any) -> None:
    """The line the launcher and the runbook both look for after a clean stop.

    A fixed token (``shutdown_flushed``) rather than prose, because the thing an operator needs
    to know after Ctrl+C is whether the observer got its queue out before the grace expired, and
    grepping for one word beats reading five services' worth of shutdown chatter.
    """
    extra = " ".join(f"{k}={v}" for k, v in detail.items())
    log.info("shutdown_flushed service=%s%s", service, f" {extra}" if extra else "")
