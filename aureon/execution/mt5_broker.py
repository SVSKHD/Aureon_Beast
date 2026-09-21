"""The live MetaTrader 5 broker (§30-§36).

The second and last module permitted to import MetaTrader5 (CLAUDE.md), and like the
data provider it imports it **lazily inside ``connect()``** so the whole suite runs on
Linux against ``FakeBroker``.

## The retcode map is a safety boundary, not a convenience

``BrokerError`` and a rejected ``BrokerOrderResult`` mean opposite things to the
executor: a rejection resolves the request to FAILED, an error leaves it EXECUTING for
reconciliation. So a retcode this module does not recognise is treated as a
**rejection**, because a rejection is the answer the broker actually gave -- it returned
a code, so it did decide. Only the absence of an answer (a raised exception, a ``None``
result) is an unknown outcome. Getting that backwards either strands good requests or,
far worse, invites a re-send.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from aureon.data.mt5_provider import server_epoch_to_utc, utc_to_server_epoch
from aureon.execution.broker_interface import BrokerError, BrokerInterface
from aureon.models.broker import AccountInfo, BrokerDeal, BrokerOrder, BrokerPosition
from aureon.models.enums import (
    AccountMode,
    DealEntry,
    Direction,
    FailureCode,
    FillingMode,
    OrderType,
)
from aureon.models.market import QuoteSnapshot, SymbolInfo
from aureon.models.trade import BrokerOrderRequest, BrokerOrderResult

log = logging.getLogger(__name__)

TRADE_RETCODE_DONE = 10_009
TRADE_RETCODE_DONE_PARTIAL = 10_010
TRADE_RETCODE_PLACED = 10_008

#: MT5 retcode → FailureCode. Everything absent is a rejection with BROKER_REJECTED.
RETCODE_FAILURES: dict[int, FailureCode] = {
    10_004: FailureCode.REQUOTE,
    10_006: FailureCode.BROKER_REJECTED,  # request rejected
    10_007: FailureCode.BROKER_REJECTED,  # cancelled by trader
    10_013: FailureCode.INVALID_STOPS,  # invalid request
    10_014: FailureCode.VOLUME_INVALID,  # invalid volume
    10_015: FailureCode.INVALID_STOPS,  # invalid price
    10_016: FailureCode.STOPS_TOO_CLOSE,  # invalid stops
    10_018: FailureCode.MARKET_CLOSED,  # market closed
    10_019: FailureCode.INSUFFICIENT_MARGIN,  # no money
    10_021: FailureCode.STALE_QUOTE,  # no quotes to process
    10_026: FailureCode.TRADING_DISABLED,  # autotrading disabled by server
    10_027: FailureCode.TRADING_DISABLED,  # autotrading disabled by client terminal
    10_030: FailureCode.FILLING_MODE_UNSUPPORTED,  # unsupported filling mode
    10_031: FailureCode.CONNECTION_LOST,  # no connection
    10_034: FailureCode.MAX_OPEN_POSITIONS,  # limit reached
}

_ORDER_TYPE_ATTR = {
    OrderType.MARKET_BUY: "ORDER_TYPE_BUY",
    OrderType.MARKET_SELL: "ORDER_TYPE_SELL",
    OrderType.BUY_STOP: "ORDER_TYPE_BUY_STOP",
    OrderType.SELL_STOP: "ORDER_TYPE_SELL_STOP",
    OrderType.BUY_LIMIT: "ORDER_TYPE_BUY_LIMIT",
    OrderType.SELL_LIMIT: "ORDER_TYPE_SELL_LIMIT",
}
_FILLING_ATTR = {
    FillingMode.FOK: "ORDER_FILLING_FOK",
    FillingMode.IOC: "ORDER_FILLING_IOC",
    FillingMode.RETURN: "ORDER_FILLING_RETURN",
}
_ENTRY_BY_CODE = {0: DealEntry.IN, 1: DealEntry.OUT, 2: DealEntry.INOUT}
_REASON_BY_CODE = {
    0: "client",
    1: "mobile",
    2: "web",
    3: "expert",
    4: "sl",
    5: "tp",
    6: "so",
    7: "rollover",
}


class MT5Broker(BrokerInterface):
    """Places and reads orders through a live MT5 terminal."""

    def __init__(
        self,
        *,
        market_tz: str,
        login: int | None = None,
        password: str | None = None,
        server: str | None = None,
        terminal_path: str | None = None,
    ) -> None:
        self.market_tz = market_tz
        self._login = login
        self._password = password
        self._server = server
        self._terminal_path = terminal_path
        self._mt5: Any | None = None

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        try:
            import MetaTrader5 as mt5  # noqa: N813  (vendor's own casing)
        except ImportError as exc:
            raise BrokerError(
                "MetaTrader5 is unavailable. It is Windows-only; install the 'mt5' extra "
                "there, or use FakeBroker in tests."
            ) from exc

        kwargs: dict[str, Any] = {}
        if self._terminal_path:
            kwargs["path"] = self._terminal_path
        if self._login:
            kwargs.update(login=self._login, password=self._password, server=self._server)
        if not mt5.initialize(**kwargs):
            raise BrokerError(f"MT5 initialize failed: {mt5.last_error()}")
        self._mt5 = mt5

    def close(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()
            self._mt5 = None

    @property
    def mt5(self) -> Any:
        if self._mt5 is None:
            raise BrokerError("broker is not connected; call connect() first")
        return self._mt5

    # ── Account and symbol ────────────────────────────────────────────────────

    def account_info(self) -> AccountInfo:
        info = self.mt5.account_info()
        if info is None:
            raise BrokerError(f"account_info failed: {self.mt5.last_error()}")
        return AccountInfo(
            login=int(info.login),
            currency=str(info.currency),
            balance=float(info.balance),
            equity=float(info.equity),
            margin=float(info.margin),
            margin_free=float(info.margin_free),
            margin_level=float(info.margin_level) or None,
            leverage=int(info.leverage),
            server=str(info.server),
            # 11A F-3. Read here, once, at the only place the terminal will tell us. A
            # missing or unrecognised trade_mode maps to UNKNOWN, which every guard treats
            # as real money.
            mode=AccountMode.from_trade_mode(getattr(info, "trade_mode", None)),
        )

    def symbol_info(self, symbol: str) -> SymbolInfo:
        from aureon.data.mt5_provider import _TRADE_MODE_NAMES, decode_filling_modes

        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise BrokerError(f"symbol_info({symbol}) failed: {self.mt5.last_error()}")
        return SymbolInfo(
            symbol=symbol,
            point=float(info.point),
            digits=int(info.digits),
            volume_min=float(info.volume_min),
            volume_max=float(info.volume_max),
            volume_step=float(info.volume_step),
            stops_level=int(getattr(info, "trade_stops_level", 0)),
            filling_modes=decode_filling_modes(int(getattr(info, "filling_mode", 0))),
            trade_mode=_TRADE_MODE_NAMES.get(int(getattr(info, "trade_mode", -1)), "unknown"),
            spread=int(getattr(info, "spread", 0)) or None,
        )

    def quote(self, symbol: str) -> QuoteSnapshot:
        tick = self.mt5.symbol_info_tick(symbol)
        if tick is None:
            raise BrokerError(f"symbol_info_tick({symbol}) failed: {self.mt5.last_error()}")
        return QuoteSnapshot(
            symbol=symbol,
            bid=float(tick.bid),
            ask=float(tick.ask),
            captured_at=server_epoch_to_utc(float(tick.time), self.market_tz),
            point=self.symbol_info(symbol).point,
        )

    # ── Sending ───────────────────────────────────────────────────────────────

    def send_market_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        return self._send(request, pending=False)

    def send_pending_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        return self._send(request, pending=True)

    def _send(self, request: BrokerOrderRequest, *, pending: bool) -> BrokerOrderResult:
        mt5 = self.mt5
        payload: dict[str, Any] = {
            "action": mt5.TRADE_ACTION_PENDING if pending else mt5.TRADE_ACTION_DEAL,
            "symbol": request.symbol,
            "volume": request.volume,
            "type": int(getattr(mt5, _ORDER_TYPE_ATTR[request.order_type])),
            "magic": request.magic,
            "comment": request.comment,
            "deviation": request.deviation_points,
        }
        if request.price is not None:
            payload["price"] = request.price
        elif not pending:
            tick = mt5.symbol_info_tick(request.symbol)
            if tick is None:
                raise BrokerError(f"no tick for {request.symbol}: {mt5.last_error()}")
            payload["price"] = (
                float(tick.ask) if request.direction is Direction.BUY else float(tick.bid)
            )
        if request.sl is not None:
            payload["sl"] = request.sl
        if request.tp is not None:
            payload["tp"] = request.tp
        if request.filling_mode is not None:
            payload["type_filling"] = int(getattr(mt5, _FILLING_ATTR[request.filling_mode]))

        try:
            result = mt5.order_send(payload)
        except Exception as exc:  # noqa: BLE001
            # An exception says nothing about whether the order reached the server, so
            # it must surface as an unknown outcome, never as a rejection.
            raise BrokerError(f"order_send raised: {exc}") from exc

        if result is None:
            raise BrokerError(f"order_send returned None: {mt5.last_error()}")
        return self._to_result(result)

    def _to_result(self, result: Any) -> BrokerOrderResult:
        retcode = int(getattr(result, "retcode", -1))
        raw = dict(getattr(result, "_asdict", lambda: {"repr": repr(result)})())
        ok = retcode in {TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL, TRADE_RETCODE_PLACED}

        deal_id = int(getattr(result, "deal", 0) or 0)
        return BrokerOrderResult(
            ok=ok,
            retcode=retcode,
            retcode_name=str(getattr(result, "comment", "") or "") or None,
            order_ticket=int(getattr(result, "order", 0) or 0) or None,
            deal_ids=(deal_id,) if deal_id else (),
            position_id=int(getattr(result, "position", 0) or 0) or None,
            fill_price=float(getattr(result, "price", 0.0) or 0.0) or None,
            filled_volume=float(getattr(result, "volume", 0.0) or 0.0) or None,
            failure_code=(
                None
                if ok
                # An unmapped retcode is still an ANSWER, so it is a rejection.
                else RETCODE_FAILURES.get(retcode, FailureCode.BROKER_REJECTED)
            ),
            message=None if ok else f"retcode {retcode}: {getattr(result, 'comment', '')}",
            raw=raw,
        )

    # ── Cancel and close ──────────────────────────────────────────────────────

    def cancel_order(self, ticket: int) -> BrokerOrderResult:
        mt5 = self.mt5
        try:
            result = mt5.order_send(
                {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket}
            )
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"cancel raised: {exc}") from exc
        if result is None:
            raise BrokerError(f"cancel returned None: {mt5.last_error()}")
        return self._to_result(result)

    def close_position(
        self, position_id: int, volume: float | None = None
    ) -> BrokerOrderResult:
        mt5 = self.mt5
        positions = mt5.positions_get(ticket=position_id)
        if not positions:
            return BrokerOrderResult(
                ok=False,
                retcode=10_013,
                failure_code=FailureCode.BROKER_REJECTED,
                message=f"position {position_id} is not open",
            )
        position = positions[0]
        closing_type = (
            mt5.ORDER_TYPE_SELL if int(position.type) == 0 else mt5.ORDER_TYPE_BUY
        )
        tick = mt5.symbol_info_tick(position.symbol)
        if tick is None:
            raise BrokerError(f"no tick for {position.symbol}: {mt5.last_error()}")
        payload = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": volume or float(position.volume),
            "type": closing_type,
            "position": position_id,
            "price": float(tick.bid) if int(position.type) == 0 else float(tick.ask),
            "magic": int(position.magic),
            "comment": "aureon-close",
        }
        try:
            result = mt5.order_send(payload)
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"close raised: {exc}") from exc
        if result is None:
            raise BrokerError(f"close returned None: {mt5.last_error()}")
        return self._to_result(result)

    # ── Reading ───────────────────────────────────────────────────────────────

    def open_positions(self) -> list[BrokerPosition]:
        positions = self.mt5.positions_get() or ()
        return [
            BrokerPosition(
                position_id=int(p.ticket),
                symbol=str(p.symbol),
                direction=Direction.BUY if int(p.type) == 0 else Direction.SELL,
                volume=float(p.volume),
                open_price=float(p.price_open),
                open_time=server_epoch_to_utc(float(p.time), self.market_tz),
                sl=float(p.sl) or None,
                tp=float(p.tp) or None,
                profit=float(p.profit),
                swap=float(p.swap),
                magic=int(p.magic),
                comment=str(p.comment or "") or None,
            )
            for p in positions
        ]

    def pending_orders(self) -> list[BrokerOrder]:
        orders = self.mt5.orders_get() or ()
        by_code = {int(getattr(self.mt5, attr)): kind for kind, attr in _ORDER_TYPE_ATTR.items()}
        found: list[BrokerOrder] = []
        for o in orders:
            kind = by_code.get(int(o.type))
            if kind is None or kind.is_market:
                continue
            found.append(
                BrokerOrder(
                    order_ticket=int(o.ticket),
                    symbol=str(o.symbol),
                    order_type=kind,
                    volume=float(o.volume_current),
                    price=float(o.price_open),
                    sl=float(o.sl) or None,
                    tp=float(o.tp) or None,
                    placed_at=server_epoch_to_utc(float(o.time_setup), self.market_tz),
                    magic=int(o.magic),
                    comment=str(o.comment or "") or None,
                )
            )
        return found

    def orders_history(self, from_utc: datetime, to_utc: datetime) -> list[BrokerOrder]:
        start = datetime.fromtimestamp(utc_to_server_epoch(from_utc, self.market_tz), UTC)
        end = datetime.fromtimestamp(utc_to_server_epoch(to_utc, self.market_tz), UTC)
        orders = self.mt5.history_orders_get(start, end) or ()
        by_code = {int(getattr(self.mt5, attr)): kind for kind, attr in _ORDER_TYPE_ATTR.items()}
        found: list[BrokerOrder] = []
        for o in orders:
            kind = by_code.get(int(o.type))
            if kind is None:
                continue
            found.append(
                BrokerOrder(
                    order_ticket=int(o.ticket),
                    symbol=str(o.symbol),
                    order_type=kind,
                    volume=float(o.volume_initial),
                    price=float(o.price_open),
                    placed_at=server_epoch_to_utc(float(o.time_setup), self.market_tz),
                    magic=int(o.magic),
                    comment=str(o.comment or "") or None,
                )
            )
        return found

    def deals_history(self, from_utc: datetime, to_utc: datetime) -> list[BrokerDeal]:
        start = datetime.fromtimestamp(utc_to_server_epoch(from_utc, self.market_tz), UTC)
        end = datetime.fromtimestamp(utc_to_server_epoch(to_utc, self.market_tz), UTC)
        deals = self.mt5.history_deals_get(start, end) or ()
        found: list[BrokerDeal] = []
        for d in deals:
            entry = _ENTRY_BY_CODE.get(int(getattr(d, "entry", 0)))
            if entry is None or float(d.volume) <= 0:
                continue
            found.append(
                BrokerDeal(
                    deal_id=int(d.ticket),
                    order_ticket=int(d.order) or None,
                    position_id=int(getattr(d, "position_id", 0)) or None,
                    symbol=str(d.symbol),
                    direction=Direction.BUY if int(d.type) == 0 else Direction.SELL,
                    entry=entry,
                    volume=float(d.volume),
                    price=float(d.price),
                    executed_at=server_epoch_to_utc(float(d.time), self.market_tz),
                    profit=float(d.profit),
                    commission=float(d.commission),
                    swap=float(d.swap),
                    magic=int(d.magic),
                    comment=str(d.comment or "") or None,
                    reason=_REASON_BY_CODE.get(int(getattr(d, "reason", -1))),
                )
            )
        return found
