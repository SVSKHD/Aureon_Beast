"""Independent diagnostics/backtest for an exact persisted Aureon V1 model artifact.

Never refits or mutates lifecycle state. The payoff summary is deliberately transparent:
selected clean_10 outcomes receive +target; other selected outcomes receive -stop. This is
not a tick-level execution simulation; exit-policy replay remains the path-dependent test.
"""
from __future__ import annotations

from statistics import mean, median
from typing import Any

from aureon.services.v1_model_training import evaluate_v1_artifact

DIAGNOSTIC_THRESHOLDS = (0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60)


def _probability(row: dict[str, Any]) -> float:
    return float((row.get("probabilities") or {}).get("clean_10", 0.0))


def _actual_clean(row: dict[str, Any]) -> bool:
    return bool((row.get("actual") or {}).get("clean_10"))


def _payoff(rows: list[dict[str, Any]], target_move: float, stop_move: float) -> dict[str, Any]:
    wins = sum(_actual_clean(row) for row in rows)
    losses = len(rows) - wins
    gp, gl = wins * target_move, losses * stop_move
    net = gp - gl
    equity = peak = max_dd = 0.0
    for row in sorted(rows, key=lambda one: (str(one.get("timestamp") or ""), str(one.get("setup_id") or ""))):
        equity += target_move if _actual_clean(row) else -stop_move
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(rows) if rows else 0.0,
        "gross_profit_move": gp,
        "gross_loss_move": gl,
        "net_move": net,
        "profit_factor": gp / gl if gl else (float("inf") if gp else 0.0),
        "max_drawdown_move": max_dd,
        "average_move_per_trade": net / len(rows) if rows else 0.0,
    }


def _period_classification(model: Any, start: str, end: str) -> str:
    trained_through = str(getattr(model, "trained_through", "") or "")
    if not trained_through:
        return "UNKNOWN"
    if end <= trained_through:
        return "IN_SAMPLE"
    if start > trained_through:
        return "OUT_OF_SAMPLE"
    return "OVERLAPS_TRAINING"


def backtest_saved_model(
    *,
    models: Any,
    training_memory: Any,
    symbol: str,
    model_id: str,
    start_market_date: str,
    end_market_date: str,
    threshold: float = 0.55,
    target_move: float = 10.0,
    stop_move: float = 7.0,
    usd_per_move: float | None = None,
) -> dict[str, Any]:
    if not 0 <= threshold <= 1:
        raise ValueError("threshold must be between 0 and 1")
    if target_move <= 0 or stop_move <= 0:
        raise ValueError("target_move and stop_move must be positive")
    if usd_per_move is not None and usd_per_move <= 0:
        raise ValueError("usd_per_move must be positive")

    symbol = symbol.upper()
    model = models.get_model(model_id)
    if model is None:
        raise ValueError(f"model not found: {model_id}")
    if model.symbol.upper() != symbol:
        raise ValueError(f"{model_id} belongs to {model.symbol}, not {symbol}")

    examples = list(training_memory.canonical_between(symbol, start_market_date, end_market_date))
    if not examples:
        raise ValueError(f"{symbol}: no canonical examples in {start_market_date}..{end_market_date}")

    evaluation = evaluate_v1_artifact(model, examples)
    rows = list(evaluation["scored_rows"])
    selected = [row for row in rows if _probability(row) >= threshold]
    payoff = _payoff(selected, target_move, stop_move)

    baseline_wins = sum(_actual_clean(row) for row in rows)
    baseline_rate = baseline_wins / len(rows) if rows else 0.0
    probabilities = [_probability(row) for row in rows]
    probability_summary = {
        "min": min(probabilities) if probabilities else None,
        "mean": mean(probabilities) if probabilities else None,
        "median": median(probabilities) if probabilities else None,
        "max": max(probabilities) if probabilities else None,
    }

    threshold_sweep = []
    for cut in DIAGNOSTIC_THRESHOLDS:
        cut_rows = [row for row in rows if _probability(row) >= cut]
        stats = _payoff(cut_rows, target_move, stop_move)
        stats.update({
            "threshold": cut,
            "lift_vs_baseline": stats["win_rate"] - baseline_rate if cut_rows else None,
        })
        threshold_sweep.append(stats)

    reached = {
        target: sum(bool((row.get("actual") or {}).get(target)) for row in selected)
        for target in ("reach_5", "reach_10", "reach_20", "reach_30", "reach_40")
    }

    # mae_before_10 is the clean-entry metric: adverse excursion only until the
    # first +10 event. max_adverse_move is deliberately retained separately as
    # the full-horizon diagnostic and must not be interpreted as entry MAE.
    mae_before_10 = [
        float(row["mae_before_10"])
        for row in selected
        if row.get("mae_before_10") is not None
    ]
    full_horizon_mae = [
        float(row["max_adverse_move"])
        for row in selected
        if row.get("max_adverse_move") is not None
    ]
    mfes = [
        float(row["max_favourable_move"])
        for row in selected
        if row.get("max_favourable_move") is not None
    ]

    top_predictions = []
    for row in sorted(rows, key=_probability, reverse=True)[:10]:
        top_predictions.append({
            "setup_id": row.get("setup_id"),
            "timestamp": row.get("timestamp"),
            "probability": _probability(row),
            "actual_clean_10": _actual_clean(row),
            "mae_before_10": row.get("mae_before_10"),
            "max_favourable_move": row.get("max_favourable_move"),
            "max_adverse_move": row.get("max_adverse_move"),
        })

    report = {
        "model_id": model.model_id,
        "model_status": model.status,
        "algorithm": model.algorithm,
        "symbol": symbol,
        "period": {"from": start_market_date, "to": end_market_date},
        "period_classification": _period_classification(model, start_market_date, end_market_date),
        "trained_from": getattr(model, "trained_from", None),
        "trained_through": getattr(model, "trained_through", None),
        "threshold": threshold,
        "samples_scored": evaluation["samples"],
        **payoff,
        **reached,
        "baseline_clean_10": baseline_wins,
        "baseline_clean_10_rate": baseline_rate,
        "selected_lift_vs_baseline": payoff["win_rate"] - baseline_rate if selected else None,
        "probability_summary": probability_summary,
        "threshold_sweep": threshold_sweep,
        "top_predictions": top_predictions,
        "average_mae_before_10": mean(mae_before_10) if mae_before_10 else None,
        "average_full_horizon_mae": mean(full_horizon_mae) if full_horizon_mae else None,
        "average_mfe": mean(mfes) if mfes else None,
        # Backward-compatible key, now explicitly the correct clean-entry MAE.
        "average_mae": mean(mae_before_10) if mae_before_10 else None,
        "clean_10_metrics": (evaluation.get("metrics") or {}).get("clean_10"),
        "payoff_assumption": f"+{target_move:g} for clean_10, -{stop_move:g} otherwise",
        "model_status_unchanged": True,
    }

    if usd_per_move is not None:
        report.update({
            "usd_per_move": usd_per_move,
            "gross_profit_usd": payoff["gross_profit_move"] * usd_per_move,
            "gross_loss_usd": payoff["gross_loss_move"] * usd_per_move,
            "net_pnl_usd": payoff["net_move"] * usd_per_move,
            "max_drawdown_usd": payoff["max_drawdown_move"] * usd_per_move,
            "average_pnl_usd": payoff["average_move_per_trade"] * usd_per_move,
        })
    return report


def _pf(value: float) -> str:
    return "inf" if value == float("inf") else f"{value:.2f}"


def _num(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def render_saved_model_backtest(r: dict[str, Any]) -> str:
    p = r["probability_summary"]
    lines = [
        "SAVED MODEL BACKTEST",
        "-" * 72,
        f'Model:          {r["model_id"]}',
        f'Status:         {str(r["model_status"]).upper()} (unchanged)',
        f'Algorithm:      {r["algorithm"]}',
        f'Period:         {r["period"]["from"]} .. {r["period"]["to"]}',
        f'Period class:   {r["period_classification"]}',
        f'Trained:        {r.get("trained_from") or "?"} .. {r.get("trained_through") or "?"}',
        f'Decision:       P(clean_10) >= {r["threshold"]:.2f}',
        f'Scored setups:  {r["samples_scored"]}',
        f'Trades:         {r["trades"]}',
        f'Wins/Losses:    {r["wins"]} / {r["losses"]}',
        f'Win rate:       {r["win_rate"] * 100:.2f}%',
        "",
        f'Baseline clean: {r["baseline_clean_10"]}/{r["samples_scored"]} ({r["baseline_clean_10_rate"] * 100:.2f}%)',
        "Selected lift:  " + ("n/a" if r["selected_lift_vs_baseline"] is None else f'{r["selected_lift_vs_baseline"] * 100:+.2f} pp'),
        f'P(clean_10):    min={_num(p["min"])} mean={_num(p["mean"])} median={_num(p["median"])} max={_num(p["max"])}',
        "",
        "THRESHOLD DIAGNOSTICS",
        f'{"Cut":>5} {"Trades":>7} {"Clean":>7} {"Win%":>8} {"Lift":>9} {"Net":>8} {"PF":>7}',
    ]
    for row in r["threshold_sweep"]:
        lift = "n/a" if row["lift_vs_baseline"] is None else f'{row["lift_vs_baseline"] * 100:+.1f}pp'
        lines.append(
            f'{row["threshold"]:>5.2f} {row["trades"]:>7} {row["wins"]:>7} '
            f'{row["win_rate"] * 100:>7.1f}% {lift:>9} {row["net_move"]:>+8.1f} {_pf(row["profit_factor"]):>7}'
        )

    lines += [
        "",
        f'Gross +move:    +{r["gross_profit_move"]:.2f}',
        f'Gross -move:    -{r["gross_loss_move"]:.2f}',
        f'NET move:       {r["net_move"]:+.2f}',
        f'Profit factor:  {_pf(r["profit_factor"])}',
        f'Max drawdown:   {r["max_drawdown_move"]:.2f}',
        f'Average/trade:  {r["average_move_per_trade"]:+.2f}',
        "",
        f'+5 reached:     {r["reach_5"]}',
        f'+10 reached:    {r["reach_10"]}',
        f'+20 reached:    {r["reach_20"]}',
        f'+30 reached:    {r["reach_30"]}',
        f'+40 reached:    {r["reach_40"]}',
        f'MAE before +10: {r["average_mae_before_10"]}',
        f'Full-horizon MAE:{r["average_full_horizon_mae"]}',
        f'Average MFE:    {r["average_mfe"]}',
        "",
        "TOP CLEAN_10 PREDICTIONS",
        f'{"P":>6} {"Actual":>7} {"MAE<10":>9} {"MFE":>9}  Timestamp / setup',
    ]
    for row in r["top_predictions"]:
        lines.append(
            f'{row["probability"]:>6.3f} {str(row["actual_clean_10"]):>7} '
            f'{_num(row["mae_before_10"], 2):>9} {_num(row["max_favourable_move"], 2):>9}  '
            f'{row.get("timestamp") or row.get("setup_id") or "?"}'
        )

    if "net_pnl_usd" in r:
        lines += [
            "",
            f'USD per $1 move:{r["usd_per_move"]:.2f}',
            f'Gross profit:   $ {r["gross_profit_usd"]:.2f}',
            f'Gross loss:    -$ {r["gross_loss_usd"]:.2f}',
            f'NET P&L:        $ {r["net_pnl_usd"]:+.2f}',
            f'Max DD:         $ {r["max_drawdown_usd"]:.2f}',
            f'Avg P&L/trade:  $ {r["average_pnl_usd"]:+.2f}',
        ]
    else:
        lines += ["", "USD P&L:        not calculated (pass --usd-per-move to value price moves)"]

    lines += [
        "",
        "NOTE: MAE before +10 is the clean-entry excursion metric.",
        "      Full-horizon MAE may include movement long after +10 was reached.",
        "      Payoff uses canonical clean_10 labels, not tick-level execution.",
        "      Backtesting does not promote, reject, refit, or mutate the model.",
    ]
    return "\n".join(lines)
