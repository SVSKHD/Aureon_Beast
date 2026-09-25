"""Discord reports the executor-owned TradeRequest result after CONFIRM."""

from aureon.discord.views.confirm_view import _execution_result_embed
from aureon.models.enums import FailureCode, OrderType, TradeRequestStatus
from aureon.models.trade import TradeRequest


def _request(**overrides):
    base = dict(
        request_id="req-1",
        symbol="XAUUSD",
        order_type=OrderType.MARKET_BUY,
        volume=0.10,
        requested_by="123",
    )
    return TradeRequest(**(base | overrides))


def test_filled_result_is_reported_as_success() -> None:
    embed = _execution_result_embed(
        _request(
            status=TradeRequestStatus.FILLED,
            filled_volume=0.10,
            fill_price=2650.25,
            order_ticket=111,
            position_id=222,
        )
    )

    assert "succeeded" in (embed.title or "").lower()
    assert "FILLED" in (embed.description or "")
    assert "2650.25" in (embed.description or "")
    assert "222" in (embed.description or "")


def test_executor_failure_reason_is_reported_to_discord() -> None:
    embed = _execution_result_embed(
        _request(
            status=TradeRequestStatus.FAILED,
            failure_code=FailureCode.BROKER_REJECTED,
            failure_message="market closed by broker",
        )
    )

    assert "failed" in (embed.title or "").lower()
    assert "BROKER_REJECTED".lower() in (embed.description or "").lower()
    assert "market closed by broker" in (embed.description or "")


def test_pending_broker_order_is_reported_as_accepted() -> None:
    embed = _execution_result_embed(
        _request(
            status=TradeRequestStatus.PENDING,
            order_ticket=98765,
        )
    )

    assert "accepted" in (embed.title or "").lower()
    assert "98765" in (embed.description or "")
