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

## ``--mtf``: verifying the higher-timeframe context (12, T-12)

The default comparison EXCLUDES the ``mtf`` block, for the reason ``IGNORED_FIELDS`` gives: one
archived day cannot reproduce a read built from a rolling multi-day buffer. ``--mtf`` closes that
gap by reading several archived days, rebuilding the buffer, and diffing each detection's context
**as of its own candle** rather than once for the day.

It checks M15, M30, H1 and H4. **D1 is excluded**, and that is a stated limitation rather than an
oversight -- see ``MTF_CHECKED``. Zero disagreements earns ``SESSION VERIFIED (MTF)``; anything
else prints the disagreements and exits non-zero.

Nothing in this system gates a detection or an execution on MTF alignment, and this tool does not
change that: it verifies that a recorded read was recorded correctly. Whether alignment predicts
anything is a question for the evaluation rules, and a boundary test keeps the answer out of the
observation and execution paths.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from aureon.data.live_candle_archive import archive_path, read_archive  # noqa: E402
from aureon.engine.analysis_engine import AnalysisEngine  # noqa: E402
from aureon.models.detection import Detection  # noqa: E402
from aureon.models.enums import Timeframe  # noqa: E402
from aureon.models.market import Candle  # noqa: E402

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


# ── 12 T-12: verifying the higher-timeframe context ───────────────────────────

#: The timeframes ``--mtf`` checks, and the one it does not.
#:
#: D1 is excluded, and the reason is not arbitrary. A daily bar's boundaries come from the
#: BROKER day, whose length depends on the session, on weekends and on holidays -- which is why
#: ``mtf.BARS_PER`` has no entry for it and ``mtf._complete`` cannot decide a D1 bucket from a
#: bar count. Reconstructing a live D1 read would mean reconstructing the market calendar the
#: observer's buffer happened to hold, and a mismatch would then be a statement about the
#: calendar rather than about the aggregation. Better to check four timeframes honestly than
#: five with one of them meaningless -- and to say so here rather than let a reader assume the
#: coverage is complete.
#:
#: What that leaves untested by this tool: whether a live observer and a replay agree about D1.
#: ``tests/unit/test_mtf.py`` covers the D1 aggregation itself against bars whose buckets are
#: known by construction.
MTF_CHECKED: tuple[Timeframe, ...] = (
    Timeframe.M15,
    Timeframe.M30,
    Timeframe.H1,
    Timeframe.H4,
)

MTF_EXCLUDED: tuple[Timeframe, ...] = (Timeframe.D1,)

#: How close two EMA readings must be to count as the same number. A replay recomputes an EMA
#: over the same closes in the same order, so the arithmetic is identical -- but the two values
#: travel different routes to get here (one through a Firestore JSON round trip, one straight
#: out of pandas), and a float that survived ``json`` is not always bit-identical to one that
#: did not. A tolerance of one part in a billion is far tighter than any price tick and far
#: looser than that round trip.
EMA_TOLERANCE = 1e-9

#: The marker a verified MTF comparison earns, and the words that carry it.
MTF_VERIFIED_MARKER = "SESSION VERIFIED (MTF)"

#: How many calendar days before the date ``--mtf`` reads by default.
#:
#: Ten, because an H4 read needs ``slow`` aggregated bars -- fifty with the default pair -- and
#: fifty H4 bars is over a week of trading. Calendar days rather than trading days so this tool
#: needs no market calendar of its own; the weekend files simply do not exist and are skipped.
DEFAULT_MTF_DAYS = 10


@dataclass
class MtfComparison:
    """What the MTF diff found, per timeframe. Every count is reported, including the zeros."""

    market_date: str
    symbol: str
    days_replayed: int = 0
    candles: int = 0
    detections: int = 0
    #: Detections carrying no ``mtf`` block at all. Counted, not compared: a detection from
    #: before 11D has no context to disagree about, and calling that a mismatch would report
    #: every pre-11D session as broken.
    without_context: int = 0
    #: timeframe value -> how many reads agreed.
    agreed: dict[str, int] = field(default_factory=dict)
    #: ``(detection_id, timeframe, what differed)``.
    disagreed: list[tuple[str, str, str]] = field(default_factory=list)
    #: A timeframe the replay could not read at all, per detection. Its own bucket because the
    #: cause is almost always buffer depth rather than arithmetic: an H4 needs fifty aggregated
    #: bars, which is more than a week of M5.
    unreadable: list[tuple[str, str]] = field(default_factory=list)

    @property
    def compared(self) -> int:
        return sum(self.agreed.values()) + len(self.disagreed)

    @property
    def identical(self) -> bool:
        """Zero disagreements AND something actually compared.

        The second half matters: a run that read no context at all has zero disagreements, and
        calling that verified would hand out the marker for having looked at nothing.
        """
        return not self.disagreed and self.compared > 0

    def render(self) -> str:
        lines = [
            f"MTF context: {self.symbol} {self.market_date}",
            f"  replayed {self.candles} M5 candle(s) across {self.days_replayed} broker day(s)",
            f"  detections with context: {self.detections - self.without_context}"
            f" of {self.detections} (without: {self.without_context})",
            f"  checked: {', '.join(tf.value for tf in MTF_CHECKED)}"
            f"  ·  excluded: {', '.join(tf.value for tf in MTF_EXCLUDED)} (see MTF_CHECKED)",
        ]
        for timeframe in MTF_CHECKED:
            lines.append(f"  {timeframe.value}: {self.agreed.get(timeframe.value, 0)} agreed")
        if self.unreadable:
            lines.append(f"  unreadable in the replay: {len(self.unreadable)}")
            for detection_id, timeframe in self.unreadable[:10]:
                lines.append(f"    {detection_id} {timeframe}")
            lines.append(
                "    (a timeframe the replay had too little history to read. Widen "
                "--mtf-days; this is buffer depth, not arithmetic.)"
            )
        if self.disagreed:
            lines.append(f"  DISAGREED: {len(self.disagreed)}")
            for detection_id, timeframe, what in self.disagreed[:20]:
                lines.append(f"    {detection_id} {timeframe}: {what}")
        lines.append("")
        lines.append(
            MTF_VERIFIED_MARKER
            if self.identical
            else "MTF NOT VERIFIED — read the disagreements above before changing anything"
        )
        return "\n".join(lines)


def compare_mtf(
    detections: Sequence[Detection],
    candles: Sequence[Candle],
    *,
    market_date: str,
    symbol: str,
    days_replayed: int,
    fast: int,
    slow: int,
    market_tz: str,
) -> MtfComparison:
    """Recompute each detection's higher-timeframe context from the archive and diff it.

    Recomputed **as of each detection's own candle**, not once for the day: an H1 read at 09:00
    and one at 15:00 are different bars, and comparing every detection against a single
    end-of-day read would report the morning as broken.

    The recomputation goes through ``mtf.frames_from`` -- the same function the observer calls,
    not a second implementation of the same idea. What is being verified is that the live
    observer's buffer held the bars this archive holds; if the aggregation itself were wrong,
    both sides would be wrong identically and this tool would say IDENTICAL. That is not a gap
    in this tool but a division of labour: ``tests/unit/test_mtf.py`` checks the aggregation
    against buckets known by construction, and this checks the input.
    """
    from aureon.engine import mtf

    result = MtfComparison(
        market_date=market_date,
        symbol=symbol,
        days_replayed=days_replayed,
        candles=len(candles),
        detections=len(detections),
    )
    result.agreed = {timeframe.value: 0 for timeframe in MTF_CHECKED}

    # One pass, oldest detection first, walking the candle list forward. The detections are
    # sorted so the cursor only ever advances: a fresh scan per detection would be O(n*m) on a
    # day with three hundred candles and a hundred detections.
    ordered = sorted(detections, key=lambda d: (d.candle_open_time.utc, d.detection_id))
    closes = [candle.open_time.utc for candle in candles]
    cursor = 0
    for detection in ordered:
        stored = detection.mtf
        if stored is None or not stored.reads:
            result.without_context += 1
            continue
        at = detection.candle_open_time.utc
        while cursor + 1 < len(closes) and closes[cursor + 1] <= at:
            cursor += 1
        if cursor >= len(closes) or closes[cursor] != at:
            # The detection's own candle is not in the archive. Reported as a disagreement
            # rather than skipped: a detection whose bar was never archived is exactly the
            # thing §82 exists to surface, and swallowing it here would hide it.
            result.disagreed.append(
                (detection.detection_id, "-", "its candle is not in the archive")
            )
            continue
        window = candles[: cursor + 1]
        frames = {
            frame.timeframe: frame
            for frame in mtf.frames_from(
                window,
                fast=fast,
                slow=slow,
                timeframes=MTF_CHECKED,
                market_tz=market_tz,
            )
        }
        for timeframe in MTF_CHECKED:
            read = stored.by_timeframe.get(timeframe)
            frame = frames.get(timeframe)
            if read is None and frame is None:
                continue
            if frame is None:
                result.unreadable.append((detection.detection_id, timeframe.value))
                continue
            if read is None:
                result.disagreed.append(
                    (
                        detection.detection_id,
                        timeframe.value,
                        "the replay read it and the live detection did not",
                    )
                )
                continue
            difference = _read_difference(read, frame)
            if difference:
                result.disagreed.append(
                    (detection.detection_id, timeframe.value, difference)
                )
            else:
                result.agreed[timeframe.value] += 1
    return result


def _read_difference(read: Any, frame: Any) -> str | None:
    """What differs between a stored read and a recomputed frame, or ``None``.

    The bar's OPEN TIME first, because it is the one field where a difference is unambiguous: a
    live H1 read from the 13:00 bar and a replayed one from the 12:00 bar are looking at
    different bars, and every number below would differ as a consequence. Reporting the EMAs in
    that case would bury the cause under its symptoms.
    """
    if read.at != frame.at:
        return f"bar open {read.at.isoformat()} live vs {frame.at.isoformat()} replayed"
    if read.bias is not frame.bias:
        return f"bias {read.bias.value} live vs {frame.bias.value} replayed"
    for name in ("ema_fast", "ema_slow", "close"):
        live_value = getattr(read, name)
        replayed = getattr(frame, name)
        if (live_value is None) != (replayed is None):
            return f"{name} {live_value} live vs {replayed} replayed"
        if live_value is None:
            continue
        if abs(live_value - replayed) > EMA_TOLERANCE:
            return f"{name} {live_value!r} live vs {replayed!r} replayed"
    return None


def _pair_mismatch(
    detections: Sequence[Detection], fast: int, slow: int
) -> str | None:
    """A message when a stored context was built from a different EMA pair, else ``None``."""
    for detection in detections:
        stored = detection.mtf
        if stored is None or not stored.reads:
            continue
        if stored.ema_fast_period == 0 and stored.ema_slow_period == 0:
            continue  # a context from before the pair was recorded
        if (stored.ema_fast_period, stored.ema_slow_period) != (fast, slow):
            return (
                f"{detection.detection_id} carries an mtf context built from "
                f"{stored.ema_fast_period}/{stored.ema_slow_period}, and this run would "
                f"recompute it with {fast}/{slow}. Set AUREON_EMA_FAST and AUREON_EMA_SLOW to "
                "the values the session ran with; nothing was compared."
            )
    return None


def preceding_days(market_date: str, days: int) -> list[str]:
    """``market_date`` and the ``days`` calendar days before it, oldest first.

    Calendar days, not trading days: a missing file is simply skipped, so asking for ten covers
    a week of trading including its weekend without this tool needing a market calendar of its
    own. The alternative -- deriving trading days here -- would be a second calendar to keep in
    step with ``config.sessions``.
    """
    from datetime import date, timedelta

    last = date.fromisoformat(market_date)
    return [(last - timedelta(days=offset)).isoformat() for offset in range(days, -1, -1)]


def read_days(
    symbol: str,
    timeframe: Timeframe,
    market_dates: Sequence[str],
    *,
    market_tz: str,
    root: Path | None = None,
) -> tuple[list[Candle], int]:
    """Every archived candle across those dates, oldest first, and how many days were found.

    A missing day is skipped rather than raised on: the buffer this reconstructs is a rolling
    window, and a run that asked for ten days and found six has a shallower H4 than the live
    observer did. That shows up as ``unreadable``, which names buffer depth as the cause --
    rather than as a mismatch, which would blame the arithmetic.
    """
    found: list[Candle] = []
    days = 0
    for market_date in market_dates:
        try:
            day = read_archive(
                symbol, timeframe, market_date, market_tz=market_tz, root=root
            )
        except FileNotFoundError:
            continue
        if day:
            found.extend(day)
            days += 1
    found.sort(key=lambda candle: candle.open_time.utc)
    return found, days


def _run_mtf(args: Any, config: Any, *, symbol: str, timeframe: Timeframe) -> int:
    """The ``--mtf`` path. Its own function because it shares almost nothing with the detection
    diff: a different input window, a different comparison and a different marker."""
    from aureon.storage.detection_repository import DetectionRepository
    from aureon.storage.firebase_service import get_client

    dates = preceding_days(args.market_date, max(0, args.mtf_days))
    candles, days = read_days(
        symbol, timeframe, dates, market_tz=config.market_tz, root=args.archive_dir
    )
    if not candles:
        print(
            f"no archived candles for {symbol} across {dates[0]}..{dates[-1]}",
            file=sys.stderr,
        )
        return 2

    client = get_client(
        project_id=config.firebase_project_id,
        emulator_host=config.firestore_emulator_host,
    )
    # Only the DAY being verified, although the replay window is wider: the earlier days are
    # there to seed the buffer, not to be checked. Checking them would report every detection
    # near the start of the window as unreadable, for the same buffer-depth reason.
    day = [c for c in candles if c.open_time.market_date == args.market_date]
    if not day:
        print(f"the archive holds no candles ON {args.market_date}", file=sys.stderr)
        return 2
    live = [
        d
        for d in DetectionRepository(client).in_period(
            day[0].open_time.utc, day[-1].close_time
        )
        if d.symbol == symbol and d.timeframe is timeframe
    ]

    mismatched_pair = _pair_mismatch(live, config.ema_fast, config.ema_slow)
    if mismatched_pair is not None:
        # Refused rather than compared. The stored context names the pair it was built from, and
        # comparing a 20/50 read against a 9/21 recomputation would report every timeframe as
        # disagreeing -- a true statement about the wrong question, and one that would send
        # somebody looking at the aggregation instead of at their config.
        print(mismatched_pair, file=sys.stderr)
        return 2

    result = compare_mtf(
        live,
        candles,
        market_date=args.market_date,
        symbol=symbol,
        days_replayed=days,
        # The SAME pair the observer ran, from the same config -- not a default written here.
        # A bias from 20/50 and one from 9/21 are different claims (``MtfContext`` stores the
        # pair for exactly that reason), so a comparison against the wrong pair would report
        # every read as disagreeing and blame the archive.
        fast=config.ema_fast,
        slow=config.ema_slow,
        market_tz=config.market_tz,
    )
    print(result.render())
    return 0 if result.identical else 1


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
    parser.add_argument(
        "--mtf",
        action="store_true",
        help=(
            "verify the higher-timeframe context instead of the detections: rebuild the "
            "buffer from several archived days and diff each detection's M15/M30/H1/H4 read "
            "as of its own candle (12, T-12). D1 is excluded -- see MTF_CHECKED"
        ),
    )
    parser.add_argument(
        "--mtf-days",
        type=int,
        default=DEFAULT_MTF_DAYS,
        help=(
            "how many CALENDAR days before the date to read, to rebuild the rolling buffer "
            f"(default {DEFAULT_MTF_DAYS}). An H4 read needs fifty aggregated bars, which is "
            "more than a week of M5; too few days shows up as 'unreadable', not as a mismatch"
        ),
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

    if args.mtf:
        return _run_mtf(args, config, symbol=symbol, timeframe=timeframe)

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
