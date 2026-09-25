"""Read-only trained/shadow model status."""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def model_status_embed(model: Any, run: Any, summary: Any) -> discord.Embed:
    if model is None:
        return notice_embed(
            "🧠 Model status",
            "No trained model is registered yet.",
        )

    lines = [
        f"**Model:** `{model.model_id}`",
        f"**Status:** `{model.status}`",
        f"**Algorithm:** `{model.algorithm}`",
        f"**Training samples:** {model.training_samples}",
        f"**Training period:** {model.trained_from} → {model.trained_through}",
        "",
        "**Training metrics**",
    ]
    for target in ("six", "twenty", "forty"):
        metric = model.target_metrics.get(target)
        if metric is None:
            lines.append(f"`{target}` — not trained")
            continue
        lines.append(
            f"`{target}` n {metric.samples} · AUC {_fmt(metric.roc_auc)} · "
            f"Brier {_fmt(metric.brier)} · precision {_fmt(metric.precision)} · "
            f"recall {_fmt(metric.recall)}"
        )

    if run is not None:
        lines.extend(
            [
                "",
                f"Latest training run: `{run.status}` · {run.sample_count} samples",
            ]
        )

    lines.extend(
        [
            "",
            "**Shadow validation**",
            f"Predictions: {summary.predictions}",
            f"Reconciled: {summary.reconciled}",
            f"$6 Brier: {_fmt(summary.six_brier)}",
            f"$20 Brier: {_fmt(summary.twenty_brier)}",
            f"$40 Brier: {_fmt(summary.forty_brier)}",
            "",
            "Shadow-only research · never creates or modifies a trade",
        ]
    )
    return notice_embed("🧠 Model status", "\n".join(lines))


class ModelStatusCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def model_status(
        self,
        interaction: discord.Interaction,
        symbol: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        if self.context.models is None:
            await interaction.followup.send(
                embed=notice_embed(
                    "Model status unavailable",
                    "No model reader configured.",
                ),
                ephemeral=True,
            )
            return

        symbol = (symbol or self.context.config.symbols[0]).upper()
        model = await self.context.run(self.context.models.latest_model, symbol)
        run = await self.context.run(self.context.models.latest_training_run, symbol)
        summary = await self.context.run(self.context.models.shadow_summary, symbol)
        await interaction.followup.send(
            embed=model_status_embed(model, run, summary),
            ephemeral=True,
        )


def register(tree: Any, context: BotContext) -> None:
    commands = ModelStatusCommands(context)

    @tree.command(name="model-status", description="Latest trained and shadow model status")
    @app_commands.describe(symbol="Show one configured symbol")
    @app_commands.choices(
        symbol=[
            app_commands.Choice(name=name, value=name)
            for name in context.config.symbols
        ]
    )
    async def model_status(
        interaction: discord.Interaction,
        symbol: str | None = None,
    ) -> None:
        await commands.model_status(interaction, symbol)
