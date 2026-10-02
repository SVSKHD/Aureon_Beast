"""Aureon V4 Gold session, HTF and state-transition intelligence (TODO 043-060).

These helpers keep overlapping sessions independent, freeze M15/H1 context without
creating new alerts, and turn completed journeys into explicit transition evidence.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aureon.config.sessions import SESSION_WINDOWS
from aureon.ml.logistic import LogisticModel, fit_logistic
from aureon.ml.v1_features import V1FeatureEncoder
from aureon.models.ema_journey_v3 import EMAAnchorType, EMAMovementJourney
from aureon.models.ema_journey_v4 import (
    V4HTFContext,
    V4HTFFrameSnapshot,
    V4JourneyPhase,
    V4RemainingMovementExample,
)
from aureon.models.enums import Direction, SessionName, Timeframe
from aureon.models.market import Candle
from aureon.models.mtf import MtfContext
from aureon.services.v4_ema_model import raw_v4_features


def active_sessions(market_dt: datetime) -> tuple[SessionName, ...]:
    """All independently active sessions, preserving overlap instead of precedence."""
    moment = market_dt.time()
    return tuple(
        name
        for name in (SessionName.ASIA, SessionName.LONDON, SessionName.NEW_YORK)
        if SESSION_WINDOWS[name].contains(moment)
    )


def session_phase(name: SessionName, market_dt: datetime) -> str:
    """EARLY/MID/LATE phase within one independent session window."""
    if name is SessionName.OFF:
        return "OFF"
    window = SESSION_WINDOWS[name]
    start = market_dt.replace(
        hour=window.start.hour,
        minute=window.start.minute,
        second=0,
        microsecond=0,
    )
    end = market_dt.replace(
        hour=window.end.hour,
        minute=window.end.minute,
        second=0,
        microsecond=0,
    )
    if end <= start:
        end += timedelta(days=1)
        if market_dt < start:
            start -= timedelta(days=1)
    total = max(1.0, (end - start).total_seconds())
    fraction = (market_dt - start).total_seconds() / total
    if fraction < 1 / 3:
        return "EARLY"
    if fraction < 2 / 3:
        return "MID"
    return "LATE"


@dataclass(frozen=True)
class SessionOpenContext:
    symbol: str
    session: SessionName
    opened_at: datetime
    price: float
    ema20: float | None
    ema50: float | None
    ema200: float | None
    prior_session_high: float | None = None
    prior_session_low: float | None = None
    prior_session_close: float | None = None


@dataclass(frozen=True)
class SessionCloseOutcome:
    symbol: str
    session: SessionName
    opened_at: datetime
    closed_at: datetime
    open_price: float
    close_price: float
    high: float
    low: float
    net_move: float
    range: float
    ema20_50_crosses: int
    ema200_crosses: int
    journeys_seen: int


@dataclass
class _SessionRuntime:
    context: SessionOpenContext
    high: float
    low: float
    close: float
    ema20_50_crosses: int = 0
    ema200_crosses: int = 0
    journeys_seen: int = 0


class V4SessionTracker:
    """Independent Asia/London/New York windows, including simultaneous overlap."""

    def __init__(self) -> None:
        self._active: dict[tuple[str, SessionName], _SessionRuntime] = {}
        self._last_closed: dict[str, SessionCloseOutcome] = {}

    def on_closed_candle(
        self,
        candle: Candle,
        *,
        ema20: float | None,
        ema50: float | None,
        ema200: float | None,
        ema20_50_crosses: int = 0,
        ema200_crosses: int = 0,
        journeys_seen: int = 0,
    ) -> tuple[list[SessionOpenContext], list[SessionCloseOutcome]]:
        market_dt = candle.open_time.market
        now_active = set(active_sessions(market_dt))
        opens: list[SessionOpenContext] = []
        closes: list[SessionCloseOutcome] = []

        # Close every session that was active for this symbol but no longer contains
        # this candle. London can close while New York remains active.
        for key, runtime in list(self._active.items()):
            symbol, session = key
            if symbol != candle.symbol or session in now_active:
                continue
            outcome = self._close(runtime, candle.open_time.market)
            closes.append(outcome)
            self._last_closed[candle.symbol] = outcome
            del self._active[key]

        prior = self._last_closed.get(candle.symbol)
        for session in now_active:
            key = (candle.symbol, session)
            runtime = self._active.get(key)
            if runtime is None:
                context = SessionOpenContext(
                    symbol=candle.symbol,
                    session=session,
                    opened_at=candle.open_time.market,
                    price=candle.open,
                    ema20=ema20,
                    ema50=ema50,
                    ema200=ema200,
                    prior_session_high=None if prior is None else prior.high,
                    prior_session_low=None if prior is None else prior.low,
                    prior_session_close=None if prior is None else prior.close_price,
                )
                runtime = _SessionRuntime(
                    context=context,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                )
                self._active[key] = runtime
                opens.append(context)
            else:
                runtime.high = max(runtime.high, candle.high)
                runtime.low = min(runtime.low, candle.low)
                runtime.close = candle.close

            runtime.ema20_50_crosses += int(ema20_50_crosses)
            runtime.ema200_crosses += int(ema200_crosses)
            runtime.journeys_seen += int(journeys_seen)

        return opens, closes

    def close_all(self, at: datetime) -> list[SessionCloseOutcome]:
        closed = [self._close(runtime, at) for runtime in self._active.values()]
        for outcome in closed:
            self._last_closed[outcome.symbol] = outcome
        self._active.clear()
        return closed

    @staticmethod
    def _close(runtime: _SessionRuntime, at: datetime) -> SessionCloseOutcome:
        context = runtime.context
        return SessionCloseOutcome(
            symbol=context.symbol,
            session=context.session,
            opened_at=context.opened_at,
            closed_at=at,
            open_price=context.price,
            close_price=runtime.close,
            high=runtime.high,
            low=runtime.low,
            net_move=runtime.close - context.price,
            range=runtime.high - runtime.low,
            ema20_50_crosses=runtime.ema20_50_crosses,
            ema200_crosses=runtime.ema200_crosses,
            journeys_seen=runtime.journeys_seen,
        )


def sessionwise_remaining_report(
    examples: list[V4RemainingMovementExample],
    *,
    min_samples: int = 1,
) -> dict[str, dict[str, float | int | bool]]:
    """Outcome comparison by the session+phase frozen on each observable anchor."""
    buckets: dict[str, list[V4RemainingMovementExample]] = defaultdict(list)
    for row in examples:
        key = f"{row.features.base.session}:{row.features.base.session_phase}"
        buckets[key].append(row)

    result: dict[str, dict[str, float | int | bool]] = {}
    for key, rows in buckets.items():
        n = len(rows)
        if n < min_samples:
            result[key] = {"samples": n, "insufficient": True}
            continue
        result[key] = {
            "samples": n,
            "insufficient": False,
            "reach_3_rate": sum(r.labels.reached_3 for r in rows) / n,
            "reach_5_rate": sum(r.labels.reached_5 for r in rows) / n,
            "reach_10_rate": sum(r.labels.reached_10 for r in rows) / n,
            "mean_remaining_mfe": sum(r.labels.remaining_mfe for r in rows) / n,
            "mean_remaining_mae": sum(r.labels.remaining_mae for r in rows) / n,
        }
    return result


def sessionwise_training_sets(
    examples: list[V4RemainingMovementExample],
    *,
    min_samples: int = 20,
) -> dict[str, list[V4RemainingMovementExample]]:
    """Return stable per-session cohorts only when sample support is sufficient."""
    buckets: dict[str, list[V4RemainingMovementExample]] = defaultdict(list)
    for row in examples:
        buckets[row.features.base.session].append(row)
    return {
        session: rows
        for session, rows in buckets.items()
        if len(rows) >= min_samples
    }


def freeze_htf_context(mtf: MtfContext | None) -> V4HTFContext:
    """Freeze only M15/H1 reads already observable at the M5 anchor close."""
    if mtf is None:
        return V4HTFContext()
    by_tf = mtf.by_timeframe

    def one(timeframe: Timeframe) -> V4HTFFrameSnapshot | None:
        row = by_tf.get(timeframe)
        if row is None:
            return None
        return V4HTFFrameSnapshot(
            timeframe=row.timeframe,
            ema_fast=row.ema_fast,
            ema_slow=row.ema_slow,
            close=row.close,
            bias=row.bias.value,
        )

    return V4HTFContext(
        m15=one(Timeframe.M15),
        h1=one(Timeframe.H1),
        alignment=mtf.alignment.value,
    )


def htf_alignment_effect(
    examples: list[V4RemainingMovementExample],
    *,
    min_samples: int = 1,
) -> dict[str, dict[str, float | int | bool]]:
    groups: dict[str, list[V4RemainingMovementExample]] = defaultdict(list)
    for row in examples:
        groups[row.features.base.htf_alignment].append(row)
    result: dict[str, dict[str, float | int | bool]] = {}
    for alignment, rows in groups.items():
        n = len(rows)
        if n < min_samples:
            result[alignment] = {"samples": n, "insufficient": True}
            continue
        result[alignment] = {
            "samples": n,
            "insufficient": False,
            "reach_5_rate": sum(r.labels.reached_5 for r in rows) / n,
            "reach_10_rate": sum(r.labels.reached_10 for r in rows) / n,
            "mean_remaining_mfe": sum(r.labels.remaining_mfe for r in rows) / n,
            "mean_remaining_mae": sum(r.labels.remaining_mae for r in rows) / n,
        }
    return result


@dataclass(frozen=True)
class V4TransitionExample:
    journey_id: str
    symbol: str
    direction: Direction
    market_date: str
    source: V4JourneyPhase
    destination: V4JourneyPhase
    observed: bool


def transition_dataset(
    journeys: list[EMAMovementJourney],
    *,
    pullbacks: dict[str, Any] | None = None,
    reentries: dict[str, list[Any]] | None = None,
) -> list[V4TransitionExample]:
    """Canonical transition evidence for TODO 054-059.

    Every eligible source contributes a positive or negative transition row so
    failures remain training evidence instead of disappearing from the dataset.
    """
    pullbacks = pullbacks or {}
    reentries = reentries or {}
    rows: list[V4TransitionExample] = []

    for journey in journeys:
        has_pre = any(a.anchor_type is EMAAnchorType.PRE_CROSS for a in journey.anchors)
        confirmed = [
            a
            for a in journey.anchors
            if a.anchor_type in {EMAAnchorType.EMA20_50_CROSS, EMAAnchorType.EMA200_CROSS}
        ]
        expanded = any(a.outcome.mfe >= 3.0 for a in confirmed if a.outcome.completed)
        pullback = pullbacks.get(journey.journey_id)
        reentry_rows = reentries.get(journey.journey_id, [])
        continued = any(
            getattr(getattr(row, "outcome", None), "reached_3", False)
            for row in reentry_rows
        )
        exhausted = bool(confirmed) and all(
            a.outcome.completed and a.outcome.mfe < 5.0 for a in confirmed
        )

        if has_pre:
            rows.append(
                V4TransitionExample(
                    journey.journey_id,
                    journey.symbol,
                    journey.direction,
                    journey.market_date,
                    V4JourneyPhase.PRE_CROSS,
                    V4JourneyPhase.CONFIRMED_CROSS,
                    bool(confirmed),
                )
            )
        if confirmed:
            rows.append(
                V4TransitionExample(
                    journey.journey_id,
                    journey.symbol,
                    journey.direction,
                    journey.market_date,
                    V4JourneyPhase.CONFIRMED_CROSS,
                    V4JourneyPhase.EXPANSION,
                    expanded,
                )
            )
        if expanded:
            rows.append(
                V4TransitionExample(
                    journey.journey_id,
                    journey.symbol,
                    journey.direction,
                    journey.market_date,
                    V4JourneyPhase.EXPANSION,
                    V4JourneyPhase.PULLBACK,
                    pullback is not None,
                )
            )
        if pullback is not None:
            rows.append(
                V4TransitionExample(
                    journey.journey_id,
                    journey.symbol,
                    journey.direction,
                    journey.market_date,
                    V4JourneyPhase.PULLBACK,
                    V4JourneyPhase.CONTINUATION,
                    continued,
                )
            )
        if confirmed:
            rows.append(
                V4TransitionExample(
                    journey.journey_id,
                    journey.symbol,
                    journey.direction,
                    journey.market_date,
                    V4JourneyPhase.EXPANSION,
                    V4JourneyPhase.EXHAUSTION,
                    exhausted,
                )
            )
    return rows


def transition_rates(
    rows: list[V4TransitionExample],
    *,
    min_samples: int = 1,
) -> dict[str, dict[str, float | int | bool]]:
    groups: dict[str, list[V4TransitionExample]] = defaultdict(list)
    for row in rows:
        groups[f"{row.source.value}->{row.destination.value}"].append(row)
    result: dict[str, dict[str, float | int | bool]] = {}
    for key, group in groups.items():
        n = len(group)
        if n < min_samples:
            result[key] = {"samples": n, "insufficient": True}
            continue
        result[key] = {
            "samples": n,
            "insufficient": False,
            "transition_rate": sum(row.observed for row in group) / n,
        }
    return result


@dataclass(frozen=True)
class V4ContinuationBundle:
    encoder: V1FeatureEncoder
    targets: dict[str, dict[str, Any]]
    samples: int


def _fit_binary(vectors: list[list[float]], labels: list[int]) -> dict[str, Any]:
    if all(label == labels[0] for label in labels):
        return {
            "kind": "constant",
            "probability": (sum(labels) + 1.0) / (len(labels) + 2.0),
        }
    return {
        "kind": "logistic",
        "model": fit_logistic(vectors, labels).to_dict(),
    }


def _predict(payload: dict[str, Any], vector: list[float]) -> float:
    if payload["kind"] == "constant":
        return float(payload["probability"])
    return LogisticModel.from_dict(payload["model"]).probability(vector)


def fit_continuation_specialist(
    examples: list[V4RemainingMovementExample],
    *,
    min_samples: int = 20,
) -> V4ContinuationBundle:
    """TODO 060: specialist for short continuation, separate from runner models."""
    if len(examples) < min_samples:
        raise ValueError(
            f"need at least {min_samples} continuation examples; have {len(examples)}"
        )
    raw = [raw_v4_features(row.features) for row in examples]
    encoder = V1FeatureEncoder.fit(raw)
    vectors = [encoder.transform(*row) for row in raw]
    targets = {
        "reach_3": _fit_binary(
            vectors,
            [1 if row.labels.reached_3 else 0 for row in examples],
        ),
        "reach_5": _fit_binary(
            vectors,
            [1 if row.labels.reached_5 else 0 for row in examples],
        ),
        "reach_10": _fit_binary(
            vectors,
            [1 if row.labels.reached_10 else 0 for row in examples],
        ),
    }
    return V4ContinuationBundle(encoder, targets, len(examples))


def predict_continuation(
    bundle: V4ContinuationBundle,
    example: V4RemainingMovementExample,
) -> dict[str, float]:
    vector = bundle.encoder.transform(*raw_v4_features(example.features))
    probabilities = {
        name: _predict(payload, vector)
        for name, payload in bundle.targets.items()
    }
    previous = 1.0
    for name in ("reach_3", "reach_5", "reach_10"):
        probabilities[name] = min(previous, probabilities[name])
        previous = probabilities[name]
    return probabilities
