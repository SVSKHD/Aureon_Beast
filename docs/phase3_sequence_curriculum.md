# Phase 3 — JAN23 Sequence Training Curriculum

Phase 3 converts accepted Phase-2 sequence records into a leakage-safe, research-only
curriculum. It does **not** train a model.

## Contract

- Dataset identity: `JAN23_RESEARCH`
- `research_only=true`
- `production_eligible=false`
- `champion_promotion_allowed=false`
- `live_execution_allowed=false`
- No model fitting, registry writes, promotion, shadowing, or execution.

Only timestamps already frozen by Phase 2 become snapshots: cross, independently
observable counter-move, exhaustion, and continuation/re-entry. Missing stages are not
invented.

Every snapshot contains `features` and `labels`. Future outcomes, MFE/MAE, target
hits and time-to-target are labels only. A recursive leakage check rejects forbidden
future keys in features.

Splits are chronological at **sequence level** (70/15/15). Every snapshot from a
sequence inherits that sequence's partition; snapshots are never randomly shuffled.

## Run

```bash
python scripts/phase3_sequence_curriculum.py \
  --input artifacts/phase2_sequences.jsonl \
  --output-dir artifacts \
  --prefix phase3_jan23
```

Artifacts:

- `phase3_jan23_examples.jsonl`
- `phase3_jan23_train.jsonl`
- `phase3_jan23_validation.jsonl`
- `phase3_jan23_test.jsonl`
- `phase3_jan23_report.json`
- `phase3_jan23_feature_schema.json`

Phase 3 succeeds only when the report emits `ANTI-LEAKAGE PASS` and
`PHASE_3_CURRICULUM_PASS`. Phase 4 owns all model training.
