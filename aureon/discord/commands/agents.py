"""Read-only directory of Aureon's market and learning agents."""

from __future__ import annotations

from typing import Any

import discord

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext


AGENT_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("📈 Market intelligence", (
        ("EMA Cross", "Detects EMA20/EMA50 crosses and immediate directional context."),
        ("EMA200 Pre-Cross", "Watches pressure and approach behaviour before EMA200 interaction."),
        ("EMA200 Cross", "Detects price/EMA200 crossings for larger trend context."),
        ("EMA + RSI Eligibility", "Checks whether EMA structure and RSI make a setup eligible."),
        ("RSI", "Reads momentum and RSI state around the setup."),
        ("Wick", "Measures rejection wicks and candle rejection behaviour."),
        ("Breakout", "Detects structure breaks and expansion behaviour."),
        ("Liquidity", "Reads liquidity and market-structure conditions around a setup."),
        ("Volume Participation", "Measures whether volume participation supports the movement."),
        ("Market Regime", "Classifies the surrounding market regime."),
        ("Session Trend", "Tracks Asia/London/New York session direction and structure."),
        ("Market Journey", "Follows one movement across its full lifecycle instead of isolated candles."),
    )),
    ("🧭 Context & coordination", (
        ("Higher Timeframe", "Adds M15/H1 context to the M5 decision stream."),
        ("Daily Market Bias", "Maintains the evolving directional/context bias for the broker day."),
        ("Expansion Opportunity", "Assesses whether meaningful movement/expansion may remain."),
        ("Market Director", "Combines specialist observations into a higher-level assessment."),
        ("Symbol Intelligence", "Applies symbol-specific behaviour and tuning for Gold, Silver and future symbols."),
        ("Cross-Venue Replication", "Compares/replicates observations across feeds such as MT5 and cTrader."),
        ("Agent Highway", "Connects agents and tracks agent invocation/health state."),
    )),
    ("🧠 Learning & governance", (
        ("EOD Training Memory", "Builds restart-safe completed-day training examples from frozen setup snapshots."),
        ("Learning Memory", "Persists immutable setup snapshots and resolves their later outcomes without future leakage."),
        ("V3 EMA Learning", "Provides mature chronological validation, model training and operational learning infrastructure."),
        ("V4 Journey Learning", "Learns pre-cross, cross, expansion, pullback, re-entry, continuation and exhaustion behaviour."),
        ("Adaptive Learning", "Fits probability models for movement targets, quality and loss-control outcomes."),
        ("Evolution Agent", "Controls Candidate → Challenger → Shadow → Champion model governance; never trades."),
    )),
)


class AgentsCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def agents(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        embed = discord.Embed(
            title="🧠 Aureon Agent Directory",
            description=(
                "Read-only map of Aureon's specialist intelligence, context, learning and "
                "governance components. These agents observe/learn; they do not grant live execution."
            ),
        )
        total = 0
        for heading, agents in AGENT_GROUPS:
            total += len(agents)
            value = "\n".join(f"**{name}** — {purpose}" for name, purpose in agents)
            embed.add_field(name=heading, value=value, inline=False)
        embed.set_footer(text=f"{total} agent/services listed · XAUUSD + XAGUSD shared intelligence architecture")
        await interaction.followup.send(embed=embed, ephemeral=True)


def register(tree: Any, context: BotContext) -> None:
    commands = AgentsCommands(context)

    @tree.command(name="agents", description="List Aureon agents and what each one does")
    async def agents(interaction: discord.Interaction) -> None:
        await commands.agents(interaction)
