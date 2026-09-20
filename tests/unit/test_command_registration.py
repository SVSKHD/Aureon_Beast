"""Every command Aureon implements is actually on the tree (9A, §71).

A command handler that is never registered is not a bug the suite would otherwise notice:
the module imports, its rules are unit-tested, its tests pass — and the command does not
exist in Discord. The failure is silent and only a human opening the slash-command menu
sees it.

So this registers the real ``register_all`` on a real ``app_commands.CommandTree`` and asks
what is on it. The tree is discord.py's own, not a stand-in, because it is also what would
reject a malformed option, a duplicate name or more choices than Discord allows — a fake
tree would get to define those rules for itself.
"""

from __future__ import annotations

import discord
import pytest
from discord import app_commands

from aureon.config import AureonConfig
from aureon.discord.commands import register_all

GOLD = "XAUUSD"
SILVER = "XAGUSD"

#: Every command a trader is told exists (docs/RUNBOOK.md).
EXPECTED = {
    "status",
    "execute",
    "execute-trade",
    "close",
    "cancel-order",
    "close-trade",
    "trading",
}


class RegistrationContext:
    """Only what ``register`` reads. A registration must not need a Firestore client."""

    def __init__(self, config: AureonConfig) -> None:
        self.config = config


def tree_for(*symbols: str) -> app_commands.CommandTree:
    config = AureonConfig(
        symbols=symbols or (GOLD,),
        evaluation_rules={GOLD: "XAU_OUTCOME_V2", SILVER: "XAG_OUTCOME_V1"},
    )
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    register_all(tree, RegistrationContext(config))
    return tree


@pytest.fixture(scope="module")
def tree() -> app_commands.CommandTree:
    return tree_for(GOLD, SILVER)


def test_every_documented_command_is_registered(tree: app_commands.CommandTree) -> None:
    names = {command.name for command in tree.get_commands()}
    assert EXPECTED <= names


def test_the_shortcut_and_the_wizard_both_exist(tree: app_commands.CommandTree) -> None:
    """9A. The shortcut is an addition, not a replacement.

    ``/execute-trade`` is still the only way to place a pending order with stops, so
    removing it in favour of the shortcut would remove capability rather than typing.
    """
    assert tree.get_command("execute") is not None
    assert tree.get_command("execute-trade") is not None


def options(tree: app_commands.CommandTree, command: str) -> dict:
    return {p.name: p for p in tree.get_command(command).parameters}


def test_the_shortcut_offers_exactly_the_symbols_this_deployment_observes() -> None:
    """A choice list is what a trader picks from, so it must match the process's reality."""
    both = options(tree_for(GOLD, SILVER), "execute")["symbol"]
    assert [choice.value for choice in both.choices] == [GOLD, SILVER]

    gold_only = options(tree_for(GOLD), "execute")["symbol"]
    assert [choice.value for choice in gold_only.choices] == [GOLD]


def test_the_shortcut_requires_a_symbol_a_side_and_a_lot(tree) -> None:
    """The lot has no default here or in the signature: a wrong size costs money."""
    params = options(tree, "execute")
    assert params["symbol"].required is True
    assert params["side"].required is True
    assert params["lot"].required is True
    # The detection link is the one optional field, and it is a typed id (§50).
    assert params["detection"].required is False


def test_the_side_is_a_choice_rather_than_free_text(tree) -> None:
    """"long" or "b" on the one command that moves money is not a convenience."""
    assert [choice.value for choice in options(tree, "execute")["side"].choices] == [
        "buy",
        "sell",
    ]


# ── 9A: the symbol option, wherever a symbol changes the answer ────────────────

#: Commands whose answer depends on which symbol is meant, and what the option is called
#: there. ``/close`` requires it -- the symbol IS the target. Everywhere else it is
#: optional, because a deployment observing one symbol must not have to name it.
SYMBOL_AWARE = {
    "status": False,
    "execute": True,
    "execute-trade": True,
    "close": True,
    "cancel-order": False,
    "close-trade": False,
}


@pytest.mark.parametrize(("command", "required"), sorted(SYMBOL_AWARE.items()))
def test_each_symbol_aware_command_offers_the_configured_symbols(
    tree: app_commands.CommandTree, command: str, required: bool
) -> None:
    """One list, from the config, everywhere. A command left behind is the one a trader
    uses on the symbol it cannot see."""
    option = options(tree, command)["symbol"]
    assert [choice.value for choice in option.choices] == [GOLD, SILVER]
    assert option.required is required
