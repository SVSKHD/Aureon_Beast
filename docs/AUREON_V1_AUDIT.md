# Aureon Beast V1 architecture audit

Audit performed against the existing repository and the open adaptive-loss-control branch before V1 code changes.

## Status map

| Requirement | Status | Existing implementation | Gap / conflict |
|---|---|---|---|
| Existing deterministic agents / analysis | existing | `AnalysisEngine`, agent roster, Agent Highway, setup engine, MTF/context agents | Preserve unchanged as the market-observation layer. |
| Frozen setup evidence | partial | immutable detections; setup events carry frozen `context_snapshot`; shadow predictions persist feature snapshots | Current ML feature extraction is only a small subset of available agent/context state. |
| Outcome tracking | existing/partial | `OutcomeTracker`, setup evaluation, EOD movement tracking, decision replay MFE/MAE | Learning contracts disagree on +6, +10, +20/+40 and EOD vs fixed horizons. |
| Training memory | existing/partial | `TrainingExample`, `TrainingMemoryRepository`, migration 0004/0005 | Old schema is EOD/+6 specific. No canonical clean-move record. |
| Model training | existing/partial | logistic baseline, adaptive logistic/boosted challengers | Old persisted trainer is +6/+20/+40; adaptive learner lives beside rather than inside canonical memory. |
| Model registry | partial | `ModelRegistryEntry`, repository, candidate/shadow/retired | No explicit champion/challenger/rejected lifecycle, parentage, promotion history or governance metadata. |
| Walk-forward validation | existing/partial | `WalkForwardBacktester`; date-ordered decision backtest | Persisted walk-forward path targets old +6 contract and lacks clean_10 metrics/segmentation. |
| Shadow inference | existing/partial | `ShadowModelService`, persistent predictions + reconciliation | Only active shadow model, old schemas/targets, no champion-vs-challenger paired evaluation. |
| Production Champion | missing | no distinct champion lookup/status | A shadow activation currently retires another shadow, but no protected Champion concept exists. |
| Evolution governance | missing | manual training/backtest/shadow activation scripts | No EvolutionAgent or persistent promotion/rejection decision log. |
| Champion prediction boundary | missing/partial | shadow inference explicitly cannot execute; Director/Risk/Execution boundaries exist | No champion prediction service feeding intelligence into a decision policy while failing closed. |
| Exit manager | partial | TradeManager + ProfitGuardian; open PR adds +5 runner handoff | Needs canonical configurable runner policy and result/excursion record suitable for later exit learning. |
| Local-first operation | existing | SQLite is supported application storage; local candle archive/outbox/state | Learning/model backup is not yet organized as explicit local assets/snapshots. |
| Google Drive backup | missing | no Drive dependency or hot-path coupling | Need optional asynchronous/offline uploader boundary; trading must never wait on it. |
| Monthly snapshots | missing | none | Need immutable local monthly snapshots + checksummed manifest. |
| Observability | partial | logs, ops events, model registry, predictions, audits | No single learning/evolution log containing model/prediction/outcome/promotion reasoning. |
| Fail-closed ML | partial | shadow contract mismatch skips predictions | Champion prediction service and lifecycle operations need explicit corruption/schema guards. |
| Historical schema migration | existing principle / partial implementation | migrations are append-only; old +6 contracts explicit | Need new versioned schemas while preserving old rows/readers. |
| Backtest CLI | partial | MT5/file/archive replay, train/test, adaptive outputs, money sim, +5 trail branch | Needs canonical clean-label output, champion/challenger testing and walk-forward entry point. |
| Reproducibility | partial | saved JSON artifacts, model artifacts, source labels | Need artifact metadata/config snapshot/checksums and canonical training examples. |
| Continuous learning loop | partial | EOD memory -> training -> shadow exists as separate services | Missing one coherent canonical record + lifecycle orchestration. |

## Conflicting contracts found

1. `EOD_SETUP_FEATURES_V1 / FAVOURABLE_MOVE_LADDER_V2` uses +6 as the primary label and day-close semantics.
2. Persisted `ModelTrainer` and `WalkForwardBacktester` train +6/+20/+40.
3. `decision_backtest.py` uses +10 within 12 M5 bars.
4. `adaptive_learning.py` uses +5/+10/+20/+30/+40 over a configurable longer horizon.
5. Setup/event storage already preserves more state than `ml/features.py` exposes to the learner.
6. Shadow infrastructure exists but is challenger-only; there is no protected Champion.
7. Current model statuses are free-text and only practically use candidate/shadow/retired.
8. The +5 trailing branch improves management, but entry labels and model governance are still separate from the persisted learning pipeline.

## V1 reuse plan

No broad rewrite. V1 will extend the existing boundaries:

- **FeatureBuilder**: build immutable `AUREON_FEATURES_V1` snapshots from setup events / replay state.
- **OutcomeTracker / canonical outcome builder**: add `AUREON_CLEAN_MOVE_V1` with clean +10 / MAE <= 7 and +5/+10/+20/+30/+40 ladder.
- **TrainingMemory**: persist canonical records beside legacy rows.
- **ModelTrainer**: retain logistic baseline and boosted-stump challenger, train clean_10 plus the target ladder.
- **ModelRegistry**: add explicit candidate/challenger/shadow/champion/retired/rejected lifecycle and parent/metrics metadata.
- **PredictionService**: one schema-checked inference path usable by Champion and Shadow services.
- **EvolutionAgent**: govern validation, shadow admission and promotion; never predict the market itself.
- **ExitManager**: keep deterministic +5 runner/trailing policy separate from entry ML.
- **BackupService**: local snapshot/manifest first; optional asynchronous Drive uploader callback second.
- **main_backtest.py**: remain the primary historical CLI and gain canonical labels / walk-forward support.

Legacy contracts remain readable and are never silently reinterpreted.
