"""Slash command registration."""

from __future__ import annotations

from typing import Any

from aureon.discord.context import BotContext


def register_all(tree: Any, context: BotContext) -> None:
    """Register every Aureon command on the tree."""
    from aureon.discord.commands.backtest_status import register as register_backtest_status
    from aureon.discord.commands.control import register as register_control
    from aureon.discord.commands.execute import register as register_execute_shortcut
    from aureon.discord.commands.execute_trade import register as register_execute
    from aureon.discord.commands.model_status import register as register_model_status
    from aureon.discord.commands.monitor import register as register_monitor
    from aureon.discord.commands.note import register as register_note
    from aureon.discord.commands.ops import register as register_ops
    from aureon.discord.commands.remind import register as register_remind
    from aureon.discord.commands.setups import register as register_setups
    from aureon.discord.commands.status import register as register_status
    from aureon.discord.commands.trading import register as register_trading
    from aureon.discord.commands.training_status import register as register_training_status
    from aureon.discord.commands.training_weekly import register as register_training_weekly

    register_execute(tree, context)
    register_execute_shortcut(tree, context)
    register_status(tree, context)
    register_control(tree, context)
    register_trading(tree, context)
    register_remind(tree, context)
    register_monitor(tree, context)
    register_note(tree, context)
    register_ops(tree, context)
    register_setups(tree, context)
    register_training_status(tree, context)
    register_training_weekly(tree, context)
    register_model_status(tree, context)
    register_backtest_status(tree, context)
