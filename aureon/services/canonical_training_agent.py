"""Canonical V1 training-memory builder.

Unlike the legacy EOD label builder, this service does not define the market outcome by the
calendar. A caller may run it at EOD or later, but a row is written only when the configured
future-candle horizon is actually available.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aureon.models.base import to_utc, utc_now
from aureon.services.learning_contract import (
    FeatureBuilder,
    canonical_example,
    outcome_from_future_candles,
)


class CanonicalTrainingAgent:
    agent_name = "canonical_training_memory"
    agent_version = "1.0.0"

    def __init__(
        self,
        memory: Any,
        *,
        horizon_bars: int = 864,
        clean_target: float = 10.0,
        clean_max_mae: float = 7.0,
        now: Any = utc_now,
    ) -> None:
        if horizon_bars < 1:
            raise ValueError("horizon_bars must be >= 1")
        self.memory = memory
        self.horizon_bars = horizon_bars
        self.clean_target = clean_target
        self.clean_max_mae = clean_max_mae
        self._now = now

    def resolve(
        self,
        *,
        setup: Any,
        event: Any,
        future_candles: list[Any],
        market_date: str | None = None,
    ) -> Any | None:
        """Write one canonical row once the full outcome horizon exists.

        future_candles MUST start strictly after the setup feature-freeze candle.
        Refusing a short horizon prevents "not reached yet" from becoming a false label.
        """
        if len(future_candles) < self.horizon_bars:
            return None
        features = FeatureBuilder.from_setup_event(setup, event)
        first = future_candles[0]
        opened = getattr(getattr(first, "open_time", None), "utc", None)
        if opened is None:
            raise ValueError("future candle has no UTC open time")
        if to_utc(opened) < features.timestamp:
            raise ValueError("future candle precedes frozen setup timestamp")

        outcome = outcome_from_future_candles(
            candles=future_candles,
            entry_price=features.reference_price,
            direction=features.direction,
            horizon_bars=self.horizon_bars,
            clean_target=self.clean_target,
            clean_max_mae=self.clean_max_mae,
        )
        date_value = market_date or getattr(setup, "market_date", None)
        if not date_value:
            date_value = features.timestamp.date().isoformat()
        example = canonical_example(
            features=features,
            outcome=outcome,
            market_date=str(date_value),
            generated_at=to_utc(self._now()),
        )
        return self.memory.write_canonical(example)

    def resolve_many(
        self,
        items: list[tuple[Any, Any, list[Any]]],
    ) -> list[Any]:
        written = []
        for setup, event, future in items:
            example = self.resolve(
                setup=setup,
                event=event,
                future_candles=future,
            )
            if example is not None:
                written.append(example)
        return written
