# Aureon V1.5 Phase 1 — EMA sequence labeling

This phase adds research-only sequence labels around EMA 20/50 crossover events.

## Reused components

- `EmaCrossAgent` remains the source of crossover events and indicator evidence.
- Existing frozen-context conventions in `learning_contract.py` remain the leakage boundary.
- Existing candle/replay infrastructure supplies chronological candles.
- Existing deterministic agents remain unchanged.

## New components

- `EMASequenceSnapshot`: immutable facts known on the cross candle.
- `EMASequenceLabel`: future-only research label.
- `EMASequenceRecord`: snapshot + label.
- `EMASequenceLabeler`: deterministic labeler with configurable research thresholds.

## Phase-1 gate

No training, model lifecycle, execution, shadow activation, or champion replacement is
performed by this code. Thresholds are configuration knobs for diagnostics, not claims that
the hypothesised pattern is valid. Phase 2 must run broad historical distributions before
any new ML curriculum may consume these labels.

## Leakage rules

1. Snapshot creation reads only the crossover detection and explicitly supplied frozen context.
2. Future candles are consumed only by the label path.
3. Full horizon is required so incomplete futures are not mislabeled as failures.
4. Same-M5-bar continuation/pullback ordering is marked `AMBIGUOUS_PATH`.
5. Historical extrema may exist in labels/diagnostics; they are never copied into snapshots.

## Acceptance criteria

- Symmetric BUY/SELL handling.
- Configurable continuation, pullback, deep-pullback and failure thresholds.
- Independent +6/+10/+20/+30/+40 ladders.
- MFE/MAE-style max favourable/adverse movement retained.
- Conservative intrabar ambiguity.
- Unit coverage for immediate continuation, pullback continuation, deep pullback,
  failure, ambiguity and leakage/horizon guards.
- No model training or execution imports.


## Counter-move and re-entry completion

A pullback is not automatically a counter-trade. The labeler accepts a counter-move
candidate only when an observable-state row contains independent deterministic
directional evidence opposite the original EMA cross. The candidate entry is the close
of that evidence candle; MFE, MAE and +6/+10/+20/+30/+40 timing are measured strictly
after that entry.

Pullback exhaustion is likewise observable-time-only: after a qualified counter-move,
the first independent directional evidence returning to the original thesis is frozen
as the exhaustion/re-entry candidate. A failed continuation remains a negative example;
the candidate is not moved retrospectively to the pullback extreme.

The `observable_states` input is expected to be produced by chronological replay from
the existing deterministic agents. Outcome/future keys are stripped from candidate
context as a second leakage barrier.
