"""The launcher, tested with real child processes (12, T-1).

Every process-shaped promise here is checked by starting actual subprocesses:
``tests/fixtures/child_stub.py`` stands in for a service. Mocking ``Popen`` was the obvious
cheaper option and is the wrong one -- what T-1 promises is about process groups, signal
delivery and exit codes, and a fake ``Popen`` agrees with the test no matter which signal the
production code sends.

The env-file tests are ordinary unit tests, because a dotenv file is just a file.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from aureon.services.supervisor import (
    DEFAULT_SERVICES,
    DEFAULT_SHUTDOWN_GRACE_SECONDS,
    ENV_FILE_VAR,
    AureonSupervisor,
    EnvFileError,
    ServiceSpec,
    StartupFailed,
    _grace_from_env,
    load_env_file,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
STUB = "tests/fixtures/child_stub.py"

#: Small on purpose. The production default is one second per service, which would make this
#: module take half a minute for nothing: the grace is a smoke check, and what it is checking
#: (does the child die instantly) happens well inside a tenth of a second.
FAST_STARTUP = 0.25


def stub(
    name: str,
    *,
    behaviour: str = "serve",
    order_file: Path | None = None,
    marker: Path | None = None,
    code: int = 0,
    delay: float = 0.0,
    flush_seconds: float = 0.3,
) -> ServiceSpec:
    argv: list[str] = [STUB, "--name", name, "--behaviour", behaviour]
    if order_file is not None:
        argv += ["--order-file", str(order_file)]
    if marker is not None:
        argv += ["--marker", str(marker)]
    if code:
        argv += ["--code", str(code)]
    if delay:
        argv += ["--delay", str(delay)]
    if behaviour == "flush":
        argv += ["--flush-seconds", str(flush_seconds)]
    return ServiceSpec(name, tuple(argv))


def supervisor(*specs: ServiceSpec, **kwargs: object) -> AureonSupervisor:
    kwargs.setdefault("startup_grace_seconds", FAST_STARTUP)
    kwargs.setdefault("shutdown_grace_seconds", 3.0)
    kwargs.setdefault("terminate_grace_seconds", 3.0)
    return AureonSupervisor(services=specs, cwd=REPO_ROOT, **kwargs)  # type: ignore[arg-type]


class RecordingOps:
    """The ops register's ``observe`` contract, remembered rather than written."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool, str]] = []

    def observe(self, name: str, *, active: bool, detail: str = "", **_: object) -> None:
        self.calls.append((name, active, detail))


# ── the service map ───────────────────────────────────────────────────────────


def test_default_supervisor_services_are_complete() -> None:
    assert [(s.name, s.argv) for s in DEFAULT_SERVICES] == [
        ("observer", ("main_observer.py",)),
        ("monitor", ("main_monitor.py",)),
        ("executor", ("main_executor.py",)),
        ("discord", ("main_discord.py",)),
        ("review", ("main_review.py", "watch")),
    ]


def test_the_review_watcher_runs_as_its_own_isolated_process() -> None:
    """``main_review.py watch``, not a thread inside the observer.

    The watcher wakes on a schedule and writes reviews; running it inside the observer would put
    a weekly aggregation over hundreds of documents in the same process as the candle loop, and a
    slow read would show up as a missed candle. Its argv carries the subcommand because the same
    entrypoint also runs one-off daily and weekly reports.
    """
    review = next(s for s in DEFAULT_SERVICES if s.name == "review")
    assert review.argv == ("main_review.py", "watch")
    assert review.name not in {s.name for s in DEFAULT_SERVICES if s is not review}


# ── flag selection ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "flag,gone",
    [
        ("--no-observer", "observer"),
        ("--no-monitor", "monitor"),
        ("--no-executor", "executor"),
        ("--no-discord", "discord"),
        ("--no-review", "review"),
    ],
)
def test_each_no_flag_removes_exactly_one_service(flag: str, gone: str) -> None:
    import main_aureon

    parser = _parser()
    args = parser.parse_args([flag])
    names = [s.name for s in main_aureon._selected(args, parser)]
    assert gone not in names
    assert names == [s.name for s in DEFAULT_SERVICES if s.name != gone]


def test_disabling_a_service_does_not_reorder_the_rest() -> None:
    """Order comes from DEFAULT_SERVICES, never from the flags.

    Planted as the tidying instinct -- ``for name in sorted(disabled)`` -- this fails: the stack
    would start discord, executor, observer, review, which puts the executor ahead of the
    observer and Discord ahead of both. That is the one ordering §75 forbids, because a human
    could confirm a request into an executor whose lease is not claimed yet.

    (A first plant that merely iterated ``disabled.items()`` survived, and rightly: that dict is
    built in DEFAULT_SERVICES order, so the outcome was identical. The plant was wrong, not the
    test -- worth writing down, because "the plant survived" is not always the test's fault.)
    """
    import main_aureon

    parser = _parser()
    args = parser.parse_args(["--no-monitor"])
    assert [s.name for s in main_aureon._selected(args, parser)] == [
        "observer",
        "executor",
        "discord",
        "review",
    ]


def test_disabling_every_service_is_refused() -> None:
    import main_aureon

    parser = _parser()
    args = parser.parse_args(
        ["--no-observer", "--no-monitor", "--no-executor", "--no-discord", "--no-review"]
    )
    with pytest.raises(SystemExit):
        main_aureon._selected(args, parser)


def _parser():
    """The launcher's OWN parser, so a renamed flag fails here instead of drifting quietly."""
    import main_aureon

    return main_aureon.build_parser()


# ── start order ───────────────────────────────────────────────────────────────


def test_services_start_in_the_declared_order(tmp_path: Path) -> None:
    order = tmp_path / "order.txt"
    sup = supervisor(
        *(stub(name, order_file=order) for name in ("observer", "monitor", "executor")),
    )
    try:
        sup.start()
    finally:
        sup.stop()
    assert order.read_text(encoding="utf-8").split() == ["observer", "monitor", "executor"]


# ── the preflight gate ────────────────────────────────────────────────────────


def test_a_failing_preflight_starts_nothing(tmp_path: Path) -> None:
    """A stack that starts on a failed preflight is the thing the preflight exists to prevent.

    The stub exits on its own rather than serving, deliberately: planted with the gate removed,
    a serving stub makes ``run`` block in its watch loop and the test HANGS instead of failing.
    A hanging test is worse than a failing one -- it is what a ten-minute plant run taught here.
    """
    order = tmp_path / "order.txt"
    sup = supervisor(
        stub("observer", behaviour="exit", code=9, order_file=order),
        preflight_argv=("-c", "import sys; sys.exit(7)"),
    )
    assert sup.run() == 7
    assert sup.processes == {}
    assert not order.exists()


def test_no_preflight_skips_the_check_and_warns(tmp_path: Path, caplog) -> None:
    order = tmp_path / "order.txt"
    sup = supervisor(
        stub("observer", behaviour="exit", order_file=order),
        # Would exit 7 and stop the run if it were consulted at all.
        preflight_argv=("-c", "import sys; sys.exit(7)"),
    )
    with caplog.at_level("WARNING"):
        code = sup.run(preflight=False)
    assert code != 7
    assert order.read_text(encoding="utf-8").split() == ["observer"]
    assert any("--no-preflight" in r.message for r in caplog.records)


# ── one child dying takes the stack down ──────────────────────────────────────


def test_a_child_that_dies_during_startup_stops_the_rest(tmp_path: Path) -> None:
    order = tmp_path / "order.txt"
    sup = supervisor(
        stub("observer", order_file=order),
        stub("monitor", behaviour="exit", code=2, order_file=order),
        stub("executor", order_file=order),
    )
    with pytest.raises(StartupFailed):
        sup.start()
    # The executor was never reached, and the observer is stopped by the caller.
    assert order.read_text(encoding="utf-8").split() == ["observer", "monitor"]
    sup.stop()
    assert sup.processes["observer"].poll() is not None


def test_a_child_that_dies_while_running_stops_the_rest_and_exits_non_zero(
    tmp_path: Path,
) -> None:
    order = tmp_path / "order.txt"
    ops = RecordingOps()
    sup = supervisor(
        stub("observer", order_file=order),
        # Past the startup grace, so this is the "during running" path.
        stub("monitor", behaviour="exit", code=3, delay=FAST_STARTUP * 3, order_file=order),
        stub("executor", order_file=order),
        ops=ops,
    )
    assert sup.run(preflight=False) == 3
    assert order.read_text(encoding="utf-8").split() == ["observer", "monitor", "executor"]
    assert all(p.poll() is not None for p in sup.processes.values())
    assert ("supervisor_stack_down", False, "running: observer, monitor, executor") in ops.calls
    down = [c for c in ops.calls if c[1] is True]
    assert len(down) == 1
    assert "monitor exited with code 3" in down[0][2]
    assert "during running" in down[0][2]


def test_a_child_exiting_zero_is_still_a_non_zero_launcher(tmp_path: Path) -> None:
    """A service stopping by itself is not success.

    Planted as ``return exited.code`` and this is the only test that fails: systemd would read
    the zero as "asked to stop" and leave Aureon down until somebody noticed.
    """
    sup = supervisor(
        stub("observer"),
        stub("monitor", behaviour="exit", code=0, delay=FAST_STARTUP * 3),
    )
    assert sup.run(preflight=False) == 1


def test_the_ops_record_is_optional(tmp_path: Path) -> None:
    """No register (Firestore unreachable) must not stop the stack from being managed."""
    sup = supervisor(
        stub("observer"),
        stub("monitor", behaviour="exit", code=4, delay=FAST_STARTUP * 3),
        ops=None,
    )
    assert sup.run(preflight=False) == 4


def test_an_ops_register_that_raises_does_not_break_the_shutdown() -> None:
    class Broken:
        def observe(self, *_: object, **__: object) -> None:
            raise RuntimeError("firestore is down")

    sup = supervisor(
        stub("observer"),
        stub("monitor", behaviour="exit", code=5, delay=FAST_STARTUP * 3),
        ops=Broken(),
    )
    assert sup.run(preflight=False) == 5


# ── cooperative shutdown ──────────────────────────────────────────────────────


def test_the_children_get_time_to_flush_before_anything_is_terminated(
    tmp_path: Path, caplog
) -> None:
    """The observer's queue is the reason the grace exists.

    Three assertions, and the first version of this test had only the weakest of them. "The
    marker file exists" is not enough: a supervisor that never signals at all still ends up
    terminating the child, the child handles SIGTERM the same way it handles SIGINT, and the
    marker appears anyway. Two plants -- ``stop`` with the interrupt removed, and a zero grace --
    both survived that version.

    So the marker records WHICH signal arrived (SIGINT means the cooperative path ran, SIGTERM
    means it did not), and the log is checked for the absence of a termination (nothing was cut
    off, as opposed to being cut off and recovering).
    """
    markers = {name: tmp_path / f"{name}.flushed" for name in ("observer", "monitor")}
    sup = supervisor(
        *(
            stub(name, behaviour="flush", marker=markers[name], flush_seconds=0.4)
            for name in markers
        ),
    )
    sup.start()
    with caplog.at_level("WARNING"):
        sup.stop()

    for name, marker in markers.items():
        assert marker.exists(), f"{name} was cut off before it could flush"
        assert sup.processes[name].returncode == 0
        written = marker.read_text(encoding="utf-8")
        assert "signal=SIGINT" in written, (
            f"{name} was stopped by {written.strip()!r}, not by the cooperative interrupt"
        )

    terminations = [r.message for r in caplog.records if "terminating it" in r.message]
    assert terminations == [], f"a child was terminated instead of being waited for: {terminations}"


def test_a_child_that_ignores_the_interrupt_is_terminated_after_the_grace() -> None:
    """The grace is a bound, not a promise. A hung child must not hold the launcher forever."""
    sup = supervisor(
        stub("observer", behaviour="hang"),
        shutdown_grace_seconds=0.4,
        terminate_grace_seconds=3.0,
    )
    sup.start()
    started = time.monotonic()
    sup.stop()
    elapsed = time.monotonic() - started
    process = sup.processes["observer"]
    assert process.poll() is not None
    assert process.returncode != 0
    assert elapsed >= 0.4, "the grace was not honoured"
    assert elapsed < 10, "termination did not bound the wait"


def test_an_interrupt_while_watching_stops_the_stack_and_exits_zero(
    tmp_path: Path, monkeypatch
) -> None:
    marker = tmp_path / "observer.flushed"
    sup = supervisor(stub("observer", behaviour="flush", marker=marker, flush_seconds=0.2))

    def interrupted(**_: object):
        raise KeyboardInterrupt

    monkeypatch.setattr(sup, "wait", interrupted)
    assert sup.run(preflight=False) == 0
    assert marker.exists()


def test_stop_is_safe_to_call_twice() -> None:
    sup = supervisor(stub("observer"))
    sup.start()
    sup.stop()
    sup.stop()
    assert sup.processes["observer"].poll() is not None


def test_children_do_not_receive_the_consoles_ctrl_c() -> None:
    """Each child is in its own process group, so the launcher is the only one signalling.

    Checked through the process group id rather than by sending a signal to the test runner:
    ``getpgid(child) != getpgid(self)`` is precisely the property that keeps a terminal Ctrl+C
    from reaching the children behind the supervisor's back.
    """
    if os.name == "nt":  # pragma: no cover - POSIX-only assertion
        pytest.skip("process groups are checked differently on Windows")
    sup = supervisor(stub("observer"))
    sup.start()
    try:
        child = sup.processes["observer"]
        assert os.getpgid(child.pid) != os.getpgid(os.getpid())
    finally:
        sup.stop()


# ── the grace knob ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", DEFAULT_SHUTDOWN_GRACE_SECONDS),
        ("45", 45.0),
        ("2.5", 2.5),
        ("nonsense", DEFAULT_SHUTDOWN_GRACE_SECONDS),
        ("0", DEFAULT_SHUTDOWN_GRACE_SECONDS),
        ("-3", DEFAULT_SHUTDOWN_GRACE_SECONDS),
    ],
)
def test_the_shutdown_grace_comes_from_the_environment(
    raw: str, expected: float, monkeypatch
) -> None:
    """Zero and negative are refused, not obeyed: they mean "terminate at once"."""
    monkeypatch.setenv("AUREON_SHUTDOWN_GRACE_SECONDS", raw)
    assert _grace_from_env() == expected


def test_the_default_grace_is_twenty_seconds() -> None:
    assert DEFAULT_SHUTDOWN_GRACE_SECONDS == 20.0


# ── the env file ──────────────────────────────────────────────────────────────


def test_load_env_file_applies_values_without_overwriting_existing(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n"
        "AUREON_ONE=one\n"
        'export AUREON_TWO="two words"\n'
        "AUREON_KEEP=file-value\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AUREON_KEEP", "process-value")
    monkeypatch.delenv("AUREON_ONE", raising=False)
    monkeypatch.delenv("AUREON_TWO", raising=False)

    loaded = load_env_file(path)
    assert loaded.found is True
    assert loaded.applied == 2
    assert os.environ["AUREON_ONE"] == "one"
    assert os.environ["AUREON_TWO"] == "two words"
    assert os.environ["AUREON_KEEP"] == "process-value"


def test_the_keys_the_environment_won_are_named_not_counted(
    tmp_path: Path, monkeypatch
) -> None:
    """Because "why is it using the wrong project" is answered by the key's name."""
    path = tmp_path / ".env"
    path.write_text(
        "AUREON_FIREBASE_PROJECT_ID=from-file\nAUREON_COLLECTION_PREFIX=from-file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AUREON_FIREBASE_PROJECT_ID", "from-environment")
    monkeypatch.delenv("AUREON_COLLECTION_PREFIX", raising=False)

    loaded = load_env_file(path)
    assert loaded.overridden_by_environment == ("AUREON_FIREBASE_PROJECT_ID",)
    assert os.environ["AUREON_FIREBASE_PROJECT_ID"] == "from-environment"
    assert os.environ["AUREON_COLLECTION_PREFIX"] == "from-file"


def test_override_true_is_available_but_not_the_default(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / ".env"
    path.write_text("AUREON_ONE=from-file\n", encoding="utf-8")
    monkeypatch.setenv("AUREON_ONE", "from-environment")

    assert load_env_file(path).applied == 0
    assert os.environ["AUREON_ONE"] == "from-environment"

    assert load_env_file(path, override=True).applied == 1
    assert os.environ["AUREON_ONE"] == "from-file"


def test_a_malformed_line_is_refused_rather_than_skipped(tmp_path: Path) -> None:
    """A dropped ``AUREON_ALLOW_LIVE_EXECUTION=true`` is a safety-relevant silence.

    python-dotenv's own ``load_dotenv`` skips the line and reports success, which is why the
    parser is used directly here.
    """
    path = tmp_path / ".env"
    path.write_text("AUREON_ONE=one\nAUREON_ALLOW_LIVE_EXECUTION true\n", encoding="utf-8")
    with pytest.raises(EnvFileError) as caught:
        load_env_file(path)
    assert "AUREON_ALLOW_LIVE_EXECUTION true" in str(caught.value)


def test_missing_env_file_is_allowed_and_says_so(tmp_path: Path) -> None:
    loaded = load_env_file(tmp_path / "missing.env")
    assert loaded.found is False
    assert loaded.applied == 0


def test_the_launcher_tells_the_children_which_env_file_it_read(
    tmp_path: Path, monkeypatch
) -> None:
    """So the preflight's ``config`` row can name the file rather than assume ``.env`` (T-3)."""
    import main_aureon

    path = tmp_path / "custom.env"
    path.write_text("AUREON_PROBE_VALUE=set\n", encoding="utf-8")
    monkeypatch.delenv("AUREON_PROBE_VALUE", raising=False)
    monkeypatch.delenv(ENV_FILE_VAR, raising=False)
    recorded: dict[str, object] = {}

    class Fake:
        def __init__(self, **kwargs: object) -> None:
            recorded.update(kwargs)

        def run(self, **_: object) -> int:
            recorded[ENV_FILE_VAR] = os.environ.get(ENV_FILE_VAR)
            return 0

    monkeypatch.setattr(main_aureon, "AureonSupervisor", Fake)
    monkeypatch.setattr(main_aureon, "build_ops_register", lambda: None)
    assert main_aureon.main(["--env-file", str(path), "--no-preflight"]) == 0
    assert recorded[ENV_FILE_VAR] == str(path)
    assert os.environ["AUREON_PROBE_VALUE"] == "set"


def test_a_malformed_env_file_stops_the_launcher_before_anything_starts(
    tmp_path: Path, monkeypatch
) -> None:
    import main_aureon

    path = tmp_path / "bad.env"
    path.write_text("NOT A PAIR\n", encoding="utf-8")

    def never(**_: object):
        raise AssertionError("the launcher started a supervisor on a broken .env")

    monkeypatch.setattr(main_aureon, "AureonSupervisor", never)
    assert main_aureon.main(["--env-file", str(path)]) == 2


# ── a child whose configuration is missing ────────────────────────────────────


def test_discord_without_a_token_is_a_visible_startup_failure() -> None:
    """Run for real: the promise is that the stack stops, which needs a non-zero exit.

    ``--no-discord`` is the documented way to run without it, and this is why: a bot with no
    token cannot answer anybody, and a stack that kept running would present an interface
    nobody can reach as a healthy one.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("AUREON_DISCORD", "AUREON_AUTHORIZED"))
    }
    result = subprocess.run(
        [sys.executable, "main_discord.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    assert "AUREON_DISCORD_TOKEN" in result.stderr + result.stdout
