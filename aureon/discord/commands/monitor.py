"""``/monitor`` — what the measured record says about a detection (§62-§64, 9D).

    /monitor symbol:XAUUSD
    /monitor symbol:XAUUSD detection:5f3a...

Every number this command shows was measured. The trend read was computed by the observer
and published into the symbol's state document; the percentages are frequencies over stored
`detection_evaluations` rows; the target and stop are quantiles of excursions those same
rows recorded. There is no model here and no forecast, and the footer says so on every
render.

## What it refuses to do

* **It does not prefill a trade.** The target and stop are labelled estimates and nothing
  downstream reads them: `/execute` still asks for the lot and still requires CONFIRM, and
  no code path anywhere sets an SL or a TP from this screen (§64).
* **It does not publish a percentage from a handful of detections.** Below thirty COMPLETE
  evaluations it says "insufficient history (n=…)" and stops. A rate from eleven prior
  detections is read exactly like a rate from three hundred.
* **It does not compute an indicator.** Discord holds no data provider (CLAUDE.md), so the
  trend read is read from Firestore like everything else on the screen.
* **It does not resolve a disagreement.** When the published trend read points one way and
  the detection the other, the screen says so in as many words rather than averaging them.

## Why the cohort is read here rather than in the service

``assessment_service`` is arithmetic over data it is handed, which is what makes it testable
without a database. Assembling the population -- reading detections and their evaluations,
and dropping everything at or after the detection being assessed -- is I/O, so it lives in
the command with the rest of the I/O.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import discord
from discord import app_commands

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext
from aureon.discord.embeds import monitor_embed, notice_embed
from aureon.discord.service import build_monitor, symbol_state_of, unobserved_symbol_notice
from aureon.evaluation.rules import get_rule
from aureon.models.base import to_utc, utc_now
from aureon.models.detection import Detection
from aureon.services.assessment_service import build_assessment, population_from

log = logging.getLogger(__name__)

#: How far back "the latest detection" reaches when no id is given. Ninety minutes because
#: a cross from three hours ago is not what somebody typing `/monitor` is asking about, and
#: answering with it would be answering a different question without saying so.
RECENT_MINUTES = 90.0

#: How much history the cohort is drawn from. Ninety days is enough for thirty detections of
#: most shapes and bounded enough that the command stays a read rather than a scan; a cohort
#: that reached back indefinitely would also reach back past agent-version bumps, where the
#: population forks (§12).
COHORT_DAYS = 90

#: How many detections the cohort read will pull. A ceiling, not a target: the widening stops
#: at the first cohort of thirty, so this only bounds the worst case.
COHORT_LIMIT = 2_000


class MonitorCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def monitor(
        self,
        interaction: discord.Interaction,
        symbol: str,
        detection: str | None = None,
    ) -> None:
        # Deferred first: several Firestore reads follow and three seconds is not much.
        await interaction.response.defer(thinking=True, ephemeral=True)
        context = self.context
        symbol = symbol.upper()

        unobserved = unobserved_symbol_notice(symbol, context.config.symbols)
        if unobserved:
            await self._refuse(interaction, "No such symbol here", unobserved)
            return

        try:
            subject = await self._subject(symbol, detection)
        except Exception:  # noqa: BLE001 - a readout must not die silently
            log.exception("/monitor could not read detections for %s", symbol)
            await self._refuse(
                interaction,
                "Assessment unavailable",
                "Could not read detections from Firestore. The observer may still be "
                "running — this command is the thing that failed.",
            )
            return

        if subject is None:
            await self._refuse(
                interaction,
                "No recent detection",
                f"Nothing directional on {symbol} in the last {RECENT_MINUTES:g} minutes. "
                "Name one with `detection:` to assess an older one."
                if detection is None
                else f"No detection `{detection}` on {symbol}.",
            )
            return

        try:
            screen = await self._assess(subject)
        except Exception:  # noqa: BLE001
            log.exception("/monitor failed for %s", subject.detection_id)
            await self._refuse(
                interaction,
                "Assessment unavailable",
                "Could not build the assessment. Nothing was stored.",
            )
            return

        await interaction.followup.send(embed=monitor_embed(screen), ephemeral=True)

    # ── Reads ─────────────────────────────────────────────────────────────────

    async def _subject(self, symbol: str, detection_id: str | None) -> Detection | None:
        """The detection being assessed: the named one, or the latest directional one."""
        context = self.context
        if detection_id:
            found = await context.run(context.detections.get, detection_id)
            # A detection from another symbol is a mistyped id, not a request to switch
            # instruments -- the same cross-check `/close-trade symbol:` makes (9A-6).
            if found is None or found.symbol != symbol:
                return None
            return found

        since = utc_now() - timedelta(minutes=RECENT_MINUTES)
        recent = await context.run(
            context.detections.recent_for_symbol, symbol, since=since, limit=25
        )
        directional = [d for d in recent if d.direction is not None]
        if not directional:
            return None
        return max(directional, key=lambda d: to_utc(d.detected_at.utc))

    async def _assess(self, subject: Detection) -> Any:
        context = self.context
        rule = get_rule(context.config.rule_id_for(subject.symbol))
        moment = to_utc(subject.detected_at.utc)

        history = await context.run(
            context.detections.in_period, moment - timedelta(days=COHORT_DAYS), moment
        )
        history = [d for d in history if d.symbol == subject.symbol][:COHORT_LIMIT]
        evaluations = await context.run(
            context.evaluations.get_many,
            [d.detection_id for d in history] + [subject.detection_id],
            rule.rule_id,
        )
        population = population_from(history, evaluations, before=moment)

        spec = await context.run(context.symbols.get, subject.symbol)
        assessment = build_assessment(
            subject,
            trend=await self._trend(subject.symbol),
            rule=rule,
            population=population,
            subject_evaluation=evaluations.get(subject.detection_id),
            pair=_pair_for(rule),
            point=spec.point if spec is not None else 0.01,
        )
        if context.assessments is not None:
            # Stored BEFORE it is rendered, so the weekly review can score what a human was
            # actually shown rather than what the numbers would say when re-derived later.
            await context.run(context.assessments.store, assessment)
        return build_monitor(assessment, subject)

    async def _trend(self, symbol: str) -> Any:
        """The observer's published trend read, or an empty one if it has not published.

        An empty read renders as `sideways` with no evidence, which is visibly nothing --
        where a trend read computed here would be Discord computing an indicator, and a
        fabricated bias would be indistinguishable from a measured one.
        """
        from aureon.models.assessment import TrendRead
        from aureon.models.enums import TrendBias

        state = await context_state(self.context, symbol)
        if state is not None and state.trend_read is not None:
            return state.trend_read
        return TrendRead(
            bias=TrendBias.SIDEWAYS,
            evidence=("no trend read published — is the observer running?",),
            candles=0,
        )

    async def _refuse(
        self, interaction: discord.Interaction, title: str, message: str
    ) -> None:
        await interaction.followup.send(
            embed=notice_embed(title, message, bad=True), ephemeral=True
        )


async def context_state(context: BotContext, symbol: str) -> Any:
    """That symbol's own state document, or None."""
    state = await context.run(
        context.system_state.read_symbol, symbol, context.config.timeframes[0]
    )
    return symbol_state_of(state, symbol)


def _pair_for(rule: Any) -> tuple[float, float]:
    """The favourable/adverse pair the "+T before −S" line is measured over.

    Taken from the rule's own ladder rather than configured separately: a pair naming
    thresholds the rule does not evaluate would have no stored data behind it, and one
    configured elsewhere would drift from the rule it is measured under. The smallest rung
    is used because it is the one with enough observations to say anything about.
    """
    smallest = rule.thresholds[0]
    return (smallest, smallest)


def register(tree: Any, context: BotContext) -> None:
    commands = MonitorCommands(context)

    @tree.command(
        name="monitor",
        description="What the measured record says about a detection (never advice)",
    )
    @app_commands.describe(
        symbol="Which symbol",
        detection="A detection id; omit for the latest directional one",
    )
    @app_commands.choices(
        symbol=[app_commands.Choice(name=name, value=name) for name in context.config.symbols]
    )
    async def monitor(
        interaction: discord.Interaction, symbol: str, detection: str | None = None
    ) -> None:
        await commands.monitor(interaction, symbol, detection)
