# Aureon Beast

**Aureon is a market setup intelligence system.**

It watches XAUUSD and other configured symbols, combines several deterministic agents, records what they see, evaluates what happened afterwards, and sends useful market context to Discord.

> **Aureon finds setups. The human decides whether to trade.**

A detection is not an order. Agent agreement is not an order. Research/model output is not an order.

## How Aureon works

```text
MT5 market data
      ↓
Deterministic agents
      ↓
Market + higher-timeframe context
      ↓
Setup / opportunity analysis
      ↓
Discord alert and explanation
      ↓
HUMAN DECISION
      ↓
Manual trade (if wanted)
```

The research layer then studies what happened after each frozen setup: favourable movement, adverse movement, +$5/+10/+20+ continuation, failures, sessions and market regimes. This evidence can improve future setup filtering without allowing hindsight into the original detection.

## Current production agents

The default observer roster is defined in `main_observer.py -> default_agents()`.

| Agent | What it does | Role |
| --- | --- | --- |
| **EMA Cross** | Detects bullish/bearish fast/slow EMA crosses on closed candles. RSI is recorded as context, not a gate. | Directional setup event |
| **EMA-RSI Eligibility** | Records whether EMA/RSI conditions satisfy the configured eligibility context. | Setup context |
| **RSI** | Records transitions into/out of overbought and oversold zones. It does not say BUY/SELL. | Context only |
| **Session Trend** | Describes directional behaviour for the current trading session. | Trend context |
| **Wick** | Detects meaningful upper/lower wick rejection. It does not independently issue a trade direction. | Rejection context |
| **Liquidity** | Detects sweeps of tracked highs/lows followed by rejection. | Reversal/liquidity context |
| **Breakout** | Detects closes beyond the same tracked levels used by the liquidity agent. | Continuation context |
| **Market Journey** | Describes how price has travelled through the current market path. | Market context |
| **Market Regime** | Classifies the market environment (trend/range/compression/expansion style context). | Regime context |
| **Volume Participation** | Measures relative participation, volume/tick-volume behaviour, VWAP and candle expansion. | Participation context |

Liquidity and Breakout intentionally share one `LevelTracker`, so they evaluate the same market levels.

## Higher-level intelligence

The observer also contains higher-level services around the base agents:

- **Higher Timeframe Agent** — adds higher-timeframe alignment/context.
- **Daily Market Bias** — maintains the broader daily directional context.
- **Market Director** — combines already-observed market state into a higher-level view.
- **Expansion Opportunity Agent** — evaluates expansion/continuation opportunity.
- **Symbol Intelligence Agent** — supplies symbol-specific tuning and market schedule information.
- **Cross-Venue Replication Agent** — tracks relevant cross-venue context where configured.

These components provide context and setup intelligence. They do not turn a detection into an automatic trade.

## What a useful Aureon alert should answer

Aureon should make it easy for the human trader to understand:

```text
Direction:          BUY / SELL
EMA state:          cross / early context
Session trend:      aligned / mixed / opposed
HTF context:        aligned / mixed
Market regime:      trend / range / expansion / compression
Liquidity:          sweep / none
Breakout:           present / none
Wick rejection:     present / none
Participation:      strong / normal / weak
Daily bias:         bullish / bearish / mixed

Setup status:       valid / weak / conflicting
Expected movement:  research evidence for +$5 / +$10 / +$20+
Invalidation:       structural context

FINAL ACTION: HUMAN DECISION
```

The exact fields shown depend on what evidence is available. Aureon should expose disagreement between agents rather than hide it behind a single score.

## Research and learning

Aureon stores frozen detections and evaluates what happened **after** them.

Current research focuses on questions such as:

- How often does a setup reach +$5, +$10, +$15, +$20 and larger moves?
- What was MAE before the favourable move?
- Does waiting for pullback/re-entry improve the setup?
- Which sessions and market regimes produce cleaner continuation?
- Which agents add useful information and which add noise?
- When does a +$10 move have credible runner potential?

Research scripts and ML experiments are **advisory only**. A model may estimate movement probabilities, but it does not replace the deterministic setup agents or make the human trading decision.

## Storage

Aureon runs locally on Windows.

- Application state: `data/aureon.db` (SQLite/WAL)
- Raw candle history: Parquet
- Durable delivery queue: `outbox.db`

Firebase/Firestore is not required for the current local runtime.

Initialize or verify storage:

```bash
python scripts/setup_local_sqlite.py
python scripts/setup_local_sqlite.py --check
```

## Start Aureon

Create the environment and install dependencies:

```bash
python -m venv .venv
pip install -e ".[dev]"
```

MetaTrader5 is an optional Windows dependency:

```bash
pip install -e ".[mt5]"
```

Configure `.env`, then run:

```bash
python main_aureon.py
```

The launcher supervises the observer, monitor, Discord, review/learning services and any explicitly enabled execution service. **Starting Aureon does not mean Aureon should autonomously trade.**

For day-to-day operation see:

- `docs/RUNBOOK.md`
- `docs/MT5_SESSION_CHECKLIST.md`
- `docs/V1_RELEASE_RUNBOOK.md`

## Agent evidence

To inspect whether agents are producing normalized evidence:

```bash
python scripts/report_agent_evidence.py --symbol XAUUSD
```

Weekly evidence:

```text
/training-weekly symbol:XAUUSD
```

Model/research status where configured:

```text
/model-status symbol:XAUUSD
/backtest-status symbol:XAUUSD
```

## Core rules

1. **Agents detect and describe setups.**
2. **No detection automatically means TRADE.**
3. **The human makes the final trading decision.**
4. **Future candles may evaluate a setup but may never create the historical setup.**
5. **Research, backtests and ML stay separate from live observations.**
6. **Agent disagreement is useful information and must remain visible.**
7. **Risk and execution assumptions used in research are not automatically production rules.**

That is Aureon's job: **observe clearly, find good setups, explain the evidence, learn from outcomes, and leave the final trade decision to the human.**
