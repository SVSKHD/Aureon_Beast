"""Making two writers actually overlap.

A test that starts two threads and joins them proves almost nothing about locking. The
operations here take well under a millisecond, so thread A usually finishes and commits
before thread B has started -- and the test passes whether or not any lock exists. Removing
``FOR UPDATE`` from every repository in this package left five such tests green, which is
how this module came to exist (decision 352).

The Firestore repository had the same hole, and said so in its own docstring: its unit tests
ran the read-check-write with no isolation and the atomicity was asserted nowhere. Porting
the tests without porting a real race would have carried the hole across the migration.

**What this does instead.** ``interleave`` runs two callables with a guaranteed overlap:

1. A starts its transaction and takes its lock, then signals.
2. B waits for that signal, then attempts the same lock.
3. A holds its transaction open for ``hold`` seconds, then commits.

With a lock, B blocks at step 2 until A commits and then reads the state A wrote. Without
one, B reads the pre-A state immediately and both writers apply. The asymmetry is what makes
this test able to fail: a spurious PASS would need B to take longer than ``hold`` to issue
one statement, and a spurious FAILURE is not possible at all -- B either sees A's write or it
does not.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

#: How long the first writer holds its transaction open after taking the lock. Generous on
#: purpose: the cost of a larger value is a slower test, and the cost of too small a value is
#: a test that stops detecting a missing lock.
HOLD_SECONDS = 0.75

#: How long to wait for a thread. Must exceed HOLD_SECONDS by enough that a BLOCKED second
#: writer -- the correct behaviour -- is never mistaken for a hung one.
JOIN_TIMEOUT = 30.0


class Interleaved:
    """The outcome of a deliberately overlapped pair of writers."""

    def __init__(self) -> None:
        self.results: dict[str, Any] = {}
        self.errors: dict[str, BaseException] = {}

    @property
    def succeeded(self) -> list[str]:
        return sorted(self.results)

    @property
    def failed(self) -> list[str]:
        return sorted(self.errors)

    def __repr__(self) -> str:
        return f"Interleaved(succeeded={self.succeeded}, failed={self.failed})"


def interleave(
    first: Callable[[Callable[[], None]], Any],
    second: Callable[[], Any],
    *,
    hold: float = HOLD_SECONDS,
) -> Interleaved:
    """Run ``first`` and ``second`` with ``second`` starting while ``first`` holds its lock.

    ``first`` is handed a callable to invoke once it has taken its lock and is still inside
    its transaction. Everything after that call happens while ``second`` is already trying.

    Both outcomes are collected rather than raised, because for most of these tests EITHER
    writer may legitimately be the one refused -- what matters is that exactly one succeeds.
    """
    locked = threading.Event()
    outcome = Interleaved()

    def run_first() -> None:
        def signal() -> None:
            locked.set()
            # Hold the lock while the second writer tries to take it.
            threading.Event().wait(hold)

        try:
            outcome.results["first"] = first(signal)
        except BaseException as exc:  # noqa: BLE001 - a refusal is a legitimate outcome
            outcome.errors["first"] = exc
        finally:
            locked.set()  # never leave the other thread waiting on a crash

    def run_second() -> None:
        if not locked.wait(timeout=JOIN_TIMEOUT):
            outcome.errors["second"] = TimeoutError("the first writer never took its lock")
            return
        try:
            outcome.results["second"] = second()
        except BaseException as exc:  # noqa: BLE001
            outcome.errors["second"] = exc

    threads = [
        threading.Thread(target=run_first, name="first"),
        threading.Thread(target=run_second, name="second"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT)
        assert not thread.is_alive(), f"{thread.name} did not finish; a lock is probably held"
    return outcome
