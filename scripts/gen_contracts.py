#!/usr/bin/env python3
"""Generate ``docs/CONTRACTS.md`` from the Pydantic models.

Run after any model change and commit the result:

    python scripts/gen_contracts.py

The cross-phase checklist requires this to produce **no diff** unless a model
changed. The output is therefore fully deterministic: fields render in
declaration order, enums in definition order, and nothing here consults the clock,
the environment, or a dict whose order could vary between runs. A timestamp in the
header would make the file differ on every run and destroy the check.

``--check`` exits non-zero when the committed file is stale, which is what CI
should run.
"""

from __future__ import annotations

import argparse
import sys
from enum import StrEnum
from pathlib import Path
from types import UnionType
from typing import Union, get_args, get_origin

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from pydantic import BaseModel  # noqa: E402

from aureon import models as m  # noqa: E402
from aureon.config.sessions import (  # noqa: E402
    SESSION_CONFIG_VERSION,
    SESSION_PRECEDENCE,
    SESSION_WINDOWS,
)
from aureon.storage import paths  # noqa: E402

OUTPUT = REPO_ROOT / "docs" / "CONTRACTS.md"

# Enums documented in CONTRACTS.md, in a fixed order.
ENUMS: tuple[type[StrEnum], ...] = (
    m.Direction,
    m.OrderType,
    m.FillingMode,
    m.Timeframe,
    m.MarketState,
    m.SessionName,
    m.Freshness,
    m.TradeRequestStatus,
    m.TradeStatus,
    m.TradeSource,
    m.LinkType,
    m.FailureCode,
    m.ControlRequestKind,
    m.ControlRequestStatus,
    m.HorizonStatus,
    m.HorizonKind,
    m.ReferencePrice,
    m.PathClassification,
    m.ExcursionSource,
    m.ExecutionClassification,
    m.DealEntry,
)

# Value models that are embedded in documents rather than stored alone.
VALUE_MODELS: tuple[type[BaseModel], ...] = (
    m.MarketTime,
    m.Candle,
    m.QuoteSnapshot,
    m.SymbolInfo,
    m.CandleContext,
    m.IndicatorSnapshot,
    m.SessionContext,
    m.BrokerOrderRequest,
    m.BrokerOrderResult,
    m.AccountInfo,
    m.BrokerPosition,
    m.BrokerOrder,
    m.BrokerDeal,
    m.PendingOrder,
    m.Excursion,
    m.EvaluationRule,
    m.Horizon,
    m.HorizonResult,
    m.InferredLink,
    m.ThresholdOutcome,
    m.HorizonOutcome,
    m.SymbolState,
)


def type_name(annotation: object) -> str:
    """Render a type annotation compactly and deterministically."""
    if annotation is type(None):
        return "null"
    # Annotated[...] carries validators and serializers whose repr includes a
    # lambda's memory address -- which changes every process and would make this
    # file differ on every run. The metadata is serialization detail, not
    # contract, so strip it and document the underlying type. This also renders
    # UtcDatetime and pydantic's AwareDatetime as plain `datetime`.
    if hasattr(annotation, "__metadata__"):
        return type_name(get_args(annotation)[0])
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        parts = [type_name(a) for a in get_args(annotation)]
        # Sort only the null to the end; keep declared order otherwise so the
        # rendering is stable but still reads as the author wrote it.
        non_null = [p for p in parts if p != "null"]
        return " | ".join(non_null) + (" | null" if "null" in parts else "")
    if origin in (list, tuple, set, frozenset):
        args = [a for a in get_args(annotation) if a is not Ellipsis]
        inner = ", ".join(type_name(a) for a in args)
        return f"{origin.__name__}[{inner}]" if inner else origin.__name__
    if origin is dict:
        k, v = get_args(annotation)
        return f"dict[{type_name(k)}, {type_name(v)}]"
    if isinstance(annotation, type):
        if issubclass(annotation, StrEnum):
            return annotation.__name__
        return annotation.__name__
    return str(annotation).replace("typing.", "")


def render_model(model: type[BaseModel], *, heading: str) -> list[str]:
    lines = [f"### {heading}", ""]
    doc = (model.__doc__ or "").strip().splitlines()
    if doc:
        lines += [doc[0].strip(), ""]
    lines += ["| field | type | required | default | notes |", "|---|---|---|---|---|"]
    for name, field in model.model_fields.items():
        required = "yes" if field.is_required() else "no"
        if field.is_required():
            default = "—"
        elif field.default_factory is not None:
            # NEVER call the factory: utc_now() would stamp a live timestamp and
            # make this file differ on every run, breaking the no-diff check the
            # cross-phase checklist depends on. Render the factory's name instead.
            factory_name = getattr(field.default_factory, "__name__", "factory")
            default = f"`{factory_name}()`"
        else:
            default = f"`{field.get_default()!r}`"
        note = (field.description or "").replace("|", "\\|").replace("\n", " ")
        # Union types render as "A | null"; the bare pipe would split the markdown
        # table cell, so escape it the same way the notes column is escaped.
        rendered = type_name(field.annotation).replace("|", "\\|")
        lines.append(
            f"| `{name}` | `{rendered}` | {required} | {default} | {note} |"
        )
    lines.append("")
    return lines


def transitions_table(title: str, table: dict) -> list[str]:
    lines = [f"### {title}", "", "| from | to |", "|---|---|"]
    for current, allowed in table.items():
        targets = ", ".join(f"`{s}`" for s in sorted(str(a) for a in allowed))
        lines.append(f"| `{current}` | {targets or '_terminal_'} |")
    lines.append("")
    return lines


def build() -> str:
    lines: list[str] = [
        "# Aureon contracts",
        "",
        "**Generated — do not edit by hand.** Run `python scripts/gen_contracts.py`",
        "after any model change and commit the result.",
        "",
        f"Schema version: **{m.SCHEMA_VERSION}** (§6, decision 12) — carried on every",
        "stored document.",
        "",
        "---",
        "",
        "## Collections",
        "",
        "| collection | document id | model |",
        "|---|---|---|",
        f"| `{paths.DETECTIONS}` | `detection_id` | `Detection` |",
        f"| `{paths.DETECTION_EVALUATIONS}` | `{{detection_id}}__{{rule_id}}` "
        "| `DetectionEvaluation` |",
        f"| `{paths.SESSIONS}` | `{{market_date}}__{{session}}` | _(Phase 2)_ |",
        f"| `{paths.TRADE_REQUESTS}` | `request_id` | `TradeRequest` |",
        f"| `{paths.TRADES}` | `trade_id` | `Trade` |",
        f"| `{paths.CONTROL_REQUESTS}` | `control_id` | `ControlRequest` |",
        f"| `{paths.AUDIT_LOGS}` | `audit_id` | `AuditRecord` |",
        f"| `{paths.HEARTBEATS}` | `{{service}}` | `Heartbeat` |",
        f"| `{paths.SYSTEM_STATE}` | `{paths.SYSTEM_STATE_DOC}` | `SystemState` |",
        f"| `{paths.SETTINGS}` | `{paths.EXECUTION_SETTINGS_DOC}` | `ExecutionSettings` |",
        f"| `{paths.DAILY_REVIEWS}` | `{{market_date}}` | `DailyReview` |",
        f"| `{paths.WEEKLY_REVIEWS}` | `{{iso_year}}-W{{iso_week}}` | `WeeklyReview` |",
        "",
        "Tick data is never stored in Firestore. There is no `pending_orders`",
        "collection (decision 9): a pending order *is* the `PENDING` trade request.",
        "",
        "---",
        "",
        "## Documents",
        "",
    ]

    for model in m.DOCUMENT_MODELS:
        lines += render_model(model, heading=model.__name__)

    lines += ["---", "", "## Embedded value models", ""]
    for model in VALUE_MODELS:
        lines += render_model(model, heading=model.__name__)

    lines += ["---", "", "## Enums", ""]
    for enum in ENUMS:
        doc = (enum.__doc__ or "").strip().splitlines()
        lines += [f"### {enum.__name__}", ""]
        if doc:
            lines += [doc[0].strip(), ""]
        lines += ["| member | value |", "|---|---|"]
        for member in enum:
            lines.append(f"| `{member.name}` | `{member.value}` |")
        lines.append("")

    lines += [
        "---",
        "",
        "## State machine",
        "",
        "Every status write goes through `aureon.models.enums.assert_transition`",
        "(CLAUDE.md). A status with no outgoing edges is terminal.",
        "",
    ]
    lines += transitions_table(
        "TradeRequestStatus (§25, decision 1)", dict(m.TRADE_REQUEST_TRANSITIONS)
    )
    lines += transitions_table("TradeStatus", dict(m.TRADE_TRANSITIONS))
    lines += transitions_table("HorizonStatus (§22)", dict(m.HORIZON_TRANSITIONS))
    lines += transitions_table(
        "ControlRequestStatus (§46, §47)", dict(m.CONTROL_REQUEST_TRANSITIONS)
    )

    lines += [
        "---",
        "",
        "## Sessions",
        "",
        f"`SESSION_CONFIG_VERSION = {SESSION_CONFIG_VERSION}` (§18, decision 7), stamped on",
        "every detection so a later boundary change cannot silently reinterpret old data.",
        "",
        "| session | window (market time) |",
        "|---|---|",
    ]
    for name, window in SESSION_WINDOWS.items():
        lines.append(f"| `{name.value}` | `{window.start:%H:%M}`–`{window.end:%H:%M}` |")
    precedence = " → ".join(f"`{s.value}`" for s in SESSION_PRECEDENCE)
    lines += ["", f"Overlaps resolve by fixed precedence: {precedence}.", ""]

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the committed CONTRACTS.md is stale",
    )
    args = parser.parse_args()

    content = build()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != content:
            print(
                f"{OUTPUT.relative_to(REPO_ROOT)} is stale; "
                "run `python scripts/gen_contracts.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"{OUTPUT.relative_to(REPO_ROOT)} is up to date.")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(content, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)} ({len(content.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
