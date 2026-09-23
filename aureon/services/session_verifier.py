"""What a finished session has to prove before its numbers are used (P-3).

The manual half of §82 asks an operator to look at five things after the close. Every one
of them is a comparison a person can get wrong by being tired, and four of them are
silent when they fail -- an observer that died at 14:00 leaves an archive that looks
perfectly normal, just shorter, and nothing about the stored detections says they stop
three hours early.

So: six checks, the same five-status vocabulary as preflight, and the outputs kept as
evidence blocks rather than summarised into a verdict nobody can re-derive.

## The checks, and the silence each one breaks

1. ``archive`` -- the candles the observer processed exist on disk, and their span is
   reported. A session that lost its archive cannot be compared against a replay at all,
   so the whole of §82 rests on this one file.
2. ``archive_gaps`` -- gaps wider than the evaluation tracker's tolerance, which
   INVALIDATE any horizon spanning them. A session with an hour missing in the middle
   still produces a full-looking outcome table, with the missing hour's detections
   quietly excluded.
3. ``live_vs_replay`` -- ``scripts/compare_live_vs_replay.py``, whose exit code is the
   gate. This is the only check that can tell an engine difference from a broker
   difference.
4. ``detections_stored`` -- Firestore actually holds them. The observer can emit
   detections that never leave the outbox.
5. ``outbox_drained`` -- and if it does not, the count is the finding. Pending rows mean
   detections were produced and never stored, which makes every count in the document a
   lower bound.
6. ``observer_ran_to_the_close`` -- the observer's heartbeat against the last archived
   candle. This is the one that catches a process that died at lunchtime.

Nothing here writes to cloud storage: it reads through ``PeriodReader``, which has no write
method, so a verification cannot alter what it is verifying.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from aureon.config.config import AureonConfig
from aureon.models.base import utc_now
from aureon.models.enums import Timeframe
from aureon.services.checks import CheckReport, CheckResult, Status
from aureon.services.session_evidence import Block

#: A gap wider than this many timeframes invalidates an evaluation horizon spanning it
#: (§22). The same number the outcome tracker uses, named here so the two cannot drift:
#: a verification that tolerated a wider gap than the tracker would report a clean
#: session whose outcomes were silently thinned.
GAP_TOLERANCE_TIMEFRAMES = 2

#: How long after the last archived candle the observer's heartbeat may lag before the
#: session is treated as having ended early. Two candles: one for the normal gap between
#: a candle close and the next heartbeat, one for slack.
HEARTBEAT_TOLERANCE_CANDLES = 2


@dataclass
class Comparison:
    """The result of running the live-vs-replay script."""

    exit_code: int
    output: str


@dataclass
class SessionVerifier:
    """Runs the post-session checks. Every input is injectable, as in ``Preflight``."""

    config: AureonConfig
    market_date: str
    symbol: str
    timeframe: Timeframe
    archive_dir: Path | None = None
    outbox_path: Path | None = None
    client_factory: Callable[[], Any] | None = None
    comparison_runner: Callable[[], Comparison] | None = None
    #: 12 T-12. The MTF comparison, injected like the one above. ``None`` SKIPs the check
    #: rather than defaulting to a run: it needs several archived days, and a verifier that
    #: silently read ten days of history when the caller asked about one would be doing
    #: something the caller did not ask for and would not see in the output.
    mtf_runner: Callable[[], Comparison] | None = None
    now: Callable[[], datetime] = utc_now
    blocks: list[Block] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._candles: list[Any] = []
        self._detections: list[Any] = []
        self._evaluations: dict[str, Any] = {}
        self._client: Any | None = None
        if self.outbox_path is None:
            self.outbox_path = Path(self.config.outbox_path)

    # ── Wiring ────────────────────────────────────────────────────────────────

    def _client_or_none(self) -> Any | None:
        if self._client is None:
            factory = self.client_factory or self._default_client
            self._client = factory()
        return self._client

    def _default_client(self) -> Any:
        from aureon.storage.runtime import build_storage

        return build_storage(
            account_scope=self.config.account_scope,
            state_heartbeat_seconds=self.config.state_heartbeat_seconds,
        )

    def _default_comparison(self) -> Comparison:
        """Invoke the comparison script in-process and keep its whole output.

        In-process rather than as a subprocess so a traceback lands in the evidence file
        instead of in a shell nobody kept, and so a test can drive it.
        """
        import contextlib
        import io

        from scripts.compare_live_vs_replay import main as compare_main

        argv = [self.market_date, "--symbol", self.symbol,
                "--timeframe", self.timeframe.value]
        if self.archive_dir is not None:
            argv += ["--archive-dir", str(self.archive_dir)]
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = compare_main(argv)
        except Exception as exc:  # a crash is a result, and belongs in the document
            return Comparison(
                exit_code=3, output=f"{out.getvalue()}{err.getvalue()}\n{exc!r}"
            )
        return Comparison(exit_code=code, output=out.getvalue() + err.getvalue())

    # ── The run ───────────────────────────────────────────────────────────────

    def run(self) -> CheckReport:
        report = CheckReport()
        for check in (
            self.check_archive,
            self.check_archive_gaps,
            self.check_live_vs_replay,
            self.check_mtf_replay,
            self.check_detections_stored,
            self.check_outbox_drained,
            self.check_observer_ran_to_the_close,
        ):
            report.results.append(check())
        self.blocks.extend(self._outcome_blocks())
        return report

    # ── Checks ────────────────────────────────────────────────────────────────

    def check_archive(self) -> CheckResult:
        from aureon.data.live_candle_archive import archive_path, read_archive

        path = archive_path(
            self.symbol, self.timeframe, self.market_date, root=self.archive_dir
        )
        try:
            self._candles = read_archive(
                self.symbol,
                self.timeframe,
                self.market_date,
                market_tz=self.config.market_tz,
                root=self.archive_dir,
            )
        except FileNotFoundError:
            return CheckResult(
                "archive",
                Status.FAIL,
                f"{path} does not exist",
                remedy=(
                    "Without the archive the live path cannot be compared to a replay at "
                    "all (§82). A hard kill loses the day's unflushed tail; only a clean "
                    "SIGTERM flushes it."
                ),
            )
        except Exception as exc:
            return CheckResult(
                "archive", Status.FAIL, f"{path}: {type(exc).__name__}: {exc}"
            )
        if not self._candles:
            return CheckResult("archive", Status.FAIL, f"{path} holds no candles")

        first, last = self._candles[0], self._candles[-1]
        span_hours = (
            last.close_time - first.open_time.utc
        ).total_seconds() / 3600
        return CheckResult(
            "archive",
            Status.PASS,
            f"{path} — {len(self._candles)} candles, "
            f"{first.open_time.utc.isoformat()} … {last.close_time.isoformat()} "
            f"({span_hours:.1f}h)",
        )

    def check_archive_gaps(self) -> CheckResult:
        """Gaps that would invalidate an evaluation horizon (§22).

        Reported, never repaired. A thin-liquidity minute and a broker hiccup look the
        same from here, and both legitimately produce INVALID horizons -- what matters is
        that a reader of the outcome table knows how much of the day was unobserved.
        """
        if not self._candles:
            return CheckResult(
                "archive_gaps", Status.SKIP, "not checked: no archive to read"
            )
        tolerance = self.timeframe.seconds * GAP_TOLERANCE_TIMEFRAMES
        gaps = [
            (a.close_time, b.open_time.utc, (b.open_time.utc - a.close_time).total_seconds())
            for a, b in zip(self._candles, self._candles[1:], strict=False)
            if (b.open_time.utc - a.close_time).total_seconds() > tolerance
        ]
        if not gaps:
            return CheckResult(
                "archive_gaps",
                Status.PASS,
                f"no gap wider than {GAP_TOLERANCE_TIMEFRAMES} candles",
            )
        widest = max(gaps, key=lambda g: g[2])
        self.blocks.append(
            Block(
                title="Gaps in the archive",
                body="\n".join(
                    f"{start.isoformat()} → {end.isoformat()}  ({seconds / 60:.0f} min)"
                    for start, end, seconds in gaps
                ),
            )
        )
        return CheckResult(
            "archive_gaps",
            Status.WARN,
            f"{len(gaps)} gap(s) wider than {GAP_TOLERANCE_TIMEFRAMES} candles; widest "
            f"{widest[2] / 60:.0f} min at {widest[0].isoformat()}",
            remedy=(
                "Every evaluation horizon spanning a gap is INVALID, not COMPLETE, so "
                "the outcome table below covers less of the day than its counts suggest."
            ),
        )

    def check_live_vs_replay(self) -> CheckResult:
        """The §82 comparison. Its exit code is the gate, its output is the evidence."""
        if not self._candles:
            return CheckResult(
                "live_vs_replay",
                Status.SKIP,
                "not run: there is no archive to replay",
                remedy="Fix the archive check first. Nothing was compared.",
            )
        runner = self.comparison_runner or self._default_comparison
        result = runner()
        self.blocks.append(
            Block(
                title="Live vs replay",
                body=result.output,
                command=f"python scripts/compare_live_vs_replay.py {self.market_date}",
            )
        )
        if result.exit_code == 0:
            return CheckResult(
                "live_vs_replay", Status.PASS, "IDENTICAL (exit 0)"
            )
        if result.exit_code == 1:
            return CheckResult(
                "live_vs_replay",
                Status.FAIL,
                "differences found (exit 1)",
                remedy=(
                    "Read the three kinds separately: missing points at the outbox or the "
                    "window, extra usually at a leftover agent_version, mismatched at "
                    "something writing a detection the engine would not produce. The "
                    "table in docs/MT5_SESSION_CHECKLIST.md says where to look first."
                ),
            )
        return CheckResult(
            "live_vs_replay",
            Status.FAIL,
            f"the comparison could not run (exit {result.exit_code})",
            remedy="Its output is in the block below; nothing was compared.",
        )

    def check_mtf_replay(self) -> CheckResult:
        """The 12 T-12 comparison: does the recorded higher-timeframe context reproduce?

        A SEPARATE check from ``live_vs_replay``, not a widening of it. The detection comparison
        replays one archived day and deliberately ignores the ``mtf`` block, because a read built
        from a rolling multi-day buffer cannot be reproduced from one day. This reads several and
        answers the other question.

        SKIP rather than FAIL when it cannot run. A session verified before this check existed is
        not retroactively unverified, and an archive that does not reach back far enough is a
        statement about the archive rather than about the observer -- which is why a shallow
        window reports "unreadable" in the tool's own output instead of disagreements.
        """
        runner = self.mtf_runner
        if runner is None:
            return CheckResult(
                "mtf_replay",
                Status.SKIP,
                "not run: no MTF comparison was wired in",
                remedy=(
                    "python scripts/compare_live_vs_replay.py "
                    f"{self.market_date} --mtf, by hand."
                ),
            )
        result = runner()
        self.blocks.append(
            Block(
                title="MTF context vs replay",
                body=result.output,
                command=(
                    f"python scripts/compare_live_vs_replay.py {self.market_date} --mtf"
                ),
            )
        )
        if result.exit_code == 0:
            return CheckResult("mtf_replay", Status.PASS, "IDENTICAL (exit 0)")
        if result.exit_code == 1:
            return CheckResult(
                "mtf_replay",
                Status.FAIL,
                "the recorded context does not reproduce (exit 1)",
                remedy=(
                    "Read the disagreements in the block below. A differing BAR OPEN means the "
                    "live buffer and the archive disagree about which bar was last closed; "
                    "differing EMAs with the same bar means something wrote a read the "
                    "aggregation would not produce. 'unreadable' is neither -- it is buffer "
                    "depth, so widen --mtf-days."
                ),
            )
        return CheckResult(
            "mtf_replay",
            Status.SKIP,
            f"the MTF comparison could not run (exit {result.exit_code})",
            remedy="Its output is in the block below; nothing was compared.",
        )

    def check_detections_stored(self) -> CheckResult:
        """Local application storage holds the day's detections, and how many of each agent.

        Separate from the comparison on purpose: the comparison answers "do live and
        replay agree", which is also satisfied by both producing nothing.
        """
        from collections import Counter

        from aureon.reviews.periods import day_period
        period = day_period(self.market_date, self.config.market_tz)
        try:
            storage = self._client_or_none()
            reader = storage.period_reader
            self._detections = [
                d
                for d in reader.detections_in(period.start, period.end)
                if d.symbol == self.symbol and d.timeframe is self.timeframe
            ]
            self._evaluations = reader.evaluations_for(
                self._detections, self._rule_id()
            )
        except Exception as exc:
            self._client = None
            return CheckResult(
                "detections_stored",
                Status.FAIL,
                f"{type(exc).__name__}: {exc}",
                remedy="Nothing was read, so no count below is evidence of anything.",
            )

        by_agent = Counter(d.agent_name for d in self._detections)
        detail = (
            f"{len(self._detections)} detections, {len(self._evaluations)} evaluated "
            f"under {self._rule_id()} "
            f"({', '.join(f'{a}={n}' for a, n in sorted(by_agent.items())) or 'none'})"
        )
        if not self._detections:
            return CheckResult(
                "detections_stored",
                Status.FAIL,
                detail,
                remedy=(
                    "A whole session that stored nothing is a failure even if every "
                    "other check passes: there is nothing to have been right about."
                ),
            )
        directional = sum(1 for d in self._detections if d.direction is not None)
        if directional and not self._evaluations:
            return CheckResult(
                "detections_stored",
                Status.WARN,
                f"{detail} — {directional} directional detections carry no evaluation",
                remedy=(
                    "Outcomes are written as horizons resolve, so a session verified "
                    "immediately after the close legitimately has few. Re-run tomorrow; "
                    "if they are still absent, the tracker never ran."
                ),
            )
        return CheckResult("detections_stored", Status.PASS, detail)

    def check_outbox_drained(self) -> CheckResult:
        from aureon.outbox.local_outbox import LocalOutbox

        path = self.outbox_path or Path(self.config.outbox_path)
        try:
            with LocalOutbox(path) as outbox:
                pending = outbox.pending_count()
                delivered = outbox.delivered_count()
        except Exception as exc:
            return CheckResult(
                "outbox_drained", Status.FAIL, f"{path}: {type(exc).__name__}: {exc}"
            )
        detail = f"{path} — {pending} pending, {delivered} delivered"
        if pending:
            return CheckResult(
                "outbox_drained",
                Status.FAIL,
                detail,
                remedy=(
                    f"{pending} detection(s) never reached local storage, so every stored "
                    "count in this document is a lower bound and the comparison above "
                    "will report them as missing."
                ),
            )
        return CheckResult("outbox_drained", Status.PASS, detail)

    def check_observer_ran_to_the_close(self) -> CheckResult:
        """The heartbeat against the last archived candle.

        The failure this catches leaves no other trace: a process that died at 14:00
        produces an archive, detections and outcomes that all look normal and simply stop.
        """
        from aureon.storage import paths
        if not self._candles:
            return CheckResult(
                "observer_ran_to_the_close",
                Status.SKIP,
                "not checked: no archive to compare a heartbeat against",
            )
        try:
            beat = self._client_or_none().heartbeats.read(paths.SERVICE_OBSERVER)
        except Exception as exc:
            return CheckResult(
                "observer_ran_to_the_close",
                Status.FAIL,
                f"{type(exc).__name__}: {exc}",
            )
        last_candle = self._candles[-1].close_time
        if beat is None:
            return CheckResult(
                "observer_ran_to_the_close",
                Status.WARN,
                f"no {paths.heartbeat_path(paths.SERVICE_OBSERVER)} document",
                remedy=(
                    "Nothing says when the observer stopped, so an early death cannot be "
                    "ruled out from here. The archive's last candle is "
                    f"{last_candle.isoformat()}."
                ),
            )
        lag = (last_candle - beat.updated_at).total_seconds()
        tolerance = self.timeframe.seconds * HEARTBEAT_TOLERANCE_CANDLES
        detail = (
            f"last heartbeat {beat.updated_at.isoformat()}, last candle "
            f"{last_candle.isoformat()} ({lag / 60:+.0f} min)"
        )
        if lag > tolerance:
            return CheckResult(
                "observer_ran_to_the_close",
                Status.FAIL,
                f"{detail} — the observer stopped beating before its last candle",
                remedy=(
                    "The archive extends past the last heartbeat, which means the "
                    "process was gone while candles were still arriving. Everything "
                    "after that point is missing from local storage, not from the market."
                ),
            )
        return CheckResult("observer_ran_to_the_close", Status.PASS, detail)

    # ── Evidence ──────────────────────────────────────────────────────────────

    def _outcome_blocks(self) -> list[Block]:
        """The day's outcomes, per agent and per session, from what was STORED.

        From the stored evaluations rather than a replay: a replay would report what the
        engine would produce, and that is the other half of the comparison above, not
        this one.
        """
        if not self._detections:
            return []
        from aureon.evaluation.outcome_report import aggregate, render_markdown
        from aureon.evaluation.rules import get_rule

        try:
            rule = get_rule(self._rule_id())
        except KeyError:
            return []
        report = aggregate(
            self._detections,
            self._evaluations,
            rule,
            point=self._point(),
        )
        rendered = "\n".join(render_markdown(report)).strip()
        if not rendered:
            return []
        return [
            Block(
                title=f"Outcomes — {rule.rule_id}",
                body=rendered,
                command=(
                    f"python scripts/report_outcomes.py --rule {rule.rule_id} "
                    f"--from {self.market_date} --to {self.market_date}"
                ),
                markdown=True,
            )
        ]

    def _rule_id(self) -> str:
        """THIS symbol's rule (9A).

        Looking up gold's rule for a silver session would find no evaluations at all and
        report "0 evaluated under XAU_OUTCOME_V2" -- a verdict about the wrong rule, which
        reads as a broken evaluator rather than as a verifier asking the wrong question.
        """
        return self.config.rule_id_for(self.symbol)

    def _point(self) -> float:
        """The symbol's tick, from the tuning table rather than a hard-coded 0.01."""
        from aureon.config.symbol_tuning import tuning_for

        return tuning_for(self.symbol).point
