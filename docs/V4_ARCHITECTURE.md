# Aureon V4 Gold — Architecture

V4 extends the deterministic EMA journey architecture; it does not replace the observer/execution separation.

```
MARKET / CLOSED CANDLES
        |
        v
PRE-CROSS PRESSURE
  P(cross soon)
        |
        v
CONFIRMED EMA20/50 CROSS <---- EMA200 larger context
        |
        +--> movement already consumed
        +--> remaining +3/+5/+10/+20/+30/+40
        |
        v
EXPANSION
        |
        v
PULLBACK
  depth / ATR / retracement
  EMA20 / EMA50 / EMA200 / structure retests
  tick-volume contraction
        |
        v
RE-ENTRY OBSERVATION
  research anchor only
        |
        v
CONTINUATION
  tick-volume re-expansion
  remaining movement / MFE / MAE
        |
        +------> EXHAUSTION / REVERSAL / CLOSED
        |
        v
RESOLVED OUTCOME MEMORY
        |
        +--> specialist models
        +--> similar historical journeys
        +--> uncertainty / OOD
        +--> placement + management research
        |
        v
WEEKLY / DRIFT RETRAINING
        |
        v
Candidate -> Challenger -> Shadow -> Champion
        |
        v
DISCORD JOURNEY INTELLIGENCE
        |
        v
HUMAN DECIDES
```

## Context planes

Every V4 anchor can carry facts frozen at that moment: M5 journey state, M15/H1 context, EMA20/50/200 relationships, session and session phase, overlap state, relative MT5 tick volume, movement consumed, pullback state and structure. Future outcomes are stored separately.

## Learning plane

V4 learns from resolved outcomes, including failures. Weekly cadence and emergency drift can train a new versioned Candidate. A Candidate cannot skip governance. Base-rate improvement and later unseen evidence are required before Shadow/Champion states.

Specialists cover continuation, remaining movement, pullback continuation, exhaustion and MAE. Regime routing uses sufficiently populated context cells; sparse cells fall back instead of publishing unsupported precision.

Historical similarity retrieves nearby resolved journeys from frozen observable features. Similarity is evidence, not causality. Low support, weak similarity or ambiguous probabilities can mark a prediction out-of-distribution.

## Research plane

Placement research compares pre-cross, confirmed cross and pullback/re-entry anchors, pairing the same journey where opportunity cost is measured. Management research evaluates fixed +3/+5, partial+runner and pullback continuation counterfactuals. These are research summaries, not realized P&L.

## Discord plane

One EMA movement remains one Discord journey thread. V4 cards show remaining-move probabilities, expected excursion, reliability, pullback observations, research-only re-entry observations and similar-history evidence. Discord formats frozen facts; it does not compute indicators or market truth.

## Parity and safety

Live and replay paths must produce identical hashes for frozen V4 features and journey context, and identical predictions from the same model artifact. Any mismatch fails the parity contract.

V4 adds no autonomous execution path. The repository's existing separation remains: observation/intelligence -> Discord -> human decision -> existing confirmed execution flow. V4 itself never creates an order.
