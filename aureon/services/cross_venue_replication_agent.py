"""Agent 19: Cross-Venue Replication / Latency Bridge.

Agent 19 does not send broker orders. It converts a fast source-venue observation into an
immutable execution blueprint and validates whether that same idea is still actionable at
a slower/different target venue.

The source can be MT5 and the target cTrader, but the models are venue-neutral so replay,
paper and future venues use the same contract.

Important:
- expected latency is a budget/reference, never a forced sleep;
- target quote is always re-read on the target venue;
- target volume is never copied from the source venue;
- late or moved opportunities are skipped rather than chased.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from aureon.models.base import to_utc
from aureon.models.cross_venue import (
    CrossVenueBlueprint,
    CrossVenueDecision,
    ExecutionVenue,
    LeadLagClass,
    ReplicationState,
    VenueFillSample,
    VenueLatencySample,
)
from aureon.models.enums import Direction
from aureon.models.market import QuoteSnapshot


class CrossVenueReplicationAgent:
    agent_name = "cross_venue_replication"
    agent_version = "1.0.0"

    def create_blueprint(
        self,
        *,
        source_symbol: str,
        target_symbol: str,
        direction: Direction,
        observed_at: datetime,
        source_price: float,
        scenario_signature: str | None = None,
        preferred_zone_low: float | None = None,
        preferred_zone_high: float | None = None,
        invalidation_price: float | None = None,
        primary_target_move: float = 10.0,
        expected_delay_ms: float = 0.0,
        max_valid_delay_ms: float = 2500.0,
        max_price_drift: float = 2.0,
        min_capture_gap: float = 0.0,
        max_capture_gap: float | None = None,
        max_spread_points: float | None = None,
        source_detection_id: str | None = None,
        source_request_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> CrossVenueBlueprint:
        observed = to_utc(observed_at)
        raw_id = "|".join(
            [
                "cvb1",
                source_symbol.upper(),
                target_symbol.upper(),
                direction.value,
                observed.isoformat(),
                scenario_signature or "",
                source_detection_id or "",
                source_request_id or "",
            ]
        )
        blueprint_id = "cvb_" + hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:24]
        return CrossVenueBlueprint(
            blueprint_id=blueprint_id,
            source_venue=ExecutionVenue.MT5,
            target_venue=ExecutionVenue.CTRADER,
            source_symbol=source_symbol.upper(),
            target_symbol=target_symbol.upper(),
            direction=direction,
            source_observed_at=observed,
            source_price=source_price,
            scenario_signature=scenario_signature,
            preferred_zone_low=preferred_zone_low,
            preferred_zone_high=preferred_zone_high,
            invalidation_price=invalidation_price,
            primary_target_move=primary_target_move,
            expected_delay_ms=expected_delay_ms,
            max_valid_delay_ms=max_valid_delay_ms,
            max_price_drift=max_price_drift,
            min_capture_gap=min_capture_gap,
            max_capture_gap=max_capture_gap,
            max_spread_points=max_spread_points,
            source_detection_id=source_detection_id,
            source_request_id=source_request_id,
            metadata=dict(metadata or {}),
        )

    def validate_target(
        self,
        blueprint: CrossVenueBlueprint,
        *,
        quote: QuoteSnapshot,
        observed_at: datetime,
        market_open: bool,
        risk_allowed: bool,
        source_still_valid: bool = True,
        target_display_price: float | None = None,
    ) -> tuple[CrossVenueDecision, VenueLatencySample]:
        target_at = to_utc(observed_at)
        delay_ms = max(
            0.0,
            (target_at - blueprint.source_observed_at).total_seconds() * 1000.0,
        )
        target_price = quote.price_for(is_buy=blueprint.direction is Direction.BUY)
        signed_drift = (target_price - blueprint.source_price) * blueprint.direction.sign
        abs_drift = abs(target_price - blueprint.source_price)
        # Positive lead_gap means the target executable price is still BEHIND the MT5
        # source move in a favourable direction: BUY => target ask below source,
        # SELL => target bid above source.
        lead_gap = -signed_drift
        display_lead_gap = None
        if target_display_price is not None:
            display_drift = (
                (target_display_price - blueprint.source_price)
                * blueprint.direction.sign
            )
            display_lead_gap = -display_drift
        spread_points = quote.spread_points

        evidence = [
            f"source={blueprint.source_venue.value}:{blueprint.source_symbol}",
            f"target={blueprint.target_venue.value}:{blueprint.target_symbol}",
            f"delay={delay_ms:.0f}ms",
            f"price_drift={signed_drift:+g}",
            f"lead_gap={lead_gap:+g}",
        ]

        lead_lag_class = self._classify_lead_lag(
            lead_gap=lead_gap,
            display_lead_gap=display_lead_gap,
            min_capture_gap=blueprint.min_capture_gap,
            max_capture_gap=blueprint.max_capture_gap,
            source_still_valid=source_still_valid,
        )

        state = ReplicationState.EXECUTABLE
        reason = "target venue still satisfies the source blueprint"

        if not source_still_valid:
            state = ReplicationState.SKIP_SOURCE_INVALID
            reason = "MT5 lead setup is no longer valid"
        elif not market_open:
            state = ReplicationState.SKIP_MARKET
            reason = "target venue market is not open"
        elif not risk_allowed:
            state = ReplicationState.SKIP_RISK
            reason = "target-venue risk gate did not allow replication"
        elif delay_ms > blueprint.max_valid_delay_ms:
            state = ReplicationState.SKIP_LATE
            reason = (
                f"target arrived after {delay_ms:.0f}ms; "
                f"limit is {blueprint.max_valid_delay_ms:.0f}ms"
            )
        elif (
            blueprint.max_spread_points is not None
            and (spread_points is None or spread_points > blueprint.max_spread_points)
        ):
            state = ReplicationState.SKIP_SPREAD
            reason = "target spread is unavailable or exceeds the blueprint limit"
        elif self._invalidated(blueprint, target_price):
            state = ReplicationState.SKIP_INVALIDATED
            reason = "target price crossed the source blueprint invalidation"
        elif abs_drift > blueprint.max_price_drift and lead_gap <= 0:
            state = ReplicationState.SKIP_MOVED
            reason = (
                f"target price moved {abs_drift:g} beyond the MT5 source; "
                f"maximum allowed drift is {blueprint.max_price_drift:g}"
            )
        elif (
            blueprint.max_capture_gap is not None
            and lead_gap > blueprint.max_capture_gap
        ):
            state = ReplicationState.SKIP_MOVED
            reason = (
                f"target is {lead_gap:g} behind source, exceeding the "
                f"allowed lead-lag gap {blueprint.max_capture_gap:g}"
            )

        within_zone = self._within_zone(blueprint, target_price)
        if (
            state is ReplicationState.EXECUTABLE
            and within_zone is False
            and blueprint.preferred_zone_low is not None
            and blueprint.preferred_zone_high is not None
        ):
            state = ReplicationState.SKIP_MOVED
            reason = "target quote is outside the source setup entry zone"

        # A genuine executable lag is the special capture case. We only promote it
        # after every normal market/risk/spread/invalidation/zone gate has passed.
        if state is ReplicationState.EXECUTABLE:
            if lead_lag_class is LeadLagClass.EXECUTABLE_LAG:
                state = ReplicationState.CAPTURE_WINDOW
                reason = (
                    f"cTrader executable quote is {lead_gap:g} behind the MT5-confirmed "
                    "move and the source setup remains valid"
                )
            elif lead_gap > 0:
                state = ReplicationState.TARGET_CATCHING_UP
                reason = "target venue is behind MT5 but below the configured capture gap"

        decision = CrossVenueDecision(
            blueprint_id=blueprint.blueprint_id,
            state=state,
            actual_delay_ms=delay_ms,
            target_price=target_price,
            signed_price_drift=signed_drift,
            absolute_price_drift=abs_drift,
            lead_gap=lead_gap,
            lead_lag_class=lead_lag_class,
            source_still_valid=source_still_valid,
            target_display_price=target_display_price,
            display_lead_gap=display_lead_gap,
            expected_delay_ms=blueprint.expected_delay_ms,
            delay_delta_ms=delay_ms - blueprint.expected_delay_ms,
            within_entry_zone=within_zone,
            spread_points=spread_points,
            reason=reason,
            evidence=tuple(evidence),
        )
        sample = VenueLatencySample(
            blueprint_id=blueprint.blueprint_id,
            source_venue=blueprint.source_venue,
            target_venue=blueprint.target_venue,
            symbol=blueprint.target_symbol,
            source_observed_at=blueprint.source_observed_at,
            target_observed_at=target_at,
            latency_ms=delay_ms,
            source_price=blueprint.source_price,
            target_price=target_price,
            signed_price_drift=signed_drift,
        )
        return decision, sample

    @staticmethod
    def _within_zone(blueprint: CrossVenueBlueprint, price: float) -> bool | None:
        if blueprint.preferred_zone_low is None or blueprint.preferred_zone_high is None:
            return None
        return blueprint.preferred_zone_low <= price <= blueprint.preferred_zone_high

    @staticmethod
    def _invalidated(blueprint: CrossVenueBlueprint, price: float) -> bool:
        if blueprint.invalidation_price is None:
            return False
        if blueprint.direction is Direction.BUY:
            return price <= blueprint.invalidation_price
        return price >= blueprint.invalidation_price


    def record_fill(
        self,
        blueprint: CrossVenueBlueprint,
        *,
        target_quote_price: float,
        fill_price: float,
        observed_at: datetime,
    ) -> VenueFillSample:
        """Record whether cTrader's visible/executable lag survived through the fill."""

        return VenueFillSample(
            blueprint_id=blueprint.blueprint_id,
            source_venue=blueprint.source_venue,
            target_venue=blueprint.target_venue,
            symbol=blueprint.target_symbol,
            target_quote_price=target_quote_price,
            fill_price=fill_price,
            source_price=blueprint.source_price,
            fill_slippage_from_quote=(
                (fill_price - target_quote_price) * blueprint.direction.sign
            ),
            fill_slippage_from_source=(
                (fill_price - blueprint.source_price) * blueprint.direction.sign
            ),
            observed_at=to_utc(observed_at),
        )

    @staticmethod
    def _classify_lead_lag(
        *,
        lead_gap: float,
        display_lead_gap: float | None,
        min_capture_gap: float,
        max_capture_gap: float | None,
        source_still_valid: bool,
    ) -> LeadLagClass:
        if not source_still_valid:
            return LeadLagClass.REVERSAL
        if max_capture_gap is not None and lead_gap > max_capture_gap:
            return LeadLagClass.DIVERGENCE
        if lead_gap >= min_capture_gap and lead_gap > 0:
            return LeadLagClass.EXECUTABLE_LAG
        if (
            display_lead_gap is not None
            and display_lead_gap >= min_capture_gap
            and display_lead_gap > 0
            and lead_gap <= 0
        ):
            return LeadLagClass.DISPLAY_ONLY_LAG
        if abs(lead_gap) <= max(min_capture_gap, 1e-12):
            return LeadLagClass.NO_LAG
        return LeadLagClass.DIVERGENCE
