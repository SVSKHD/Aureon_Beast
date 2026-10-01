#!/usr/bin/env python3
"""Run multi-month causal entry + profit-protection/runner research."""
import argparse,json,sys
from datetime import datetime,timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from aureon.config import AureonConfig
from aureon.models.enums import Timeframe
from aureon.services.multimonth_causal_ema_scanner import scan,one_position
from aureon.services.profit_protection_runner_research import research,POLICIES
from main_backtest import _fetch_mt5_chunked
def dt(s):return datetime.fromisoformat(s.replace("Z","+00:00"))
def main():
 p=argparse.ArgumentParser();p.add_argument("--from",dest="start",required=True);p.add_argument("--to",dest="end",required=True);p.add_argument("--hold-bars",type=int,default=288);p.add_argument("--output",default="artifacts/multimonth_profit_protection_research.json");a=p.parse_args()
 start=dt(a.start+"T00:00:00+00:00");end=dt(a.end+"T23:59:59+00:00");c=AureonConfig.from_env()
 from aureon.data.mt5_provider import MT5DataProvider
 provider=MT5DataProvider(market_tz=c.market_tz,login=c.mt5_login,password=c.mt5_password,server=c.mt5_server,terminal_path=c.mt5_terminal_path);provider.connect()
 try:candles=_fetch_mt5_chunked(provider,symbol="XAUUSD",start=start-timedelta(days=3),end=end+timedelta(minutes=5*(a.hold_bars+2)),timeframe=Timeframe.M5)
 finally:provider.close()
 raw=scan(candles);period=lambda s:start<=dt(str(s["timestamp"]))<=end
 raw={k:[s for s in v if period(s)] for k,v in raw.items()}
 tradable={k:one_position(v,a.hold_bars) for k,v in raw.items()}
 result={k:research(v,candles,a.hold_bars) for k,v in tradable.items()}
 report={"study":"MULTIMONTH_PROFIT_PROTECTION_RUNNER_RESEARCH","research_only":True,"new_phase":False,"entry_logic_changed":False,"future_used_for_entry":False,"hold_bars":a.hold_bars,"period":{"from":a.start,"to":a.end},"execution_costs":{"spread":.2,"slippage_each_side":.1},"policies":POLICIES,"one_position_at_a_time":True,"strategies":result,"note":"Management-policy research only. Same-M5-bar activation/stop ambiguity is resolved conservatively; use M1/tick before production."}
 out=Path(a.output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,indent=2,sort_keys=True,default=str),encoding="utf-8")
 print("✓ entries unchanged; causal EMA cross/re-entry");print("✓ staged +5/+10/+15/+20 protection and $20/$30/$50 runners");print("✓ monthly P&L, DD, PF, BUY/SELL and exit reasons");print(f"Report: {out}");return 0
if __name__=="__main__":raise SystemExit(main())
