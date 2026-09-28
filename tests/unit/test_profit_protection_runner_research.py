from types import SimpleNamespace
from aureon.services.profit_protection_runner_research import simulate,summarize
def b(h,l,c):return SimpleNamespace(high=h,low=l,close=c)
S={"sequence_id":"x","timestamp":"2023-01-01T00:00:00+00:00","direction":"BUY","entry_price":100}
def test_target():
 r=simulate(S,[b(111,99,110)],{"initial_stop":-7,"locks":[],"target":10});assert r["exit_reason"]=="TARGET" and r["gross_move"]==10
def test_lock_then_stop():
 p={"initial_stop":-7,"locks":[(5,2)],"target":20};r=simulate(S,[b(106,99,105),b(107,101,102)],p);assert r["gross_move"]==2
def test_same_bar_conservative():
 p={"initial_stop":-7,"locks":[(5,2)],"target":20};r=simulate(S,[b(106,97,104)],p);assert r["exit_reason"]=="SAME_BAR_AMBIGUOUS" and r["gross_move"]==-7
def test_timeout_real_close():
 r=simulate(S,[b(103,99,102)],{"initial_stop":-7,"locks":[],"target":10});assert r["exit_reason"]=="TIMEOUT_EXIT" and r["gross_move"]==2
def test_sell_symmetric():
 s={**S,"direction":"SELL"};r=simulate(s,[b(101,89,90)],{"initial_stop":-7,"locks":[],"target":10});assert r["gross_move"]==10
