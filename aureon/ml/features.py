"""Stable feature extraction for offline training and live shadow inference."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from aureon.models.enums import DirectionContext
from aureon.models.training import TrainingExample

AGENT_NAMES = ("ema_cross", "rsi", "session_trend", "wick", "liquidity", "breakout")
NUMERIC_KEYS = (
    "timeframe_minutes",
    "direction_sign",
    "ema_gap",
    "rsi_centered",
    "agent_confidence",
    "agent_alignment_sum",
)
CATEGORICAL_KEYS = ("family", "session", "trend", "mtf_alignment", "volatility_regime")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _alignment_value(value: Any) -> float:
    value = str(value or "").lower()
    if value == "aligned":
        return 1.0
    if value == "opposed":
        return -1.0
    return 0.0


def raw_training_features(example: TrainingExample) -> tuple[dict[str, float], dict[str, str]]:
    context = example.context or {}
    ema_fast = _float(context.get("ema_fast"))
    ema_slow = _float(context.get("ema_slow"))
    alignments = [
        _alignment_value((example.agent_read or {}).get(name, {}).get("alignment"))
        for name in AGENT_NAMES
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
        "agent_alignment_sum": sum(alignments) / float(len(AGENT_NAMES)),
    }
    for name, alignment in zip(AGENT_NAMES, alignments, strict=True):
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
    snapshot = event.context_snapshot or {}
    ema_fast = _float(snapshot.get("ema_fast"))
    ema_slow = _float(snapshot.get("ema_slow"))
    alignments = [
        _alignment_value(snapshot.get(f"agent_{name}_alignment"))
        for name in AGENT_NAMES
    ]
    numeric = {
        "timeframe_minutes": float(setup.timeframe.minutes),
        "direction_sign": (
            1.0
            if setup.direction_context is DirectionContext.BULLISH
            else -1.0
            if setup.direction_context is DirectionContext.BEARISH
            else 0.0
        ),
        "ema_gap": ema_fast - ema_slow,
        "rsi_centered": (_float(snapshot.get("rsi"), 50.0) - 50.0) / 50.0,
        "agent_confidence": _float(snapshot.get("agent_confidence_pct")) / 100.0,
        "agent_alignment_sum": sum(alignments) / float(len(AGENT_NAMES)),
    }
    for name, alignment in zip(AGENT_NAMES, alignments, strict=True):
        numeric[f"agent_{name}_alignment"] = alignment

    context = getattr(setup, "context_summary", None)
    categorical = {
        "family": setup.family.value,
        "session": str(getattr(getattr(context, "session", None), "value", "unknown")),
        "trend": str(snapshot.get("trend") or "unknown"),
        "mtf_alignment": str(
            getattr(getattr(context, "mtf_alignment", None), "value", "unknown")
        ),
        "volatility_regime": str(getattr(context, "volatility_regime", None) or "unknown"),
    }
    return numeric, categorical


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
        categorical_values = {
            key: tuple(sorted({categorical.get(key, "unknown") for _, categorical in rows}))
            for key in CATEGORICAL_KEYS
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
        for key in CATEGORICAL_KEYS:
            for value in self.categorical_values.get(key, ()):
                names.append(f"{key}={value}")
        return tuple(names)

    def transform(self, numeric: dict[str, float], categorical: dict[str, str]) -> list[float]:
        vector = [
            (numeric.get(key, 0.0) - self.means.get(key, 0.0))
            / self.scales.get(key, 1.0)
            for key in self.numeric_names
        ]
        for key in CATEGORICAL_KEYS:
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
