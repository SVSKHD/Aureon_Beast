"""Historical validation of the live exit manager (V1 item 7).

Drives the SAME ``DeterministicExitManager`` the monitor runs, candle by candle, over the
setups a replay produced, and compares it with a fixed +target take-profit. Every number
is measured from candle highs/lows; a bar that touches both the stop and the target is
counted as the stop (fail-closed), the same convention the money simulation uses.

Reported per policy:

* ``net_move``: sum of realised moves (price units, no costs)
* ``runners_captured``: setups whose MFE reached +20 and which exited at or beyond +10
* ``premature_exits``: exits below +5 on setups whose MFE later reached +10
* ``mfe_captured``: realised / MFE, averaged over setups with MFE > 0
* ``given_back``: average profit given back from the peak
* ``max_drawdown``: worst peak-to-trough of the cumulative realised curve
* ``exits_by_reason``: how the manager closed positions (hard_risk, protective stop, ...)

This is evidence for tuning, not a promise about the future. Parameters are tuned only
from what the grid shows, never invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aureon.management.exit_manager import DeterministicExitManager, ExitPolicy
from aureon.models.base import to_utc
from aureon.models.enums import Direction, ManagementPhase


@dataclass(frozen=True)
class ExitTrade:
    at: str
    direction: str
    entry_price: float
    result: str
    exit_reason: str
    exit_bar: int
    realized_move: float
    mfe: float
    mae: float
    given_back: float
    reached: dict[str, bool] = field(default_factory=dict)


@dataclass
class ExitPolicyResult:
    label: str
    policy: dict[str, Any]
    trades: list[ExitTrade]

    @property
    def summary(self) -> dict[str, Any]:
        n = len(self.trades)
        if n == 0:
            return {"label": self.label, "samples": 0}
        realised = [t.realized_move for t in self.trades]
        wins = [r for r in realised if r > 0]
        losses = [r for r in realised if r <= 0]
        runners = sum(1 for t in self.trades if t.mfe >= 20.0 and t.realized_move >= 10.0)
        premature = sum(1 for t in self.trades if t.mfe >= 10.0 and t.realized_move < 5.0)
        captured = [t.realized_move / t.mfe for t in self.trades if t.mfe > 0]
        curve = 0.0
        peak = 0.0
        drawdown = 0.0
        for value in realised:
            curve += value
            peak = max(peak, curve)
            drawdown = min(drawdown, curve - peak)
        reasons: dict[str, int] = {}
        for t in self.trades:
            reasons[t.result] = reasons.get(t.result, 0) + 1
        return {
            "label": self.label,
            "samples": n,
            "net_move": round(sum(realised), 2),
            "average_move": round(sum(realised) / n, 3),
            "win_rate": round(len(wins) / n, 3),
            "average_win": round(sum(wins) / len(wins), 3) if wins else None,
            "average_loss": round(sum(losses) / len(losses), 3) if losses else None,
            "runners_captured": runners,
            "premature_exits": premature,
            "mfe_captured": round(sum(captured) / len(captured), 3) if captured else None,
            "given_back": round(sum(t.given_back for t in self.trades) / n, 3),
            "max_drawdown": round(drawdown, 2),
            "reached_10": sum(1 for t in self.trades if t.reached.get("reached_10")),
            "reached_20": sum(1 for t in self.trades if t.reached.get("reached_20")),
            "reached_30": sum(1 for t in self.trades if t.reached.get("reached_30")),
            "reached_40": sum(1 for t in self.trades if t.reached.get("reached_40")),
            "exits_by_reason": reasons,
            "policy": self.policy,
        }


def _first_future_index(candles: list[Any], at: str) -> int | None:
    moment = to_utc(datetime.fromisoformat(at))
    for index, candle in enumerate(candles):
        if to_utc(candle.open_time.utc) >= moment:
            return index
    return None


def _bar_prices(bar: Any, direction: Direction) -> tuple[float, float]:
    """(favourable extreme, adverse extreme) of one bar for this direction."""
    if direction is Direction.BUY:
        return float(bar.high), float(bar.low)
    return float(bar.low), float(bar.high)


def simulate_fixed_target(
    rows: list[Any],
    candles: list[Any],
    *,
    target_move: float = 10.0,
    stop_move: float = 7.0,
    hold_bars: int = 864,
) -> ExitPolicyResult:
    """Baseline: a fixed +target take-profit with a fixed stop, no trailing."""
    trades: list[ExitTrade] = []
    for row in rows:
        if not getattr(row, "eligible", False):
            continue
        start = _first_future_index(candles, row.at)
        if start is None:
            continue
        future = candles[start : start + hold_bars]
        if not future:
            continue
        direction = Direction.BUY if row.direction == "buy" else Direction.SELL
        entry = float(row.entry_price)
        mfe = mae = 0.0
        result = "timeout"
        realized = None
        exit_bar = len(future)
        reached = {f"reached_{n}": False for n in (5, 10, 20, 30, 40)}
        for offset, bar in enumerate(future, start=1):
            favourable_price, adverse_price = _bar_prices(bar, direction)
            favourable = max(0.0, (favourable_price - entry) * direction.sign)
            adverse = max(0.0, (entry - adverse_price) * direction.sign)
            mfe, mae = max(mfe, favourable), max(mae, adverse)
            for n in (5, 10, 20, 30, 40):
                if favourable >= n:
                    reached[f"reached_{n}"] = True
            if adverse >= stop_move:  # fail closed on a bar that touches both
                result, realized, exit_bar = "stop", -stop_move, offset
                break
            if favourable >= target_move:
                result, realized, exit_bar = "target", target_move, offset
                break
        if realized is None:
            last = future[-1]
            realized = (float(last.close) - entry) * direction.sign
        trades.append(
            ExitTrade(
                at=row.at,
                direction=row.direction,
                entry_price=entry,
                result=result,
                exit_reason=result,
                exit_bar=exit_bar,
                realized_move=realized,
                mfe=mfe,
                mae=mae,
                given_back=max(0.0, mfe - realized),
                reached=reached,
            )
        )
    return ExitPolicyResult(
        label=f"fixed_tp_{target_move:g}_sl_{stop_move:g}",
        policy={"target_move": target_move, "stop_move": stop_move},
        trades=trades,
    )


def simulate_exit_manager(
    rows: list[Any],
    candles: list[Any],
    *,
    policy: ExitPolicy,
    stop_move: float = 7.0,
    hold_bars: int = 864,
    label: str | None = None,
) -> ExitPolicyResult:
    """Run the live DeterministicExitManager over historical candles.

    The initial stop is ``entry -/+ stop_move`` (the same fixed risk as the baseline) so the
    only difference between the two simulations is what happens after entry. Each bar is
    fed with its high and low so the ratchet, the ladder and the exits see exactly what the
    monitor would have seen from quotes inside that bar; the exit price is the stop level
    (or the bar close on a guardian/structural exit), never a price the bar did not trade.
    """
    manager = DeterministicExitManager(policy)
    trades: list[ExitTrade] = []
    for row in rows:
        if not getattr(row, "eligible", False):
            continue
        start = _first_future_index(candles, row.at)
        if start is None:
            continue
        future = candles[start : start + hold_bars]
        if not future:
            continue
        direction = Direction.BUY if row.direction == "buy" else Direction.SELL
        entry = float(row.entry_price)
        opened_at = to_utc(future[0].open_time.utc)
        state = manager.open(
            entry_price=entry,
            direction=direction,
            initial_stop=entry - direction.sign * stop_move,
            now=opened_at,
        )
        result = "timeout"
        realized = None
        exit_bar = len(future)
        for offset, bar in enumerate(future, start=1):
            stop_before = state.current_stop
            state, decision = manager.assess(
                state,
                current_price=float(bar.close),
                high=float(bar.high),
                low=float(bar.low),
                now=to_utc(getattr(bar, "close_time", bar.open_time).utc)
                if hasattr(getattr(bar, "close_time", bar.open_time), "utc")
                else opened_at,
            )
            if state.phase is ManagementPhase.EXIT_PENDING:
                exit_bar = offset
                priority = state.exit_priority.value
                if priority == "hard_risk" and stop_before is not None:
                    exit_price = stop_before
                    result = "hard_risk"
                elif (
                    priority == "profit_protection"
                    and stop_before is not None
                    and ("protective stop" in (state.exit_reason or ""))
                ):
                    exit_price = stop_before
                    result = "protective_stop"
                else:
                    exit_price = float(bar.close)
                    result = priority
                realized = (exit_price - entry) * direction.sign
                break
        if realized is None:
            realized = (float(future[-1].close) - entry) * direction.sign
            result = "timeout_runner" if state.trail_activated else "timeout"
        trades.append(
            ExitTrade(
                at=row.at,
                direction=row.direction,
                entry_price=entry,
                result=result,
                exit_reason=state.exit_reason or result,
                exit_bar=exit_bar,
                realized_move=realized,
                mfe=state.mfe,
                mae=state.mae,
                given_back=max(0.0, state.mfe - realized),
                reached={
                    f"reached_{n}": getattr(state, f"reached_{n}") for n in (5, 10, 20, 30, 40)
                },
            )
        )
    return ExitPolicyResult(
        label=label
        or (
            f"exit_manager_act{policy.activation_move:g}_lock{policy.minimum_lock:g}"
            f"_trail{policy.trail_fraction_of_peak:g}"
        ),
        policy={**policy.as_dict(), "stop_move": stop_move},
        trades=trades,
    )


def compare_exit_policies(
    rows: list[Any],
    candles: list[Any],
    *,
    stop_move: float = 7.0,
    fixed_target: float = 10.0,
    hold_bars: int = 864,
    policies: list[ExitPolicy] | None = None,
) -> list[dict[str, Any]]:
    """Baseline fixed +target versus the exit manager under each policy. Summaries only."""
    grid = policies or [
        ExitPolicy(),
        ExitPolicy(activation_move=5.0, minimum_lock=3.0, trail_fraction_of_peak=0.45),
        ExitPolicy(activation_move=5.0, minimum_lock=4.0, trail_fraction_of_peak=0.65),
        ExitPolicy(activation_move=7.0, minimum_lock=5.0, trail_fraction_of_peak=0.55),
    ]
    results = [
        simulate_fixed_target(
            rows, candles, target_move=fixed_target, stop_move=stop_move, hold_bars=hold_bars
        )
    ]
    for policy in grid:
        results.append(
            simulate_exit_manager(
                rows, candles, policy=policy, stop_move=stop_move, hold_bars=hold_bars
            )
        )
    return [one.summary for one in results]


def render_exit_comparison(summaries: list[dict[str, Any]]) -> str:
    header = (
        f"{'policy':<40}{'samples':>8}{'net':>9}{'avg':>7}{'win%':>6}{'runners':>8}"
        f"{'premat':>7}{'mfe%':>6}{'giveback':>9}{'maxDD':>8}"
    )
    lines = [header]
    for s in summaries:
        if s.get("samples", 0) == 0:
            lines.append(f"{s['label']:<40}{0:>8}")
            continue
        lines.append(
            f"{s['label']:<40}{s['samples']:>8}{s['net_move']:>9.1f}{s['average_move']:>7.2f}"
            f"{s['win_rate'] * 100:>5.0f}%{s['runners_captured']:>8}{s['premature_exits']:>7}"
            f"{(s['mfe_captured'] or 0) * 100:>5.0f}%{s['given_back']:>9.2f}"
            f"{s['max_drawdown']:>8.1f}"
        )
    lines.append("evidence only: no policy is adopted from this table without a demo/shadow run")
    return "\n".join(lines)
