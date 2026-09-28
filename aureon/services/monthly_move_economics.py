"""Monthly opportunity and execution-economics research for directional move paths."""
from __future__ import annotations
from collections import defaultdict
from typing import Any
LOT_SIZES=(0.01,0.05,0.10,0.25,0.50,1.00); TARGETS=(5.0,10.0,15.0,20.0); CONTRACT_SIZE=100.0
def _dd(values):
 equity=peak=dd=0.0
 for v in values: equity+=v; peak=max(peak,equity); dd=max(dd,peak-equity)
 return dd
def simulate_fixed(row:dict[str,Any],target:float,stop:float=7.0,spread:float=.2,slippage:float=.1)->dict[str,Any]:
 t=row["survivability"][str(int(target))][str(int(stop))];tb,sb=t["target_bar"],t["adverse_bar"]
 if tb is not None and (sb is None or tb<sb):result,gross="WIN",target
 elif sb is not None and (tb is None or sb<tb):result,gross="LOSS",-stop
 elif tb is not None and sb is not None and tb==sb:result,gross="AMBIGUOUS_AS_LOSS",-stop
 elif "timeout_close_move" in row:result,gross="TIMEOUT_EXIT",float(row["timeout_close_move"])
 else:result,gross="NO_FIRST_TOUCH",0.0
 net=gross-spread-(2*slippage) if result!="NO_FIRST_TOUCH" else 0.0
 return {"result":result,"gross_move":gross,"net_move":net,"pnl_by_lot":{str(l):net*l*CONTRACT_SIZE for l in LOT_SIZES}}
def policy_summary(rows:list[dict[str,Any]],target:float)->dict[str,Any]:
 trades=[simulate_fixed(r,target) for r in rows];n=len(rows)
 out={"signals":n,"wins":sum(x["result"]=="WIN" for x in trades),"losses":sum(x["result"] in {"LOSS","AMBIGUOUS_AS_LOSS"} for x in trades),"timeouts":sum(x["result"]=="TIMEOUT_EXIT" for x in trades),"unresolved":sum(x["result"]=="NO_FIRST_TOUCH" for x in trades)}
 out["capture_rate"]=out["wins"]/n if n else None;out["net_move"]=sum(x["net_move"] for x in trades)
 out["pnl_by_lot"]={str(l):sum(x["pnl_by_lot"][str(l)] for x in trades) for l in LOT_SIZES};out["max_drawdown_by_lot"]={str(l):_dd([x["pnl_by_lot"][str(l)] for x in trades]) for l in LOT_SIZES}
 gp=sum(max(0,x["net_move"]) for x in trades);gl=-sum(min(0,x["net_move"]) for x in trades);out["profit_factor"]=gp/gl if gl else (None if not gp else "INF");return out
def runner_summary(rows):
 base=[r for r in rows if r["survivability"]["10"]["7"]["target_first"]]
 return {"base_10_captured":len(base),**{f"extended_to_{x}":sum(r["continuation_after_10"]>=x for r in base) for x in (15,20,25,30,40,50)}}
def _bucket(rows):return {"signals":len(rows),"policies":{str(int(t)):policy_summary(rows,t) for t in TARGETS},"runner_after_clean_10":runner_summary(rows)}
def monthly_report(named_rows):
 strategies={}
 for name,rows in named_rows.items():
  months=defaultdict(list)
  for r in rows:months[str(r["timestamp"])[:7]].append(r)
  strategies[name]={"overall":_bucket(rows),"months":{}}
  for month,items in sorted(months.items()):strategies[name]["months"][month]={**_bucket(items),"by_direction":{d:_bucket([r for r in items if r["direction"]==d]) for d in ("BUY","SELL")}}
 return {"research_only":True,"contract_size":CONTRACT_SIZE,"lot_sizes":list(LOT_SIZES),"execution_cost_assumption":{"spread_price":.2,"slippage_price_each_side":.1,"note":"When timeout_close_move is supplied, no-first-touch trades exit at the actual horizon close and pay costs."},"fixed_policies":{"targets":[5,10,15,20],"stop":7},"strategies":strategies}
