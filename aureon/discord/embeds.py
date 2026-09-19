"""Rendering service data as Discord embeds.

Pure presentation. Every decision about *what* to show was already made in
``service.py``; this file only turns that data into embeds, so a change to the rules never
requires touching rendering and a change to rendering can never alter a rule.
"""

from __future__ import annotations

from typing import Any

from aureon.discord.service import ConfirmationScreen, StatusScreen
from aureon.models.enums import Freshness, TradeRequestStatus
from aureon.models.trade import TradeRequest

COLOUR_OK = 0x2E7D32
COLOUR_WARN = 0xF9A825
COLOUR_BAD = 0xC62828
COLOUR_INFO = 0x1565C0

FRESHNESS_ICON = {
    Freshness.LIVE: "🟢",
    Freshness.STALE: "🟡",
    Freshness.OFFLINE: "🔴",
}

STATUS_ICON = {
    TradeRequestStatus.FILLED: "✅",
    TradeRequestStatus.PARTIALLY_FILLED: "◑",
    TradeRequestStatus.PENDING: "⏳",
    TradeRequestStatus.FAILED: "❌",
    TradeRequestStatus.FAILED_STALE: "⌛",
    TradeRequestStatus.FAILED_RECONCILIATION: "⚠️",
    TradeRequestStatus.CANCELLED: "🚫",
    TradeRequestStatus.EXPIRED: "⌛",
}


def _embed(title: str, *, colour: int, description: str | None = None) -> Any:
    import discord

    return discord.Embed(title=title, description=description, colour=colour)


def confirmation_embed(screen: ConfirmationScreen) -> Any:
    """The §40 confirmation screen.

    Warnings colour the embed: a human about to confirm something that will be refused
    should see that before reading a single field.
    """
    colour = COLOUR_WARN if screen.warnings else COLOUR_INFO
    description = "\n".join(f"⚠️ {w}" for w in screen.warnings) or None
    embed = _embed(screen.title, colour=colour, description=description)
    for name, value in screen.fields:
        embed.add_field(name=name, value=value or "—", inline=True)
    embed.set_footer(
        text=f"Confirm within {screen.expires_in_seconds:g}s · quote age "
        f"{screen.quote_age_seconds:.1f}s"
    )
    return embed


def result_embed(request: TradeRequest) -> Any:
    """The outcome of a request, reported from Firestore (§8).

    Never silent: every terminal status renders, including the failures, with the reason
    the executor recorded. A human who confirmed a trade is owed an answer either way.
    """
    icon = STATUS_ICON.get(request.status, "•")
    failed = request.status in {
        TradeRequestStatus.FAILED,
        TradeRequestStatus.FAILED_STALE,
        TradeRequestStatus.FAILED_RECONCILIATION,
    }
    colour = COLOUR_BAD if failed else COLOUR_OK
    if request.status in {TradeRequestStatus.CANCELLED, TradeRequestStatus.EXPIRED}:
        colour = COLOUR_WARN

    embed = _embed(
        f"{icon} {request.symbol} {request.order_type.value} — {request.status.value}",
        colour=colour,
    )
    embed.add_field(name="Volume", value=f"{request.volume:g}", inline=True)
    if request.fill_price is not None:
        embed.add_field(name="Fill price", value=f"{request.fill_price:g}", inline=True)
    if request.filled_volume is not None:
        embed.add_field(name="Filled", value=f"{request.filled_volume:g}", inline=True)
    if request.order_ticket:
        embed.add_field(name="Order", value=str(request.order_ticket), inline=True)
    if request.position_id:
        embed.add_field(name="Position", value=str(request.position_id), inline=True)
    if request.failure_code is not None:
        embed.add_field(
            name="Reason", value=f"`{request.failure_code.value}`", inline=False
        )
    if request.failure_message:
        embed.add_field(name="Detail", value=request.failure_message[:1000], inline=False)
    embed.set_footer(text=f"request {request.request_id}")
    return embed


def status_embed(screen: StatusScreen) -> Any:
    """The ``/status`` screen (§59, §61-§63)."""
    icon = FRESHNESS_ICON.get(screen.overall, "•")
    colour = {
        Freshness.LIVE: COLOUR_OK,
        Freshness.STALE: COLOUR_WARN,
        Freshness.OFFLINE: COLOUR_BAD,
    }.get(screen.overall, COLOUR_INFO)

    embed = _embed(f"{icon} Aureon — {screen.overall.value.upper()}", colour=colour)
    embed.add_field(
        name="Services",
        value="\n".join(
            f"{FRESHNESS_ICON.get(s.freshness, '•')} `{s.name}` {s.freshness.value}"
            + (f" — {s.detail}" if s.detail else "")
            for s in screen.services
        )
        or "—",
        inline=False,
    )
    if screen.symbols:
        embed.add_field(
            name="Market",
            value="\n".join(f"`{name}` {state}" for name, state in screen.symbols),
            inline=False,
        )
    embed.add_field(
        name="Trading",
        value="🟢 enabled" if screen.trading_enabled else "🔴 disabled",
        inline=True,
    )
    embed.add_field(name="Open trades", value=str(screen.open_trades), inline=True)
    embed.add_field(name="Pending", value=str(screen.pending_requests), inline=True)
    if screen.review_summary is not None:
        embed.add_field(name="Latest review", value=screen.review_summary, inline=False)
    return embed


def notice_embed(title: str, message: str, *, bad: bool = False) -> Any:
    return _embed(title, colour=COLOUR_BAD if bad else COLOUR_INFO, description=message)
