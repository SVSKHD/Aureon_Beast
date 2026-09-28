"""Directional candle-path research for EMA-cross and confirmed re-entry.

Research-only: price direction, first-touch survivability, and continuation.
No RSI dependency, model retraining, promotion, or live execution.
"""
from __future__ import annotations
from typing import Any

MOVE_LEVELS=(5.0,10.0,15.0,20.0,25.0,30.0,40.0,50.0)
ADVERSE_LEVELS=(5.0,7.0,10.0,15.0)

def _fav_adv(direction:str,entry:float,bar:Any)->tuple[float,float]:
    if direction=="BUY": return max(0.0,float(bar.high)-entry),max(0.0,entry-float(bar.low))
    if direction=="SELL": return max(0.0,entry-float(bar.low)),max(0.0,float(bar.high)-entry)
    raise ValueError(f"unsupported direction: {direction}")

def trace_signal(signal:dict[str,Any],future:list[Any],*,hold_bars:int=288)->dict[str,Any]:
    direction=str(signal["direction"]).upper(); entry=float(signal["entry_price"])
    fav={x:None for x in MOVE_LEVELS}; adv={x:None for x in ADVERSE_LEVELS}
    mfe=mae=0.0; mfe_bar=mae_bar=None
    for i,bar in enumerate(future[:hold_bars],1):
        f,a=_fav_adv(direction,entry,bar)
        if f>mfe: mfe,mfe_bar=f,i
        if a>mae: mae,mae_bar=a,i
        for x in MOVE_LEVELS:
            if fav[x] is None and f>=x: fav[x]=i
        for x in ADVERSE_LEVELS:
            if adv[x] is None and a>=x: adv[x]=i
    survivability={}
    for target in MOVE_LEVELS:
        tk=str(int(target)); survivability[tk]={}
        for stop in ADVERSE_LEVELS:
            sk=str(int(stop)); tb,sb=fav[target],adv[stop]
            survivability[tk][sk]={"target_first":tb is not None and (sb is None or tb<sb),
                "same_bar_ambiguous":tb is not None and sb is not None and tb==sb,
                "target_bar":tb,"adverse_bar":sb}
    reached10=fav[10.0]
    runner=max((x for x in MOVE_LEVELS if reached10 is not None and fav[x] is not None and fav[x]>=reached10),default=0.0)
    return {"sequence_id":signal.get("sequence_id"),"snapshot_id":signal.get("snapshot_id"),
        "timestamp":signal.get("timestamp"),"stage":signal.get("stage"),"direction":direction,
        "entry_price":entry,"bars_observed":min(len(future),hold_bars),"mfe":mfe,"mfe_bar":mfe_bar,
        "mae":mae,"mae_bar":mae_bar,"first_favourable":{str(int(k)):v for k,v in fav.items()},
        "first_adverse":{str(int(k)):v for k,v in adv.items()},"survivability":survivability,
        "continuation_after_10":runner}

def _group(items:list[dict[str,Any]])->dict[str,Any]:
    n=len(items); moves={}
    for level in MOVE_LEVELS:
        k=str(int(level)); reached=[r for r in items if r["first_favourable"][k] is not None]
        clean={}
        for stop in ADVERSE_LEVELS:
            sk=str(int(stop)); wins=[r for r in items if r["survivability"][k][sk]["target_first"]]
            clean[sk]={"count":len(wins),"rate":len(wins)/n if n else None}
        moves[k]={"reached":len(reached),"reached_rate":len(reached)/n if n else None,"clean_before_adverse":clean}
    return {"signals":n,"mean_mfe":sum(r["mfe"] for r in items)/n if n else None,
        "mean_mae":sum(r["mae"] for r in items)/n if n else None,"moves":moves}

def summarize(rows:list[dict[str,Any]])->dict[str,Any]:
    by_direction={d:_group([r for r in rows if r["direction"]==d]) for d in ("BUY","SELL")}
    return {**_group(rows),"by_direction":by_direction,
        "opportunity_scorecard":{"minimum_useful_5_before_minus7":sum(r["survivability"]["5"]["7"]["target_first"] for r in rows),
          "base_10_before_minus7":sum(r["survivability"]["10"]["7"]["target_first"] for r in rows),
          "clean_15_before_minus7":sum(r["survivability"]["15"]["7"]["target_first"] for r in rows),
          "clean_20_before_minus7":sum(r["survivability"]["20"]["7"]["target_first"] for r in rows),
          "runners_25_plus":sum(r["continuation_after_10"]>=25 for r in rows),
          "runners_30_plus":sum(r["continuation_after_10"]>=30 for r in rows),
          "runners_40_plus":sum(r["continuation_after_10"]>=40 for r in rows),
          "runners_50_plus":sum(r["continuation_after_10"]>=50 for r in rows)},
        "continuation_after_10":{str(int(x)):sum(r["continuation_after_10"]>=x for r in rows) for x in MOVE_LEVELS if x>=10}}
