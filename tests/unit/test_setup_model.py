"""The setup document, its id, and the repository that moves both together (12, T-6).

Three properties, and each one has a specific silent failure behind it:

* **the id is deterministic** — otherwise a replay over the archived day mints a second
  population and the live-vs-replay check compares two sets that can never match;
* **the event write is idempotent** — otherwise a restart mid-session duplicates a transition, or
  raises a ``TransitionError`` from a state it already reached;
* **every list is bounded** — otherwise a document grows until a write fails at a size limit
  nobody was watching, which takes the whole day's tail with it.

None of the three announces itself when broken. The document still renders, the review still
counts, and the numbers are wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aureon.models.base import MarketTime
from aureon.models.enums import (
    DirectionContext,
    HistorySource,
    MtfAlignment,
    SessionName,
    SetupAnchorKind,
    SetupEventType,
    SetupFamily,
    SetupState,
    Timeframe,
    TransitionError,
    TrendBias,
    assert_setup_transition,
)
from aureon.models.identity import (
    SETUP_ID_COMPONENTS,
    price_bin,
    setup_event_id,
    setup_id,
    setup_id_components,
)
from aureon.models.setup import (
    MAX_CONTEXT_KEYS,
    MAX_CONTEXT_VALUE_CHARS,
    MAX_LINKED_DETECTIONS,
    Setup,
    SetupAnchor,
    SetupContextSummary,
    SetupEvent,
    SetupReference,
)
from aureon.services.agent_confluence import build_agent_confluence
from aureon.storage import paths
from aureon.storage.setup_repository import MissingSetup, SetupRepository, latest_state

NOW = datetime(2026, 9, 16, 9, 35, tzinfo=UTC)
MARKET_TZ = "Europe/Athens"
MARKET_DATE = "2026-09-16"

IDENTITY = {
    "account_scope": "primary",
    "symbol": "XAUUSD",
    "timeframe": "M5",
    "family": SetupFamily.LIQUIDITY_REVERSAL.value,
    "direction_context": DirectionContext.BULLISH.value,
    "market_date": MARKET_DATE,
    "anchor_kind": SetupAnchorKind.LIQUIDITY_LEVEL.value,
    "anchor_price_bin": "48250",
    "setup_version": "1.0.0",
}


def a_setup(**overrides) -> Setup:
    base = {
        "setup_id": setup_id(**IDENTITY),
        "account_scope": "primary",
        "symbol": "XAUUSD",
        "timeframe": Timeframe.M5,
        "family": SetupFamily.LIQUIDITY_REVERSAL,
        "direction_context": DirectionContext.BULLISH,
        "market_date": MARKET_DATE,
        "anchor": SetupAnchor(
            kind=SetupAnchorKind.LIQUIDITY_LEVEL, price=2412.5, level_type="session_high"
        ),
        "invalidation_price": 2410.0,
        "opened_at": NOW,
    }
    base.update(overrides)
    return Setup(**base)


def an_event(setup: Setup, **overrides) -> SetupEvent:
    event_type = overrides.pop("event_type", SetupEventType.WATCH_STARTED)
    at = overrides.pop("at", NOW)
    base = {
        "event_id": setup_event_id(
            setup_id=setup.setup_id, candle_close=at, event_type=event_type.value
        ),
        "setup_id": setup.setup_id,
        "event_type": event_type,
        "from_state": setup.state,
        "to_state": overrides.pop("to_state", SetupState.WATCH),
        "market_time": MarketTime.from_utc(at, MARKET_TZ),
    }
    base.update(overrides)
    return SetupEvent(**base)


# ── the id ────────────────────────────────────────────────────────────────────


def test_the_component_order_is_frozen_and_the_hash_uses_exactly_it() -> None:
    """Asserted as the TUPLE, not only as a stable hash.

    A stable hash says the function is a function. It does not say which nine things went into
    it, and "the anchor kind is part of the identity" is a claim a reader should be able to check
    without reversing a digest.
    """
    assert SETUP_ID_COMPONENTS == (
        "account_scope",
        "symbol",
        "timeframe",
        "family",
        "direction_context",
        "market_date",
        "anchor_kind",
        "anchor_price_bin",
        "setup_version",
    )
    assert setup_id_components(**IDENTITY) == tuple(
        IDENTITY[name] for name in SETUP_ID_COMPONENTS
    )


def test_the_same_inputs_give_the_same_id_in_a_second_process() -> None:
    """Live and replay must agree. Nothing here may consult a clock or a random source."""
    assert setup_id(**IDENTITY) == setup_id(**IDENTITY)
    assert len(setup_id(**IDENTITY)) == 64


@pytest.mark.parametrize("component", SETUP_ID_COMPONENTS)
def test_changing_any_component_changes_the_id(component: str) -> None:
    """One test per component, so a failure names which one stopped mattering.

    The dangerous direction is a component that is silently NOT hashed: two structures would share
    an id, and the second would overwrite the first with no error anywhere.
    """
    changed = dict(IDENTITY)
    changed[component] = f"{changed[component]}-different"
    assert setup_id(**changed) != setup_id(**IDENTITY)


def test_a_version_bump_forks_the_population_rather_than_editing_it() -> None:
    """The same reasoning §12 applies to ``agent_version``.

    When a family's rules change, the setups judged by the old rules and the new ones must be
    separable -- otherwise a review comparing them is comparing a mixture to itself.
    """
    old = setup_id(**IDENTITY)
    new = setup_id(**{**IDENTITY, "setup_version": "2.0.0"})
    assert old != new


def test_two_prices_in_one_bin_are_one_setup() -> None:
    """A level is a region, not a number.

    Without binning, a sweep to 2412.50 and one to 2412.52 are two setups, and the population a
    review counts would grow with the broker's tick size rather than with the market.
    """
    assert price_bin(2412.50, bin_size=0.05) == price_bin(2412.52, bin_size=0.05)
    assert price_bin(2412.50, bin_size=0.05) != price_bin(2412.60, bin_size=0.05)


def test_the_bin_size_is_required_and_must_be_positive() -> None:
    """Decision 141: a threshold in points is different money per instrument.

    No default, so the caller cannot bin silver's price with gold's bin by omission.
    """
    with pytest.raises(ValueError, match="positive"):
        price_bin(2412.5, bin_size=0)
    with pytest.raises(ValueError, match="positive"):
        price_bin(2412.5, bin_size=-0.05)


def test_the_bin_is_an_integer_index_not_a_rounded_float() -> None:
    """``repr(round(x, 2))`` is not stable enough to key an identity on."""
    binned = price_bin(2412.5, bin_size=0.05)
    assert binned.lstrip("-").isdigit(), binned


def test_a_negative_price_bins_consistently() -> None:
    """Not a real instrument price, but the arithmetic must not change sign behaviour.

    Floor division is chosen so a value exactly on a boundary always lands in the same bin on
    both sides of zero; a truncating conversion would fold -0.5 and +0.5 into the same index.
    """
    assert price_bin(-1.0, bin_size=0.5) != price_bin(1.0, bin_size=0.5)


def test_an_event_id_is_deterministic_over_the_candle_close_and_type() -> None:
    sid = setup_id(**IDENTITY)
    first = setup_event_id(setup_id=sid, candle_close=NOW, event_type="confirmed")
    assert first == setup_event_id(setup_id=sid, candle_close=NOW, event_type="confirmed")
    assert first != setup_event_id(
        setup_id=sid, candle_close=NOW, event_type="pullback_started"
    )
    assert first != setup_event_id(
        setup_id=sid, candle_close=NOW + timedelta(minutes=5), event_type="confirmed"
    )


def test_an_event_id_needs_a_setup() -> None:
    with pytest.raises(ValueError, match="setup_id"):
        setup_event_id(setup_id="", candle_close=NOW, event_type="confirmed")


# ── the state machine ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (SetupState.OBSERVING, SetupState.WATCH),
        (SetupState.WATCH, SetupState.DEVELOPING),
        (SetupState.DEVELOPING, SetupState.CONFIRMED),
        (SetupState.CONFIRMED, SetupState.PULLBACK),
        (SetupState.PULLBACK, SetupState.CONTINUATION),
        (SetupState.CONTINUATION, SetupState.COMPLETED),
        # The two-way door off CONFIRMED and PULLBACK.
        (SetupState.CONFIRMED, SetupState.FAKEOUT_RISK),
        (SetupState.PULLBACK, SetupState.FAKEOUT_RISK),
        (SetupState.FAKEOUT_RISK, SetupState.CONFIRMED),
        (SetupState.FAKEOUT_RISK, SetupState.INVALIDATED),
        # A setup that runs straight to its objective never pulls back.
        (SetupState.CONFIRMED, SetupState.COMPLETED),
        # A trend that continues, pulls back and continues again is ONE setup.
        (SetupState.CONTINUATION, SetupState.PULLBACK),
    ],
)
def test_the_legal_edges_are_legal(current: SetupState, new: SetupState) -> None:
    assert_setup_transition(current, new)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (SetupState.OBSERVING, SetupState.CONFIRMED),
        (SetupState.WATCH, SetupState.CONFIRMED),
        (SetupState.DEVELOPING, SetupState.PULLBACK),
        (SetupState.OBSERVING, SetupState.COMPLETED),
        (SetupState.WATCH, SetupState.OBSERVING),
        (SetupState.DEVELOPING, SetupState.WATCH),
        (SetupState.FAKEOUT_RISK, SetupState.PULLBACK),
        (SetupState.FAKEOUT_RISK, SetupState.COMPLETED),
    ],
    ids=str,
)
def test_skipping_a_step_or_going_backwards_is_refused(
    current: SetupState, new: SetupState
) -> None:
    """A setup that jumped from WATCH to CONFIRMED would have no DEVELOPING event, so its
    history could not explain its state -- which is the whole point of keeping both."""
    with pytest.raises(TransitionError):
        assert_setup_transition(current, new)


@pytest.mark.parametrize(
    "current",
    [
        SetupState.OBSERVING,
        SetupState.WATCH,
        SetupState.DEVELOPING,
        SetupState.CONFIRMED,
        SetupState.PULLBACK,
        SetupState.CONTINUATION,
        SetupState.FAKEOUT_RISK,
    ],
    ids=str,
)
def test_invalidated_is_reachable_from_every_non_terminal_state(
    current: SetupState,
) -> None:
    """Including OBSERVING: a setup whose preconditions stop holding before anything happened
    is invalidated rather than deleted, because "it was there and came to nothing" is data."""
    assert_setup_transition(current, SetupState.INVALIDATED)


@pytest.mark.parametrize(
    "terminal", [SetupState.COMPLETED, SetupState.INVALIDATED], ids=str
)
def test_a_terminal_state_goes_nowhere(terminal: SetupState) -> None:
    for state in SetupState:
        with pytest.raises(TransitionError, match="terminal"):
            assert_setup_transition(terminal, state)


# ── the document's own rules ──────────────────────────────────────────────────


def test_a_terminal_setup_must_carry_its_closing_time() -> None:
    with pytest.raises(ValueError, match="closed_at is not set"):
        a_setup(state=SetupState.COMPLETED)


def test_a_live_setup_must_not_carry_one() -> None:
    with pytest.raises(ValueError, match="closed_at is set"):
        a_setup(state=SetupState.WATCH, closed_at=NOW)


def test_the_linked_detection_list_is_capped() -> None:
    with pytest.raises(ValueError, match="linked detections"):
        a_setup(linked_detection_ids=tuple(f"d{i}" for i in range(MAX_LINKED_DETECTIONS + 1)))


def test_duplicate_linked_detections_are_refused() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        a_setup(linked_detection_ids=("d1", "d1"))


def test_with_linked_trims_to_the_cap_and_keeps_the_newest() -> None:
    """The trim lives on the model, so it happens in one place.

    "The caller trims it" is how an array grows until a write fails -- and the caller here is a
    loop that runs every five minutes for hours.
    """
    setup = a_setup(
        linked_detection_ids=tuple(f"d{i}" for i in range(MAX_LINKED_DETECTIONS))
    )
    trimmed = setup.with_linked("newest")
    assert len(trimmed) == MAX_LINKED_DETECTIONS
    assert trimmed[-1] == "newest"
    assert "d0" not in trimmed


def test_an_already_linked_detection_is_not_moved_to_the_end() -> None:
    """The order is the order things happened. Re-ordering it would make the list lie."""
    setup = a_setup(linked_detection_ids=("d1", "d2", "d3"))
    assert setup.with_linked("d1") == ("d1", "d2", "d3")


def test_the_params_snapshot_is_capped() -> None:
    with pytest.raises(ValueError, match="params_snapshot"):
        a_setup(params_snapshot={f"k{i}": "v" for i in range(MAX_CONTEXT_KEYS + 1)})


def test_the_setup_carries_no_message_id() -> None:
    """T-11 edits one Discord message per setup, and its id lives on the NOTIFICATION.

    Putting it here would make Discord a writer of ``setups`` -- a collection §71 does not permit
    it. One field is not worth widening that boundary, and this test is what stops somebody
    adding it back for convenience.
    """
    assert "message_id" not in Setup.model_fields


def test_an_unknown_field_is_refused() -> None:
    with pytest.raises(ValueError):
        a_setup(stop_loss=2400.0)


# ── the event's own rules ─────────────────────────────────────────────────────


def test_a_context_snapshot_is_capped_in_both_dimensions() -> None:
    setup = a_setup()
    with pytest.raises(ValueError, match="context_snapshot has"):
        an_event(setup, context_snapshot={f"k{i}": "v" for i in range(MAX_CONTEXT_KEYS + 1)})
    with pytest.raises(ValueError, match="chars"):
        an_event(setup, context_snapshot={"k": "x" * (MAX_CONTEXT_VALUE_CHARS + 1)})


def test_a_descriptive_event_must_not_change_state() -> None:
    """Otherwise it is a lifecycle event wearing the wrong name, and every reader that filters
    on ``is_watch_event`` would be filtering out a transition."""
    setup = a_setup()
    with pytest.raises(ValueError, match="descriptive event"):
        an_event(
            setup,
            event_type=SetupEventType.PROXIMITY_LIQUIDITY_LEVEL,
            to_state=SetupState.WATCH,
        )


def test_a_descriptive_event_with_the_same_state_is_fine() -> None:
    setup = a_setup()
    event = an_event(
        setup,
        event_type=SetupEventType.PROXIMITY_LIQUIDITY_LEVEL,
        to_state=SetupState.OBSERVING,
    )
    assert event.event_type.is_watch_event is True


def test_the_lifecycle_events_are_not_watch_events() -> None:
    for event_type in (
        SetupEventType.OPENED,
        SetupEventType.WATCH_STARTED,
        SetupEventType.CONFIRMED,
        SetupEventType.COMPLETED,
        SetupEventType.INVALIDATED,
        SetupEventType.EXPIRED,
    ):
        assert event_type.is_watch_event is False, event_type


# ── the repository ────────────────────────────────────────────────────────────


@pytest.fixture
def repository(firestore) -> SetupRepository:
    return SetupRepository(firestore)


def test_live_context_refresh_updates_confidence_without_inventing_an_event(repository) -> None:
    """A quiet OBSERVING candle may refresh the card without becoming setup history."""
    opened = repository.open(a_setup())
    confluence = build_agent_confluence(
        SimpleNamespace(
            ema_fast=101.0,
            ema_slow=100.0,
            rsi=58.0,
            trend=TrendBias.BULLISH,
            detections=(),
        ),
        DirectionContext.BULLISH,
    )
    context = SetupContextSummary(
        session=SessionName.LONDON,
        mtf_alignment=MtfAlignment.BULLISH,
        volatility_regime="normal",
    )

    refreshed = repository.refresh_live_context(
        opened.setup_id,
        context_summary=context,
        agent_confluence=confluence,
        now=NOW + timedelta(minutes=5),
    )

    assert refreshed.agent_confluence.confidence_pct == 50
    assert refreshed.agent_confluence.aligned_count == 3
    assert refreshed.event_count == 0
    assert refreshed.last_event_id is None
    assert repository.events(opened.setup_id) == []


def test_opening_a_setup_twice_does_not_overwrite_the_first(repository) -> None:
    """The id is deterministic, so the engine derives the same one every candle.

    A ``set`` here would replace a setup that had reached CONFIRMED with a fresh OBSERVING one --
    not a hypothetical, but what happens on the second candle of every setup's life.
    """
    opened = repository.open(a_setup())
    assert opened.state is SetupState.OBSERVING

    advanced = repository.record(
        an_event(opened, to_state=SetupState.WATCH), now=NOW
    )[0]
    assert advanced.state is SetupState.WATCH

    again = repository.open(a_setup())
    assert again.state is SetupState.WATCH, "open() overwrote a setup that had advanced"


def test_recording_the_same_event_twice_changes_nothing_and_raises_nothing(
    repository, firestore
) -> None:
    """The idempotent path, which is what a restart and a replay both take.

    Note what would happen without the early return: the second call would re-apply the state
    change, hit ``assert_transition`` from WATCH, and raise -- so a harmless re-processing would
    become an error on every restart.
    """
    setup = repository.open(a_setup())
    event = an_event(setup, to_state=SetupState.WATCH)

    first_setup, _, applied = repository.record(event, now=NOW)
    assert applied is True
    assert first_setup.event_count == 1
    writes_after_first = firestore.writes

    second_setup, stored, applied_again = repository.record(event, now=NOW)
    assert applied_again is False
    assert second_setup.event_count == 1, "the count moved on a re-processed candle"
    assert second_setup.state is SetupState.WATCH
    assert stored.event_id == event.event_id
    assert firestore.writes == writes_after_first, "a no-op wrote to Firestore"


def test_an_illegal_transition_is_refused_by_the_repository(repository) -> None:
    """The gate is in the write path, not only in the engine above it.

    An engine bug that asked for WATCH -> CONFIRMED must not be able to store it: the document
    would then be in a state its own history cannot explain.
    """
    setup = repository.open(a_setup())
    repository.record(an_event(setup, to_state=SetupState.WATCH), now=NOW)
    reread = repository.get(setup.setup_id)

    with pytest.raises(TransitionError):
        repository.record(
            an_event(
                reread,
                event_type=SetupEventType.CONFIRMED,
                to_state=SetupState.CONFIRMED,
                at=NOW + timedelta(minutes=5),
            ),
            now=NOW + timedelta(minutes=5),
        )
    assert repository.get(setup.setup_id).state is SetupState.WATCH


def test_a_refused_transition_writes_neither_the_event_nor_the_state(
    repository, firestore
) -> None:
    """Both or neither. A stored event for a refused transition is a history that contradicts
    the document it describes."""
    setup = repository.open(a_setup())
    repository.record(an_event(setup, to_state=SetupState.WATCH), now=NOW)
    reread = repository.get(setup.setup_id)
    illegal = an_event(
        reread,
        event_type=SetupEventType.CONFIRMED,
        to_state=SetupState.CONFIRMED,
        at=NOW + timedelta(minutes=5),
    )
    with pytest.raises(TransitionError):
        repository.record(illegal, now=NOW)
    assert repository.get_event(setup.setup_id, illegal.event_id) is None


def test_the_event_records_the_state_it_actually_moved_from(repository) -> None:
    """``from_state`` is overwritten with what was stored, not trusted from the caller.

    An engine holding a stale copy of the setup would otherwise write a history that starts from
    a state the setup was not in.
    """
    setup = repository.open(a_setup())
    repository.record(an_event(setup, to_state=SetupState.WATCH), now=NOW)
    stale = a_setup()  # still OBSERVING, as the engine's first copy was

    _, stored, _ = repository.record(
        an_event(
            stale,
            event_type=SetupEventType.DEVELOPING,
            to_state=SetupState.DEVELOPING,
            at=NOW + timedelta(minutes=5),
            from_state=SetupState.OBSERVING,
        ),
        now=NOW + timedelta(minutes=5),
    )
    assert stored.from_state is SetupState.WATCH


def test_reaching_a_terminal_state_stamps_the_closing_time(repository) -> None:
    setup = repository.open(a_setup())
    closed, _, _ = repository.record(
        an_event(
            setup,
            event_type=SetupEventType.INVALIDATED,
            to_state=SetupState.INVALIDATED,
            reason="expired",
        ),
        now=NOW,
    )
    assert closed.closed_at == NOW
    assert closed.is_terminal is True


def test_an_expiry_is_an_invalidation_with_a_reason(repository) -> None:
    """So "nothing happened in time" and "the structure broke" stay separable in a review."""
    setup = repository.open(a_setup())
    _, stored, _ = repository.record(
        an_event(
            setup,
            event_type=SetupEventType.EXPIRED,
            to_state=SetupState.INVALIDATED,
            reason="expired",
        ),
        now=NOW,
    )
    assert stored.to_state is SetupState.INVALIDATED
    assert stored.reason == "expired"


def test_recording_against_a_setup_that_does_not_exist_is_refused(repository) -> None:
    setup = a_setup()
    with pytest.raises(MissingSetup):
        repository.record(an_event(setup, to_state=SetupState.WATCH), now=NOW)


def test_the_linked_detection_rides_along_in_the_same_write(repository) -> None:
    """A separate update afterwards would be a second write outside the transaction."""
    setup = repository.open(a_setup())
    moved, _, _ = repository.record(
        an_event(setup, to_state=SetupState.WATCH),
        linked_detection_id="abc123",
        invalidation_price=2409.0,
        now=NOW,
    )
    assert moved.linked_detection_ids == ("abc123",)
    assert moved.invalidation_price == 2409.0


def test_the_events_read_back_oldest_first(repository) -> None:
    setup = repository.open(a_setup())
    repository.record(an_event(setup, to_state=SetupState.WATCH), now=NOW)
    reread = repository.get(setup.setup_id)
    repository.record(
        an_event(
            reread,
            event_type=SetupEventType.DEVELOPING,
            to_state=SetupState.DEVELOPING,
            at=NOW + timedelta(minutes=5),
        ),
        now=NOW + timedelta(minutes=5),
    )
    events = repository.events(setup.setup_id)
    assert [e.event_type for e in events] == [
        SetupEventType.WATCH_STARTED,
        SetupEventType.DEVELOPING,
    ]
    assert latest_state(events) is SetupState.DEVELOPING


def test_a_descriptive_event_does_not_change_the_latest_state(repository) -> None:
    setup = repository.open(a_setup())
    repository.record(an_event(setup, to_state=SetupState.WATCH), now=NOW)
    reread = repository.get(setup.setup_id)
    repository.record(
        an_event(
            reread,
            event_type=SetupEventType.PROXIMITY_POC,
            to_state=SetupState.WATCH,
            at=NOW + timedelta(minutes=5),
        ),
        now=NOW + timedelta(minutes=5),
    )
    assert latest_state(repository.events(setup.setup_id)) is SetupState.WATCH


def test_open_setups_excludes_the_terminal_ones(repository) -> None:
    live = repository.open(a_setup())
    other = repository.open(
        a_setup(
            setup_id=setup_id(**{**IDENTITY, "anchor_price_bin": "48300"}),
            anchor=SetupAnchor(kind=SetupAnchorKind.POC, price=2415.0),
        )
    )
    repository.record(
        an_event(
            other,
            event_type=SetupEventType.INVALIDATED,
            to_state=SetupState.INVALIDATED,
        ),
        now=NOW,
    )
    found = repository.open_setups(symbol="XAUUSD")
    assert [s.setup_id for s in found] == [live.setup_id]


def test_open_setups_does_not_return_event_documents(repository) -> None:
    """A collection query returns DIRECT children only.

    The in-memory double matched on a path prefix, so ``setups`` also returned every
    ``setups/{id}/events/{eid}`` row -- which would have made this query hand back events as if
    they were setups, and every test of it pass while the read was wrong. Fixed in the double,
    and pinned here because the fake is the thing most likely to drift back.
    """
    setup = repository.open(a_setup())
    repository.record(an_event(setup, to_state=SetupState.WATCH), now=NOW)
    assert len(repository.events(setup.setup_id)) == 1
    assert [s.setup_id for s in repository.open_setups(symbol="XAUUSD")] == [
        setup.setup_id
    ]


def test_a_setup_for_another_symbol_is_not_returned(repository) -> None:
    repository.open(a_setup())
    repository.open(
        a_setup(
            setup_id=setup_id(**{**IDENTITY, "symbol": "XAGUSD"}),
            symbol="XAGUSD",
        )
    )
    assert [s.symbol for s in repository.open_setups(symbol="XAGUSD")] == ["XAGUSD"]


def test_the_events_live_under_their_setup(repository) -> None:
    setup = repository.open(a_setup())
    event = an_event(setup, to_state=SetupState.WATCH)
    repository.record(event, now=NOW)
    expected = paths.setup_event_path(setup.setup_id, event.event_id)
    assert expected.startswith(f"{paths.SETUPS}/{setup.setup_id}/events/")
    assert repository.get_event(setup.setup_id, event.event_id) is not None


# ── the context blocks ────────────────────────────────────────────────────────


def test_the_reference_says_whether_it_is_evidence() -> None:
    """A synthetic cohort and a measured one are different claims (11C)."""
    assert SetupReference(history_source=HistorySource.REAL, real_days=40).is_evidence
    assert not SetupReference(history_source=HistorySource.SYNTHETIC).is_evidence
    assert not SetupReference(history_source=HistorySource.MIXED).is_evidence
    assert not SetupReference().is_evidence


def test_the_context_summary_defaults_to_mixed_rather_than_to_aligned() -> None:
    """An absence of evidence must not read as agreement."""
    assert SetupContextSummary().mtf_alignment is MtfAlignment.MIXED


def test_the_context_summary_carries_the_four_axes_a_review_groups_by() -> None:
    summary = SetupContextSummary(
        mtf_alignment=MtfAlignment.ALIGNED,
        volatility_regime="high",
        price_vs_va="above",
        session=SessionName.LONDON,
    )
    assert summary.session is SessionName.LONDON
    assert summary.volatility_regime == "high"


# ── the vocabulary (T-6's half of T-8's rule) ─────────────────────────────────


def test_no_setup_field_is_an_instruction() -> None:
    """The model docstring claims this; here is the check.

    ``direction_context`` is BULLISH/BEARISH/NEUTRAL and never BUY/SELL, and no field is a target,
    a lot size or a recommendation. The difference is not cosmetic: a card that says BUY has told
    a human what to do, and the one thing this system must never do is decide.

    Checked on the field NAMES and on the enum VALUES rather than on the whole source, so a
    docstring explaining why BUY is absent does not trip it.
    """
    forbidden = (
        "buy",
        "sell",
        "take_profit",
        "stop_loss",
        "target_price",
        "entry_price",
        "lot",
        "volume",
        "signal",
        "recommendation",
    )
    names = set(Setup.model_fields) | set(SetupEvent.model_fields)
    names |= set(SetupAnchor.model_fields) | set(SetupContextSummary.model_fields)
    names |= set(SetupReference.model_fields)
    for field in names:
        assert not any(word in field.lower() for word in forbidden), (
            f"the setup models have a field called {field!r}, which reads as an instruction"
        )
    for value in DirectionContext:
        assert value.value in {"bullish", "bearish", "neutral"}, value


def test_neither_setup_module_can_reach_the_broker() -> None:
    """``invalidation_price`` is where the structure's claim is falsified, not a stop-loss.

    Nothing here places or moves an order, and the guard never sees this field. Checked on the
    IMPORTS via the AST rather than on the source text -- a first version grepped for the string
    and failed on the repository's own docstring, which explains the rule it was checking. A
    check that fires on prose about itself is a check people delete.

    ``aureon/services/setup_engine.py`` (T-7) is covered by the boundary suite instead, because
    ``services`` is on the observation side; ``aureon/storage`` is shared and is not, which is why
    these two modules are asserted here.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for module in ("aureon/models/setup.py", "aureon/storage/setup_repository.py"):
        tree = ast.parse((root / module).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        offenders = sorted(
            name
            for name in imported
            if name == "aureon.execution" or name.startswith("aureon.execution.")
        )
        assert not offenders, (
            f"{module} imports {offenders}; a setup must not be able to reach the broker"
        )
