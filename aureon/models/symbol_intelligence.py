"""Agent 18 models: symbol classification, tuning provenance and server activity."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from aureon.models.base import AureonModel


class InstrumentClass(StrEnum):
    METAL = "metal"
    FOREX = "forex"
    CRYPTO = "crypto"
    INDEX = "index"
    ENERGY = "energy"
    OTHER = "other"


class SymbolRunState(StrEnum):
    ACTIVE = "active"
    PREOPEN = "preopen"
    CLOSED = "closed"
    STALE = "stale"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class SymbolIntelligenceProfile(AureonModel):
    symbol: str
    instrument_class: InstrumentClass
    configured: bool = True
    supported: bool = False
    state: SymbolRunState = SymbolRunState.UNKNOWN
    market_state: str = "unknown"
    active: bool = False
    reason: str
    tuning_source: str
    tuning_version: str = "SYMBOL_TUNING_V1"
    point: float | None = None
    digits: int | None = None
    trade_mode: str | None = None
    overridden_fields: tuple[str, ...] = ()
    tuning: dict[str, float] = Field(default_factory=dict)


class SymbolIntelligenceReport(AureonModel):
    server_role: str = "windows_mt5"
    configured_symbols: tuple[str, ...] = ()
    active_symbols: tuple[str, ...] = ()
    inactive_symbols: tuple[str, ...] = ()
    by_class: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    profiles: tuple[SymbolIntelligenceProfile, ...] = ()
