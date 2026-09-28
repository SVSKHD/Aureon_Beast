from aureon.services.monthly_move_economics import simulate_fixed,policy_summary,monthly_report
def row(direction="BUY",target=2,stop=None,runner=20):
 surv={str(t):{"7":{"target_bar":target if t<=20 else None,"adverse_bar":stop,"target_first":target is not None and (stop is None or target<stop),"same_bar_ambiguous":target is not None and stop==target}} for t in (5,10,15,20,25,30,40,50)}
 return {"timestamp":"2023-01-02T00:00:00+00:00","direction":direction,"survivability":surv,"continuation_after_10":runner}
def test_fixed_win_economics():
 x=simulate_fixed(row(),10); assert x["result"]=="WIN"; assert x["net_move"]==9.6; assert round(x["pnl_by_lot"]["0.25"],2)==240
def test_loss_economics():
 x=simulate_fixed(row(target=None,stop=1),10); assert x["result"]=="LOSS"; assert x["net_move"]==-7.4
def test_unresolved_not_fake_profit():
 x=simulate_fixed(row(target=None,stop=None),10); assert x["result"]=="NO_FIRST_TOUCH"; assert x["net_move"]==0
def test_month_direction_and_runner():
 r=monthly_report({"reentry":[row("BUY"),row("SELL")]}); m=r["strategies"]["reentry"]["months"]["2023-01"]
 assert m["by_direction"]["BUY"]["signals"]==1 and m["by_direction"]["SELL"]["signals"]==1
 assert m["runner_after_clean_10"]["base_10_captured"]==2 and m["runner_after_clean_10"]["extended_to_20"]==2
