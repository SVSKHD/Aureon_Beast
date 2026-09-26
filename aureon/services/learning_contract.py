"""Canonical V1 feature and outcome builders.

The feature builder only reads facts frozen at the setup event. The outcome builder only
reads candles after the setup timestamp. Keeping them separate is the main leakage guard.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from aureon.models.base import to_utc, utc_now
from aureon.models.enums import Direction, DirectionContext, Timeframe
from aureon.models.learning_v1 import (
    AgentFeatureState,
    CanonicalTrainingExample,
    CleanMoveOutcomeV1,
    FEATURE_SCHEMA_V1,
    FeatureSnapshotV1,
    LABEL_SCHEMA_V1,
)

TARGETS = (5.0, 10.0, 20.0, 30.0, 40.0)
KNOWN_AGENTS = (
    "ema_cross",
    "ema_rsi_eligibility",
    "rsi",
    "session_trend",
    "market_journey",
    "market_regime",
    "volume_participation",
    "wick",
    "liquidity",
    "breakout",
    "higher_timeframe",
    "market_director",
    "expansion_opportunity",
)


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower().strip()
        if lowered in {"true", "yes", "1", "swept"}:
            return True
        if lowered in {"false", "no", "0", "none"}:
            return False
    return bool(value)


def _direction(value: Any) -> Direction:
    if isinstance(value, Direction):
        return value
    if isinstance(value, DirectionContext):
        if value is DirectionContext.BULLISH:
            return Direction.BUY
        if value is DirectionContext.BEARISH:
            return Direction.SELL
        raise ValueError("neutral setup cannot become a directional V1 training example")
    text = str(getattr(value, "value", value)).lower()
    if text in {"buy", "bullish", "long"}:
        return Direction.BUY
    if text in {"sell", "bearish", "short"}:
        return Direction.SELL
    raise ValueError(f"unsupported directional value {value!r}")


def _agent_states(snapshot: dict[str, Any]) -> dict[str, AgentFeatureState]:
    result: dict[str, AgentFeatureState] = {}
    explicit = snapshot.get("agent_states")
    if isinstance(explicit, dict):
        for name, payload in explicit.items():
            if isinstance(payload, AgentFeatureState):
                result[str(name)] = payload
            elif isinstance(payload, dict):
                result[str(name)] = AgentFeatureState.model_validate(payload)
    names = set(KNOWN_AGENTS)
    for key in snapshot:
        if not key.startswith("agent_"):
            continue
        tail = key[len("agent_") :]
        for suffix in ("_stance", "_confidence", "_alignment", "_observation", "_state"):
            if tail.endswith(suffix):
                names.add(tail[: -len(suffix)])
                break
    for name in sorted(names):
        prefix = f"agent_{name}"
        values = {
            "stance": snapshot.get(f"{prefix}_stance"),
            "confidence": _float(snapshot.get(f"{prefix}_confidence")),
            "alignment": snapshot.get(f"{prefix}_alignment"),
            "observation": snapshot.get(f"{prefix}_observation"),
            "state": snapshot.get(f"{prefix}_state"),
        }
        if any(value is not None for value in values.values()):
            result[name] = AgentFeatureState(**values)
    return result


class FeatureBuilder:
    """Build immutable AUREON_FEATURES_V1 snapshots."""

    feature_schema = FEATURE_SCHEMA_V1

    @staticmethod
    def from_setup_event(
        setup: Any,
        event: Any,
        *,
        extra_context: dict[str, Any] | None = None,
    ) -> FeatureSnapshotV1:
        snapshot = dict(getattr(event, "context_snapshot", {}) or {})
        if extra_context:
            snapshot.update(extra_context)
        timestamp = to_utc(event.market_time.utc)
        direction = _direction(setup.direction_context)
        reference_price = (
            _float(snapshot.get("reference_price"))
            or _float(snapshot.get("entry_price"))
            or _float(snapshot.get("price"))
            or _float(snapshot.get("close"))
            or float(setup.anchor.price)
        )
        atr = _float(snapshot.get("atr"))
        ema_fast = _float(snapshot.get("ema_fast"))
        ema_slow = _float(snapshot.get("ema_slow"))
        ema_gap = _float(snapshot.get("ema_gap"))
        if ema_gap is None and ema_fast is not None and ema_slow is not None:
            ema_gap = ema_fast - ema_slow

        agents = _agent_states(snapshot)
        supporting = _int(snapshot.get("supporting_agents"))
        opposing = _int(snapshot.get("opposing_agents"))
        neutral = _int(snapshot.get("neutral_agents"))
        if supporting is None:
            supporting = sum(
                state.alignment == "aligned" for state in agents.values()
            )
        if opposing is None:
            opposing = sum(
                state.alignment == "opposed" for state in agents.values()
            )
        if neutral is None:
            neutral = max(0, len(agents) - supporting - opposing)

        return FeatureSnapshotV1(
            setup_id=setup.setup_id,
            symbol=setup.symbol,
            timeframe=setup.timeframe,
            timestamp=timestamp,
            frozen_at=timestamp,
            direction=direction,
            reference_price=reference_price,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            ema_gap=ema_gap,
            ema_gap_change=_float(snapshot.get("ema_gap_change")),
            ema_slope=_float(snapshot.get("ema_slope")),
            cross_direction=snapshot.get("cross_direction"),
            bars_since_cross=_int(snapshot.get("bars_since_cross")),
            rsi=_float(snapshot.get("rsi")),
            rsi_zone=snapshot.get("rsi_zone"),
            rsi_change=_float(snapshot.get("rsi_change") or snapshot.get("rsi_slope")),
            atr=atr,
            ema_gap_atr=(
                ema_gap / atr if ema_gap is not None and atr is not None and atr > 0 else None
            ),
            volatility_regime=(
                snapshot.get("volatility_regime")
                or getattr(setup.context_summary, "volatility_regime", None)
            ),
            session=(
                snapshot.get("session")
                or getattr(getattr(setup.context_summary, "session", None), "value", None)
            ),
            time_of_day=timestamp.strftime("%H:%M"),
            day_of_week=timestamp.weekday(),
            htf_trend=snapshot.get("htf_trend") or snapshot.get("higher_timeframe_trend"),
            htf_alignment=(
                snapshot.get("htf_alignment")
                or getattr(
                    getattr(setup.context_summary, "mtf_alignment", None),
                    "value",
                    None,
                )
            ),
            market_regime=snapshot.get("market_regime") or snapshot.get("regime"),
            wick_state=snapshot.get("wick_state"),
            wick_direction=snapshot.get("wick_direction"),
            wick_strength=_float(snapshot.get("wick_strength")),
            liquidity_state=snapshot.get("liquidity_state"),
            liquidity_sweep=_bool(snapshot.get("liquidity_sweep")),
            liquidity_detail=snapshot.get("liquidity_detail"),
            breakout_state=snapshot.get("breakout_state"),
            breakout_direction=snapshot.get("breakout_direction"),
            breakout_strength=_float(snapshot.get("breakout_strength")),
            participation_state=(
                snapshot.get("participation_state")
                or snapshot.get("volume_participation")
            ),
            market_structure_state=(
                snapshot.get("market_structure_state")
                or snapshot.get("structure_state")
            ),
            supporting_agents=supporting,
            opposing_agents=opposing,
            neutral_agents=neutral,
            agents=agents,
            context=snapshot,
        )

    @staticmethod
    def from_replay_row(row: Any, *, timestamp: datetime | None = None) -> FeatureSnapshotV1:
        at = to_utc(timestamp or datetime.fromisoformat(row.at))
        direction = _direction(row.direction)
        atr = _float(getattr(row, "atr14", None))
        ema_gap = _float(row.ema_gap)
        agents = {
            str(name): AgentFeatureState(state="present")
            for name in getattr(row, "same_candle_agents", ())
        }
        return FeatureSnapshotV1(
            setup_id=f"replay:{row.symbol}:{row.timeframe}:{row.at}",
            symbol=row.symbol,
            timeframe=Timeframe(row.timeframe),
            timestamp=at,
            frozen_at=at,
            direction=direction,
            reference_price=float(row.entry_price),
            ema_fast=_float(row.ema_fast),
            ema_slow=_float(row.ema_slow),
            ema_gap=ema_gap,
            ema_gap_change=_float(row.ema_gap_change),
            rsi=_float(row.rsi),
            atr=atr,
            ema_gap_atr=(
                ema_gap / atr if ema_gap is not None and atr is not None and atr > 0 else None
            ),
            htf_trend=str(row.htf_state),
            htf_alignment=str(row.htf_state),
            market_regime=getattr(row, "regime", None),
            participation_state=getattr(row, "participation", None),
            supporting_agents=int(getattr(row, "director_supporting", 0)),
            opposing_agents=int(getattr(row, "director_opposing", 0)),
            neutral_agents=0,
            agents=agents,
            context={
                "director_state": getattr(row, "director_state", None),
                "event": getattr(row, "event", None),
            },
        )


def outcome_from_future_candles(
    *,
    candles: list[Any],
    entry_price: float,
    direction: Direction,
    horizon_bars: int,
    clean_target: float = 10.0,
    clean_max_mae: float = 7.0,
) -> CleanMoveOutcomeV1:
    """Resolve the canonical clean-move and target-ladder outcome.

    The input list must already contain ONLY candles after the frozen setup timestamp.
    No feature computation occurs here. V1's label meaning is immutable: +10 with
    MAE before first +10 <= 7. A different experiment requires a new label schema.
    """
    if clean_target != 10.0 or clean_max_mae != 7.0:
        raise ValueError(
            "AUREON_CLEAN_MOVE_V1 is fixed at clean_target=10 and clean_max_mae=7; "
            "create a new label schema for different thresholds"
        )
    future = candles[:horizon_bars]
    reached = {target: False for target in TARGETS}
    bars_to: dict[float, int | None] = {target: None for target in TARGETS}
    max_favourable = 0.0
    max_adverse = 0.0
    running_adverse = 0.0
    mae_before_10: float | None = None
    ambiguous_10 = False

    for offset, bar in enumerate(future, start=1):
        if direction is Direction.BUY:
            favourable = max(0.0, float(bar.high) - entry_price)
            adverse = max(0.0, entry_price - float(bar.low))
        else:
            favourable = max(0.0, entry_price - float(bar.low))
            adverse = max(0.0, float(bar.high) - entry_price)

        prior_adverse = running_adverse
        max_favourable = max(max_favourable, favourable)
        max_adverse = max(max_adverse, adverse)
        running_adverse = max(running_adverse, adverse)

        for target in TARGETS:
            if not reached[target] and favourable >= target:
                reached[target] = True
                bars_to[target] = offset

        if mae_before_10 is None and favourable >= clean_target:
            mae_before_10 = running_adverse
            # If adverse > boundary was already seen on an earlier candle, the
            # clean label is simply false. Ambiguity applies only when +10 and
            # the first >boundary excursion occur inside this same M5 candle.
            if prior_adverse <= clean_max_mae and adverse > clean_max_mae:
                ambiguous_10 = True

    clean_10 = bool(
        reached[10.0]
        and mae_before_10 is not None
        and mae_before_10 <= clean_max_mae
        and not ambiguous_10
    )
    resolved_at = None
    if future:
        last = future[-1]
        close_time = getattr(last, "close_time", None)
        resolved_at = getattr(close_time, "utc", close_time)
        if resolved_at is not None:
            resolved_at = to_utc(resolved_at)

    return CleanMoveOutcomeV1(
        resolved_at=resolved_at,
        horizon_bars=horizon_bars,
        clean_target=clean_target,
        clean_max_mae=clean_max_mae,
        clean_10=clean_10,
        path_ambiguous=ambiguous_10,
        reached_5=reached[5.0],
        reached_10=reached[10.0],
        reached_20=reached[20.0],
        reached_30=reached[30.0],
        reached_40=reached[40.0],
        bars_to_5=bars_to[5.0],
        bars_to_10=bars_to[10.0],
        bars_to_20=bars_to[20.0],
        bars_to_30=bars_to[30.0],
        bars_to_40=bars_to[40.0],
        max_favourable_move=max_favourable,
        max_adverse_move=max_adverse,
        mae_before_10=mae_before_10,
    )


def canonical_example(
    *,
    features: FeatureSnapshotV1,
    outcome: CleanMoveOutcomeV1,
    market_date: str,
    generated_at: datetime,
) -> CanonicalTrainingExample:
    if features.feature_schema != FEATURE_SCHEMA_V1:
        raise ValueError("feature schema mismatch")
    if outcome.label_schema != LABEL_SCHEMA_V1:
        raise ValueError("label schema mismatch")
    digest = hashlib.sha256(
        f"{features.setup_id}|{FEATURE_SCHEMA_V1}|{LABEL_SCHEMA_V1}".encode("utf-8")
    ).hexdigest()
    return CanonicalTrainingExample(
        example_id=digest,
        market_date=market_date,
        setup_id=features.setup_id,
        symbol=features.symbol,
        timeframe=features.timeframe,
        features=features,
        outcome=outcome,
        generated_at=to_utc(generated_at),
    )



def canonical_examples_from_replay(
    rows: list[Any],
    candles: list[Any],
    *,
    horizon_bars: int = 864,
    clean_target: float = 10.0,
    clean_max_mae: float = 7.0,
    generated_at: datetime | None = None,
) -> list[CanonicalTrainingExample]:
    """Produce the same canonical schema from historical replay as live resolution."""
    from bisect import bisect_left

    opens = [candle.open_time.utc for candle in candles]
    moment = to_utc(generated_at or utc_now())
    result: list[CanonicalTrainingExample] = []
    for row in rows:
        if not bool(getattr(row, "eligible", False)):
            continue
        features = FeatureBuilder.from_replay_row(row)
        start = bisect_left(opens, features.timestamp)
        future = candles[start : start + horizon_bars]
        if len(future) < horizon_bars:
            continue
        outcome = outcome_from_future_candles(
            candles=future,
            entry_price=features.reference_price,
            direction=features.direction,
            horizon_bars=horizon_bars,
            clean_target=clean_target,
            clean_max_mae=clean_max_mae,
        )
        result.append(
            canonical_example(
                features=features,
                outcome=outcome,
                market_date=features.timestamp.date().isoformat(),
                generated_at=moment,
            )
        )
    return result
