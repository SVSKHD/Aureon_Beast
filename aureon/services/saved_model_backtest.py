"""Independent backtest for an exact persisted Aureon V1 model artifact.

Never refits or mutates lifecycle state. The payoff summary is deliberately transparent:
selected clean_10 outcomes receive +target; other selected outcomes receive -stop. This is
not a tick-level execution simulation; exit-policy replay remains the path-dependent test.
"""
from __future__ import annotations
from typing import Any
from aureon.services.v1_model_training import evaluate_v1_artifact

def backtest_saved_model(*, models: Any, training_memory: Any, symbol: str, model_id: str,
    start_market_date: str, end_market_date: str, threshold: float = 0.55,
    target_move: float = 10.0, stop_move: float = 7.0,
    usd_per_move: float | None = None) -> dict[str, Any]:
    if not 0 <= threshold <= 1: raise ValueError("threshold must be between 0 and 1")
    if target_move <= 0 or stop_move <= 0: raise ValueError("target_move and stop_move must be positive")
    if usd_per_move is not None and usd_per_move <= 0: raise ValueError("usd_per_move must be positive")
    symbol = symbol.upper()
    model = models.get_model(model_id)
    if model is None: raise ValueError(f"model not found: {model_id}")
    if model.symbol.upper() != symbol: raise ValueError(f"{model_id} belongs to {model.symbol}, not {symbol}")
    examples = list(training_memory.canonical_between(symbol, start_market_date, end_market_date))
    if not examples: raise ValueError(f"{symbol}: no canonical examples in {start_market_date}..{end_market_date}")
    evaluation = evaluate_v1_artifact(model, examples)
    selected = [r for r in evaluation["scored_rows"] if float((r.get("probabilities") or {}).get("clean_10", 0)) >= threshold]
    wins = sum(bool((r.get("actual") or {}).get("clean_10")) for r in selected)
    losses = len(selected) - wins
    gp, gl = wins * target_move, losses * stop_move
    net = gp - gl
    equity = peak = max_dd = 0.0
    for r in selected:
        equity += target_move if bool((r.get("actual") or {}).get("clean_10")) else -stop_move
        peak = max(peak, equity); max_dd = max(max_dd, peak - equity)
    reached = {t: sum(bool((r.get("actual") or {}).get(t)) for r in selected)
               for t in ("reach_5","reach_10","reach_20","reach_30","reach_40")}
    maes=[float(r["max_adverse_move"]) for r in selected if r.get("max_adverse_move") is not None]
    mfes=[float(r["max_favourable_move"]) for r in selected if r.get("max_favourable_move") is not None]
    report={"model_id":model.model_id,"model_status":model.status,"algorithm":model.algorithm,
      "symbol":symbol,"period":{"from":start_market_date,"to":end_market_date},"threshold":threshold,
      "samples_scored":evaluation["samples"],"trades":len(selected),"wins":wins,"losses":losses,
      "win_rate":wins/len(selected) if selected else 0.0,"gross_profit_move":gp,"gross_loss_move":gl,
      "net_move":net,"profit_factor":gp/gl if gl else (float("inf") if gp else 0.0),
      "max_drawdown_move":max_dd,"average_move_per_trade":net/len(selected) if selected else 0.0,
      "average_mae":sum(maes)/len(maes) if maes else None,"average_mfe":sum(mfes)/len(mfes) if mfes else None,
      **reached,"clean_10_metrics":(evaluation.get("metrics") or {}).get("clean_10"),
      "payoff_assumption":f"+{target_move:g} for clean_10, -{stop_move:g} otherwise","model_status_unchanged":True}
    if usd_per_move is not None:
        report.update({"usd_per_move":usd_per_move,"gross_profit_usd":gp*usd_per_move,
          "gross_loss_usd":gl*usd_per_move,"net_pnl_usd":net*usd_per_move,
          "max_drawdown_usd":max_dd*usd_per_move,
          "average_pnl_usd":report["average_move_per_trade"]*usd_per_move})
    return report

def render_saved_model_backtest(r: dict[str, Any]) -> str:
    pf="inf" if r["profit_factor"] == float("inf") else f'{r["profit_factor"]:.2f}'
    lines=["SAVED MODEL BACKTEST","-"*64,f'Model:          {r["model_id"]}',
      f'Status:         {str(r["model_status"]).upper()} (unchanged)',f'Algorithm:      {r["algorithm"]}',
      f'Period:         {r["period"]["from"]} .. {r["period"]["to"]}',f'Decision:       P(clean_10) >= {r["threshold"]:.2f}',
      f'Scored setups:  {r["samples_scored"]}',f'Trades:         {r["trades"]}',f'Wins/Losses:    {r["wins"]} / {r["losses"]}',
      f'Win rate:       {r["win_rate"]*100:.2f}%',"",f'Gross +move:    +{r["gross_profit_move"]:.2f}',
      f'Gross -move:    -{r["gross_loss_move"]:.2f}',f'NET move:       {r["net_move"]:+.2f}',
      f'Profit factor:  {pf}',f'Max drawdown:   {r["max_drawdown_move"]:.2f}',f'Average/trade:  {r["average_move_per_trade"]:+.2f}',"",
      f'+5 reached:     {r["reach_5"]}',f'+10 reached:    {r["reach_10"]}',f'+20 reached:    {r["reach_20"]}',
      f'+30 reached:    {r["reach_30"]}',f'+40 reached:    {r["reach_40"]}',f'Average MAE:    {r["average_mae"]}',f'Average MFE:    {r["average_mfe"]}']
    if "net_pnl_usd" in r:
        lines += ["",f'USD per $1 move:{r["usd_per_move"]:.2f}',f'Gross profit:   $ {r["gross_profit_usd"]:.2f}',
          f'Gross loss:    -$ {r["gross_loss_usd"]:.2f}',f'NET P&L:        $ {r["net_pnl_usd"]:+.2f}',
          f'Max DD:         $ {r["max_drawdown_usd"]:.2f}',f'Avg P&L/trade:  $ {r["average_pnl_usd"]:+.2f}']
    else: lines += ["","USD P&L:        not calculated (pass --usd-per-move to value price moves)"]
    lines += ["","NOTE: payoff summary uses canonical clean_10 labels, not tick-level execution.",
      "      Backtesting does not promote, reject, refit, or otherwise mutate the model."]
    return "\n".join(lines)
