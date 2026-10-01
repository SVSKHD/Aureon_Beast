# Aureon V3 EMA model operations

This runbook pins the operational rules introduced by TODO 089-094.

## Reproducible training

Every V3 model stores:
- the SHA-256 hash of the exact canonical-example snapshot,
- random seed (default `0`; current trainer is deterministic but the seed is still pinned),
- Git commit from `AUREON_GIT_COMMIT` or `git rev-parse HEAD`.

These values are stored in both model hyperparameters/validation provenance and the
artifact used to derive the model id.

## Champion drift

After resolved journey outcomes reconcile live predictions, the current V3 Champion is
checked on its most recent reconciled predictions.

Default drift gate:
- minimum 30 resolved predictions,
- Brier <= 0.24,
- calibration error <= 0.12,
- false-positive rate <= 0.45.

Breaching any gate retires the degraded Champion and restores the most recent prior
activated model with the same feature/label/model contract. If no prior Champion exists,
the degraded Champion is retired and V3 confidence fails closed until a new Champion is
promoted.

## Live vs replay parity

The same frozen V3 feature snapshot must hash identically in live and replay. The same
model over those snapshots must also produce an identical prediction hash. CI exercises
this on the existing live-shaped MarketEngine and ReplayEngine paths.

## Rolling unseen validation

Unseen-day reporting uses a rolling window clamped to 10-20 scored/released market days.
The default reporting window is 15 days. One isolated trading day is not treated as
enough evidence for model quality.

## Retraining cadence

A new Candidate is due when any one condition is met:
- 7 calendar days since the latest V3 training run,
- at least 30 newly resolved canonical examples after the latest training cutoff,
- Champion live drift is detected.

Retraining never skips governance. A newly trained model still starts as Candidate and
must pass Challenger, Shadow and Champion gates.

## Probability sample counts

Every Discord model probability carries the number of validation observations supporting
that target, for example `P(+10) 64% (n=212)`. Agent agreement remains separate and is
not a model probability.
