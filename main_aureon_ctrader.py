"""Independent Aureon cTrader runtime.

This launcher is intentionally separate from main_aureon.py. It owns only the cTrader
process lifecycle and never initializes MetaTrader5.

The concrete cTrader Open API transport is the next integration boundary. Until it is
configured, this runner fails closed with an actionable message rather than falling back
to MT5.
"""

from __future__ import annotations

import logging
import sys

from aureon.config import AureonConfig
from aureon.data.base_provider import MarketDataError
from aureon.providers.ctrader import CTraderDataProvider, CTraderTransport


log = logging.getLogger("aureon.ctrader")


def build_ctrader_transport(config: AureonConfig) -> CTraderTransport:
    """Build the live cTrader transport.

    Kept as an explicit boundary so the Open API/OAuth implementation can be added without
    changing the shared Aureon agents or the MT5 launcher.
    """
    raise MarketDataError(
        "cTrader Open API transport is not configured yet. "
        "Implement the authenticated CTraderTransport before starting this runtime."
    )


def build_ctrader_provider(config: AureonConfig) -> CTraderDataProvider:
    return CTraderDataProvider(
        transport=build_ctrader_transport(config),
        market_tz=config.market_tz,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    config = AureonConfig.from_env().model_copy(update={"broker_source": "CTRADER"})
    if not config.ctrader_enabled:
        log.info("cTrader runtime disabled by AUREON_CTRADER_ENABLED=false")
        return 0
    log.info(
        "starting independent cTrader runtime: broker=%s symbols=%s channel=%s",
        config.broker_source,
        ",".join(config.symbols),
        config.broker_alert_channel_id or "disabled",
    )

    # Deliberately connect only to cTrader. There is no MT5 import or fallback here.
    provider = build_ctrader_provider(config)
    provider.connect()
    try:
        # The shared observer wiring will be connected to this provider in the cTrader
        # integration PR. Keeping that step explicit prevents an accidental MT5 fallback.
        raise MarketDataError(
            "cTrader provider connected, but the independent observer wiring is not "
            "enabled yet. Complete the cTrader observer integration before live use."
        )
    finally:
        provider.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except MarketDataError as exc:
        log.error("%s", exc)
        sys.exit(2)
