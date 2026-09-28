#!/usr/bin/env python3
"""JAN23 directional EMA-cross vs re-entry move-path research."""
from __future__ import annotations
import argparse,json,sys
from datetime import timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from aureon.config import AureonConfig
from aureon.models.base import to_utc
from aureon.models.enums import Timeframe
from aureon.services.jan23_reentry_move_research import trace_signal,summarize
from main_backtest import _fetch_mt5_chunked
from scripts.phase5_jan23_pipeline import _build_signals,_json,_jsonl,_phase2_crosses,_stamp,_verify_phase4

def main():
 p=argparse.ArgumentParser(); p.add_argument("--artifacts",default="artifacts")
 p.add_argument("--hold-bars",type=int,default=288,help="M5 bars; default 24 trading hours")
 p.add_argument("--output",default="artifacts/jan23_reentry_move_research.json")
 a=p.parse_args(); root=Path(a.artifacts)
 phase2=_jsonl(root/"phase2_sequences.jsonl"); train=_jsonl(root/"phase3_jan23_train.jsonl")
 test=_jsonl(root/"phase3_jan23_test.jsonl"); phase4=_json(root/"phase4_jan23_challenger_report.json")
 model=_verify_phase4(phase4,train,test); signals=_build_signals(test,_phase2_crosses(phase2),model)
 selected={"ema_cross":signals["original_cross_immediate"],"reentry":signals["wait_confirmation"]}
 flat=[x for rows in selected.values() for x in rows]
 start=min(_stamp(x["timestamp"]) for x in flat)-timedelta(minutes=10)
 end=max(_stamp(x["timestamp"]) for x in flat)+timedelta(minutes=5*(a.hold_bars+10))
 c=AureonConfig.from_env()
 from aureon.data.mt5_provider import MT5DataProvider
 provider=MT5DataProvider(market_tz=c.market_tz,login=c.mt5_login,password=c.mt5_password,server=c.mt5_server,terminal_path=c.mt5_terminal_path)
 print("Fetching raw XAUUSD M5 candles only...",flush=True); provider.connect()
 try: candles=_fetch_mt5_chunked(provider,symbol="XAUUSD",start=start,end=end,timeframe=Timeframe.M5)
 finally: provider.close()
 details={}; summaries={}
 for name,rows in selected.items():
  traced=[]
  for s in rows:
   future=[bar for bar in candles if to_utc(bar.open_time.utc)>_stamp(s["timestamp"])][:a.hold_bars]
   traced.append(trace_signal(s,future,hold_bars=a.hold_bars))
  details[name]=traced; summaries[name]=summarize(traced)
 report={"study":"JAN23_DIRECTIONAL_MOVE_PATH_RESEARCH","research_only":True,"new_phase":False,
  "retraining_performed":False,"live_execution_allowed":False,"rsi_used":False,
  "base_goal_move":10,"minimum_useful_move":5,"move_levels":[5,10,15,20,25,30,40,50],
  "adverse_levels":[5,7,10,15],"same_m5_bar_policy":"AMBIGUOUS_NOT_CLEAN_WIN",
  "hold_bars":a.hold_bars,"comparison":"EMA_CROSS_VS_REENTRY_CONTINUATION",
  "summaries":summaries,"signals":details,
  "note":"Directional descriptive evidence only. BUY/SELL differences are reported, not hard-coded. Do not tune repeatedly on this JAN23 test partition."}
 out=Path(a.output); out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(report,indent=2,sort_keys=True),encoding="utf-8")
 print("✓ EMA CROSS vs REENTRY directional paths measured")
 print("✓ clean $5/base $10/$15/$20 and $25-$50 runners scored")
 print("✓ BUY and SELL reported separately; RSI not used")
 print("✓ first-touch ordering vs -$5/-$7/-$10/-$15 measured"); print(f"Report: {out}"); return 0
if __name__=="__main__": raise SystemExit(main())
