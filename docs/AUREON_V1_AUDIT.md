# Aureon Beast V1 architecture audit

Audit target: repository `main` at the start of the V1 runtime-wiring work.

The objective is to extend existing components, not replace them.

## Status map

| Requirement | Status at audit | Existing implementation | Gap found |
|---|---|---|---|
| Deterministic analysis / agents | existing | `AnalysisEngine`, EMA/RSI/wick/liquidity/breakout/session/regime/volume agents, MTF, MarketDirector, Agent Highway | Preserve unchanged. |
| Frozen setup snapshot | existing / partial wiring | immutable `FeatureSnapshotV1`, `FeatureBuilder`, setup event snapshots | Live observer did not explicitly freeze every observer-owned indicator/context value; enrich the snapshot source. |
| Canonical clean label | existing | `AUREON_CLEAN_MOVE_V1`, +10 with MAE <= 7, BUY/SELL-aware, ambiguous same-bar fail-closed | Preserve. |
| Outcome independent of EOD | existing | `LearningMemoryService`, `CanonicalTrainingAgent` resolve by future-candle horizon | Preserve legacy EOD records separately. |
| Canonical training example | existing | `CanonicalTrainingExample` + local SQL repository/migration | Preserve. |
| Multi-target labels | existing | clean_10 + reach_5/10/20/30/40 | Preserve monotonic reach-probability boundary. |
| Training algorithms | existing | logistic baseline + boosted-stump challenger | Preserve; no neural/RL expansion. |
| Model lifecycle | existing | candidate/challenger/shadow/champion/retired/rejected, parent model, metadata | Preserve. |
| Protected Champion | existing | atomic `promote_champion()`; candidate cannot skip shadow | Preserve. |
| EvolutionAgent | existing | validation, walk-forward gate, shadow evaluation, promotion/rejection, persistent decisions | Not continuously orchestrated. |
| Model metrics | existing / partial | precision, recall, Brier, log loss, ROC-AUC, FPR, MAE/MFE, segmented validation | Core V1 metrics exist; runtime orchestration needs to use them consistently. |
| Walk-forward validation | existing | expanding chronological folds; encoder fitted on training slice; resolved-at leakage guard | Preserve. |
| Shadow inference | conflicting wiring | persistent V1/legacy shadow predictions + reconciliation | V1 observer path passed `extra_context` into a PredictionService signature that did not accept it, causing valid live inference to fail closed. |
| Champion intelligence boundary | existing / wiring gap | `PredictionService`, `EntryIntelligence`, Agent Highway publication | Fix live context argument wiring; do not grant execution authority. |
| +5 runner / trailing | existing | Agent 14/16 + deterministic no-fixed-TP trailing simulator | Preserve configurable management. |
| Entry vs exit learning separation | existing | V1 entry models separate from deterministic manager/guardian | Preserve. |
| Backtesting | existing | `main_backtest.py`: canonical labels, persistence, walk-forward, money, trailing, legacy compatibility | Preserve primary CLI. |
| Local-first storage | existing | local SQL/SQLite repositories, outbox, archive/state | Preserve as operational primary. |
| Drive backup | existing / manual | `BackupService`, checksum/mtime mirror to optional Drive Desktop folder | No continuous orchestration. |
| Monthly snapshots | existing / manual | immutable `backups/YYYY-MM` + SHA-256 manifest | No runtime scheduler/sidecar. |
| Observability | existing / partial | model predictions, evolution logs, training/backtest artifacts, ops/logs | Add clear cycle-level logs. |
| Fail-closed ML | existing | schema checks, exceptions return no prediction | Fix argument mismatch so valid models are not accidentally skipped. |
| Legacy migration | existing | legacy EOD/+6 schemas remain readable beside V1 migration | Preserve; never reinterpret old rows. |
| Continuous learning loop | missing orchestration | train/backtest/evolve/backup scripts exist independently | Add one local background cycle that never executes trades. |

## Conflicts retained intentionally for migration

Legacy contracts remain versioned and readable:

- `EOD_SETUP_FEATURES_V1`
- `FAVOURABLE_MOVE_LADDER_V2`
- historical +6 labels
- legacy model-training/shadow readers

V1 does **not** silently reinterpret them. New learning uses:

- `AUREON_FEATURES_V1`
- `AUREON_CLEAN_MOVE_V1`
- `AUREON_ENTRY_MODEL_V1`

## Wiring plan from this audit

1. Fix Champion/Shadow V1 live inference context contract.
2. Freeze richer observer-owned setup state without recomputation.
3. Add a resilient continuous-learning sidecar that:
   - evaluates active shadow evidence first;
   - waits for enough new canonical examples;
   - trains logistic + boosted candidates;
   - runs chronological V1 walk-forward validation;
   - lets `EvolutionAgent` qualify/reject/admit one challenger;
   - never promotes except through the existing shadow policy.
4. Add automatic monthly local snapshot creation and asynchronous Drive mirror in that sidecar.
5. Supervise the sidecar while preserving the execution boundary.
6. Add tests for the fixed inference path, sidecar isolation, and snapshot non-overwrite behaviour.

No deterministic market agent, risk gate, execution guard, broker adapter, or trade ownership rule is replaced by this work.
