from aureon.services.multimonth_causal_ema_scanner import ema,one_position
def test_ema_is_causal_prefix_stable():
 xs=[float(i) for i in range(1,80)]; a=ema(xs[:60],20); b=ema(xs,20)
 assert a[-1]==b[59]
def test_one_position_filters_overlap():
 s=[{"bar_index":10},{"bar_index":20},{"bar_index":50}]
 x=one_position(s,25);assert [r["bar_index"] for r in x]==[10,50]
def test_one_position_allows_after_hold():
 s=[{"bar_index":10},{"bar_index":36}];assert len(one_position(s,25))==2
