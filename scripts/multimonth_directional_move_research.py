#!/usr/bin/env python3
"""Fast multi-month causal EMA directional research from raw XAUUSD M5."""
from __future__ import annotations
import argparse,json,sys
from datetime import datetime,timedelta,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from aureon.config import AureonConfig
from aureon.models.enums import Timeframe
from aureon.models.base import to_utc
from aureon.services.multimonth_causal_ema_scanner import scan,one_position
from aureon.services.jan23_reentry_move_research import trace_signal,summarize
from aureon.services.monthly_move_economics import monthly_report
from main_backtest import _fetch_mt5_chunked

def stamp(v):
 if isinstance(v,datetime): return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
 return datetime.fromisoformat(str(v).replace("Z","+00:00"))

def trace_all(signals,candles,hold):
 out=[]
 for s in signals:
  future=candles[s["bar_index"]+1:s["bar_index"]+1+hold]
  r=trace_signal(s,future,hold_bars=hold)
  if future:
   last=future[-1]; close=float(last.close); sign=1 if s["direction"]=="BUY" else -1
   r["timeout_close_move"]=(close-float(s["entry_price"]))*sign
  else:r["timeout_close_move"]=0.0
  out.append(r)
 return out

def monthly_counts(rows):
 months={}
 for r in rows:
  m=str(r["timestamp"])[:7]; x=months.setdefault(m,{"signals":0,"BUY":0,"SELL":0,"clean_5":0,"clean_10":0,"clean_15":0,"clean_20":0})
  x["signals"]+=1;x[r["direction"]]+=1
  for t in (5,10,15,20):x[f"clean_{t}"]+=int(r["survivability"][str(t)]["7"]["target_first"])
 return months

def main():
 p=argparse.ArgumentParser();p.add_argument("--from",dest="start",required=True);p.add_argument("--to",dest="end",required=True)
 p.add_argument("--hold-bars",type=int,default=288);p.add_argument("--output",default="artifacts/multimonth_directional_move_research.json")
 a=p.parse_args(); start=stamp(a.start+"T00:00:00+00:00"); end=stamp(a.end+"T23:59:59+00:00")
 c=AureonConfig.from_env()
 from aureon.data.mt5_provider import MT5DataProvider
 provider=MT5DataProvider(market_tz=c.market_tz,login=c.mt5_login,password=c.mt5_password,server=c.mt5_server,terminal_path=c.mt5_terminal_path)
 print(f"Fetching raw XAUUSD M5 {a.start} -> {a.end} ...",flush=True);provider.connect()
 try:candles=_fetch_mt5_chunked(provider,symbol="XAUUSD",start=start-timedelta(days=3),end=end+timedelta(minutes=5*(a.hold_bars+2)),timeframe=Timeframe.M5)
 finally:provider.close()
 raw=scan(candles); period=lambda s:start<=stamp(s["timestamp"])<=end
 raw={k:[s for s in v if period(s)] for k,v in raw.items()}
 traced={k:trace_all(v,candles,a.hold_bars) for k,v in raw.items()}
 tradable_signals={k:one_position(v,a.hold_bars) for k,v in raw.items()}
 tradable={k:trace_all(v,candles,a.hold_bars) for k,v in tradable_signals.items()}
 report={"study":"MULTIMONTH_CAUSAL_EMA_DIRECTIONAL_RESEARCH","research_only":True,"new_phase":False,"rsi_used":False,
  "causal_signal_generation":True,"future_used_for_signal":False,"period":{"from":a.start,"to":a.end},"hold_bars":a.hold_bars,
  "signal_definition":{"cross":"EMA20/50 current-bar crossover","pullback":"price revisits EMA20 or >=$1 adverse move","reentry":"cross direction remains valid; after pullback price closes directionally beyond EMA20 and EMA separation recovers"},
  "raw_overlapping":{"monthly_counts":{k:monthly_counts(v) for k,v in traced.items()},"summaries":{k:summarize(v) for k,v in traced.items()}},
  "one_position_at_a_time":{"monthly_counts":{k:monthly_counts(v) for k,v in tradable.items()},"summaries":{k:summarize(v) for k,v in tradable.items()},"economics":monthly_report(tradable)},
  "signals":traced,"tradable_signals":tradable,
  "note":"Research scanner only. Re-entry definition is deterministic and causal but must be validated across months before any production rule."}
 out=Path(a.output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,indent=2,sort_keys=True,default=str),encoding="utf-8")
 print(f"✓ crosses={len(raw['ema_cross'])} reentries={len(raw['reentry'])}")
 print(f"✓ one-position crosses={len(tradable['ema_cross'])} reentries={len(tradable['reentry'])}")
 print("✓ monthly clean $5/$10/$15/$20 + $25-$50 runner evidence + P&L/DD")
 print(f"Report: {out}");return 0
if __name__=="__main__":raise SystemExit(main())
