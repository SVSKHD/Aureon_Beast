"""Slash command registration."""

from __future__ import annotations

from typing import Any

from aureon.discord.context import BotContext


def register_all(tree: Any, context: BotContext) -> None:
    """Register every Aureon command on the tree."""
    from aureon.discord.commands.control import register as register_control
    from aureon.discord.commands.execute import register as register_execute_shortcut
    from aureon.discord.commands.execute_trade import register as register_execute
    from aureon.discord.commands.status import register as register_status
    from aureon.discord.commands.trading import register as register_trading

    register_execute(tree, context)
    register_execute_shortcut(tree, context)
    register_status(tree, context)
    register_control(tree, context)
    register_trading(tree, context)
