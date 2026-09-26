"""Canonical Aureon V1 learning contract.

One feature freeze + one outcome definition are shared by live training memory,
historical replay, model training, shadow reconciliation and walk-forward validation.

Nothing in this module has execution authority.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Iterable

from aureon.models.enums import Direction, DirectionContext, SetupFamily, Timeframe
from aureon.models.training import CanonicalOutcome, TrainingExample

FEATURE_SCHEMA_V1 = "AUREON_FEATURES_V1"
LABEL_SCHEMA_V1 = "AUREON_CLEAN_MOVE_V1"
MODEL_SCHEMA_V1 = "AUREON_CLEAN_MOVE_MODEL_V1"

CLEAN_TARGET_DEFAULT = 10.0
CLEAN_MAX_MAE_DEFAULT = 7.0
TARGETS: tuple[float, ...] = (5.0, 10.0, 20.0, 30.0, 40.0)

AGENT_NAMES: tuple[str, ...] = (
    "ema_cross",
    "ema_rsi_eligibility",
    "rsi",
    "liquidity",
    "wick",
    "breakout",
    "session_trend",
    "market_journey",
    "market_regime",
    "volume_participation",
    "higher_timeframe",
    "expansion_opportunity",
    "market_director",
)


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _enum_text(value: Any, default: str = "unknown") -> str:
    raw = _value(value)
    return default if raw is None else str(raw)


def _direction_text(direction: Any) -> str:
    raw = str(_value(direction) or "").lower()
    if raw in {"buy", "bullish", "long"}:
        return "buy"
    if raw in {"sell", "bearish", "short"}:
        return "sell"
    return raw or "unknown"


def _anchor_price(setup: Any) -> float | None:
    anchor = getattr(setup, "anchor", None)
    for key in ("price", "level", "reference_price", "anchor_price"):
        value = getattr(anchor, key, None) if anchor is not None else None
        if value is not None:
            return _float(value)
        if isinstance(anchor, dict) and anchor.get(key) is not None:
            return _float(anchor.get(key))
    return None


class FeatureBuilder:
    """Freeze exactly what Aureon knew at setup time.

    Raw setup/event context is retained whole for reproducibility, while stable named
    fields are also promoted for model encoders and human inspection.
    """

    feature_schema = FEATURE_SCHEMA_V1

    @staticmethod
    def _agent_states(snapshot: dict[str, Any], confluence: Any = None) -> dict[str, dict[str, Any]]:
        states: dict[str, dict[str, Any]] = {}
        confluence_payload = (
            confluence.model_dump(mode="json")
            if hasattr(confluence, "model_dump")
            else dict(confluence)
            if isinstance(confluence, dict)
            else {}
        )
        per_agent = confluence_payload.get("agents") or confluence_payload.get("by_agent") or {}

        for name in AGENT_NAMES:
            prefix = f"agent_{name}"
            payload = per_agent.get(name) if isinstance(per_agent, dict) else None
            payload = payload if isinstance(payload, dict) else {}
            states[name] = {
                "stance": snapshot.get(f"{prefix}_stance", payload.get("stance")),
                "confidence": snapshot.get(
                    f"{prefix}_confidence",
                    payload.get("confidence"),
                ),
                "alignment": snapshot.get(
                    f"{prefix}_alignment",
                    payload.get("alignment"),
                ),
                "observation": snapshot.get(
                    f"{prefix}_observation",
                    payload.get("observation") or payload.get("state"),
                ),
                "state": snapshot.get(
                    f"{prefix}_state",
                    payload.get("state"),
                ),
            }
        return states

    @classmethod
    def freeze_setup(cls, setup: Any, event: Any) -> dict[str, Any]:
        snapshot = dict(getattr(event, "context_snapshot", None) or {})
        context = getattr(setup, "context_summary", None)
        confluence = getattr(setup, "agent_confluence", None)
        agents = cls._agent_states(snapshot, confluence)

        aligned = opposed = neutral = 0
        for payload in agents.values():
            alignment = str(payload.get("alignment") or "").lower()
            if alignment == "aligned":
                aligned += 1
            elif alignment == "opposed":
                opposed += 1
            else:
                neutral += 1

        ema_fast = _float(snapshot.get("ema_fast"))
        ema_slow = _float(snapshot.get("ema_slow"))
        ema_gap = _float(snapshot.get("ema_gap"))
        if ema_gap is None and ema_fast is not None and ema_slow is not None:
            ema_gap = ema_fast - ema_slow

        atr = _float(
            snapshot.get("atr")
            or snapshot.get("atr14")
            or snapshot.get("volatility_atr")
        )
        entry = _float(
            snapshot.get("reference_price")
            or snapshot.get("price")
            or _anchor_price(setup)
        )
        frozen_at = getattr(getattr(event, "market_time", None), "utc", None)

        return {
            "feature_schema": FEATURE_SCHEMA_V1,
            "symbol": str(getattr(setup, "symbol", "")).upper(),
            "timestamp": frozen_at.isoformat() if frozen_at is not None else None,
            "direction": _direction_text(getattr(setup, "direction_context", None)),
            "entry_price": entry,
            "timeframe": _enum_text(getattr(setup, "timeframe", None)),
            "family": _enum_text(getattr(setup, "family", None)),
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "ema_gap": ema_gap,
            "ema_gap_change": _float(snapshot.get("ema_gap_change")),
            "ema_slope": _float(snapshot.get("ema_slope") or snapshot.get("ema_fast_slope")),
            "cross_direction": snapshot.get("cross_direction"),
            "bars_since_cross": _float(snapshot.get("bars_since_cross")),
            "rsi": _float(snapshot.get("rsi")),
            "rsi_zone": snapshot.get("rsi_zone"),
            "rsi_change": _float(snapshot.get("rsi_change") or snapshot.get("rsi_slope")),
            "atr": atr,
            "ema_gap_atr": (
                ema_gap / atr
                if ema_gap is not None and atr is not None and atr > 0
                else None
            ),
            "volatility_regime": (
                snapshot.get("volatility_regime")
                or getattr(context, "volatility_regime", None)
            ),
            "session": _enum_text(
                getattr(context, "session", None)
                or snapshot.get("session")
            ),
            "time_of_day": (
                frozen_at.strftime("%H:%M") if frozen_at is not None else None
            ),
            "day_of_week": (
                frozen_at.weekday() if frozen_at is not None else None
            ),
            "higher_timeframe_trend": (
                snapshot.get("higher_timeframe_trend")
                or snapshot.get("htf_state")
            ),
            "mtf_alignment": _enum_text(
                getattr(context, "mtf_alignment", None)
                or snapshot.get("mtf_alignment")
            ),
            "market_regime": (
                snapshot.get("market_regime")
                or snapshot.get("regime")
            ),
            "trend": snapshot.get("trend"),
            "range_state": snapshot.get("range_state"),
            "compression": snapshot.get("compression"),
            "expansion": snapshot.get("expansion"),
            "chop": snapshot.get("chop"),
            "wick_state": snapshot.get("wick_state"),
            "wick_direction": snapshot.get("wick_direction"),
            "wick_strength": _float(snapshot.get("wick_strength")),
            "liquidity_state": snapshot.get("liquidity_state"),
            "liquidity_sweep": snapshot.get("liquidity_sweep"),
            "breakout_state": snapshot.get("breakout_state"),
            "breakout_direction": snapshot.get("breakout_direction"),
            "breakout_strength": _float(snapshot.get("breakout_strength")),
            "volume_participation": (
                snapshot.get("volume_participation")
                or snapshot.get("participation")
            ),
            "market_structure": snapshot.get("market_structure"),
            "supporting_agent_count": int(
                snapshot.get("supporting_agent_count")
                or snapshot.get("director_supporting")
                or aligned
            ),
            "opposing_agent_count": int(
                snapshot.get("opposing_agent_count")
                or snapshot.get("director_opposing")
                or opposed
            ),
            "neutral_agent_count": int(snapshot.get("neutral_agent_count") or neutral),
            "agents": agents,
            # Frozen raw evidence is intentionally retained. Future encoders may learn
            # from a field without re-running an old setup through newer agent code.
            "raw_snapshot": snapshot,
        }

    @classmethod
    def freeze_replay(cls, row: Any, *, atr: float | None = None) -> dict[str, Any]:
        agents = {
            str(name): {
                "stance": None,
                "confidence": None,
                "alignment": None,
                "observation": "present_same_candle",
                "state": None,
            }
            for name in getattr(row, "same_candle_agents", ())
        }
        entry = _float(getattr(row, "entry_price", None))
        ema_gap = _float(getattr(row, "ema_gap", None))
        return {
            "feature_schema": FEATURE_SCHEMA_V1,
            "symbol": str(getattr(row, "symbol", "")).upper(),
            "timestamp": getattr(row, "at", None),
            "direction": _direction_text(getattr(row, "direction", None)),
            "entry_price": entry,
            "timeframe": str(getattr(row, "timeframe", "M5")),
            "family": SetupFamily.MOMENTUM_TRANSITION.value,
            "ema_fast": _float(getattr(row, "ema_fast", None)),
            "ema_slow": _float(getattr(row, "ema_slow", None)),
            "ema_gap": ema_gap,
            "ema_gap_change": _float(getattr(row, "ema_gap_change", None)),
            "ema_slope": None,
            "cross_direction": "bullish" if _direction_text(getattr(row, "direction", None)) == "buy" else "bearish",
            "bars_since_cross": 0.0,
            "rsi": _float(getattr(row, "rsi", None)),
            "rsi_zone": None,
            "rsi_change": None,
            "atr": atr,
            "ema_gap_atr": (
                ema_gap / atr if ema_gap is not None and atr is not None and atr > 0 else None
            ),
            "volatility_regime": None,
            "session": "unknown",
            "time_of_day": (
                datetime.fromisoformat(row.at).strftime("%H:%M")
                if getattr(row, "at", None)
                else None
            ),
            "day_of_week": (
                datetime.fromisoformat(row.at).weekday()
                if getattr(row, "at", None)
                else None
            ),
            "higher_timeframe_trend": getattr(row, "htf_state", None),
            "mtf_alignment": getattr(row, "htf_state", None),
            "market_regime": getattr(row, "regime", None),
            "trend": None,
            "range_state": None,
            "compression": None,
            "expansion": None,
            "chop": None,
            "wick_state": None,
            "wick_direction": None,
            "wick_strength": None,
            "liquidity_state": None,
            "liquidity_sweep": None,
            "breakout_state": None,
            "breakout_direction": None,
            "breakout_strength": None,
            "volume_participation": getattr(row, "participation", None),
            "market_structure": None,
            "supporting_agent_count": int(getattr(row, "director_supporting", 0) or 0),
            "opposing_agent_count": int(getattr(row, "director_opposing", 0) or 0),
            "neutral_agent_count": 0,
            "agents": agents,
            "raw_snapshot": {
                "director_state": getattr(row, "director_state", None),
                "event": getattr(row, "event", None),
            },
        }


class OutcomeTracker:
    """Resolve V1 clean/multi-target labels from future bars only."""

    label_schema = LABEL_SCHEMA_V1

    @staticmethod
    def resolve(
        *,
        entry_price: float,
        direction: str | Direction | DirectionContext,
        bars: Iterable[Any],
        clean_target: float = CLEAN_TARGET_DEFAULT,
        clean_max_mae: float = CLEAN_MAX_MAE_DEFAULT,
    ) -> CanonicalOutcome:
        if clean_target <= 0 or clean_max_mae < 0:
            raise ValueError("clean_target must be > 0 and clean_max_mae must be >= 0")
        direction_text = _direction_text(direction)
        if direction_text not in {"buy", "sell"}:
            raise ValueError(f"unsupported direction {direction!r}")

        reached = {int(target): False for target in TARGETS}
        bars_to: dict[int, int | None] = {int(target): None for target in TARGETS}
        max_favourable = 0.0
        max_adverse = 0.0
        adverse_until_10 = 0.0
        mae_before_10: float | None = None
        ambiguous_clean = False

        for offset, bar in enumerate(bars, start=1):
            if direction_text == "buy":
                favourable = max(0.0, float(bar.high) - entry_price)
                adverse = max(0.0, entry_price - float(bar.low))
            else:
                favourable = max(0.0, entry_price - float(bar.low))
                adverse = max(0.0, float(bar.high) - entry_price)

            max_favourable = max(max_favourable, favourable)
            max_adverse = max(max_adverse, adverse)

            if not reached[10]:
                adverse_until_10 = max(adverse_until_10, adverse)

            for target in TARGETS:
                key = int(target)
                if not reached[key] and favourable >= target:
                    reached[key] = True
                    bars_to[key] = offset

            if reached[10] and mae_before_10 is None:
                mae_before_10 = adverse_until_10
                # M5 cannot prove whether the target or the adverse extreme happened
                # first inside this candle. Flag it and classify conservatively.
                ambiguous_clean = (
                    favourable >= clean_target and adverse > clean_max_mae
                )

        clean_10 = bool(
            reached[10]
            and mae_before_10 is not None
            and mae_before_10 <= clean_max_mae
        )
        return CanonicalOutcome(
            clean_10=clean_10,
            reached_5=reached[5],
            reached_10=reached[10],
            reached_20=reached[20],
            reached_30=reached[30],
            reached_40=reached[40],
            mae_before_10=mae_before_10,
            max_favourable_move=max_favourable,
            max_adverse_move=max_adverse,
            bars_to_5=bars_to[5],
            bars_to_10=bars_to[10],
            bars_to_20=bars_to[20],
            bars_to_30=bars_to[30],
            bars_to_40=bars_to[40],
            ambiguous_clean_10_bar=ambiguous_clean,
        )


def enforce_target_probability_order(probabilities: dict[str, float]) -> dict[str, float]:
    """Enforce P40 <= P30 <= P20 <= P10 <= P5 without changing clean_10."""
    result = dict(probabilities)
    previous = 1.0
    for key in ("reach_5", "reach_10", "reach_20", "reach_30", "reach_40"):
        if key not in result:
            continue
        value = max(0.0, min(1.0, float(result[key])))
        value = min(previous, value)
        result[key] = value
        previous = value
    return result


def canonical_example_from_replay(
    *,
    row: Any,
    future_bars: Iterable[Any],
    generated_at: datetime,
    clean_target: float = CLEAN_TARGET_DEFAULT,
    clean_max_mae: float = CLEAN_MAX_MAE_DEFAULT,
    atr: float | None = None,
) -> TrainingExample:
    features = FeatureBuilder.freeze_replay(row, atr=atr)
    outcome = OutcomeTracker.resolve(
        entry_price=float(row.entry_price),
        direction=row.direction,
        bars=future_bars,
        clean_target=clean_target,
        clean_max_mae=clean_max_mae,
    )
    at = datetime.fromisoformat(row.at)
    digest = hashlib.sha256(
        f"{row.symbol}|{row.timeframe}|{row.at}|{row.direction}|{row.entry_price}".encode("utf-8")
    ).hexdigest()[:24]
    direction_context = (
        DirectionContext.BULLISH if _direction_text(row.direction) == "buy"
        else DirectionContext.BEARISH
    )
    return TrainingExample(
        example_id=f"v1_{digest}",
        market_date=at.date().isoformat(),
        symbol=row.symbol,
        timeframe=Timeframe(row.timeframe),
        setup_id=f"replay_{digest}",
        family=SetupFamily.MOMENTUM_TRANSITION,
        direction_context=direction_context,
        setup_version="agent20_replay_v1",
        feature_schema_version=FEATURE_SCHEMA_V1,
        label_schema_version=LABEL_SCHEMA_V1,
        context={},
        agent_read=features.get("agents", {}),
        features=features,
        outcome=outcome,
        setup_created_at=at,
        feature_frozen_at=at,
        outcome_resolved_at=generated_at,
        entry_price=float(row.entry_price),
        direction=_direction_text(row.direction),
        # Legacy fields remain populated where their meaning can be preserved.
        six_dollar_status="unavailable",
        six_dollar_reached=None,
        twenty_dollar_reached=outcome.reached_20,
        forty_dollar_reached=outcome.reached_40,
        max_favourable_move_price=outcome.max_favourable_move,
        evaluation_complete=True,
        generated_at=generated_at,
    )
