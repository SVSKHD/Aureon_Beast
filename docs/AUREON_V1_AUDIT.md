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


## Gap-closure status (after the V1 gap-closure change)

| Gap | Status | Where |
|---|---|---|
| 1 canonical schema authoritative | closed | `assert_canonical_examples` in every V1 trainer/evaluator/walk-forward path; `SchemaContractError` |
| 2 live deterministic exit manager | closed (recording by default; acting is opt-in) | `aureon/management/exit_manager.py`, `Trade.management_state`, `trade_management_events`, `PositionMonitor._update_management_state`, `ControlRequestKind.MODIFY_STOP`, `BrokerInterface.modify_position` |
| 3 manual trade isolation | closed | `Trade` validator, `TradeRepository.update_management`, `ControlWorker._ownership_refusal` |
| 4 one Aureon position | closed | `rule_within_aureon_position_limit`, `rule_positions_known`, `PredictionService(position_provider=...)` -> `HOLD_EXISTING_POSITION` |
| 5 stable EntryIntelligence contract | closed | `aureon/models/learning_v1.py` `EntryIntelligence`, `EntryDecision` |
| Daily Market Bias Agent | closed | `aureon/services/daily_market_bias.py`, `aureon/models/market_bias.py`, observer wiring, replay wiring |
| canonical feature additions | closed (additive, same schema id) | `FeatureSnapshotV1` daily/session fields, `raw_v1_features`, `FeatureBuilder._bias_fields` |
| 6 training coverage | closed | `aureon/services/training_coverage.py`; stored on each trained model |
| 7 low-coverage / OOD | closed | `assess_coverage` -> `CoverageAssessment` on every `EntryIntelligence` |
| 8 generation report | closed | `EvolutionAgent.generation_report` -> `model_evolution_log` |
| 9 failure learning | closed | `ModelPrediction.decision` / `outcome_class`, `experience_summary` |
| 10 foundation workflow | closed (CLI; needs historical candles to run) | `aureon/services/foundation_pipeline.py`, `scripts/foundation_training.py`, `main_backtest.py --timeframe` |
| 11 unseen month | closed | `LearningExam`, `UnseenMonthWorkflow`, `LeakageError` |
| 12 restart / recovery | closed for the listed items | management state persisted on the trade; deterministic control ids; `freeze_setup` never re-freezes; promotion refused twice; bias state store |
| 13 single health report | closed | `aureon/services/v1_health.py`, `scripts/v1_status.py`, `main_learning.py --status` |
| 14 tests | added | `tests/unit/test_daily_market_bias.py`, `test_exit_manager.py`, `test_v1_schema_and_coverage.py`, `test_entry_intelligence.py`, `test_position_management_v1.py`, `test_unseen_month_workflow.py`, `test_evolution_generation_report.py`, `test_v1_health_and_backup.py` |

Not built, deliberately: sequence models, transformers, LLM decisions, RL, a separate exit or
risk model, a vector database, pyramiding, distributed training. Session-transition labels
and the full bias snapshot are preserved on every setup for V2.
