"""Every service listens for the same three signals (12, T-1).

The interesting one is ``SIGBREAK``. Aureon runs on a Windows VPS, and there the launcher cannot
use CTRL_C: ``GenerateConsoleCtrlEvent`` ignores the process-group argument for ``CTRL_C_EVENT``,
so a Ctrl+C aimed at one child would either go nowhere or go to everything. CTRL_BREAK can be
aimed, and it arrives in the child as ``SIGBREAK`` -- a signal whose default action is to
terminate. A service that registered only SIGINT would therefore be killed outright by the
launcher's own cooperative shutdown, losing exactly the outbox drain the grace period exists to
protect.

That failure cannot be reproduced on Linux, which is the whole reason it is asserted here rather
than left to a manual Windows run: the box where it matters is not the box the suite runs on.
"""

from __future__ import annotations

import signal

import pytest

from aureon.services.shutdown import flushed, install_handlers, stop_signals


def test_every_stop_signal_this_platform_has_is_listened_for() -> None:
    names = {getattr(sig, "name", str(sig)) for sig in stop_signals()}
    assert "SIGINT" in names
    assert "SIGTERM" in names
    if hasattr(signal, "SIGBREAK"):  # pragma: no cover - Windows only
        assert "SIGBREAK" in names


def test_windows_ctrl_break_is_covered_by_name_on_every_platform() -> None:
    """The register is by NAME, so the Windows-only signal is not forgotten on Linux.

    Planted as ``("SIGINT",)`` this fails, which is the point: the list is the contract, and it
    has to be checkable from the platform that cannot deliver the signal.
    """
    import inspect

    source = inspect.getsource(stop_signals)
    assert "SIGBREAK" in source
    assert "SIGTERM" in source


def test_installing_handlers_returns_what_it_registered() -> None:
    seen: list[str] = []
    before = {sig: signal.getsignal(sig) for sig in stop_signals()}
    try:
        registered = install_handlers(lambda: seen.append("stopped"), service="probe")
        assert set(registered) == set(stop_signals())
        for sig in registered:
            handler = signal.getsignal(sig)
            assert callable(handler)
            handler(int(sig), None)
        assert seen == ["stopped"] * len(registered)
    finally:
        for sig, handler in before.items():
            signal.signal(sig, handler)


def test_the_handler_only_sets_the_flag_and_never_flushes() -> None:
    """A signal handler runs between bytecodes; a Firestore write in one is a deadlock waiting.

    The loop's ``finally`` does the flushing. All the handler may do is ask the loop to stop.
    """
    import inspect

    source = inspect.getsource(install_handlers)
    for forbidden in ("flush", "client", "repository", "requests"):
        assert forbidden not in source.split('"""')[2], (
            f"the handler body mentions {forbidden!r}; it must only set the stop flag"
        )


@pytest.mark.parametrize("service", ["observer", "monitor", "executor"])
def test_the_flushed_line_carries_a_greppable_token(service: str, caplog) -> None:
    """``shutdown_flushed`` is a fixed token because an operator greps for it after Ctrl+C."""
    with caplog.at_level("INFO"):
        flushed(service, queued=0)
    assert any(
        "shutdown_flushed" in record.message and f"service={service}" in record.message
        for record in caplog.records
    )


@pytest.mark.parametrize(
    "entrypoint", ["main_observer.py", "main_monitor.py", "main_executor.py", "main_review.py"]
)
def test_every_long_lived_entrypoint_uses_the_shared_installer(entrypoint: str) -> None:
    """Four copies of the same three lines is how one of them ends up missing SIGBREAK.

    Checked in the source rather than by running the entrypoints, which need MT5 and Firestore.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[2].joinpath(entrypoint).read_text(encoding="utf-8")
    assert "install_handlers(" in source, f"{entrypoint} registers its own signal handlers"
    assert "signal.signal(" not in source, (
        f"{entrypoint} still registers a handler directly; use install_handlers"
    )
