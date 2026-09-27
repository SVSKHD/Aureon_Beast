# Phase 2 — Historical Hypothesis Validation

This phase produces evidence only. It does not fit, register, promote, shadow, or execute
a model.

## Reproducible run

Use XAUUSD M5 history covering 2023-01-01 through 2026-01-31 plus warm-up and the
configured future horizon:

```bash
python scripts/phase2_sequence_research.py --data /path/XAUUSD_M5.parquet
# or:
python scripts/phase2_sequence_research.py --data-dir data/live_candles
# or on the Windows/MT5 host:
python scripts/phase2_sequence_research.py --mt5
```

The runner reuses `AnalysisEngine`, `default_agents`, and `EMASequenceLabeler`.
Every EMA 20/50 cross is replayed chronologically. Candidate timestamps and prices are
the observable candle closes created in Phase 1; future bars only produce labels.

Outputs:
- `artifacts/phase2_sequences.jsonl`: canonical research rows.
- `artifacts/phase2_sequence_report.json`: counts, distributions, target hit rates,
  candidate MFE/MAE, time-to-target, breakdowns and the evidence gate.

## Gate semantics

The automated gate checks data adequacy/integrity, both-direction coverage and ambiguous
path rate. It deliberately does **not** invent a profitability or predictive threshold.
Even a technical PASS leaves `phase3_approved=false`. Phase 3 requires explicit review
that the historical evidence supports a useful sequence hypothesis.

If the report is unsupported, inconsistent, or data quality fails: STOP. Do not train.
