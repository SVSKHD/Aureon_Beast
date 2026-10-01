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
    lines = [f"⚠️ {w}" for w in screen.warnings]
    # Info lines come after the warnings and carry no icon: 9D's "last assessment" is
    # context, and an order screen that decorated it would be nudging.
    lines += list(getattr(screen, "info", []))
    description = "\n".join(lines) or None
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

    # The scope is in the title, not a footnote: a reader who asked for one symbol must
    # not mistake its panel for the whole deployment, and vice versa (9A).
    scope = f" · {screen.symbol}" if screen.symbol else ""
    embed = _embed(
        f"{icon} Aureon{scope} — {screen.overall.value.upper()}", colour=colour
    )
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
        market = "\n".join(f"`{name}` {state}" for name, state in screen.symbols)
        # 11B: the next open goes above the per-symbol rows, not in a footer. "Closed" on
        # its own reads as a fault to anybody who has not checked the calendar, and the
        # first thing they would do about it is restart something.
        closed = screen.closed_line
        embed.add_field(
            name="Market",
            value=f"💤 {closed}\n{market}" if closed else market,
            inline=False,
        )
    if screen.symbol_registry:
        embed.add_field(
            name="Symbols / Agent 18",
            value="\n".join(screen.symbol_registry),
            inline=False,
        )
    if screen.agent_highway:
        embed.add_field(
            name="Agent Highway",
            value="\n".join(screen.agent_highway),
            inline=False,
        )
    if screen.intelligence:
        embed.add_field(
            name="V1 intelligence",
            value="\n".join(screen.intelligence)[:1024],
            inline=False,
        )
    embed.add_field(
        name="Trading",
        value="🟢 enabled" if screen.trading_enabled else "🔴 disabled",
        inline=True,
    )
    embed.add_field(name="Open trades", value=str(screen.open_trades), inline=True)
    embed.add_field(name="Pending", value=str(screen.pending_requests), inline=True)
    for panel in screen.live_panels:
        embed.add_field(
            name=f"{panel.symbol} ({panel.market_state})",
            value="```\n" + "\n".join(panel.lines) + "\n```",
            inline=False,
        )
    if screen.review_summary is not None:
        embed.add_field(name="Latest review", value=screen.review_summary, inline=False)
    # §59's last line. In the footer rather than a field: it is the provenance of
    # everything above it, and a reader who doubts a number looks here.
    embed.set_footer(text=screen.updated_line)
    return embed


def notice_embed(title: str, message: str, *, bad: bool = False) -> Any:
    return _embed(title, colour=COLOUR_BAD if bad else COLOUR_INFO, description=message)


def _direction_style(direction: str | None) -> tuple[str, int]:
    """Colour describes direction only, never quality or execution clearance."""
    direction = str(direction or "").lower()
    if direction in {"buy", "bullish", "up"}:
        return "🟢", COLOUR_OK
    if direction in {"sell", "bearish", "down"}:
        return "🔴", COLOUR_BAD
    return "🟡", COLOUR_WARN


def _readable(value: str) -> str:
    return str(value or "—").replace("_", " ")


def notification_embed(screen: Any) -> Any:
    """Mobile-first alert: headline, key facts, then concise context (9C).

    Full evidence and identifiers remain on the screen/stored detection; notification
    buttons still use the original identifiers. Discord supports an accent, not a custom
    message background. The operator explicitly requested directional colours.
    """
    facts = dict(screen.fields)
    session_summary = "SESSION CLOSED" in screen.title
    pre_cross = "PRE-CROSS PRESSURE" in screen.title
    direction = facts.get("Trend") if session_summary else screen.side
    icon, colour = _direction_style(direction)
    if pre_cross:
        icon, colour = "🟡", COLOUR_WARN
    embed = _embed(f"{icon} {screen.title}", colour=colour)

    if pre_cross:
        embed.description = (
            f"**{facts.get('Price', '—')}** · "
            f"{_readable(facts.get('Session', '—')).title()}\n"
            f"Bias **{facts.get('Bias', '—')}** · "
            f"Trend **{facts.get('Trend', '—')}**\n"
            f"**{facts.get('Status', 'EMA200 cross NOT confirmed')}**"
        )
        embed.add_field(
            name="Pressure context",
            value=(
                f"**Pattern:** {_readable(facts.get('Pattern', '—'))}\n"
                f"**Distance:** {facts.get('Distance to EMA200', '—')}\n"
                f"**EMA20 / EMA50:** {_readable(facts.get('EMA20 / EMA50', '—'))}\n"
                f"**Momentum:** {facts.get('Momentum', '—')}"
            ),
            inline=False,
        )
        if facts.get("Agent consensus"):
            embed.add_field(
                name="Agent agreement",
                value=facts["Agent consensus"],
                inline=False,
            )
        if facts.get("Model confidence"):
            embed.add_field(
                name="Model confidence",
                value=facts["Model confidence"],
                inline=False,
            )
    elif "Cross" in facts:
        embed.description = (
            f"**{facts.get('Price', '—')}** · {_readable(facts.get('Session', '—')).title()}\n"
            f"Trend **{_readable(facts.get('Trend', '—'))}** · "
            f"Quality **{_readable(facts.get('Quality', '—'))}**"
        )
        context = []
        for name in ("EMA20 / EMA50", "EMA200", "EMA200 context", "EMA20 / EMA50 context"):
            if name in facts:
                context.append(f"**{name}:** {_readable(facts[name])}")
        if context:
            embed.add_field(name="EMA context", value="\n".join(context), inline=False)
        consensus = facts.get("Agent consensus", "")
        if consensus:
            lines = consensus.splitlines()
            # The first line is the decorative meter plus its categorical label.
            label = lines[0].lstrip("█░ ").strip() or "Unavailable"
            counts = lines[1] if len(lines) > 1 else "Counts unavailable"
            counts = counts.replace("✅ ", "").replace("➖ ", "").replace("❌ ", "")
            embed.add_field(
                name=f"Agent agreement · {label}", value=counts, inline=False
            )
        else:
            embed.add_field(name="Agent agreement", value="Unavailable", inline=False)
        if facts.get("Model confidence"):
            embed.add_field(
                name="Model confidence",
                value=facts["Model confidence"],
                inline=False,
            )
        if facts.get("Move since pre-cross"):
            embed.add_field(
                name="Move since pre-cross",
                value=facts["Move since pre-cross"],
                inline=False,
            )
        # Pattern adds a distinct fact; the Analysis paragraph repeats the same readings.
        if facts.get("Pattern"):
            embed.add_field(
                name="Pattern", value=_readable(facts["Pattern"]).capitalize(), inline=False
            )
    elif session_summary:
        embed.description = (
            f"Trend **{_readable(facts.get('Trend', '—'))}** · "
            f"Next **{facts.get('Now entering', '—')}**\n"
            f"**Change / Range:** {facts.get('Change / Range', '—')}\n"
            f"**O / H / L / C:** {facts.get('O / H / L / C', '—')}"
        )
        for name in ("Latest EMA20 / EMA50", "Latest Price / EMA200"):
            embed.add_field(
                name=name.replace("Latest", "Latest cross ·"),
                value=_readable(facts.get(name, "—")), inline=False,
            )
    else:
        # Other configured agents keep their facts, without mobile column stacking.
        embed.description = "\n".join(
            f"**{name}:** {_readable(value)}" for name, value in screen.fields
        )[:4096]

    footer = "Research only · not a recommendation · colour = direction"
    if "Cross" in facts or pre_cross:
        footer += " · agreement ≠ win probability"
    if pre_cross:
        footer += " · early pressure ≠ confirmed cross"
    if any("volume" in name.lower() for name in facts) and not session_summary:
        footer += " · MT5 tick volume, not exchange volume"
    footer += f" · ref {screen.detection_id[:12]}"
    embed.set_footer(text=footer)
    return embed


def reminder_embed(screen: Any) -> Any:
    """A fired price alert, rendered from its frozen snapshot (9C)."""
    embed = _embed(
        screen.title,
        colour=COLOUR_INFO,
        description=(f"> {screen.note}" if screen.note else None),
    )
    for name, value in screen.fields:
        embed.add_field(name=name, value=value or "—", inline=True)
    embed.set_footer(text=screen.footer)
    return embed


def setup_embed(screen: Any) -> Any:
    """Render a dense setup as a small number of clearly separated scan blocks.

    Discord embeds allow at most 25 fields. Grouping related facts into full-width blocks
    both keeps us well below that limit and gives the eye real whitespace between sections.
    The final clearance is always the last field.
    """
    description = [screen.description] if screen.description else []
    embed = _embed(screen.title, colour=COLOUR_INFO, description="\n".join(description) or None)

    by_name = {name: value or "—" for name, value in screen.fields}
    confirmation = by_name.pop("Confirmation", "—")

    def section(title: str, names: tuple[str, ...]) -> None:
        lines: list[str] = []
        for name in names:
            if name not in by_name:
                continue
            value = str(by_name[name])
            if "\n" in value:
                lines.append(f"**{name}**\n{value}")
            else:
                lines.append(f"**{name}:** {value}")
        if lines:
            embed.add_field(name=title, value="\n".join(lines), inline=False)

    section(
        "1 · SETUP",
        ("State", "Timeframe", "Anchor", "Invalidation", "Context", "Move ladder", "Events"),
    )
    section(
        "2 · TREND",
        ("Present trend", "Asia trend", "London trend", "Trend evidence"),
    )
    section(
        "3 · MOMENTUM",
        (
            "EMA20 / EMA50",
            "EMA cross status",
            "Early EMA status",
            "RSI status",
            "Setup trend @ event",
        ),
    )
    section(
        "4 · AGENT CONFIDENCE",
        ("Agent confidence", "6-agent read"),
    )
    section(
        "5 · CONFIRMATION EVIDENCE",
        ("Badges", "Early EMA", "MTF confirmation", "Blockers"),
    )
    section("6 · TRACE", ("Linked detections",))

    known = {
        "State", "Timeframe", "Anchor", "Invalidation", "Context", "Move ladder", "Events",
        "Present trend", "Asia trend", "London trend", "Trend evidence",
        "EMA20 / EMA50", "EMA cross status", "Early EMA status", "RSI status",
        "Setup trend @ event", "Agent confidence", "6-agent read",
        "Badges", "Early EMA", "MTF confirmation", "Blockers", "Linked detections",
    }
    leftovers = [(name, value) for name, value in by_name.items() if name not in known]
    if leftovers:
        embed.add_field(
            name="MORE CONTEXT",
            value="\n".join(f"**{name}:** {value}" for name, value in leftovers),
            inline=False,
        )

    if screen.reference:
        embed.add_field(
            name="7 · HISTORICAL REFERENCE",
            value="\n".join(screen.reference),
            inline=False,
        )

    # One-glance decision surface: last, full-width, and visually isolated from all evidence.
    embed.add_field(
        name="FINAL CHECK",
        value=(
            f"**{confirmation}**\n"
            "Manual review required. Execution remains hidden unless the current setup is cleared."
        ),
        inline=False,
    )

    # Keep the PNG as a normal message attachment rather than squeezing it inside the
    # embed's fixed-width image slot. Discord then gives the chart its own preview area.
    embed.set_footer(text=screen.footer)
    return embed

def monitor_embed(screen: Any) -> Any:
    """A `/monitor` readout (9D).

    Neutral in colour like 9C's, and for the same reason: a green embed over a bullish bias
    would read as approval, and the eye reaches a colour before it reaches a confidence
    interval. The one-line summary is the description rather than a field, because it is the
    part that gets quoted and it should be the part that carries the n.
    """
    description = [screen.next_move]
    if screen.disagreement:
        description.append(f"⚠ {screen.disagreement}")
    if screen.insufficient:
        description.append(f"**{screen.insufficient}**")
    if screen.history:
        # In the DESCRIPTION, above the numbers, not in the footer (11C, F-9). "Every rate
        # here describes the generator" is not a caveat a reader should meet after they have
        # already read the rates.
        description.append(f"⚠ **{screen.history}**")

    embed = _embed(
        f"{screen.title} — assessment",
        colour=COLOUR_INFO,
        description="\n\n".join(description),
    )
    embed.add_field(name="Bias", value=screen.bias, inline=True)
    embed.add_field(
        name="Cohort",
        value=screen.cohort + (f"\n{screen.dropped}" if screen.dropped else ""),
        inline=False,
    )
    # The evidence sits under the bias rather than in place of it: the summary is a vote
    # over these facts, and a reader who disagrees with the vote can see what it counted.
    embed.add_field(name="Evidence", value="\n".join(screen.evidence) or "—", inline=False)
    for name, value in screen.fields:
        embed.add_field(name=name, value=value or "—", inline=False)
    embed.set_footer(text=screen.footer)
    return embed
