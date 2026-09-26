"""Feature encoding for the canonical Aureon V1 entry-learning contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from aureon.models.enums import Direction
from aureon.models.learning_v1 import FeatureSnapshotV1


def _alignment(value: str | None) -> float:
    text = str(value or "").lower()
    if text in {"aligned", "supporting", "bullish", "buy"}:
        return 1.0
    if text in {"opposed", "opposing", "bearish", "sell"}:
        return -1.0
    return 0.0


def raw_v1_features(
    snapshot: FeatureSnapshotV1,
) -> tuple[dict[str, float], dict[str, str]]:
    """Flatten one immutable V1 snapshot without discarding individual agents."""
    numeric: dict[str, float] = {
        "timeframe_minutes": float(snapshot.timeframe.minutes),
        "direction_sign": 1.0 if snapshot.direction is Direction.BUY else -1.0,
        "ema_fast": float(snapshot.ema_fast or 0.0),
        "ema_slow": float(snapshot.ema_slow or 0.0),
        "ema_gap": float(snapshot.ema_gap or 0.0),
        "ema_gap_change": float(snapshot.ema_gap_change or 0.0),
        "ema_slope": float(snapshot.ema_slope or 0.0),
        "bars_since_cross": float(snapshot.bars_since_cross or 0),
        "rsi": float(snapshot.rsi if snapshot.rsi is not None else 50.0),
        "rsi_change": float(snapshot.rsi_change or 0.0),
        "atr": float(snapshot.atr or 0.0),
        "ema_gap_atr": float(snapshot.ema_gap_atr or 0.0),
        "supporting_agents": float(snapshot.supporting_agents),
        "opposing_agents": float(snapshot.opposing_agents),
        "neutral_agents": float(snapshot.neutral_agents),
        "hour": float(snapshot.timestamp.hour),
        "weekday": float(snapshot.day_of_week or 0),
        "wick_strength": float(snapshot.wick_strength or 0.0),
        "breakout_strength": float(snapshot.breakout_strength or 0.0),
        "liquidity_sweep": 1.0 if snapshot.liquidity_sweep else 0.0,
    }
    categorical: dict[str, str] = {
        "cross_direction": str(snapshot.cross_direction or "unknown"),
        "rsi_zone": str(snapshot.rsi_zone or "unknown"),
        "volatility_regime": str(snapshot.volatility_regime or "unknown"),
        "session": str(snapshot.session or "unknown"),
        "htf_trend": str(snapshot.htf_trend or "unknown"),
        "htf_alignment": str(snapshot.htf_alignment or "unknown"),
        "market_regime": str(snapshot.market_regime or "unknown"),
        "wick_state": str(snapshot.wick_state or "unknown"),
        "wick_direction": str(snapshot.wick_direction or "unknown"),
        "liquidity_state": str(snapshot.liquidity_state or "unknown"),
        "breakout_state": str(snapshot.breakout_state or "unknown"),
        "breakout_direction": str(snapshot.breakout_direction or "unknown"),
        "participation_state": str(snapshot.participation_state or "unknown"),
        "market_structure_state": str(snapshot.market_structure_state or "unknown"),
    }

    for name, state in sorted(snapshot.agents.items()):
        numeric[f"agent:{name}:alignment"] = _alignment(state.alignment)
        numeric[f"agent:{name}:confidence"] = float(state.confidence or 0.0)
        categorical[f"agent:{name}:stance"] = str(state.stance or "unknown")
        categorical[f"agent:{name}:state"] = str(state.state or "unknown")
        categorical[f"agent:{name}:observation"] = str(state.observation or "unknown")

    return numeric, categorical


@dataclass(frozen=True)
class V1FeatureEncoder:
    numeric_names: tuple[str, ...]
    categorical_values: dict[str, tuple[str, ...]]
    means: dict[str, float]
    scales: dict[str, float]

    @classmethod
    def fit(
        cls,
        rows: Iterable[tuple[dict[str, float], dict[str, str]]],
    ) -> "V1FeatureEncoder":
        rows = list(rows)
        if not rows:
            raise ValueError("cannot fit V1 feature encoder without rows")
        numeric_names = tuple(
            sorted({key for numeric, _ in rows for key in numeric})
        )
        categorical_names = sorted(
            {key for _, categorical in rows for key in categorical}
        )
        categorical_values = {
            key: tuple(
                sorted(
                    {
                        categorical.get(key, "unknown")
                        for _, categorical in rows
                    }
                )
            )
            for key in categorical_names
        }

        means: dict[str, float] = {}
        scales: dict[str, float] = {}
        for key in numeric_names:
            values = [float(numeric.get(key, 0.0)) for numeric, _ in rows]
            mean = sum(values) / len(values)
            variance = sum((value - mean) ** 2 for value in values) / len(values)
            means[key] = mean
            scales[key] = max(variance**0.5, 1e-9)

        return cls(
            numeric_names=numeric_names,
            categorical_values=categorical_values,
            means=means,
            scales=scales,
        )

    @property
    def feature_names(self) -> tuple[str, ...]:
        names = list(self.numeric_names)
        for key in sorted(self.categorical_values):
            for value in self.categorical_values[key]:
                names.append(f"{key}={value}")
        return tuple(names)

    def transform(
        self,
        numeric: dict[str, float],
        categorical: dict[str, str],
    ) -> list[float]:
        vector = [
            (float(numeric.get(key, 0.0)) - self.means.get(key, 0.0))
            / max(self.scales.get(key, 1.0), 1e-9)
            for key in self.numeric_names
        ]
        for key in sorted(self.categorical_values):
            current = categorical.get(key, "unknown")
            vector.extend(
                1.0 if current == value else 0.0
                for value in self.categorical_values[key]
            )
        return vector

    def to_dict(self) -> dict[str, Any]:
        return {
            "numeric_names": list(self.numeric_names),
            "categorical_values": {
                key: list(values)
                for key, values in self.categorical_values.items()
            },
            "means": dict(self.means),
            "scales": dict(self.scales),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "V1FeatureEncoder":
        return cls(
            numeric_names=tuple(payload.get("numeric_names", ())),
            categorical_values={
                key: tuple(values)
                for key, values in (payload.get("categorical_values") or {}).items()
            },
            means={
                key: float(value)
                for key, value in (payload.get("means") or {}).items()
            },
            scales={
                key: float(value)
                for key, value in (payload.get("scales") or {}).items()
            },
        )
