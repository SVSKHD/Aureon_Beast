"""Canonical V1 training-memory builder.

EOD/background scheduling is only a storage cadence. An example is written only after the
multi-session XAU_CLEAN_MOVE_V1 horizon is actually COMPLETE. Pending is unknown, never false.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime
from typing import Any

from aureon.models.enums import DirectionContext, HorizonStatus, SetupEventType
from aureon.models.training import CanonicalOutcome, TrainingExample
from aureon.services.learning_contract import (
    FEATURE_SCHEMA_V1,
    LABEL_SCHEMA_V1,
    FeatureBuilder,
)

V1_RULE_ID = "XAU_CLEAN_MOVE_V1"
V1_HORIZON_ID = "learning_864"


class TrainingMemoryBuilder:
    """Turn frozen setup evidence + later completed outcome into one canonical row."""

    feature_schema_version = FEATURE_SCHEMA_V1
    label_schema_version = LABEL_SCHEMA_V1

    def __init__(
        self,
        *,
        setups: Any,
        evaluations: Any,
        memory: Any,
        point: float,
        rule_id: str = V1_RULE_ID,
        horizon_id: str = V1_HORIZON_ID,
        clean_max_mae: float = 7.0,
    ) -> None:
        if point <= 0:
            raise ValueError("point must be positive")
        if clean_max_mae < 0:
            raise ValueError("clean_max_mae must be non-negative")
        self.setups = setups
        self.evaluations = evaluations
        self.memory = memory
        self.point = point
        self.rule_id = rule_id
        self.horizon_id = horizon_id
        self.clean_max_mae = clean_max_mae

    def build_resolved_day(
        self,
        *,
        symbol: str,
        market_date: str,
        generated_at: datetime,
    ) -> list[TrainingExample]:
        """Write every setup from this day whose V1 multi-session outcome is resolved."""
        written: list[TrainingExample] = []
        for setup in self.setups.for_market_date(symbol.upper(), market_date):
            if getattr(setup, "confirmed_at", None) is None:
                continue
            evaluation = self.evaluations.get(setup.setup_id, self.rule_id)
            if evaluation is None:
                continue
            example = self.from_setup(setup, evaluation, generated_at=generated_at)
            if example is None:
                continue
            self.memory.write_example(example)
            written.append(example)
        return written

    def from_setup(
        self,
        setup: Any,
        evaluation: Any,
        *,
        generated_at: datetime,
    ) -> TrainingExample | None:
        confirmed = self._confirmed_event(setup.setup_id)
        if confirmed is None:
            return None
        horizon = next(
            (one for one in evaluation.horizons if one.horizon_id == self.horizon_id),
            None,
        )
        if horizon is None or horizon.status is not HorizonStatus.COMPLETE:
            return None
        if evaluation.reference_value is None:
            return None

        features = FeatureBuilder.freeze_setup(setup, confirmed)
        features = dict(features)
        # The executable reference is the first price available after confirmation.
        features["entry_price"] = float(evaluation.reference_value)

        def reached(target: int) -> bool:
            return bool(horizon.reached.get(str(target), False))

        def bars_to(target: int) -> int | None:
            seconds = horizon.time_to.get(str(target))
            if seconds is None:
                return None
            return max(1, int(math.ceil(float(seconds) / setup.timeframe.seconds)))

        mae_points = horizon.mae_before.get("10")
        mae_before_10 = None if mae_points is None else float(mae_points) * self.point
        max_favourable = max(0.0, float(horizon.mfe or 0.0) * self.point)
        max_adverse = abs(min(0.0, float(horizon.mae or 0.0))) * self.point
        clean = bool(
            reached(10)
            and mae_before_10 is not None
            and mae_before_10 <= self.clean_max_mae
            and not bool(horizon.path_ambiguous and mae_before_10 > self.clean_max_mae)
        )

        outcome = CanonicalOutcome(
            clean_10=clean,
            reached_5=reached(5),
            reached_10=reached(10),
            reached_20=reached(20),
            reached_30=reached(30),
            reached_40=reached(40),
            mae_before_10=mae_before_10,
            max_favourable_move=max_favourable,
            max_adverse_move=max_adverse,
            bars_to_5=bars_to(5),
            bars_to_10=bars_to(10),
            bars_to_20=bars_to(20),
            bars_to_30=bars_to(30),
            bars_to_40=bars_to(40),
            ambiguous_clean_10_bar=bool(horizon.path_ambiguous),
        )

        direction = (
            "buy"
            if setup.direction_context is DirectionContext.BULLISH
            else "sell"
            if setup.direction_context is DirectionContext.BEARISH
            else None
        )
        if direction is None:
            return None

        return TrainingExample(
            example_id=self._example_id(setup.setup_id),
            market_date=setup.market_date,
            symbol=setup.symbol,
            timeframe=setup.timeframe,
            setup_id=setup.setup_id,
            family=setup.family,
            direction_context=setup.direction_context,
            setup_version=setup.setup_version,
            feature_schema_version=FEATURE_SCHEMA_V1,
            label_schema_version=LABEL_SCHEMA_V1,
            context={},
            agent_read=features.get("agents", {}),
            features=features,
            outcome=outcome,
            setup_created_at=setup.opened_at,
            feature_frozen_at=confirmed.market_time.utc,
            outcome_resolved_at=horizon.completed_at,
            entry_price=float(evaluation.reference_value),
            direction=direction,
            # Legacy fields stay intentionally non-authoritative on V1 rows.
            six_dollar_status="unavailable",
            six_dollar_reached=None,
            twenty_dollar_reached=outcome.reached_20,
            forty_dollar_reached=outcome.reached_40,
            max_favourable_move_price=outcome.max_favourable_move,
            mfe_points=horizon.mfe,
            mae_points=horizon.mae,
            evaluation_rule_id=evaluation.rule_id,
            evaluation_complete=True,
            generated_at=generated_at,
        )

    def _confirmed_event(self, setup_id: str) -> Any | None:
        for event in self.setups.events.for_setup(setup_id):
            if event.event_type is SetupEventType.CONFIRMED:
                return event
        return None

    @staticmethod
    def _example_id(setup_id: str) -> str:
        raw = f"{setup_id}|{FEATURE_SCHEMA_V1}|{LABEL_SCHEMA_V1}"
        return "training_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:28]
