"""Stable feature extraction for offline training and live Champion/Shadow inference.

Legacy EOD_SETUP_FEATURES_V1 rows remain readable. AUREON_FEATURES_V1 rows use the
same encoder but expose the richer frozen agent/context snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from aureon.models.enums import DirectionContext
from aureon.models.training import TrainingExample
from aureon.services.learning_contract import (
    AGENT_NAMES as V1_AGENT_NAMES,
    FEATURE_SCHEMA_V1,
    FeatureBuilder,
)

LEGACY_AGENT_NAMES = ("ema_cross", "rsi", "session_trend", "wick", "liquidity", "breakout")
LEGACY_CATEGORICAL_KEYS = (
    "family", "session", "trend", "mtf_alignment", "volatility_regime"
)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _alignment_value(value: Any) -> float:
    value = str(value or "").lower()
    if value in {"aligned", "supporting", "bullish", "buy"}:
        return 1.0
    if value in {"opposed", "opposing", "bearish", "sell"}:
        return -1.0
    return 0.0


def _direction_sign(value: Any) -> float:
    raw = str(getattr(value, "value", value) or "").lower()
    if raw in {"bullish", "buy", "long"}:
        return 1.0
    if raw in {"bearish", "sell", "short"}:
        return -1.0
    return 0.0


def _timeframe_minutes(value: Any) -> float:
    minutes = getattr(value, "minutes", None)
    if minutes is not None:
        return float(minutes)
    raw = str(getattr(value, "value", value) or "").upper()
    if raw.startswith("M") and raw[1:].isdigit():
        return float(raw[1:])
    if raw.startswith("H") and raw[1:].isdigit():
        return float(raw[1:]) * 60.0
    return 5.0


def _canonical_features(
    snapshot: dict[str, Any],
) -> tuple[dict[str, float], dict[str, str]]:
    agents = snapshot.get("agents") or {}
    numeric = {
        "timeframe_minutes": _timeframe_minutes(snapshot.get("timeframe")),
        "direction_sign": _direction_sign(snapshot.get("direction")),
        "ema_fast": _float(snapshot.get("ema_fast")),
        "ema_slow": _float(snapshot.get("ema_slow")),
        "ema_gap": _float(snapshot.get("ema_gap")),
        "ema_gap_change": _float(snapshot.get("ema_gap_change")),
        "ema_slope": _float(snapshot.get("ema_slope")),
        "bars_since_cross": _float(snapshot.get("bars_since_cross")),
        "rsi": _float(snapshot.get("rsi"), 50.0),
        "rsi_centered": (_float(snapshot.get("rsi"), 50.0) - 50.0) / 50.0,
        "rsi_change": _float(snapshot.get("rsi_change")),
        "atr": _float(snapshot.get("atr")),
        "ema_gap_atr": _float(snapshot.get("ema_gap_atr")),
        "wick_strength": _float(snapshot.get("wick_strength")),
        "breakout_strength": _float(snapshot.get("breakout_strength")),
        "supporting_agent_count": _float(snapshot.get("supporting_agent_count")),
        "opposing_agent_count": _float(snapshot.get("opposing_agent_count")),
        "neutral_agent_count": _float(snapshot.get("neutral_agent_count")),
        "day_of_week": _float(snapshot.get("day_of_week")),
    }
    for name in V1_AGENT_NAMES:
        payload = agents.get(name) if isinstance(agents, dict) else {}
        payload = payload if isinstance(payload, dict) else {}
        numeric[f"agent_{name}_confidence"] = _float(payload.get("confidence"))
        numeric[f"agent_{name}_alignment"] = _alignment_value(payload.get("alignment"))

    categorical = {
        "family": str(snapshot.get("family") or "unknown"),
        "session": str(snapshot.get("session") or "unknown"),
        "trend": str(snapshot.get("trend") or "unknown"),
        "mtf_alignment": str(snapshot.get("mtf_alignment") or "unknown"),
        "volatility_regime": str(snapshot.get("volatility_regime") or "unknown"),
        "market_regime": str(snapshot.get("market_regime") or "unknown"),
        "higher_timeframe_trend": str(snapshot.get("higher_timeframe_trend") or "unknown"),
        "cross_direction": str(snapshot.get("cross_direction") or "unknown"),
        "rsi_zone": str(snapshot.get("rsi_zone") or "unknown"),
        "wick_state": str(snapshot.get("wick_state") or "unknown"),
        "wick_direction": str(snapshot.get("wick_direction") or "unknown"),
        "liquidity_state": str(snapshot.get("liquidity_state") or "unknown"),
        "liquidity_sweep": str(snapshot.get("liquidity_sweep") or "unknown"),
        "breakout_state": str(snapshot.get("breakout_state") or "unknown"),
        "breakout_direction": str(snapshot.get("breakout_direction") or "unknown"),
        "volume_participation": str(snapshot.get("volume_participation") or "unknown"),
        "market_structure": str(snapshot.get("market_structure") or "unknown"),
    }
    for name in V1_AGENT_NAMES:
        payload = agents.get(name) if isinstance(agents, dict) else {}
        payload = payload if isinstance(payload, dict) else {}
        categorical[f"agent_{name}_stance"] = str(payload.get("stance") or "unknown")
        categorical[f"agent_{name}_alignment_state"] = str(payload.get("alignment") or "unknown")
        categorical[f"agent_{name}_state"] = str(
            payload.get("state") or payload.get("observation") or "unknown"
        )
    return numeric, categorical


def raw_training_features(example: TrainingExample) -> tuple[dict[str, float], dict[str, str]]:
    if (
        example.feature_schema_version == FEATURE_SCHEMA_V1
        and isinstance(example.features, dict)
        and example.features
    ):
        return _canonical_features(example.features)

    context = example.context or {}
    ema_fast = _float(context.get("ema_fast"))
    ema_slow = _float(context.get("ema_slow"))
    alignments = [
        _alignment_value((example.agent_read or {}).get(name, {}).get("alignment"))
        for name in LEGACY_AGENT_NAMES
    ]
    numeric = {
        "timeframe_minutes": float(example.timeframe.minutes),
        "direction_sign": (
            1.0
            if example.direction_context is DirectionContext.BULLISH
            else -1.0
            if example.direction_context is DirectionContext.BEARISH
            else 0.0
        ),
        "ema_gap": ema_fast - ema_slow,
        "rsi_centered": (_float(context.get("rsi"), 50.0) - 50.0) / 50.0,
        "agent_confidence": _float(context.get("agent_confidence_pct")) / 100.0,
        "agent_alignment_sum": sum(alignments) / float(len(LEGACY_AGENT_NAMES)),
    }
    for name, alignment in zip(LEGACY_AGENT_NAMES, alignments, strict=True):
        numeric[f"agent_{name}_alignment"] = alignment

    categorical = {
        "family": example.family.value,
        "session": str(context.get("session") or "unknown"),
        "trend": str(context.get("trend") or "unknown"),
        "mtf_alignment": str(context.get("mtf_alignment") or "unknown"),
        "volatility_regime": str(context.get("volatility_regime") or "unknown"),
    }
    return numeric, categorical


def raw_setup_features(setup: Any, event: Any) -> tuple[dict[str, float], dict[str, str]]:
    return _canonical_features(FeatureBuilder.freeze_setup(setup, event))


@dataclass(frozen=True)
class FeatureEncoder:
    numeric_names: tuple[str, ...]
    categorical_values: dict[str, tuple[str, ...]]
    means: dict[str, float]
    scales: dict[str, float]

    @classmethod
    def fit(
        cls,
        rows: Iterable[tuple[dict[str, float], dict[str, str]]],
    ) -> "FeatureEncoder":
        rows = list(rows)
        numeric_names = sorted({key for numeric, _ in rows for key in numeric})
        categorical_names = sorted(
            {key for _, categorical in rows for key in categorical}
        )
        categorical_values = {
            key: tuple(sorted({categorical.get(key, "unknown") for _, categorical in rows}))
            for key in categorical_names
        }

        means: dict[str, float] = {}
        scales: dict[str, float] = {}
        for key in numeric_names:
            values = [numeric.get(key, 0.0) for numeric, _ in rows]
            mean = sum(values) / len(values) if values else 0.0
            variance = (
                sum((value - mean) ** 2 for value in values) / len(values)
                if values
                else 0.0
            )
            means[key] = mean
            scales[key] = max(variance**0.5, 1e-9)

        return cls(
            numeric_names=tuple(numeric_names),
            categorical_values=categorical_values,
            means=means,
            scales=scales,
        )

    @property
    def feature_names(self) -> tuple[str, ...]:
        names = list(self.numeric_names)
        for key in sorted(self.categorical_values):
            for value in self.categorical_values.get(key, ()):
                names.append(f"{key}={value}")
        return tuple(names)

    def transform(self, numeric: dict[str, float], categorical: dict[str, str]) -> list[float]:
        vector = [
            (numeric.get(key, 0.0) - self.means.get(key, 0.0))
            / self.scales.get(key, 1.0)
            for key in self.numeric_names
        ]
        for key in sorted(self.categorical_values):
            current = categorical.get(key, "unknown")
            vector.extend(
                1.0 if current == value else 0.0
                for value in self.categorical_values.get(key, ())
            )
        return vector

    def to_dict(self) -> dict[str, Any]:
        return {
            "numeric_names": list(self.numeric_names),
            "categorical_values": {
                key: list(values) for key, values in self.categorical_values.items()
            },
            "means": dict(self.means),
            "scales": dict(self.scales),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FeatureEncoder":
        return cls(
            numeric_names=tuple(payload.get("numeric_names", ())),
            categorical_values={
                key: tuple(values)
                for key, values in (payload.get("categorical_values") or {}).items()
            },
            means={key: float(value) for key, value in (payload.get("means") or {}).items()},
            scales={key: float(value) for key, value in (payload.get("scales") or {}).items()},
        )
