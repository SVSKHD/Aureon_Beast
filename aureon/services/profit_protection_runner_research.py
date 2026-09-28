"""Causal profit-protection and runner research on frozen XAUUSD signals."""
from __future__ import annotations
from collections import defaultdict
LOTS=(.01,.05,.10,.25,.50,1.0); CONTRACT=100.0
POLICIES={
 "FIXED_10_STOP_7":{"initial_stop":-7.0,"locks":[],"target":10.0},
 "P5_BE_RUN20":{"initial_stop":-7.0,"locks":[(5.0,0.0)],"target":20.0},
 "P5_LOCK2_P10_LOCK5_RUN20":{"initial_stop":-7.0,"locks":[(5.0,2.0),(10.0,5.0)],"target":20.0},
 "P5_BE_P10_LOCK5_P15_LOCK10_RUN30":{"initial_stop":-7.0,"locks":[(5.0,0.0),(10.0,5.0),(15.0,10.0)],"target":30.0},
 "P5_LOCK2_P10_LOCK5_P15_LOCK10_P20_LOCK15_RUN50":{"initial_stop":-7.0,"locks":[(5.0,2.0),(10.0,5.0),(15.0,10.0),(20.0,15.0)],"target":50.0},
}
def _fav_adv(bar,entry,d):
 hi=float(bar.high);lo=float(bar.low)
 return (hi-entry,entry-lo) if d=="BUY" else (entry-lo,hi-entry)
def simulate(signal,candles,policy,spread=.2,slippage=.1):
 entry=float(signal["entry_price"]);d=signal["direction"];stop=policy["initial_stop"];activated=[];exit_move=None;reason=None;bar_no=None;amb=False
 for n,b in enumerate(candles,1):
  fav,adv=_fav_adv(b,entry,d)
  # Stop uses the level active at bar open. If a new lock and its stop are both
  # touched in the same M5 bar, order is unknowable: conservatively exit at old stop.
  if adv>=-stop:
   exit_move=stop;reason="STOP_OR_LOCK";bar_no=n;break
  if fav>=policy["target"]:
   exit_move=policy["target"];reason="TARGET";bar_no=n;break
  for trigger,lock in policy["locks"]:
   if trigger not in activated and fav>=trigger:
    if adv>=-lock: exit_move=stop;reason="SAME_BAR_AMBIGUOUS";bar_no=n;amb=True;break
    activated.append(trigger);stop=max(stop,lock)
  if exit_move is not None:break
 if exit_move is None:
  if candles:
   close=float(candles[-1].close);exit_move=(close-entry) if d=="BUY" else (entry-close);reason="TIMEOUT_EXIT";bar_no=len(candles)
  else:exit_move=0.;reason="NO_DATA";bar_no=0
 net=exit_move-spread-2*slippage
 return {"sequence_id":signal.get("sequence_id"),"timestamp":signal["timestamp"],"direction":d,"policy":policy,"exit_reason":reason,"exit_bar":bar_no,"gross_move":exit_move,"net_move":net,"activated_locks":activated,"same_bar_ambiguous":amb,"pnl_by_lot":{str(l):net*l*CONTRACT for l in LOTS}}
def summarize(rows):
 eq={str(l):0. for l in LOTS};peak=dict(eq);dd=dict(eq)
 for r in rows:
  for l in LOTS:
   k=str(l);eq[k]+=r["pnl_by_lot"][k];peak[k]=max(peak[k],eq[k]);dd[k]=max(dd[k],peak[k]-eq[k])
 gp=sum(max(0,r["net_move"]) for r in rows);gl=-sum(min(0,r["net_move"]) for r in rows)
 return {"trades":len(rows),"net_move":sum(r["net_move"] for r in rows),"pnl_by_lot":eq,"max_drawdown_by_lot":dd,"profit_factor":gp/gl if gl else ("INF" if gp else None),"exit_reasons":{x:sum(r["exit_reason"]==x for r in rows) for x in ("TARGET","STOP_OR_LOCK","SAME_BAR_AMBIGUOUS","TIMEOUT_EXIT","NO_DATA")}}
def research(signals,candles,hold=288):
 result={}
 for pname,p in POLICIES.items():
  rows=[]
  for s in signals:
   rows.append(simulate(s,candles[s["bar_index"]+1:s["bar_index"]+1+hold],p))
  months=defaultdict(list)
  for r in rows:months[str(r["timestamp"])[:7]].append(r)
  result[pname]={"overall":summarize(rows),"months":{m:summarize(v) for m,v in sorted(months.items())},"by_direction":{d:summarize([r for r in rows if r["direction"]==d]) for d in ("BUY","SELL")},"trades":rows}
 return result
