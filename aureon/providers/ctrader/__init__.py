"""cTrader read-only market-data adapter.

This package is intentionally isolated from Aureon's intelligence engine. It translates
cTrader/Open API payloads into the same Candle, QuoteSnapshot and SymbolInfo contracts used
by MT5. No order-placement API belongs here.
"""

from aureon.providers.ctrader.provider import CTraderDataProvider
from aureon.providers.ctrader.transport import CTraderTransport

__all__ = ["CTraderDataProvider", "CTraderTransport"]
