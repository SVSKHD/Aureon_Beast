# Aureon V3 decisions

These are the design decisions that make the V3 EMA journey/model results comparable,
reproducible and safe to present. They are intentionally separate from implementation
details so a later refactor cannot silently change the experiment.

## V3-001 — One market move is one journey

Pre-cross pressure, EMA20/50 cross and EMA200 cross in the same direction are linked to one
movement journey when they satisfy the journey timing rules. Opposite confirmed direction
closes the old journey before the new one begins.

Why: treating linked anchors as independent opportunities inflates sample size and model
confidence.

## V3-002 — Every anchor keeps its own outcome

A journey may contain several anchors, but each anchor keeps its own spread-aware reference
price, target ladder, MFE, MAE, timing and adverse excursion.

Why: "how early did pre-cross see the move?" and "how much remained after confirmation?" are
different questions.

## V3-003 — XAUUSD and XAGUSD do not share price-distance targets

XAUUSD uses the research ladder +3/+5/+10/+20/+30/+40. XAGUSD uses its own scaled price ladder.
Other symbols require explicit or broker-point-scaled targets.

Why: one quote-currency distance is not economically equivalent across instruments.

## V3-004 — No future candle may enter a feature snapshot or prediction

Features are frozen on the event candle. The model prediction is persisted before future
outcome candles are consumed.

Why: otherwise model accuracy would contain lookahead leakage.

## V3-005 — Same-journey anchors do not receive full independent weight

Training weights sum to 1.0 per journey.

Why: one directional expansion can produce pre-cross, EMA20/50 and EMA200 events; counting each
as a full independent example would make one move dominate the dataset.

## V3-006 — Validation is chronological with purge and embargo

Random market-data shuffling is prohibited. Training outcomes overlapping the validation
boundary are purged and an embargo is applied before each test fold.

Why: labels resolve after event time and can otherwise leak information across folds.

## V3-007 — Model confidence must beat the matching historical base rate

A probability model is not useful merely because its hit rate looks high. Candidate use requires
improved probabilistic error versus same-session/same-direction history, with minimum cell counts.

Why: a model that predicts a frequent event at the unconditional rate has learned nothing useful.

## V3-008 — Model confidence and agent agreement stay separate

Agent confidence is deterministic same-candle agreement. Model confidence is a learned historical
probability. They may be displayed together but are never collapsed into one score.

Why: agreement is not a calibrated probability.

## V3-009 — Every probability shows its evidence count

Discord renders `P(target) xx% (n=N)`.

Why: a percentage without sample size invites false precision.

## V3-010 — One sparse unseen day is not enough

Operational unseen reporting aggregates 10-20 scored broker-market days, default 15.

Why: EMA events can be sparse and one day may contain too few observations to judge calibration.

## V3-011 — Drift can retire a Champion

Recent reconciled Champion predictions are monitored. Breaching the pinned live calibration
thresholds retires the degraded Champion and restores the latest prior compatible Champion when
available.

Why: passing historical validation is not permission to remain Champion indefinitely.

## V3-012 — Retraining never skips governance

Weekly cadence, enough new examples or detected drift may trigger a new Candidate. None of these
triggers may promote directly to Champion.

Why: frequency of retraining and authority to deploy are separate concerns.

## V3-013 — Model-confidence visibility is not a trading switch

`model_confidence_enabled=false` hides learned probabilities from Discord only. It does not
change `trading_enabled`, detections, stored predictions or training.

Why: presentation uncertainty must never accidentally alter money-moving permissions.

## V3-014 — Discord follows journeys, not isolated EMA messages

One Discord thread is created per EMA movement journey. Pre-cross, EMA20/50 and EMA200 messages
belonging to that journey are posted there. One final closeout compares prediction with actual
movement and records the end reason.

Why: the user should be able to read one market move as one story instead of reconstructing it
from unrelated cards.

## V3-015 — V2 comparison is frozen before V3 certification

`docs/V2_BENCHMARK_FROZEN.json` is the immutable V2 comparison artifact. It is derived from the
existing Phase-2 baseline and explicitly marked synthetic-fixture evidence.

Why: changing the old benchmark after seeing V3 would make improvement impossible to measure
honestly.
