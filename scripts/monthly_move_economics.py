#!/usr/bin/env python3
"""Build monthly capture/P&L scorecards from directional move-path research output."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from aureon.services.monthly_move_economics import monthly_report
def main():
 p=argparse.ArgumentParser(); p.add_argument("--input",default="artifacts/jan23_reentry_move_research.json"); p.add_argument("--output",default="artifacts/monthly_move_economics.json"); a=p.parse_args()
 src=json.loads(Path(a.input).read_text(encoding="utf-8"))
 named={"ema_cross":src["signals"]["ema_cross"],"reentry":src["signals"]["reentry"]}
 report={"study":"MONTHLY_DIRECTIONAL_MOVE_ECONOMICS","source_study":src.get("study"),"base_goal_move":10,"minimum_useful_move":5,**monthly_report(named)}
 out=Path(a.output); out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(report,indent=2,sort_keys=True),encoding="utf-8")
 print("✓ monthly BUY/SELL opportunity counts")
 print("✓ fixed $5/$10/$15/$20 vs -$7 economics")
 print("✓ 0.01/0.05/0.10/0.25/0.50/1.00 lot P&L + max drawdown")
 print("✓ $10 base capture and $15-$50 runner extension counts")
 print(f"Report: {out}"); return 0
if __name__=="__main__": raise SystemExit(main())
