#!/usr/bin/env python3
"""Diff a live session's stored detections against a replay of the same candles (§82).

The parity test in ``tests/replay`` proves the engine is deterministic given the same
candles. It cannot prove the live path was GIVEN the same candles, because the live input
only ever existed in memory. This closes that gap: the observer archives every closed
candle it processed, and this replays exactly those bytes and compares the result against
what reached Firestore.

## What a difference means

Read the output before changing anything, because the three failure modes have completely
different fixes:

* **missing** -- the replay produced a detection the live run never stored. Either the
  live run dropped it (an outbox that never drained, a crash between detect and enqueue)
  or the live engine saw a different window.
* **extra** -- Firestore holds a detection the replay does not produce. Usually a
  detection from an earlier code version still sitting in the collection, which is exactly
  what §12's ``agent_version`` in the id is for -- check the versions before assuming a
  bug.
* **mismatched** -- same id, different content. The most serious: the id is a pure
  function of the candle, so two documents sharing one id with different bodies means
  something wrote a detection the engine would not produce.

Exits non-zero on any difference, so it can gate a session review.

## It replays the archive, not a fresh fetch

Deliberately. A fresh broker fetch would let "the broker served different candles the
second time" masquerade as an engine difference, and those two are not the same problem.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from aureon.data.live_candle_archive import archive_path, read_archive  # noqa: E402
from aureon.engine.analysis_engine import AnalysisEngine  # noqa: E402
from aureon.models.detection import Detection  # noqa: E402
from aureon.models.enums import Timeframe  # noqa: E402

#: Fields excluded from the content comparison, each for a stated reason.
#:
#: ``schema_version`` is metadata about the document rather than about the observation, so a
#: schema bump must not read as thousands of mismatched detections.
#:
#: ``mtf`` (11D) is higher-timeframe context aggregated from a ROLLING MULTI-DAY buffer, and
#: this tool replays exactly one archived day. A live detection at 09:00 on Tuesday carries an
#: H1 read built from the preceding week; the same detection replayed from Tuesday's archive
#: alone has an hour of history and no H4 at all. Comparing them would report every detection
#: as mismatched and blame the engine for a difference that is entirely the buffer's depth --
#: which is a deployment choice (``AUREON_MTF_M5_BARS``), not a correctness parameter.
#:
#: What that costs, said plainly: this tool does NOT verify the MTF context. The aggregation
#: itself is deterministic and covered by ``tests/unit/test_mtf.py``, which checks it against
#: bars whose buckets are known by construction; what is untested by THIS tool is whether a
#: live observer and a replay agree about it, and they cannot be made to over one day.
IGNORED_FIELDS = frozenset({"schema_version", "mtf"})


@dataclass
class Comparison:
    """What the diff found. Every count is reported, including the zeros."""

    market_date: str
    symbol: str
    timeframe: str
    candles: int = 0
    live_count: int = 0
    replay_count: int = 0
    matched: int = 0
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    mismatched: list[tuple[str, list[str]]] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        return not (self.missing or self.extra or self.mismatched)

    def render(self) -> str:
        lines = [
            f"live vs replay — {self.symbol} {self.timeframe} {self.market_date}",
            f"  candles replayed   {self.candles}",
            f"  detections (live)  {self.live_count}",
            f"  detections (replay){self.replay_count:>4}",
            f"  matched            {self.matched}",
            f"  missing            {len(self.missing)}  (replay produced, never stored)",
            f"  extra              {len(self.extra)}  (stored, replay does not produce)",
            f"  mismatched         {len(self.mismatched)}  (same id, different content)",
        ]
        for label, ids in (("missing", self.missing), ("extra", self.extra)):
            for detection_id in ids[:20]:
                lines.append(f"    {label}: {detection_id}")
            if len(ids) > 20:
                lines.append(f"    {label}: (+{len(ids) - 20} more)")
        for detection_id, fields in self.mismatched[:20]:
            lines.append(f"    mismatched: {detection_id} differs on {', '.join(fields)}")
        if len(self.mismatched) > 20:
            lines.append(f"    mismatched: (+{len(self.mismatched) - 20} more)")
        lines.append("")
        lines.append("IDENTICAL" if self.identical else "DIFFERENCES FOUND")
        return "\n".join(lines)


def differing_fields(left: dict, right: dict) -> list[str]:
    """Field names whose values differ, ignoring pure document metadata."""
    keys = (set(left) | set(right)) - IGNORED_FIELDS
    return sorted(key for key in keys if left.get(key) != right.get(key))


def compare(
    live: list[Detection], replayed: list[Detection], *, market_date: str, symbol: str,
    timeframe: str, candles: int,
) -> Comparison:
    """Diff two sets of detections by id, then by content."""
    result = Comparison(
        market_date=market_date,
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
        live_count=len(live),
        replay_count=len(replayed),
    )
    live_by_id = {d.detection_id: d for d in live}
    replay_by_id = {d.detection_id: d for d in replayed}

    result.missing = sorted(set(replay_by_id) - set(live_by_id))
    result.extra = sorted(set(live_by_id) - set(replay_by_id))

    for detection_id in sorted(set(live_by_id) & set(replay_by_id)):
        left = live_by_id[detection_id].model_dump(mode="json")
        right = replay_by_id[detection_id].model_dump(mode="json")
        fields = differing_fields(left, right)
        if fields:
            result.mismatched.append((detection_id, fields))
        else:
            result.matched += 1
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("market_date", help="broker date to compare, YYYY-MM-DD")
    parser.add_argument("--symbol", default=None, help="default: the first configured")
    parser.add_argument("--timeframe", default=None, help="default: the first configured")
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=None,
        help="where the observer wrote its candles (default: data/live_candles)",
    )
    args = parser.parse_args(argv)

    from aureon.config import AureonConfig

    config = AureonConfig.from_env()
    symbol = args.symbol or config.symbols[0]
    timeframe = (
        Timeframe(args.timeframe) if args.timeframe else config.timeframes[0]
    )

    path = archive_path(symbol, timeframe, args.market_date, root=args.archive_dir)
    try:
        candles = read_archive(
            symbol,
            timeframe,
            args.market_date,
            market_tz=config.market_tz,
            root=args.archive_dir,
        )
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not candles:
        print(f"{path} holds no candles", file=sys.stderr)
        return 2

    # The same roster and the same periods the observer ran, from the same config. A
    # comparison built from a different roster would report every extra agent's
    # detections as "missing" and tell you nothing.
    from main_observer import default_agents

    engine = AnalysisEngine(
        # This SYMBOL's roster, and no point= so the tuning table's tick for it is used
        # rather than gold's. Replaying silver's archive through gold's roster would
        # report every detection as a difference and blame the engine (9A).
        default_agents(config, symbol=symbol),
        account_scope=config.account_scope,
        market_tz=config.market_tz,
    )
    replayed = engine.feed(candles)

    from aureon.storage.detection_repository import DetectionRepository
    from aureon.storage.firebase_service import get_client

    client = get_client(
        project_id=config.firebase_project_id,
        emulator_host=config.firestore_emulator_host,
    )
    # By the broker day the candles cover, so live and replay are scoped identically.
    start = candles[0].open_time.utc
    end = candles[-1].close_time
    live = [
        d
        for d in DetectionRepository(client).in_period(start, end)
        if d.symbol == symbol and d.timeframe is timeframe
    ]

    result = compare(
        live,
        [d for d in replayed if d.symbol == symbol and d.timeframe is timeframe],
        market_date=args.market_date,
        symbol=symbol,
        timeframe=timeframe.value,
        candles=len(candles),
    )
    print(result.render())
    return 0 if result.identical else 1


if __name__ == "__main__":
    raise SystemExit(main())
