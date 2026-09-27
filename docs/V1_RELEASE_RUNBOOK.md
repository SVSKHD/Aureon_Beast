# V1 real-run acceptance and freeze

Vue is outside V1. The existing agents, learning pipeline and human confirmation boundary
remain in place. This workflow collects the remaining evidence; it does not claim any real
session has run merely because a unit test passes.

## Evidence requirements

`python scripts/v1_release.py requirements` prints the exact acceptance checks. Every check
requires an operator review of raw evidence. `status` verifies review completeness and file
integrity, not the truth of an operator's claims. Checksums detect changes after review; they
are not signatures or independent proof of a broker session. Never review synthetic fixtures,
a fake broker, an empty comparison, a skipped drill, or an incomplete run as real evidence.

| Gate | Existing runner / required evidence |
|---|---|
| XAUUSD session | `session_run.py`, then `session_verify.py DATE --symbol XAUUSD`; retain before/after evidence and nonempty counts. |
| XAGUSD session | The same procedure with XAGUSD; retain separate symbol evidence. |
| Demo execution | `demo_drills.py --all --broker mt5 --evidence PATH`; complete every skipped/manual drill in `DEMO_EXECUTION_CHECKLIST.md`. Record market and pending tickets, SL change, rejection, duplicate suppression, reconnect and unknown-result recovery. |
| Discord confirmation | On a demo deployment, retain the authorized Confirm interaction, request ID, executor record and MT5 ticket. Verify an expired confirmation is refused. Redact credentials and private identifiers before sharing. |
| Windows/VPS stack | Run `python main_aureon.py`; retain preflight, configured service health, process lifecycle and supervised recovery logs. A Linux unit test is not Windows evidence. |
| Calibration | `tune_report.py --symbol XAUUSD --days 30` and XAGUSD, plus `foundation_training.py bias-report`. Review Daily Bias, liquidity, wick, breakout and session/setup settings against real outcomes and held-out data. Apply researched changes with versions in a separate commit. |
| Foundation | Build canonical XAUUSD examples from a broker export/archive; retain source, broker timezone, symbol specification, date bounds, file hashes, sample counts, candidate artifacts, walk-forward results and Champion lineage. |
| Unseen months | January adaptation, February frozen-model exam, score then release February, train Challenger with February, then a March frozen-model exam. Preserve the chronology and exact model IDs. |
| Exit policy | `foundation_training.py exits`; compare fixed +10 with activation +5 protection/trailing, record sample counts, costs and same-bar uncertainty, and select on held-out data. The selected policy must match the freeze context. |
| Live/replay + MTF | `compare_live_vs_replay.py DATE --symbol SYMBOL`, then the same with `--mtf`, for both symbols. Retain nonempty comparison counts, sufficient warm-up and zero unexplained mismatches. |
| Weekend sleep/wake | Keep the Windows stack running over a real close/open. Retain market-state transitions, sleeping heartbeats, preopen wake, resumed closed candles, request deduplication and crash logs. |
| Final freeze | Require all eleven reviewed gates, a trained Champion, matching code/model/configuration and intact raw artifacts. Produce an immutable checksum bundle. |

Real historical data and a Windows terminal are external inputs. These steps do not download
an invented dataset or launch broker operations from the evidence tool. Refer to the existing
session and demo checklists before running those runners.

## Historical sequence

Use an isolated research database and a complete broker archive with warm-up and forward
outcome bars. Dates below are examples for the requested 2026 sequence; choose a genuinely
unseen period if those months already influenced training or tuning.

```powershell
python scripts/foundation_training.py build --data-dir data/live_candles --symbol XAUUSD --from 2023-01-01 --to 2026-01-31
python scripts/foundation_training.py train --symbol XAUUSD --from 2023-01-01 --to 2026-01-31
```

Training creates candidates and may admit a Challenger to shadow. It does not automatically
create a Champion. Complete the existing EvolutionAgent validation/shadow/promotion gates.
Keep outcomes that resolve across a month boundary out of the earlier training period.

```powershell
python scripts/foundation_training.py freeze --symbol XAUUSD
python scripts/foundation_training.py exam --symbol XAUUSD --from 2026-02-01 --to 2026-02-28
python scripts/foundation_training.py build --data-dir data/live_candles --symbol XAUUSD --from 2026-02-01 --to 2026-02-28
python scripts/foundation_training.py score --exam-id FEB_EXAM_ID
python scripts/foundation_training.py release --exam-id FEB_EXAM_ID
python scripts/foundation_training.py train --symbol XAUUSD --from 2023-01-01 --to 2026-02-28
```

Retain the February exam ID printed by `exam`. Select the governed Challenger model ID,
then open March with `exam --symbol XAUUSD --model-id MODEL_ID --from 2026-03-01
--to 2026-03-31`. Build March examples and score that exam without training on March first.
Record the frozen model's artifact hash and training cut-off. This is historical blind
replay; it is not a claim that the predictions were actually generated live in February.

Run exit comparisons on a separate selection period:

```powershell
python scripts/foundation_training.py exits --data-dir data/live_candles --symbol XAUUSD --from 2026-04-01 --to 2026-04-30 --fixed-target 10 --policy 5,1,0.5 --policy 5,2,0.5 --output data/v1_evidence/raw/exit_comparison.json
```

The policy arguments above are candidates, not endorsed values. Select only after reviewing
sample coverage, costs, drawdown, captured movement and uncertainty. Do not reuse the blind
exam to tune the model or exit policy and then call it blind again.

## Prepare and review the release record

After calibration is committed and the final Champion/configuration is selected, use a clean
checkout. Keep private logs under `data/v1_evidence/raw/` (ignored by git). The ledger stores
relative paths, so retain the directory structure when moving evidence between machines.

```powershell
python scripts/freeze_v1_baseline.py --symbol XAUUSD --tag v1.0.0 --prepare-evidence data/v1_evidence/release.json
python scripts/v1_release.py requirements
python scripts/v1_release.py status --evidence data/v1_evidence/release.json
```

Preparation only writes **pending** entries. It neither freezes V1 nor passes any gate.
For example, after inspecting the actual gold session evidence:

```powershell
python scripts/v1_release.py review --evidence data/v1_evidence/release.json --gate xauusd_session --reviewer "OPERATOR" --notes "Broker date, nonempty counts, source commit and verification details" --artifact raw/session_XAUUSD.md --check session_verified --check nonempty_observations
```

Repeat for every gate, naming each individual check from `requirements` with `--check`.
Attach more raw files with repeated `--artifact`. Do not substitute a typed success statement
for raw logs, broker exports or measured reports. Historical artifacts may reference earlier
training generations: the review must establish their lineage to the final Champion. Runtime
checks must describe the release being frozen. A configuration or code change requires a new
ledger/review; do not edit its fingerprints to reuse stale approvals.

## Freeze and verify

```powershell
python scripts/v1_release.py status --evidence data/v1_evidence/release.json
python scripts/freeze_v1_baseline.py --symbol XAUUSD --tag v1.0.0 --evidence data/v1_evidence/release.json
```

`status` returns 2 while any gate is incomplete or an artifact changed. Freeze additionally
compares the active Champion and current configuration against the prepared fingerprints.
It refuses a missing Champion, missing files, colliding result filenames, unsafe tags,
incomplete evidence and an existing tag. The CLI requires a clean committed checkout.

The resulting `baselines/v1.0.0/` contains the Champion, models, exams, evolution history,
walk-forward snapshot, agent versions/parameters, symbol tuning, bias/session configuration,
exit policy, reviewed ledger, raw evidence and SHA-256 manifest. Evidence is copied, so the
bundle remains verifiable after the source logs move. The final directory appears only after
all files have been written. Preserve it in your deployment backup; it is ignored by git.
Use `aureon.services.backup_service.verify_manifest` to recheck file integrity. Tagging the
source commit is a separate operator action and is not performed by these tools.

No actual MT5, Discord, Windows session, real-data training result or V1 release has been
certified by adding this workflow. Those gates remain pending until the real runs are reviewed.
