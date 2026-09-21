"""The drill catalogue and the runner's refusals (P-4).

The drills themselves run in ``tests/failure_injection/test_demo_drills.py``, against the
emulator: every step of every drill is a read-then-conditional-write through a repository,
and the one that matters most -- two executors racing a single request -- IS Firestore's
transaction semantics, so a double would be grading its own homework.

What is here is everything that needs no store: the catalogue's shape, and the two
refusals, which are the part most likely to rot into a silent pass. A drill that cannot run
must report SKIP, and the two reasons it cannot are different:

* the store's transactions do not really isolate, so the racing drill has nothing to race;
* the broker cannot be made to misbehave on demand -- a real terminal will not widen a
  spread, drop a connection mid-send or fill a resting order because a test asked -- so the
  three drills that need that have no situation to set up.

Either way the answer is "not checked", never "fine".
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aureon.config import AureonConfig
from aureon.execution.drills import (
    CATALOGUE,
    DrillContext,
    Expectations,
    catalogue_markdown,
    drill_by_number,
    run_drill,
    run_drills,
)
from aureon.execution.fake_broker import FakeBroker
from aureon.models.settings import ExecutionSettings
from aureon.services.checks import Status
from tests.conftest import InMemoryFirestore

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)

#: What the racing drill needs and an in-memory double cannot give.
ISOLATION_DRILL = 3


def settings() -> ExecutionSettings:
    """Generous, so each drill blocks on the one limit it tightens itself."""
    return ExecutionSettings(
        trading_enabled=True,
        max_lot=10.0,
        max_spread_points=200.0,
        max_deviation_points=100,
        max_open_positions=100,
        max_daily_trades=1000,
        confirmation_ttl_seconds=300.0,
        quote_ttl_seconds=300.0,
        executor_lease_seconds=60.0,
    )


def context() -> DrillContext:
    """A fresh store and a fresh broker, as the runner gives each drill."""
    return DrillContext(
        client=InMemoryFirestore(),
        broker=FakeBroker(),
        settings=settings(),
        transactions_isolated=False,
        now=lambda: NOW,
    )


# ── The catalogue ────────────────────────────────────────────────────────────


def test_there_are_nine_drills_numbered_one_to_nine() -> None:
    assert [d.number for d in CATALOGUE] == list(range(1, 10))
    assert len({d.name for d in CATALOGUE}) == 9


def test_every_drill_says_what_it_proves_and_which_invariant() -> None:
    """The catalogue is the checklist, so a drill with no stated invariant is untestable
    by a human and unreviewable by anyone else."""
    for drill in CATALOGUE:
        assert drill.proves and len(drill.proves) > 30, drill.name
        assert "§" in drill.invariant or "CLAUDE.md" in drill.invariant, drill.name


def test_five_of_the_nine_must_never_reach_the_broker() -> None:
    """Which is what makes them safe to run anywhere, and why the other four need a demo
    account."""
    assert sum(1 for d in CATALOGUE if not d.places_orders) == 5


def test_an_unknown_drill_number_names_the_catalogue() -> None:
    with pytest.raises(KeyError, match="no drill 42"):
        drill_by_number(42)


def test_the_catalogue_renders_as_a_table() -> None:
    table = catalogue_markdown()
    assert table[0].startswith("| # | drill |")
    assert len(table) == 2 + len(CATALOGUE)


# ── The runner, without a store ──────────────────────────────────────────────
#
# The drill BODIES need real Firestore transactions -- every repository operation they
# drive is a read-then-conditional-write, and `firestore.transactional` requires a real
# Transaction object. So the drills themselves run in the emulator suite
# (tests/failure_injection/test_demo_drills.py), where the transaction semantics the
# exactly-once guarantee rests on are the real ones. What is left here is everything that
# needs no store at all.


def test_the_racing_drill_skips_rather_than_passing_without_isolation() -> None:
    """Exactly-once rests entirely on Firestore's transaction semantics, so a double that
    defined its own concurrency would be grading its own homework."""
    run = run_drill(drill_by_number(ISOLATION_DRILL), context)
    assert run.result.status is Status.SKIP
    assert "do not isolate" in run.result.detail
    assert "emulator" in (run.result.remedy or "")
    assert run.observations == [], "nothing was observed, so nothing is reported"


def test_a_skip_does_not_make_a_run_fail_and_is_counted_in_the_summary() -> None:
    report, runs = run_drills(context, numbers=[ISOLATION_DRILL])
    assert report.ok
    assert report.exit_code == 0
    assert "1 checks not run" in report.summary(
        ready="DRILLS PASSED", not_ready="DRILLS FAILED"
    )
    assert [r.drill.number for r in runs] == [ISOLATION_DRILL]


def test_a_drill_that_raises_is_a_failure_with_its_cause() -> None:
    """Rather than an exception out of the runner, which would abandon the rest."""
    from aureon.execution.drills import Drill

    def explode(ctx: DrillContext) -> Expectations:
        raise RuntimeError("the terminal went away")

    broken = Drill(
        number=99,
        name="deliberately_broken",
        proves="nothing",
        invariant="§0",
        places_orders=False,
        run=explode,
    )
    run = run_drill(broken, context)
    assert run.result.status is Status.FAIL
    assert "RuntimeError" in run.result.detail
    assert "terminal went away" in run.result.detail


def test_each_drill_gets_a_fresh_context() -> None:
    """A drill inherits nothing: a leftover setting or another drill's resting order would
    make a verdict depend on the order the drills happened to run in."""
    built: list[DrillContext] = []

    def factory() -> DrillContext:
        made = context()
        built.append(made)
        return made

    run_drills(factory, numbers=[ISOLATION_DRILL, ISOLATION_DRILL])
    assert len(built) == 2
    assert built[0] is not built[1]
    assert built[0].client is not built[1].client
    assert built[0].broker is not built[1].broker


def test_expectations_name_the_failures_they_found() -> None:
    e = Expectations()
    e.require("status is FILLED", True, "filled")
    e.require("no order reached the broker", False, "1")
    assert not e.satisfied
    assert "no order reached the broker" in e.summary()
    assert "1/2 held" in e.summary()
    assert any(line.startswith("FAIL") for line in e.lines())
    assert all("actual:" in line for line in e.lines())


def test_expectations_with_nothing_required_are_vacuously_satisfied() -> None:
    """Named so nobody mistakes it for a property worth having: this is why the emulator
    suite asserts each drill recorded observations at all."""
    assert Expectations().satisfied
    assert Expectations().lines() == []


# ── A broker that will not misbehave ─────────────────────────────────────────


class StubbornBroker:
    """A broker with no scripting surface, like a real terminal.

    Deliberately not a subclass of anything: what matters is the absence of the three
    methods the awkward drills need, which is exactly what MT5Broker also lacks.
    """

    calls: list[str] = []

    def quote(self, symbol: str):
        from aureon.models.market import QuoteSnapshot

        return QuoteSnapshot(symbol=symbol, bid=2400.0, ask=2400.3, point=0.01,
                             captured_at=NOW)


def unscriptable_context() -> DrillContext:
    return DrillContext(
        client=InMemoryFirestore(),
        broker=StubbornBroker(),
        settings=settings(),
        transactions_isolated=False,
        now=lambda: NOW,
    )


def test_a_fake_broker_is_scriptable_and_a_plain_one_is_not() -> None:
    assert context().broker_is_scriptable
    assert not unscriptable_context().broker_is_scriptable


@pytest.mark.parametrize("number", [5, 7, 8])
def test_the_awkward_drills_skip_on_a_broker_that_will_not_misbehave(number) -> None:
    """On a demo account these three have no automated form, so they must not report one."""
    run = run_drill(drill_by_number(number), unscriptable_context)
    assert run.result.status is Status.SKIP
    assert "cannot be made to misbehave" in run.result.detail
    assert "DEMO_EXECUTION_CHECKLIST" in (run.result.remedy or "")


def test_exactly_three_drills_need_a_scriptable_broker() -> None:
    """Named as a number so adding a fourth is a deliberate act: each one is a drill that
    cannot run on a demo account, which is the whole point of running them there."""
    needy = [d.number for d in CATALOGUE if d.needs_scriptable_broker]
    assert needy == [5, 7, 8]


def test_the_send_log_is_scoped_to_one_drill() -> None:
    """A real terminal is handed to every drill, so "the broker was never asked to send"
    has to mean "not by this drill" -- otherwise drill 1's fill fails drill 2."""
    broker = FakeBroker()
    broker.calls.append("send_market_order")  # as an earlier drill would have left it
    ctx = DrillContext(
        client=InMemoryFirestore(),
        broker=broker,
        settings=settings(),
        transactions_isolated=False,
        now=lambda: NOW,
    )
    assert ctx.sends() == []
    broker.calls.append("send_market_order")
    assert ctx.sends() == ["send_market_order"]


# ── 11A F-3: the refusal that never fired ─────────────────────────────────────


def terminal_reporting(mode: str):
    """A stand-in for ``MT5Broker`` that reports one account mode."""
    from aureon.execution.fake_broker import FakeBroker
    from aureon.models.enums import AccountMode

    inner = FakeBroker(mode=AccountMode(mode))

    class _Terminal:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def connect(self) -> None:
            pass

        def account_info(self):
            return inner.account_info()

    return _Terminal


@pytest.mark.parametrize("mode", ["real", "contest", "unknown"])
def test_the_drills_refuse_a_terminal_that_is_not_a_demo(monkeypatch, mode: str) -> None:
    """This guard was dead code until 11A.

    It read ``getattr(account, "is_demo", True)`` and ``AccountInfo`` has never had an
    ``is_demo`` field, so the expression was always True and the refusal could not fire --
    on a script where four drills place real orders and one drops a connection mid-send.

    CONTEST is refused alongside REAL and UNKNOWN: a contest account is somebody's
    competition entry, and a terminal that will not say what it is has not said "demo".
    """
    import aureon.execution.mt5_broker as mt5_broker
    from scripts import demo_drills

    monkeypatch.setattr(mt5_broker, "MT5Broker", terminal_reporting(mode))

    with pytest.raises(SystemExit) as raised:
        demo_drills.build_broker_factory(
            "mt5", config=AureonConfig(), allow_real=False
        )
    assert mode.upper() in str(raised.value)
    assert "--i-know-this-is-real-money" in str(raised.value)


def test_a_demo_terminal_is_accepted(monkeypatch) -> None:
    import aureon.execution.mt5_broker as mt5_broker
    from scripts import demo_drills

    monkeypatch.setattr(mt5_broker, "MT5Broker", terminal_reporting("demo"))
    factory = demo_drills.build_broker_factory(
        "mt5", config=AureonConfig(), allow_real=False
    )
    assert factory() is not None


def test_the_override_is_honoured_because_somebody_typed_it(monkeypatch) -> None:
    """``--i-know-this-is-real-money`` exists so the refusal is a speed bump rather than a
    wall. It is spelled that way on purpose."""
    import aureon.execution.mt5_broker as mt5_broker
    from scripts import demo_drills

    monkeypatch.setattr(mt5_broker, "MT5Broker", terminal_reporting("real"))
    assert demo_drills.build_broker_factory(
        "mt5", config=AureonConfig(), allow_real=True
    )() is not None
