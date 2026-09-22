"""Who writes each collection, and who reads it (12, T-2).

The question this exists to answer in one place: **which process writes `trades`?** It is the
question an operator asks when a number looks wrong, and before this module the answer was
assembled by reading five entrypoints and twenty repositories.

## Declared, and checked against the generated registry

The collection names come from ``paths.ALL_COLLECTIONS``, which is derived from the module's own
namespace (11A, F-10) precisely because a hand-maintained list drifts invisibly. The writer and
reader attribution cannot be derived that way -- "who calls this repository" is a question about
five entrypoints and a Discord package -- so it is declared here, and
``tests/unit/test_ownership.py`` asserts the two sets match exactly in both directions. A
collection added to ``paths.py`` without an entry here fails; an entry here for a collection that
no longer exists fails too.

That is the whole mechanism, and it is worth being clear about what it does and does not prove. It
proves the table in ``docs/ARCHITECTURE.md`` covers every collection. It does not prove a process
named as a reader never writes -- that is what the boundary tests in
``tests/boundary/test_architecture_boundaries.py`` are for, and the two that matter most are
enforced there rather than here: the observation side never reaches the broker, and Discord writes
only the handful of collections §71 permits it.

## Process names

The five services, plus ``launcher`` for ``main_aureon.py`` and ``tools`` for the scripts in
``scripts/`` that a human runs. Both of those last two are worth having as names rather than
folding into a service: ``tools`` appears as a writer on exactly two collections --
``symbol_specs`` and ``heartbeats``, both from the preflight -- and those are the only documents a
preflight leaves behind. ``launcher`` writes exactly one: the ``supervisor_stack_down`` row in
``ops_events``.

## What this table taught its own author

Declaring the attribution and then testing it caught two claims that were wrong on the first
pass, which is the argument for doing it this way:

* Discord does **not** write ``ops_events``. It only reads them, for ``/ops`` -- the register is
  writable in its context object because a read-only wrapper would be ceremony, not because
  Discord raises conditions. The first draft of this table said it did.
* ``ops_events`` is written today by the **observer** and the **launcher**, and by nobody else.
  ``executor_stale`` and ``monitor_stale`` are conditions the observer reports, because it is the
  process that reads the others' heartbeats. An operator who assumed each service reported its
  own would look for the wrong thing when a row went missing.
"""

from __future__ import annotations

from dataclasses import dataclass

from aureon.storage import paths

OBSERVER = "observer"
MONITOR = "monitor"
EXECUTOR = "executor"
DISCORD = "discord"
REVIEW = "review"
LAUNCHER = "launcher"
TOOLS = "tools"

PROCESSES: tuple[str, ...] = (
    OBSERVER,
    MONITOR,
    EXECUTOR,
    DISCORD,
    REVIEW,
    LAUNCHER,
    TOOLS,
)


@dataclass(frozen=True)
class Ownership:
    """One collection: what is in it, who writes it, who reads it."""

    #: The PREFIXED name, and always a constant from ``paths`` rather than a string written
    #: here. Two reasons, and the second is the one that matters:
    #:
    #: 1. §83 forbids a bare collection literal anywhere outside ``paths.py``, and a boundary
    #:    test enforces it. The first draft of this table held twenty of them and was caught.
    #:    Exempting this file would have been the easy fix and the wrong one -- an exemption is
    #:    where a real offender hides later.
    #: 2. Referencing the constant means a renamed collection is a ``NameError`` here, not a
    #:    silently stale row. A string would have gone on describing a collection that no longer
    #:    exists, which is exactly the drift this module was written to prevent.
    collection: str
    what: str
    writers: tuple[str, ...]
    readers: tuple[str, ...]

    @property
    def name(self) -> str:
        """The unprefixed name, for a document that must not carry the live prefix."""
        return unprefixed(self.collection)


OWNERSHIP: tuple[Ownership, ...] = (
    Ownership(
        paths.DETECTIONS,
        "one immutable row per agent event at a candle close; the id is a hash over seven "
        "components including agent_version (§12)",
        writers=(OBSERVER,),
        readers=(DISCORD, REVIEW, TOOLS),
    ),
    Ownership(
        paths.DETECTION_EVALUATIONS,
        "what happened after a detection, per frozen rule and horizon (§21, §22). Separate "
        "from the detection because a detection may never carry future information",
        writers=(OBSERVER,),
        readers=(DISCORD, REVIEW, TOOLS),
    ),
    Ownership(
        paths.SESSIONS,
        "one document per broker day and session: its range, and what the agents saw (§18)",
        writers=(OBSERVER,),
        readers=(DISCORD, REVIEW),
    ),
    Ownership(
        paths.TRADE_REQUESTS,
        "the only route to execution. Discord writes REQUESTED and CONFIRMED; the executor "
        "claims one with a lease and writes every state after that (§25, §41)",
        writers=(DISCORD, EXECUTOR, MONITOR),
        readers=(DISCORD, MONITOR, REVIEW),
    ),
    Ownership(
        paths.TRADES,
        "what the broker actually did, reconciled from its own deals. MT5 is the truth here; "
        "Aureon only records it (§58, §77)",
        writers=(MONITOR,),
        readers=(DISCORD, REVIEW),
    ),
    Ownership(
        paths.CONTROL_REQUESTS,
        "close / modify / cancel asked for by a human, performed by the executor under a lease",
        writers=(DISCORD, EXECUTOR),
        readers=(DISCORD, EXECUTOR),
    ),
    Ownership(
        paths.AUDIT_LOGS,
        "every state change that moved money or permission, written beside the change itself",
        writers=(EXECUTOR, MONITOR, DISCORD),
        readers=(DISCORD, TOOLS),
    ),
    Ownership(
        paths.HEARTBEATS,
        "one document per service, the source of truth for liveness (decision 10). The "
        "preflight writes one under its own name",
        writers=(OBSERVER, MONITOR, EXECUTOR, DISCORD, TOOLS),
        readers=(DISCORD, TOOLS),
    ),
    Ownership(
        paths.SYSTEM_STATE,
        "the observer's current view, one document per symbol and timeframe (9A). A derived "
        "snapshot, never an authority",
        writers=(OBSERVER,),
        readers=(DISCORD,),
    ),
    Ownership(
        paths.SETTINGS,
        "settings/execution holds trading_enabled and the runtime gates (decision 11); "
        "settings/notifications holds what Discord announces. Discord is the only writer, and "
        "the kill switch is written under a transaction",
        writers=(DISCORD,),
        readers=(EXECUTOR, DISCORD, TOOLS),
    ),
    Ownership(
        paths.SYMBOL_SPECS,
        "broker metadata published for Discord to render. A convenience copy: the execution "
        "guard re-reads the live symbol at execution time (decision 79)",
        writers=(OBSERVER, TOOLS),
        readers=(DISCORD,),
    ),
    Ownership(
        paths.DAILY_REVIEWS,
        "one aggregation per broker day, re-runnable for any past day",
        writers=(REVIEW,),
        readers=(DISCORD, TOOLS),
    ),
    Ownership(
        paths.WEEKLY_REVIEWS,
        "one aggregation per ISO week, including the scorecard that grades its own readouts",
        writers=(REVIEW,),
        readers=(DISCORD, TOOLS),
    ),
    Ownership(
        paths.NOTIFICATIONS,
        "what Discord has already said, claimed before posting so a restart does not repeat "
        "it (9C). See the claim-before-post tradeoff in docs/ARCHITECTURE.md",
        writers=(DISCORD,),
        readers=(DISCORD,),
    ),
    Ownership(
        paths.ALERTS,
        "price levels a human asked to be told about. Discord arms them; the observer sees "
        "the quote cross one and fires it once (9C)",
        writers=(DISCORD, OBSERVER),
        readers=(DISCORD, OBSERVER),
    ),
    Ownership(
        paths.ASSESSMENTS,
        "a measured cohort readout for one detection shape, with what was dropped to reach "
        "it and where its history came from (9D, 11C)",
        writers=(DISCORD,),
        readers=(DISCORD, REVIEW),
    ),
    Ownership(
        paths.TRADE_NOTES,
        "what a human wrote down about a trade or a detection",
        writers=(DISCORD,),
        readers=(DISCORD, REVIEW),
    ),
    Ownership(
        paths.OPS_EVENTS,
        "the named operational conditions and whether each is true right now, one write per "
        "edge (11A, F-15). Written by the observer (which reads the other services' "
        "heartbeats, so executor_stale and monitor_stale are its to report) and by the "
        "launcher (supervisor_stack_down). Nothing gates on these",
        writers=(OBSERVER, LAUNCHER),
        readers=(DISCORD,),
    ),
    Ownership(
        paths.MARKET_DAYS,
        "one broker day's shape per symbol: its OHLC, bar count and whether it is finished "
        "(11D)",
        writers=(OBSERVER,),
        readers=(DISCORD, REVIEW, TOOLS),
    ),
    Ownership(
        paths.SETUPS,
        "structures tracked over time, with their state machine and a per-setup `events` "
        "sub-collection holding how each one got there (12, T-6). Written only by the observer: "
        "Discord's card keeps its message id on the notification document instead, so this "
        "collection stays the observer's",
        writers=(OBSERVER,),
        readers=(DISCORD, REVIEW, TOOLS),
    ),
    Ownership(
        paths.SETUP_EVALUATIONS,
        "what a setup did after it CONFIRMED, under one frozen rule, measured by the same "
        "tracker the detections use (12, T-7). Separate from the setup because the setup is "
        "edited as it advances and an outcome on it would be future information",
        writers=(OBSERVER,),
        readers=(DISCORD, REVIEW, TOOLS),
    ),
    Ownership(
        paths.MARKET_DAY_FRAMES,
        "the bars themselves, M5/M15/H1, so a process with no terminal can rebuild a chart "
        "or a higher-timeframe bias. M1 stays in parquet (11D)",
        writers=(OBSERVER,),
        readers=(DISCORD, REVIEW, TOOLS),
    ),
)

#: Keyed by the PREFIXED name, so a lookup is ``BY_COLLECTION[paths.TRADES]`` and never a
#: string somebody typed.
BY_COLLECTION: dict[str, Ownership] = {row.collection: row for row in OWNERSHIP}


def unprefixed(prefixed: str) -> str:
    """``aureon_beast_detections`` -> ``detections``."""
    head = f"{paths.PREFIX}_"
    return prefixed[len(head) :] if prefixed.startswith(head) else prefixed


def declared() -> set[str]:
    return set(BY_COLLECTION)


def registered() -> set[str]:
    """Every collection ``paths.py`` actually defines."""
    return set(paths.ALL_COLLECTIONS)


def render_table() -> str:
    """The ownership table, for ``docs/ARCHITECTURE.md``.

    Rendered with the ``{prefix}`` placeholder rather than the live prefix, so the checked-in
    document does not differ between a test run and production (decision 111). Sorted by name so
    the generated block is stable across runs and a diff shows only what changed.
    """
    lines = [
        "| collection | what is in it | written by | read by |",
        "|---|---|---|---|",
    ]
    for row in sorted(OWNERSHIP, key=lambda one: one.name):
        lines.append(
            f"| `{{prefix}}_{row.name}` | {row.what} | "
            f"{', '.join(row.writers)} | {', '.join(row.readers)} |"
        )
    return "\n".join(lines)
