#!/usr/bin/env python3
"""Re-key detections written under the pre-§12 ``detection_id`` recipe.

The old recipe hashed ``account_scope | symbol | timeframe | agent_name | event_key |
candle_OPEN``. §12 fixes it as ``account_scope | symbol | timeframe |
candle_CLOSE | agent_name | agent_version | event_key``, so every detection written
before that change sits at an id nothing will ever look up again.

## What this does

Reads every detection, recomputes its id from its OWN stored fields, and where the
two differ writes a copy at the new id. It is a **copy, not a move**: the old
document is left exactly where it is unless ``--delete-old`` is passed. A detection
is immutable and cheap, and leaving the old one behind means a bad run is survivable.

## Idempotent

Running it twice is a no-op. The second pass recomputes the same ids, finds the new
documents already present with identical content, and reports them as ``unchanged``.
It never writes a document whose content already matches, so re-running does not
even produce Firestore writes.

## What it cannot do

It cannot split a collision the old recipe already caused. Because the old id left
``agent_version`` out, two versions of one agent observing the same candle wrote to
the SAME document and the second overwrote the first. Only one survived, and this
script faithfully moves that one -- it does not invent the other. The 9/21 history
therefore does not come from here: it comes from replaying with the agent you want,
which mints genuinely new detections under §12 ids.

## Emulator and test data only

There is no production data yet, and this script refuses to touch a project that
looks like one: without ``FIRESTORE_EMULATOR_HOST`` set it requires
``--i-know-there-is-no-production-data`` before it will write anything. The guard is
deliberately annoying. Re-keying is not reversible in the sense that matters -- the
new ids are derived from data, so a wrong recipe produces a second wrong set, and
you would then have three generations of the same detections to reason about.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from aureon.models.detection import Detection  # noqa: E402
from aureon.models.identity import detection_id  # noqa: E402
from aureon.storage import paths  # noqa: E402


@dataclass
class RekeyReport:
    """What one run did. Every count is reported, including the boring ones."""

    scanned: int = 0
    already_correct: int = 0
    rekeyed: int = 0
    unchanged: int = 0
    unreadable: int = 0
    deleted_old: int = 0
    by_agent: Counter[str] = field(default_factory=Counter)
    unreadable_ids: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            "detection re-key report",
            f"  scanned          {self.scanned}",
            f"  already correct  {self.already_correct}",
            f"  re-keyed         {self.rekeyed}",
            f"  unchanged        {self.unchanged}  (new id already present, same content)",
            f"  unreadable       {self.unreadable}",
            f"  old deleted      {self.deleted_old}",
        ]
        if self.by_agent:
            lines.append("  re-keyed by agent:")
            for agent, count in sorted(self.by_agent.items()):
                lines.append(f"    {agent:<16} {count}")
        if self.unreadable_ids:
            shown = ", ".join(self.unreadable_ids[:10])
            hidden = len(self.unreadable_ids) - 10
            more = "" if hidden <= 0 else f" (+{hidden} more)"
            lines.append(f"  unreadable ids: {shown}{more}")
        return "\n".join(lines)


def expected_id(detection: Detection) -> str:
    """The §12 id this detection should be stored at, from its own fields."""
    return detection_id(
        account_scope=detection.account_scope,
        symbol=detection.symbol,
        timeframe=detection.timeframe.value,
        candle_close=detection.detected_at.utc,
        agent_name=detection.agent_name,
        agent_version=detection.agent_version,
        event_key=detection.event_key,
    )


def rekey(
    client: Any, *, dry_run: bool = True, delete_old: bool = False
) -> RekeyReport:
    """Copy every mis-keyed detection to its §12 id."""
    report = RekeyReport()

    for doc in client.collection(paths.DETECTIONS).stream():
        report.scanned += 1
        raw = doc.to_dict() or {}
        try:
            detection = Detection.model_validate(raw)
        except Exception:  # noqa: BLE001 - one bad document must not stop the run
            report.unreadable += 1
            report.unreadable_ids.append(doc.id)
            continue

        target = expected_id(detection)
        if target == doc.id:
            report.already_correct += 1
            continue

        # The stored detection_id FIELD must move with the document, or the copy would
        # carry the id it is no longer at -- and every reader that trusts the field
        # over the document path would follow it back to the old key.
        payload = dict(raw)
        payload["detection_id"] = target

        existing = client.document(paths.detection_path(target)).get()
        if getattr(existing, "exists", False) and (existing.to_dict() or {}) == payload:
            report.unchanged += 1
            continue

        if not dry_run:
            client.document(paths.detection_path(target)).set(payload)
            if delete_old:
                client.document(paths.detection_path(doc.id)).delete()
                report.deleted_old += 1
        report.rekeyed += 1
        report.by_agent[detection.agent_name] += 1

    return report


def _guard(args: argparse.Namespace) -> str | None:
    """Refuse to write outside the emulator without an explicit acknowledgement."""
    if args.dry_run:
        return None
    if os.environ.get("FIRESTORE_EMULATOR_HOST"):
        return None
    if args.i_know_there_is_no_production_data:
        return None
    return (
        "refusing to write: FIRESTORE_EMULATOR_HOST is not set, so this may be a real "
        "project. Re-run against the emulator, or pass "
        "--i-know-there-is-no-production-data if you are certain."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        default=True,
        help="actually write. Without this the run only reports what it would do.",
    )
    parser.add_argument(
        "--delete-old",
        action="store_true",
        help="delete the old document after copying. Off by default: a copy is "
        "survivable, a move is not.",
    )
    parser.add_argument(
        "--i-know-there-is-no-production-data",
        action="store_true",
        help="acknowledge writing outside the emulator.",
    )
    args = parser.parse_args(argv)

    refusal = _guard(args)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2

    from aureon.config import AureonConfig
    from aureon.storage.firebase_service import get_client

    config = AureonConfig.from_env()
    client = get_client(
        project_id=config.firebase_project_id,
        emulator_host=config.firestore_emulator_host,
    )

    report = rekey(client, dry_run=args.dry_run, delete_old=args.delete_old)
    print(report.render())
    if args.dry_run:
        print("\n(dry run — nothing was written. Re-run with --apply.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
