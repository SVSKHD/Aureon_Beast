"""Tests for JAN23 EMA/re-entry move-path research."""
from types import SimpleNamespace
from aureon.services.jan23_reentry_move_research import trace_signal, summarize

def bar(high,low): return SimpleNamespace(high=high,low=low)

def test_buy_first_touch_and_ladder():
    s={"direction":"BUY","entry_price":100,"stage":"REENTRY_CONTINUATION","rsi":62}
    r=trace_signal(s,[bar(106,98),bar(111,99),bar(121,100)])
    assert r["first_favourable"]["5"]==1
    assert r["first_favourable"]["10"]==2
    assert r["first_favourable"]["20"]==3
    assert r["survivability"]["10"]["7"]["target_first"] is True
    assert r["rsi_bucket"]=="GE60"

def test_stop_before_target_is_not_clean():
    s={"direction":"BUY","entry_price":100}
    r=trace_signal(s,[bar(103,92),bar(112,99)])
    assert r["first_adverse"]["7"]==1
    assert r["first_favourable"]["10"]==2
    assert r["survivability"]["10"]["7"]["target_first"] is False

def test_same_bar_is_ambiguous_not_win():
    s={"direction":"SELL","entry_price":100}
    r=trace_signal(s,[bar(108,89)])
    assert r["survivability"]["10"]["7"]["same_bar_ambiguous"] is True
    assert r["survivability"]["10"]["7"]["target_first"] is False

def test_summary_separates_buy_sell():
    rows=[trace_signal({"direction":"BUY","entry_price":100,"rsi":55},[bar(111,99)]),
          trace_signal({"direction":"SELL","entry_price":100,"rsi":45},[bar(101,94)])]
    x=summarize(rows)
    assert x["signals"]==2
    assert x["by_direction"]["BUY"]["moves"]["10"]["reached"]==1
    assert x["by_direction"]["SELL"]["moves"]["5"]["reached"]==1
