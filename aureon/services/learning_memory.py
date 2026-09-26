"""Cross-session live learning memory for Aureon V1.

A confirmed setup freezes one feature snapshot. Subsequent closed candles only update the
outcome accumulator. The setup candle itself is never counted, so future price information
cannot leak back into the feature snapshot.
"""

from __future__ import annotations

import hashlib
from typing import Any

from aureon.models.base import to_utc, utc_now
from aureon.models.enums import Direction
from aureon.models.learning_v1 import CleanMoveOutcomeV1, PendingLearningSetup
from aureon.services.learning_contract import FeatureBuilder, canonical_example

TARGETS = (5, 10, 20, 30, 40)


class LearningMemoryService:
    """Freeze live setup state and resolve canonical outcomes across sessions."""

    def __init__(
        self,
        repository: Any,
        *,
        horizon_bars: int = 864,
        clean_target: float = 10.0,
        clean_max_mae: float = 7.0,
        now: Any = utc_now,
    ) -> None:
        if horizon_bars < 1:
            raise ValueError("horizon_bars must be >= 1")
        self.repository = repository
        self.horizon_bars = horizon_bars
        self.clean_target = clean_target
        self.clean_max_mae = clean_max_mae
        self._now = now

    def freeze_setup(
        self,
        setup: Any,
        event: Any,
        *,
        full_context: dict[str, Any] | None = None,
    ) -> PendingLearningSetup | None:
        """Persist the immutable setup state once. Neutral setups are not trainable."""
        try:
            features = FeatureBuilder.from_setup_event(
                setup,
                event,
                extra_context=full_context,
            )
        except ValueError:
            return None

        existing = self.repository.pending_for_setup(setup.setup_id)
        if existing is not None:
            return existing

        moment = to_utc(event.market_time.utc)
        learning_id = hashlib.sha256(
            (
                f"{setup.setup_id}|{features.feature_schema}|"
                "AUREON_CLEAN_MOVE_V1"
            ).encode("utf-8")
        ).hexdigest()
        pending = PendingLearningSetup(
            learning_id=learning_id,
            setup_id=setup.setup_id,
            symbol=setup.symbol,
            timeframe=setup.timeframe,
            market_date=setup.market_date,
            features=features,
            horizon_bars=self.horizon_bars,
            created_at=moment,
            updated_at=moment,
        )
        self.repository.write_pending(pending)
        return pending

    def on_closed_candle(self, candle: Any) -> int:
        """Advance every pending setup on this symbol/timeframe; return resolutions."""
        timeframe = getattr(candle.timeframe, "value", candle.timeframe)
        rows = self.repository.pending_for_stream(candle.symbol, str(timeframe))
        resolved = 0
        for pending in rows:
            # The feature snapshot is true at setup close. Its own candle is not an outcome bar.
            if to_utc(candle.close_time) <= pending.features.timestamp:
                continue
            updated = self._advance(pending, candle)
            self.repository.write_pending(updated)
            if updated.status == "resolved":
                outcome = self._outcome(updated, resolved_at=candle.close_time)
                example = canonical_example(
                    features=updated.features,
                    outcome=outcome,
                    market_date=updated.market_date,
                    generated_at=to_utc(candle.close_time),
                )
                self.repository.write_canonical(example)
                resolved += 1
        return resolved

    def _advance(self, pending: PendingLearningSetup, candle: Any) -> PendingLearningSetup:
        direction = pending.features.direction
        entry = pending.features.reference_price
        if direction is Direction.BUY:
            favourable = max(0.0, float(candle.high) - entry)
            adverse = max(0.0, entry - float(candle.low))
        else:
            favourable = max(0.0, entry - float(candle.low))
            adverse = max(0.0, float(candle.high) - entry)

        bar = pending.bars_seen + 1
        update: dict[str, Any] = {
            "bars_seen": bar,
            "updated_at": to_utc(candle.close_time),
            "max_favourable_move": max(pending.max_favourable_move, favourable),
            "max_adverse_move": max(pending.max_adverse_move, adverse),
        }

        for target in TARGETS:
            key = f"reached_{target}"
            bars_key = f"bars_to_{target}"
            if not getattr(pending, key) and favourable >= float(target):
                update[key] = True
                update[bars_key] = bar

        if not pending.reached_10:
            prior_adverse = pending.max_adverse_move
            if favourable >= self.clean_target:
                update["mae_before_10"] = max(prior_adverse, adverse)
                # M5 does not reveal whether target or >$7 adverse happened first inside
                # the same candle. Fail closed for the clean label and record ambiguity.
                if prior_adverse <= self.clean_max_mae and adverse > self.clean_max_mae:
                    update["path_ambiguous"] = True

        if bar >= pending.horizon_bars:
            update["status"] = "resolved"

        return pending.model_copy(update=update)

    def _outcome(
        self,
        pending: PendingLearningSetup,
        *,
        resolved_at: Any,
    ) -> CleanMoveOutcomeV1:
        clean = bool(
            pending.reached_10
            and pending.mae_before_10 is not None
            and pending.mae_before_10 <= self.clean_max_mae
            and not pending.path_ambiguous
        )
        return CleanMoveOutcomeV1(
            resolved_at=to_utc(resolved_at),
            horizon_bars=pending.horizon_bars,
            clean_target=self.clean_target,
            clean_max_mae=self.clean_max_mae,
            clean_10=clean,
            path_ambiguous=pending.path_ambiguous,
            reached_5=pending.reached_5,
            reached_10=pending.reached_10,
            reached_20=pending.reached_20,
            reached_30=pending.reached_30,
            reached_40=pending.reached_40,
            bars_to_5=pending.bars_to_5,
            bars_to_10=pending.bars_to_10,
            bars_to_20=pending.bars_to_20,
            bars_to_30=pending.bars_to_30,
            bars_to_40=pending.bars_to_40,
            max_favourable_move=pending.max_favourable_move,
            max_adverse_move=pending.max_adverse_move,
            mae_before_10=pending.mae_before_10,
        )
