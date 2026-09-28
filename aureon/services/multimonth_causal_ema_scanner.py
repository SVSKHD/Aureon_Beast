"""Fast causal EMA20/50 cross -> pullback -> re-entry scanner.

Signals are frozen using current/past M5 bars only. Future bars are never used to
create a signal; they are consumed later only for outcome/economics research.
"""
from __future__ import annotations
from typing import Any

def _v(bar,name): return float(getattr(bar,name))
def _ts(bar): return getattr(getattr(bar,"open_time",bar),"utc",getattr(bar,"open_time",None))

def ema(values:list[float],period:int)->list[float|None]:
 out=[None]*len(values)
 if len(values)<period:return out
 seed=sum(values[:period])/period; out[period-1]=seed; k=2/(period+1); prev=seed
 for i in range(period,len(values)):
  prev=values[i]*k+prev*(1-k); out[i]=prev
 return out

def scan(candles:list[Any],*,pullback_touch:float=1.0,min_recovery:float=.25,max_wait_bars:int=72)->dict[str,list[dict[str,Any]]]:
 closes=[_v(b,"close") for b in candles]; e20=ema(closes,20); e50=ema(closes,50)
 crosses=[]; reentries=[]; active=None; seq=0
 for i in range(50,len(candles)):
  if e20[i-1] is None or e50[i-1] is None: continue
  bull=e20[i-1]<=e50[i-1] and e20[i]>e50[i]; bear=e20[i-1]>=e50[i-1] and e20[i]<e50[i]
  if bull or bear:
   seq+=1; direction="BUY" if bull else "SELL"; px=closes[i]
   sig={"sequence_id":f"raw-{seq}","timestamp":str(_ts(candles[i])),"bar_index":i,"stage":"CROSS","direction":direction,"entry_price":px,"ema20":e20[i],"ema50":e50[i]}
   crosses.append(sig); active={"signal":sig,"pulled":False,"cross_sep":abs(e20[i]-e50[i])}; continue
  if not active: continue
  age=i-active["signal"]["bar_index"]
  if age>max_wait_bars: active=None; continue
  d=active["signal"]["direction"]; fast=e20[i]; slow=e50[i]
  if (d=="BUY" and fast<=slow) or (d=="SELL" and fast>=slow): active=None; continue
  # Causal pullback: price revisits fast EMA or moves >= pullback_touch against cross entry.
  entry=active["signal"]["entry_price"]
  adverse=(entry-_v(candles[i],"low")) if d=="BUY" else (_v(candles[i],"high")-entry)
  touches=_v(candles[i],"low")<=fast if d=="BUY" else _v(candles[i],"high")>=fast
  if touches or adverse>=pullback_touch: active["pulled"]=True
  if not active["pulled"]: continue
  sep=abs(fast-slow); prev_close=closes[i-1]
  directional=(closes[i]>fast and closes[i]>prev_close) if d=="BUY" else (closes[i]<fast and closes[i]<prev_close)
  recovering=sep>=max(min_recovery,active["cross_sep"])
  if directional and recovering:
   reentries.append({"sequence_id":active["signal"]["sequence_id"],"timestamp":str(_ts(candles[i])),"bar_index":i,"stage":"REENTRY_CONTINUATION","direction":d,"entry_price":closes[i],"ema20":fast,"ema50":slow,"bars_after_cross":age})
   active=None
 return {"ema_cross":crosses,"reentry":reentries}

def one_position(signals:list[dict[str,Any]],hold_bars:int)->list[dict[str,Any]]:
 kept=[]; free_at=-1
 for s in sorted(signals,key=lambda x:x["bar_index"]):
  if s["bar_index"]<=free_at: continue
  kept.append(s); free_at=s["bar_index"]+hold_bars
 return kept
