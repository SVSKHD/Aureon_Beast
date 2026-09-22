"""The setup repository through real Firestore transactions (12, T-6).

The unit tests exercise ``record``'s logic against a double with no isolation, which is worth
having and proves nothing about the one property the transaction exists for: that a setup's state
and the event explaining it move together, and that two writers cannot both win.

That property is only checkable here. Specifically:

* a real sub-collection round trip -- a ``setups/{id}/events/{id}`` document is a different thing
  from a key with a slash in it, and only the server can tell you which one you wrote;
* a real create-if-absent, so the idempotent path is exercised against Firestore's own
  ``AlreadyExists`` rather than against a double raising a lookalike;
* a real transaction retry, so two observers racing on the same candle produce one event and one
  state, not two of either.
"""

from __future__ import annotations

import concurrent.futures
from datetime import UTC, datetime, timedelta

import pytest

from aureon.models.base import MarketTime
from aureon.models.enums import (
    DirectionContext,
    SetupAnchorKind,
    SetupEventType,
    SetupFamily,
    SetupState,
    Timeframe,
    TransitionError,
)
from aureon.models.identity import price_bin, setup_event_id, setup_id
from aureon.models.setup import Setup, SetupAnchor, SetupEvent
from aureon.storage.setup_repository import SetupRepository, latest_state

pytestmark = pytest.mark.emulator

NOW = datetime(2026, 9, 16, 9, 35, tzinfo=UTC)
MARKET_TZ = "Europe/Athens"
MARKET_DATE = "2026-09-16"


def identity(**overrides) -> dict[str, str]:
    base = {
        "account_scope": "primary",
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "family": SetupFamily.LIQUIDITY_REVERSAL.value,
        "direction_context": DirectionContext.BULLISH.value,
        "market_date": MARKET_DATE,
        "anchor_kind": SetupAnchorKind.LIQUIDITY_LEVEL.value,
        "anchor_price_bin": price_bin(2412.5, bin_size=0.05),
        "setup_version": "1.0.0",
    }
    base.update(overrides)
    return base


def a_setup(**overrides) -> Setup:
    ident = overrides.pop("identity", identity())
    base = {
        "setup_id": setup_id(**ident),
        "account_scope": ident["account_scope"],
        "symbol": ident["symbol"],
        "timeframe": Timeframe.M5,
        "family": SetupFamily(ident["family"]),
        "direction_context": DirectionContext(ident["direction_context"]),
        "market_date": ident["market_date"],
        "anchor": SetupAnchor(
            kind=SetupAnchorKind(ident["anchor_kind"]),
            price=2412.5,
            level_type="session_high",
        ),
        "invalidation_price": 2410.0,
        "opened_at": NOW,
    }
    base.update(overrides)
    return Setup(**base)


def an_event(setup: Setup, *, event_type: SetupEventType, to_state: SetupState, at=NOW):
    return SetupEvent(
        event_id=setup_event_id(
            setup_id=setup.setup_id, candle_close=at, event_type=event_type.value
        ),
        setup_id=setup.setup_id,
        event_type=event_type,
        from_state=setup.state,
        to_state=to_state,
        market_time=MarketTime.from_utc(at, MARKET_TZ),
        context_snapshot={"rsi": "58.2", "ema_gap_points": "31"},
    )


@pytest.fixture
def setups(firestore_client) -> SetupRepository:
    return SetupRepository(firestore_client)


# ── the round trip ────────────────────────────────────────────────────────────


def test_a_setup_and_its_events_survive_a_real_round_trip(setups) -> None:
    """Models validated out and back in. A tuple of sub-models or an aware datetime that
    serialised badly passes a dict and fails a document."""
    setup = setups.open(a_setup(), now=NOW)
    walked = [
        (SetupEventType.WATCH_STARTED, SetupState.WATCH),
        (SetupEventType.DEVELOPING, SetupState.DEVELOPING),
        (SetupEventType.CONFIRMED, SetupState.CONFIRMED),
        (SetupEventType.PULLBACK_STARTED, SetupState.PULLBACK),
        (SetupEventType.CONTINUATION, SetupState.CONTINUATION),
    ]
    for index, (event_type, state) in enumerate(walked):
        current = setups.get(setup.setup_id)
        at = NOW + timedelta(minutes=5 * index)
        setups.record(
            an_event(current, event_type=event_type, to_state=state, at=at),
            linked_detection_id=f"detection-{index}",
            now=at,
        )

    stored = setups.get(setup.setup_id)
    assert stored.state is SetupState.CONTINUATION
    assert stored.event_count == len(walked)
    assert stored.closed_at is None
    assert len(stored.linked_detection_ids) == len(walked)

    events = setups.events(setup.setup_id)
    assert [e.event_type for e in events] == [t for t, _ in walked]
    assert latest_state(events) is SetupState.CONTINUATION
    # The context snapshot is a map of strings and must come back as one.
    assert events[0].context_snapshot["rsi"] == "58.2"


def test_the_events_are_a_real_subcollection_not_a_key_with_slashes(
    setups, firestore_client
) -> None:
    """A collection query over ``setups`` must not return the events.

    The in-memory double got this wrong by matching a path prefix, and a test written against it
    would have passed while the read returned events as setups. Here the server decides.
    """
    from aureon.storage import paths

    setup = setups.open(a_setup(), now=NOW)
    setups.record(
        an_event(setup, event_type=SetupEventType.WATCH_STARTED, to_state=SetupState.WATCH),
        now=NOW,
    )

    top_level = list(firestore_client.collection(paths.SETUPS).stream())
    assert [doc.id for doc in top_level] == [setup.setup_id]

    nested = list(
        firestore_client.collection(paths.setup_events_path(setup.setup_id)).stream()
    )
    assert len(nested) == 1


# ── idempotency, against Firestore's own AlreadyExists ────────────────────────


def test_re_recording_the_same_candle_is_a_no_op(setups) -> None:
    setup = setups.open(a_setup(), now=NOW)
    event = an_event(
        setup, event_type=SetupEventType.WATCH_STARTED, to_state=SetupState.WATCH
    )

    first, _, applied = setups.record(event, now=NOW)
    assert applied is True

    second, stored, applied_again = setups.record(event, now=NOW + timedelta(minutes=5))
    assert applied_again is False
    assert second.event_count == 1
    assert second.state is SetupState.WATCH
    assert second.updated_at == first.updated_at, "a no-op moved updated_at"
    assert stored.event_id == event.event_id
    assert len(setups.events(setup.setup_id)) == 1


def test_opening_an_existing_setup_returns_the_stored_one(setups) -> None:
    """Against a real create, which is what raises AlreadyExists."""
    setups.open(a_setup(), now=NOW)
    current = setups.get(a_setup().setup_id)
    setups.record(
        an_event(current, event_type=SetupEventType.WATCH_STARTED, to_state=SetupState.WATCH),
        now=NOW,
    )
    again = setups.open(a_setup(), now=NOW + timedelta(hours=1))
    assert again.state is SetupState.WATCH


# ── two writers, one candle ───────────────────────────────────────────────────


def test_two_observers_on_the_same_candle_produce_one_event_and_one_state(
    setups, firestore_client
) -> None:
    """The property the transaction exists for, and the only place it can be checked.

    Both threads derive the SAME event id, because the id is a hash over (setup, close, type).
    One transaction wins and the other finds the event already there -- so exactly one event is
    stored, the count moves by one, and neither raises.

    A version without the transaction would let both read OBSERVING, both write WATCH, and both
    increment a count read before the other's write: event_count would end at 1 with two events
    stored, or at 2 with one. Either way the document and its history would disagree.
    """
    setup = setups.open(a_setup(), now=NOW)
    event = an_event(
        setup, event_type=SetupEventType.WATCH_STARTED, to_state=SetupState.WATCH
    )

    def write() -> object:
        """``True``/``False`` as the repository returns, or the exception if one escapes.

        Returned rather than raised, deliberately. Firestore retries a transaction whose read set
        changed and then gives up -- five attempts by default -- so under contention a loser can
        legitimately surface an exception instead of the idempotent ``False``. That is the
        client's documented behaviour, not a property of this repository, and a test that failed
        on it would be asserting something Firestore does not promise.

        This run first went red exactly there, inside the full emulator suite and never in
        isolation, which is what prompted separating the two claims below.
        """
        try:
            return SetupRepository(firestore_client).record(event, now=NOW)[2]
        except Exception as exc:  # noqa: BLE001 - see the docstring
            return exc

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: write(), range(2)))

    # The claim that matters is about the STORED DOCUMENTS, not about the return values: one
    # event, one state, one count. That is the property the transaction exists for, and it holds
    # whether the loser returned False or gave up retrying.
    stored = setups.get(setup.setup_id)
    assert stored.state is SetupState.WATCH
    assert stored.event_count == 1, f"the count moved twice; outcomes were {outcomes}"
    assert len(setups.events(setup.setup_id)) == 1, (
        f"two events were written for one candle; outcomes were {outcomes}"
    )

    # And at least one writer must have believed it was first, or nothing happened at all.
    assert any(outcome is True for outcome in outcomes), outcomes
    assert not all(outcome is True for outcome in outcomes), (
        f"both writers thought they were first: {outcomes}"
    )


def test_two_different_events_on_the_same_candle_both_land(setups, firestore_client) -> None:
    """Different event types are different documents, so both are legitimate.

    One is descriptive and does not move the state; the other does. The count must reach two.
    """
    setup = setups.open(a_setup(), now=NOW)
    descriptive = SetupEvent(
        event_id=setup_event_id(
            setup_id=setup.setup_id,
            candle_close=NOW,
            event_type=SetupEventType.PROXIMITY_LIQUIDITY_LEVEL.value,
        ),
        setup_id=setup.setup_id,
        event_type=SetupEventType.PROXIMITY_LIQUIDITY_LEVEL,
        from_state=SetupState.OBSERVING,
        to_state=SetupState.OBSERVING,
        market_time=MarketTime.from_utc(NOW, MARKET_TZ),
    )
    setups.record(descriptive, now=NOW)
    current = setups.get(setup.setup_id)
    setups.record(
        an_event(current, event_type=SetupEventType.WATCH_STARTED, to_state=SetupState.WATCH),
        now=NOW,
    )

    stored = setups.get(setup.setup_id)
    assert stored.event_count == 2
    assert stored.state is SetupState.WATCH
    assert latest_state(setups.events(setup.setup_id)) is SetupState.WATCH


# ── the gate holds through the server too ─────────────────────────────────────


def test_an_illegal_transition_writes_nothing_at_all(setups) -> None:
    """Both writes are in one transaction, so a refused transition leaves no event behind."""
    setup = setups.open(a_setup(), now=NOW)
    setups.record(
        an_event(setup, event_type=SetupEventType.WATCH_STARTED, to_state=SetupState.WATCH),
        now=NOW,
    )
    current = setups.get(setup.setup_id)
    illegal = an_event(
        current,
        event_type=SetupEventType.CONFIRMED,
        to_state=SetupState.CONFIRMED,
        at=NOW + timedelta(minutes=5),
    )
    with pytest.raises(TransitionError):
        setups.record(illegal, now=NOW + timedelta(minutes=5))

    assert setups.get(setup.setup_id).state is SetupState.WATCH
    assert setups.get_event(setup.setup_id, illegal.event_id) is None
    assert len(setups.events(setup.setup_id)) == 1


def test_a_terminal_setup_leaves_the_open_set(setups) -> None:
    live = setups.open(a_setup(), now=NOW)
    other = setups.open(
        a_setup(identity=identity(anchor_price_bin=price_bin(2415.0, bin_size=0.05))),
        now=NOW,
    )
    setups.record(
        an_event(
            other, event_type=SetupEventType.INVALIDATED, to_state=SetupState.INVALIDATED
        ),
        now=NOW,
    )
    open_now = setups.open_setups(symbol="XAUUSD", market_date=MARKET_DATE)
    assert [s.setup_id for s in open_now] == [live.setup_id]
    closed = setups.get(other.setup_id)
    assert closed.closed_at == NOW
