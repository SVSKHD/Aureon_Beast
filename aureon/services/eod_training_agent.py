"""End-of-day training memory builder.

This service runs after a broker day is complete. It converts frozen setup-time evidence plus
later outcomes into immutable training examples. It never changes detections, setups, or trading
settings and never promotes a model.

The primary label is whether the setup achieved a favourable +6.0 XAUUSD quote-price move before
the broker day ended. The "no-SL" research variable is maximum adverse quote-price excursion
before that first +6 reach, reconstructed from the stored day frame when possible.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from statistics import median
from datetime import datetime
from typing import Any

from aureon.models.base import to_utc, utc_now
from aureon.models.enums import DirectionContext, SetupEventType, Timeframe
from aureon.models.training import (
    DailyTrainingStatus,
    TrainingExample,
    TrainingTimeframeStatus,
)

FEATURE_SCHEMA_VERSION = "EOD_SETUP_FEATURES_V1"
LABEL_SCHEMA_VERSION = "FAVOURABLE_MOVE_LADDER_V2"


class EodTrainingAgent:
    agent_name = "eod_training_memory"
    agent_version = "1.0.0"
    feature_schema_version = FEATURE_SCHEMA_VERSION
    label_schema_version = LABEL_SCHEMA_VERSION

    def __init__(
        self,
        *,
        setups: Any,
        setup_evaluations: Any,
        market_days: Any,
        memory: Any,
        rule_id: str,
        now: Any = utc_now,
        v1_builder: Any | None = None,
    ) -> None:
        self.setups = setups
        self.setup_evaluations = setup_evaluations
        self.market_days = market_days
        self.memory = memory
        self.rule_id = rule_id
        self._now = now
        self.v1_builder = v1_builder

    def build_day(self, *, symbol: str, market_date: str) -> DailyTrainingStatus:
        """Build or rebuild one completed broker day's training memory idempotently."""
        symbol = symbol.upper()
        day = self.market_days.get_day(symbol, market_date)
        if day is None or not day.complete:
            raise ValueError(
                f"{symbol} {market_date}: broker day is not stored as complete; "
                "refusing to freeze an EOD label early"
            )

        setups = self.setups.for_market_date(symbol, market_date)
        evaluations = self.setup_evaluations.get_many(setups, self.rule_id)
        generated_at = to_utc(self._now())

        rows: list[TrainingExample] = []
        for setup in setups:
            events = self.setups.events.for_setup(setup.setup_id)
            rows.append(
                self._example(
                    setup=setup,
                    events=events,
                    evaluation=evaluations.get(setup.setup_id),
                    generated_at=generated_at,
                )
            )

        for row in rows:
            self.memory.write_example(row)

        status = self._status(
            symbol=symbol,
            market_date=market_date,
            rows=rows,
            generated_at=generated_at,
        )
        self.memory.write_status(status)
        return status

    def build_v1_resolved(
        self,
        *,
        symbol: str,
        market_date: str,
    ) -> list[TrainingExample]:
        """Persist V1 rows only after their multi-session outcome is COMPLETE.

        This may be called repeatedly for the same broker day. Deterministic example ids
        make it idempotent, and pending outcomes remain absent rather than becoming misses.
        """
        if self.v1_builder is None:
            return []
        return self.v1_builder.build_resolved_day(
            symbol=symbol,
            market_date=market_date,
            generated_at=to_utc(self._now()),
        )

    def _example(
        self,
        *,
        setup: Any,
        events: list[Any],
        evaluation: Any | None,
        generated_at: datetime,
    ) -> TrainingExample:
        decision = self._decision_event(events)
        tracking = self._event(events, SetupEventType.FAVOURABLE_MOVE_6_TRACKING)
        reached = self._event(events, SetupEventType.FAVOURABLE_MOVE_6_REACHED)
        reached_20 = self._event(events, SetupEventType.FAVOURABLE_MOVE_20_REACHED)
        reached_40 = self._event(events, SetupEventType.FAVOURABLE_MOVE_40_REACHED)

        if reached is not None:
            six_status = "reached"
            six_reached: bool | None = True
        elif tracking is not None:
            six_status = "not_reached_eod"
            six_reached = False
        else:
            six_status = "unavailable"
            six_reached = None

        tracking_snapshot = getattr(tracking, "context_snapshot", {}) or {}
        reached_snapshot = getattr(reached, "context_snapshot", {}) or {}
        reference_price = self._float(tracking_snapshot.get("reference_price"))
        threshold_price = self._float(
            tracking_snapshot.get("threshold_6")
            or tracking_snapshot.get("threshold_price")
        )
        reached_at = getattr(getattr(reached, "market_time", None), "utc", None)

        time_to_six = self._time_to(tracking, reached)
        time_to_twenty = self._time_to(tracking, reached_20)
        time_to_forty = self._time_to(tracking, reached_40)

        horizon = self._training_horizon(evaluation)
        mfe = getattr(horizon, "mfe", None) if horizon is not None else None
        mae = getattr(horizon, "mae", None) if horizon is not None else None

        mae_before_six = self._mae_before_six(
            setup=setup,
            tracking=tracking,
            reached=reached,
            reference_price=reference_price,
        )
        max_favourable = self._max_favourable_move(
            setup=setup,
            tracking=tracking,
            reference_price=reference_price,
        )
        extension_after_six = (
            max(0.0, max_favourable - 6.0)
            if max_favourable is not None and reached is not None
            else None
        )

        snapshot = getattr(decision, "context_snapshot", {}) or {}
        agent_read = self._agent_read(snapshot)
        context = {
            "session": getattr(getattr(setup.context_summary, "session", None), "value", None),
            "volatility_regime": getattr(setup.context_summary, "volatility_regime", None),
            "price_vs_va": getattr(setup.context_summary, "price_vs_va", None),
            "mtf_alignment": getattr(
                getattr(setup.context_summary, "mtf_alignment", None),
                "value",
                None,
            ),
            "ema_fast": self._float(snapshot.get("ema_fast")),
            "ema_slow": self._float(snapshot.get("ema_slow")),
            "rsi": self._float(snapshot.get("rsi")),
            "trend": snapshot.get("trend"),
            "decision_event": getattr(
                getattr(decision, "event_type", None),
                "value",
                None,
            ),
            "agent_confidence_pct": self._float(snapshot.get("agent_confidence_pct")),
            "linked_detection_ids": list(setup.linked_detection_ids),
        }

        return TrainingExample(
            example_id=self._example_id(setup.setup_id),
            market_date=setup.market_date,
            symbol=setup.symbol,
            timeframe=setup.timeframe,
            setup_id=setup.setup_id,
            family=setup.family,
            direction_context=setup.direction_context,
            setup_version=setup.setup_version,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            label_schema_version=LABEL_SCHEMA_VERSION,
            context=context,
            agent_read=agent_read,
            six_dollar_status=six_status,
            six_dollar_reached=six_reached,
            six_dollar_reference_price=reference_price,
            six_dollar_threshold_price=threshold_price,
            six_dollar_reached_at=reached_at,
            time_to_six_seconds=time_to_six,
            twenty_dollar_reached=(
                True if reached_20 is not None else False if tracking is not None else None
            ),
            forty_dollar_reached=(
                True if reached_40 is not None else False if tracking is not None else None
            ),
            time_to_twenty_seconds=time_to_twenty,
            time_to_forty_seconds=time_to_forty,
            max_favourable_move_price=max_favourable,
            extension_after_six_price=extension_after_six,
            mfe_points=mfe,
            mae_points=mae,
            mae_before_six_price=mae_before_six,
            evaluation_rule_id=(
                getattr(evaluation, "evaluation_rule_id", None)
                or getattr(evaluation, "rule_id", None)
                if evaluation is not None
                else None
            ),
            evaluation_complete=bool(
                evaluation is not None and evaluation.is_fully_evaluated
            ),
            generated_at=generated_at,
        )

    def _decision_event(self, events: list[Any]) -> Any | None:
        confirmed = self._event(events, SetupEventType.CONFIRMED)
        if confirmed is not None:
            return confirmed
        # Some families can remain observational all day. The last same-day event is the
        # latest frozen state the setup itself actually recorded, never an EOD recomputation.
        return events[-1] if events else None

    @staticmethod
    def _event(events: list[Any], event_type: SetupEventType) -> Any | None:
        return next(
            (event for event in events if event.event_type is event_type),
            None,
        )

    @staticmethod
    def _agent_read(snapshot: dict[str, Any]) -> dict[str, Any]:
        names = ("ema_cross", "rsi", "session_trend", "wick", "liquidity", "breakout")
        read: dict[str, Any] = {}
        for name in names:
            prefix = f"agent_{name}"
            read[name] = {
                "stance": snapshot.get(f"{prefix}_stance"),
                "alignment": snapshot.get(f"{prefix}_alignment"),
                "observation": snapshot.get(f"{prefix}_observation"),
            }
        return read

    @staticmethod
    def _training_horizon(evaluation: Any | None) -> Any | None:
        if evaluation is None:
            return None
        complete = list(evaluation.complete_horizons)
        if not complete:
            return None
        day_close = next((one for one in complete if one.horizon_id == "day_close"), None)
        return day_close or complete[-1]

    @staticmethod
    def _time_to(tracking: Any | None, reached: Any | None) -> float | None:
        if tracking is None or reached is None:
            return None
        return max(
            0.0,
            (reached.market_time.utc - tracking.market_time.utc).total_seconds(),
        )

    def _max_favourable_move(
        self,
        *,
        setup: Any,
        tracking: Any | None,
        reference_price: float | None,
    ) -> float | None:
        if tracking is None or reference_price is None:
            return None
        frame = self.market_days.get_frame(
            setup.symbol,
            setup.market_date,
            setup.timeframe,
        )
        if frame is None or frame.truncated:
            return None

        start = tracking.market_time.utc
        best = 0.0
        seen = False
        for bar in frame.bars:
            if bar.at < start:
                continue
            seen = True
            if setup.direction_context is DirectionContext.BULLISH:
                best = max(best, bar.high - reference_price)
            elif setup.direction_context is DirectionContext.BEARISH:
                best = max(best, reference_price - bar.low)
            else:
                return None
        return max(0.0, best) if seen else 0.0

    def _mae_before_six(
        self,
        *,
        setup: Any,
        tracking: Any | None,
        reached: Any | None,
        reference_price: float | None,
    ) -> float | None:
        if tracking is None or reached is None or reference_price is None:
            return None
        frame = self.market_days.get_frame(
            setup.symbol,
            setup.market_date,
            setup.timeframe,
        )
        if frame is None or frame.truncated:
            return None

        start = tracking.market_time.utc
        end = reached.market_time.utc
        adverse = 0.0
        seen = False
        for bar in frame.bars:
            if bar.at < start or bar.at >= end:
                continue
            seen = True
            if setup.direction_context is DirectionContext.BULLISH:
                adverse = max(adverse, reference_price - bar.low)
            elif setup.direction_context is DirectionContext.BEARISH:
                adverse = max(adverse, bar.high - reference_price)
            else:
                return None
        return max(0.0, adverse) if seen else 0.0

    def _status(
        self,
        *,
        symbol: str,
        market_date: str,
        rows: list[TrainingExample],
        generated_at: datetime,
    ) -> DailyTrainingStatus:
        grouped: dict[Timeframe, list[TrainingExample]] = defaultdict(list)
        for row in rows:
            grouped[row.timeframe].append(row)

        by_timeframe_rows: list[TrainingTimeframeStatus] = []
        for timeframe, items in sorted(grouped.items(), key=lambda pair: pair[0].minutes):
            adverse = [
                one.mae_before_six_price
                for one in items
                if one.mae_before_six_price is not None
            ]
            by_timeframe_rows.append(
                TrainingTimeframeStatus(
                    timeframe=timeframe,
                    setups=len(items),
                    reached_six=sum(
                        one.six_dollar_status == "reached" for one in items
                    ),
                    not_reached_six=sum(
                        one.six_dollar_status == "not_reached_eod" for one in items
                    ),
                    unavailable_six=sum(
                        one.six_dollar_status == "unavailable" for one in items
                    ),
                    complete_evaluations=sum(
                        one.evaluation_complete for one in items
                    ),
                    mae_before_six_available=len(adverse),
                    median_mae_before_six_price=(
                        float(median(adverse)) if adverse else None
                    ),
                    max_mae_before_six_price=max(adverse) if adverse else None,
                )
            )
        by_timeframe = tuple(by_timeframe_rows)
        daily_adverse = [
            one.mae_before_six_price
            for one in rows
            if one.mae_before_six_price is not None
        ]

        return DailyTrainingStatus(
            status_id=(
                f"{symbol}_{market_date}_"
                f"{FEATURE_SCHEMA_VERSION}_{LABEL_SCHEMA_VERSION}"
            ),
            market_date=market_date,
            symbol=symbol,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            label_schema_version=LABEL_SCHEMA_VERSION,
            examples_written=len(rows),
            reached_six=sum(one.six_dollar_status == "reached" for one in rows),
            not_reached_six=sum(
                one.six_dollar_status == "not_reached_eod" for one in rows
            ),
            unavailable_six=sum(one.six_dollar_status == "unavailable" for one in rows),
            complete_evaluations=sum(one.evaluation_complete for one in rows),
            mae_before_six_available=len(daily_adverse),
            median_mae_before_six_price=(
                float(median(daily_adverse)) if daily_adverse else None
            ),
            max_mae_before_six_price=(
                max(daily_adverse) if daily_adverse else None
            ),
            by_timeframe=by_timeframe,
            generated_at=generated_at,
        )

    @staticmethod
    def _float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _example_id(setup_id: str) -> str:
        raw = f"{setup_id}|{FEATURE_SCHEMA_VERSION}|{LABEL_SCHEMA_VERSION}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
