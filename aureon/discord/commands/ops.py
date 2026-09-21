"""``/ops`` — which operational conditions are true right now (11A, F-15).

    /ops

Reads ``{prefix}_ops_events`` and nothing else. The conditions are DETECTED in the observer,
the executor and the monitor — three other processes — which is exactly why they are stored
rather than held in a variable: a register in memory could not be read by the command that
exists to show it.

Three states, and the distinction matters:

* **active** — the condition is true now, and the line says for how long. An onset three hours
  old reads as three hours old, because ``since`` records when the state began rather than when
  it was last seen.
* **clear** — it has happened and recovered, with a count. A condition that flapped ten times
  this morning and a condition that fired once and cleared both read as "fine" without it.
* **no row at all** — it has never happened since this deployment started.

A list showing only active conditions could not tell the second from the third, which is why
cleared rows are kept.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed
from aureon.services.ops_events import SPECS, render_ops

log = logging.getLogger(__name__)


class OpsCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def ops(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        context = self.context

        if context.ops_events is None:
            await interaction.followup.send(
                embed=notice_embed(
                    "Ops unavailable", "No ops-event storage is configured.", bad=True
                ),
                ephemeral=True,
            )
            return

        try:
            events = await context.run(context.ops_events.all_events)
        except Exception:  # noqa: BLE001 - a readout must not die silently
            log.exception("/ops could not read the register")
            await interaction.followup.send(
                embed=notice_embed(
                    "Ops unavailable",
                    "Could not read the ops register from Firestore. The services may still "
                    "be running — this command is the thing that failed.",
                    bad=True,
                ),
                ephemeral=True,
            )
            return

        active = [e for e in events if e.active]
        title = (
            f"{len(active)} condition(s) active" if active else "Nothing active"
        )
        body = "\n".join(render_ops(events))
        # The count of conditions that have NEVER fired is worth stating: a register with two
        # rows out of ten could mean eight are healthy or that eight are not wired up, and
        # those are very different things to believe at 03:00.
        never = len(SPECS) - len({e.name for e in events})
        if never:
            body += f"\n\n_{never} of {len(SPECS)} named conditions have never been recorded._"
        await interaction.followup.send(
            embed=notice_embed(title, body, bad=bool(active)),
            ephemeral=True,
        )


def register(tree: Any, context: BotContext) -> None:
    commands = OpsCommands(context)

    @tree.command(
        name="ops",
        description="Which operational conditions are true right now",
    )
    async def ops(interaction: discord.Interaction) -> None:
        await commands.ops(interaction)
