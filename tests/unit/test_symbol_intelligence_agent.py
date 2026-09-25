"""Agent 18: instrument families, dynamic approved tuning and active-symbol reporting."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aureon.config.symbol_tuning import UnknownSymbolTuning
from aureon.models.enums import FillingMode, MarketState
from aureon.models.market import SymbolInfo
from aureon.models.symbol_intelligence import InstrumentClass, SymbolRunState
from aureon.services.market_state_service import MarketStateResult, classify_market_state
from aureon.services.symbol_intelligence_agent import SymbolIntelligenceAgent


def _info(symbol: str, *, point: float, digits: int, trade_mode: str = "full") -> SymbolInfo:
    return SymbolInfo(
        symbol=symbol,
        point=point,
        digits=digits,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        stops_level=0,
        filling_modes=(FillingMode.FOK,),
        trade_mode=trade_mode,
    )


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("XAUUSD", InstrumentClass.METAL),
        ("XAGUSD", InstrumentClass.METAL),
        ("EURUSD", InstrumentClass.FOREX),
        ("GBPJPY.a", InstrumentClass.FOREX),
        ("BTCUSD", InstrumentClass.CRYPTO),
        ("ETHUSDm", InstrumentClass.CRYPTO),
        ("US30", InstrumentClass.INDEX),
        ("USOIL", InstrumentClass.ENERGY),
    ],
)
def test_symbol_family_classification(symbol: str, expected: InstrumentClass) -> None:
    assert SymbolIntelligenceAgent.classify(symbol) is expected


def test_approved_symbol_uses_live_broker_point() -> None:
    agent = SymbolIntelligenceAgent()
    tuning = agent.resolve_tuning("XAUUSD", point=0.1)

    assert tuning.point == 0.1
    assert "point" in tuning.overridden


def test_unreviewed_forex_is_classified_but_not_silently_tuned() -> None:
    agent = SymbolIntelligenceAgent()

    assert agent.classify("EURUSD") is InstrumentClass.FOREX
    with pytest.raises(UnknownSymbolTuning, match="classified as forex"):
        agent.resolve_tuning("EURUSD")


def test_crypto_schedule_is_not_closed_by_saturday_calendar() -> None:
    agent = SymbolIntelligenceAgent()
    schedule = agent.market_schedule("BTCUSD")
    saturday = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

    result = classify_market_state(
        now=saturday,
        symbol_info=_info("BTCUSD", point=0.01, digits=2),
        last_tick_at=saturday,
        schedule=schedule,
    )

    assert result.state is MarketState.OPEN


def test_metal_schedule_keeps_weekend_closed() -> None:
    agent = SymbolIntelligenceAgent()
    schedule = agent.market_schedule("XAUUSD")
    saturday = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

    result = classify_market_state(
        now=saturday,
        symbol_info=_info("XAUUSD", point=0.01, digits=2),
        last_tick_at=saturday,
        schedule=schedule,
    )

    assert result.state is MarketState.CLOSED


def test_report_lists_only_tradeable_reviewed_symbol_as_active() -> None:
    agent = SymbolIntelligenceAgent()

    gold = agent.inspect(
        symbol="XAUUSD",
        configured=True,
        market_result=MarketStateResult(MarketState.OPEN, "trading"),
        symbol_info=_info("XAUUSD", point=0.01, digits=2),
    )
    eur = agent.inspect(
        symbol="EURUSD",
        configured=True,
        market_result=MarketStateResult(MarketState.OPEN, "trading"),
        symbol_info=_info("EURUSD", point=0.00001, digits=5),
    )

    report = agent.report([gold, eur])

    assert gold.state is SymbolRunState.ACTIVE
    assert gold.active is True
    assert eur.instrument_class is InstrumentClass.FOREX
    assert eur.state is SymbolRunState.UNSUPPORTED
    assert eur.active is False
    assert report.active_symbols == ("XAUUSD",)
    assert report.inactive_symbols == ("EURUSD",)
    assert report.by_class["metal"] == ("XAUUSD",)
    assert report.by_class["forex"] == ("EURUSD",)
