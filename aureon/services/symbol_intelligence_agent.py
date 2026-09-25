"""Agent 18: Symbol Intelligence / Tuning Agent.

This is the single authority that says:
- what family a symbol belongs to,
- whether Aureon has approved tuning for it,
- what broker metadata overrides the profile safely,
- and whether that symbol is active on this Windows/MT5 server.

It does NOT invent research parameters. Dynamic means dynamic selection of a versioned
approved profile + live broker metadata, not self-modifying thresholds.
"""

from __future__ import annotations

import re
from collections import defaultdict

from aureon.config.symbol_tuning import has_tuning, tuning_for
from aureon.models.enums import MarketState
from aureon.models.symbol_intelligence import (
    InstrumentClass,
    SymbolIntelligenceProfile,
    SymbolIntelligenceReport,
    SymbolRunState,
)

_FX_CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
    "SGD", "HKD", "NOK", "SEK", "DKK", "PLN", "TRY", "ZAR", "MXN",
}
_CRYPTO_BASES = {
    "BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "BNB", "LTC", "BCH", "DOT", "AVAX",
}
_METALS = {"XAU", "XAG", "XPT", "XPD"}
_ENERGY_TOKENS = {"XTI", "XBR", "WTI", "BRENT", "USOIL", "UKOIL", "NGAS"}
_INDEX_TOKENS = {
    "US30", "DJ30", "NAS100", "USTEC", "SPX500", "US500", "GER40", "DE40",
    "UK100", "JP225", "AUS200", "FRA40",
}


class SymbolIntelligenceAgent:
    agent_name = "symbol_intelligence"
    agent_version = "1.0.0"
    tuning_version = "SYMBOL_TUNING_V1"

    @staticmethod
    def classify(symbol: str) -> InstrumentClass:
        raw = symbol.upper()
        # Brokers often append suffixes (.m, _pro, -ECN). Classification uses the
        # leading alphanumeric contract rather than assuming a specific broker naming style.
        clean = re.sub(r"[^A-Z0-9]", "", raw)

        if any(clean.startswith(code) for code in _METALS):
            return InstrumentClass.METAL
        if any(token in clean for token in _ENERGY_TOKENS):
            return InstrumentClass.ENERGY
        if any(token in clean for token in _INDEX_TOKENS):
            return InstrumentClass.INDEX
        if any(clean.startswith(base) for base in _CRYPTO_BASES):
            return InstrumentClass.CRYPTO

        # A conventional FX contract begins with two ISO-like three-letter currencies.
        if len(clean) >= 6 and clean[:3] in _FX_CURRENCIES and clean[3:6] in _FX_CURRENCIES:
            return InstrumentClass.FOREX
        return InstrumentClass.OTHER

    def inspect(
        self,
        *,
        symbol: str,
        configured: bool,
        market_result: object | None,
        symbol_info: object | None,
    ) -> SymbolIntelligenceProfile:
        family = self.classify(symbol)
        reviewed = has_tuning(symbol)

        point = getattr(symbol_info, "point", None)
        digits = getattr(symbol_info, "digits", None)
        trade_mode = getattr(symbol_info, "trade_mode", None)

        tuning = None
        if reviewed:
            tuning = tuning_for(symbol, point=point)

        market_state = getattr(market_result, "state", MarketState.UNKNOWN)
        reason = getattr(market_result, "reason", "market state unavailable")
        state = _run_state(market_state)

        if not reviewed:
            state = SymbolRunState.UNSUPPORTED
            active = False
            reason = (
                f"{family.value} symbol has no approved tuning profile; "
                "classification is known but trading analysis is intentionally disabled"
            )
        else:
            active = (
                configured
                and state is SymbolRunState.ACTIVE
                and trade_mode in {None, "full", "longonly", "shortonly"}
            )
            if configured and state is SymbolRunState.ACTIVE and trade_mode == "close_only":
                active = False
                reason = "broker is close-only"

        snapshot = {}
        overridden = ()
        if tuning is not None:
            snapshot = {
                "point": float(tuning.point),
                "min_penetration_points": float(tuning.min_penetration_points),
                "min_rejection_fraction": float(tuning.min_rejection_fraction),
                "min_close_beyond_points": float(tuning.min_close_beyond_points),
                "min_wick_range_ratio": float(tuning.min_wick_range_ratio),
                "min_wick_body_ratio": float(tuning.min_wick_body_ratio),
                "max_close_position": float(tuning.max_close_position),
                "min_range_points": float(tuning.min_range_points),
                "flat_points": float(tuning.flat_points),
                "volume_bin_points": float(tuning.volume_bin_points),
                "low_volatility_ratio": float(tuning.low_volatility_ratio),
                "high_volatility_ratio": float(tuning.high_volatility_ratio),
            }
            overridden = tuple(tuning.overridden)

        return SymbolIntelligenceProfile(
            symbol=symbol.upper(),
            instrument_class=family,
            configured=configured,
            supported=reviewed,
            state=state,
            market_state=getattr(market_state, "value", str(market_state)),
            active=active,
            reason=reason,
            tuning_source=("symbol_override+broker_metadata" if reviewed else "none"),
            tuning_version=self.tuning_version,
            point=float(point) if point is not None else (snapshot.get("point") if snapshot else None),
            digits=int(digits) if digits is not None else None,
            trade_mode=str(trade_mode) if trade_mode is not None else None,
            overridden_fields=overridden,
            tuning=snapshot,
        )

    def report(self, profiles: list[SymbolIntelligenceProfile]) -> SymbolIntelligenceReport:
        grouped: dict[str, list[str]] = defaultdict(list)
        for profile in profiles:
            grouped[profile.instrument_class.value].append(profile.symbol)

        configured = tuple(profile.symbol for profile in profiles if profile.configured)
        active = tuple(profile.symbol for profile in profiles if profile.active)
        inactive = tuple(profile.symbol for profile in profiles if not profile.active)

        return SymbolIntelligenceReport(
            configured_symbols=configured,
            active_symbols=active,
            inactive_symbols=inactive,
            by_class={
                name: tuple(sorted(symbols))
                for name, symbols in sorted(grouped.items())
            },
            profiles=tuple(profiles),
        )


def _run_state(state: MarketState) -> SymbolRunState:
    if state is MarketState.OPEN:
        return SymbolRunState.ACTIVE
    if state is MarketState.PREOPEN:
        return SymbolRunState.PREOPEN
    if state is MarketState.CLOSED:
        return SymbolRunState.CLOSED
    if state is MarketState.STALE:
        return SymbolRunState.STALE
    return SymbolRunState.UNKNOWN
