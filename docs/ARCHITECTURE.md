# Aureon — architecture, as built

**What this document is:** a description of the system that is in this repository, written from
the code. Every claim in it is checkable against a file named beside it, and the one table that
could drift is generated (`python scripts/gen_architecture.py`).

**What it is not:** the frozen specification. `CLAUDE.md` has told every contributor since Phase 0
to read `docs/ARCHITECTURE.md`, and the `§` references scattered through the codebase — `§12` for
detection identity, `§41` for the execution guard, `§75` for observer startup order — point at
that original spec. **That document has never been in this repository.** It was not reconstructed
here, because a plausible document nobody wrote is worse than an absent one: it would be treated
as authoritative and quietly disagree with the real spec on exactly the details the `§` numbers
were cited for.

So the `§` references remain what they always were: pointers into a document the operator holds.
What this file adds is the part that *can* be derived — what the processes are, what writes what,
and which rules are enforced by a test rather than by intention. Where a `§` appears below it is
carried over from the code that cites it, as a cross-reference, not as a claim to contain it.

---

## The flow, and the one arrow that does not exist

```
                 OBSERVE  →  UNDERSTAND / TRACK SETUP  →  HUMAN DECIDES  →  EXECUTE
                    │                  │                       │               │
              MT5 candles        agents, context,        Discord: a human    executor:
              and quotes         evaluations, setups     types a lot and     exactly one
                                                         confirms            broker order

                 DETECTION  ─╳→  AUTO TRADE          ← this arrow is absent by construction
```

The forbidden arrow is not absent by policy. It is absent because the processes that produce
detections have no broker handle to place an order with, and because
`tests/boundary/test_architecture_boundaries.py` greps the observation side for the broker's
vocabulary and fails if it appears — in code or in a comment. Prose in this repository describes
that vocabulary rather than quoting it, for that reason.

Between UNDERSTAND and HUMAN DECIDES there is no automation at all. A setup reaching CONFIRMED
posts a card in Discord; nothing about that card places an order, and the button on it opens a
wizard that requires a lot size and a second confirmation.

---

## Process boundaries

Six entry points. Five are long-lived services; the sixth starts them.

| entry point | what it does | what it may never do |
|---|---|---|
| `main_aureon.py` | the launcher: loads `.env`, runs the preflight, starts the five below in order and stops them all if one dies | change `trading_enabled`, or hold any broker or Firestore state of its own beyond one ops register |
| `main_observer.py` | reads candles and quotes, runs the agents, builds context, writes detections, evaluations, sessions, the day cache and the per-symbol state; fires armed alerts | call the broker. A detection never creates a trade. |
| `main_monitor.py` | reconciles positions and closes from MT5's own deals into `trades` | decide that a trade closed. MT5 is the truth; this process records it. |
| `main_executor.py` | claims **CONFIRMED** trade requests under a lease, runs the guard, sends exactly one order | invent a request. It only ever acts on one a human confirmed. |
| `main_discord.py` | the human interface | call the broker, or compute an indicator. It reads Firestore and writes the short list below. |
| `main_review.py` | aggregation over history — `watch` as a service, or one-off daily/weekly reports | react to anything live. It is re-runnable for any past period, which is the point. |

They do not talk to each other. Every hand-off is a Firestore document, which is why any of them
can be restarted alone, and why the review service can be pointed at last March.

### Why five processes and not one event loop

The observer holds a blocking MT5 terminal handle, the executor holds an exclusive lease, and
Discord holds a gateway websocket. In one loop, one unhandled exception stops all three, and one
terminal call that blocks for four seconds stalls the gateway heartbeat. More importantly it would
put the broker connection in the same address space as the agents, and the rule that a detection
never creates a trade would become a matter of discipline instead of a matter of what is reachable.

---

## The launcher: start and stop

`python main_aureon.py` is the documented way to run Aureon. It loads `.env` once with
python-dotenv (`override=False`, so the process environment wins), gates on
`scripts/preflight.py`, then starts five children in this order:

**observer → monitor → executor → discord → review watcher**

The order is `§75`'s and is load-bearing: the observer first so the outbox and archive are being
drained before anything produces work; the monitor before the executor so a position opened by the
executor's first poll is already being watched; the executor before Discord so a human cannot
confirm a request into a process whose lease is not claimed yet.

**Stop-on-crash, not restart.** One child exiting takes the whole stack down: the launcher logs
`child_exited`, records the `supervisor_stack_down` condition in `ops_events`, stops the rest in
reverse order and exits non-zero. A half-running stack is the dangerous state, and the dangerous
part is that it looks healthy from the one screen an operator has — an executor with no monitor
means positions nobody reconciles. Automatic restart is deliberately absent: restarting a child
that died on a poisoned document loops forever, and restarting an executor mid-reconciliation is
how one unknown broker outcome becomes two.

A child that exits **zero** on its own still produces a non-zero launcher exit. A zero would be
read by systemd as "asked to stop", leaving Aureon down until somebody noticed.

**Ctrl+C is cooperative.** Each child runs in its own process group (`start_new_session` on POSIX,
`CREATE_NEW_PROCESS_GROUP` on Windows), so the console's interrupt reaches the launcher only and
the launcher is the single owner of shutdown ordering. It interrupts in reverse order and waits
`AUREON_SHUTDOWN_GRACE_SECONDS` (default 20) before terminating anything, because the observer's
shutdown drains the outbox, flushes the parquet archive and saves its cursor. On Windows the
interrupt must be CTRL_BREAK — `GenerateConsoleCtrlEvent` ignores the process-group argument for
CTRL_C — so every service registers SIGINT, SIGTERM **and** SIGBREAK through
`aureon/services/shutdown.py`, and each logs `shutdown_flushed` when it is done.

Starting the launcher never enables trading.

---

## Data flow

Three flows, and they meet only in Firestore.

**Observation.** MT5 → observer → agents and context → evaluations → a local durable outbox →
Firestore → Discord and the reviews.

The outbox is on disk (`outbox.db`) and sits between the engine and Firestore on purpose: a
Firestore outage must not lose a detection or stall the candle loop. The queue is drained by a
worker thread, and `flush(final=True)` drains it past the stop flag on shutdown.

M1 candles are archived to parquet (`data/live_candles/`) rather than Firestore. Ticks are never
stored anywhere.

**Execution.** Discord → `trade_requests` (REQUESTED, then CONFIRMED by a human) → executor claims
one under a lease → the guard runs → one order is sent → the result is written back to the same
document. Nothing else can create a trade request, and the executor never creates one at all.

**Reconciliation.** MT5 deals → monitor → `trades`. The monitor runs regardless of
`trading_enabled` (`§58`): that switch stops the executor, and positions opened before it was
thrown are still live and still need their closes recorded — a monitor that paused alongside the
executor would blind the operator during exactly the incident that made them disable trading.

---

## Broker truth and application truth

Two sources of truth, and confusing them is the most expensive mistake available here.

**MT5 is the truth about money.** Positions, deals, fills, the account mode, the tradable symbol
and its spread. Aureon never decides that a position closed; it asks, and records the answer. An
unknown broker outcome is reconciled, never retried — `FAILED_RECONCILIATION` is terminal, and
repairing it is an operator action outside the state machine.

**Firestore is the truth about the application.** What was detected, what a human asked for, what
state each request is in, what the reviews concluded. It holds no authoritative copy of anything
the broker owns: `symbol_specs` is a published convenience copy, and the execution guard re-reads
the live symbol at execution time rather than trusting it (decision 79).

Money-moving code depends on `aureon.execution.broker_interface.BrokerInterface`, never on
MetaTrader5. Exactly two modules may import MetaTrader5 — `aureon/data/mt5_provider.py` and
`aureon/execution/mt5_broker.py` — and both import it lazily inside a function, which is what lets
the whole suite run on Linux against a fake. A boundary test enforces both halves.

---

## Observer and executor are separated by address space, and by tests

The rule: **the observer cannot trade, and the executor cannot decide.**

What enforces it, in `tests/boundary/test_architecture_boundaries.py`:

- the observation side (observer, agents, engine, evaluation, reviews) must not import
  `aureon.execution` — a static import check;
- the observation side must not mention the broker's order-placing vocabulary at all, which catches
  a duck-typed handle passed in as an argument, or MetaTrader5 reached directly;
- only the two permitted modules may import MetaTrader5, and neither at module scope;
- collection names appear only in `aureon/storage/paths.py` — a bare literal elsewhere would read
  and write a second, empty collection that looks exactly like "no data yet".

The executor's side of the rule is in the code rather than in a grep: it acts only on a request
already in `CONFIRMED`, it claims it with a lease so two executors cannot both send, and every
status write goes through `aureon.models.enums.assert_transition`.

---

## What Discord may write

Discord is the human interface and holds no broker and no data provider, so it cannot trade even
by mistake (`§71`). It reads widely and writes exactly:

- `trade_requests` — REQUESTED from the wizard, and CONFIRMED when a human confirms
- `control_requests` — close / modify / cancel, for the executor to perform
- `settings/execution.trading_enabled` — the kill switch, under a transaction
- `settings/notifications` — which agents and setup states it announces
- `alerts` — ARMED price levels a human asked to be told about
- `trade_notes` — what a human wrote down
- `assessments` — the measured cohort readout `/monitor` produces
- `notifications` — its own record of what it has already said
- `audit_logs` and `heartbeats` — beside the changes above

It computes no indicators. Every number on a Discord screen was computed by the observer or the
review service and read back.

---

## Firestore ownership

Every collection is `{prefix}_{name}`, where the prefix is `AUREON_COLLECTION_PREFIX`. One
variable moves a whole deployment's data, and it is the only thing separating an experiment from
production inside one project.

The names below are derived from `aureon/storage/paths.py`; the writer and reader attribution is
declared in `aureon/storage/ownership.py` and checked against that registry in both directions by
`tests/unit/test_ownership.py`. A collection added without an entry fails a test.

<!-- BEGIN GENERATED: ownership (python scripts/gen_architecture.py) -->

| collection | what is in it | written by | read by |
|---|---|---|---|
| `{prefix}_alerts` | price levels a human asked to be told about. Discord arms them; the observer sees the quote cross one and fires it once (9C) | discord, observer | discord, observer |
| `{prefix}_assessments` | a measured cohort readout for one detection shape, with what was dropped to reach it and where its history came from (9D, 11C) | discord | discord, review |
| `{prefix}_audit_logs` | every state change that moved money or permission, written beside the change itself | executor, monitor, discord | discord, tools |
| `{prefix}_control_requests` | close / modify / cancel asked for by a human, performed by the executor under a lease | discord, executor | discord, executor |
| `{prefix}_daily_reviews` | one aggregation per broker day, re-runnable for any past day | review | discord, tools |
| `{prefix}_detection_evaluations` | what happened after a detection, per frozen rule and horizon (§21, §22). Separate from the detection because a detection may never carry future information | observer | discord, review, tools |
| `{prefix}_detections` | one immutable row per agent event at a candle close; the id is a hash over seven components including agent_version (§12) | observer | discord, review, tools |
| `{prefix}_heartbeats` | one document per service, the source of truth for liveness (decision 10). The preflight writes one under its own name | observer, monitor, executor, discord, tools | discord, tools |
| `{prefix}_market_day_frames` | the bars themselves, M5/M15/H1, so a process with no terminal can rebuild a chart or a higher-timeframe bias. M1 stays in parquet (11D) | observer | discord, review, tools |
| `{prefix}_market_days` | one broker day's shape per symbol: its OHLC, bar count and whether it is finished (11D) | observer | discord, review, tools |
| `{prefix}_notifications` | what Discord has already said, claimed before posting so a restart does not repeat it (9C). See the claim-before-post tradeoff in docs/ARCHITECTURE.md | discord | discord |
| `{prefix}_ops_events` | the named operational conditions and whether each is true right now, one write per edge (11A, F-15). Written by the observer (which reads the other services' heartbeats, so executor_stale and monitor_stale are its to report) and by the launcher (supervisor_stack_down). Nothing gates on these | observer, launcher | discord |
| `{prefix}_sessions` | one document per broker day and session: its range, and what the agents saw (§18) | observer | discord, review |
| `{prefix}_settings` | settings/execution holds trading_enabled and the runtime gates (decision 11); settings/notifications holds what Discord announces. Discord is the only writer, and the kill switch is written under a transaction | discord | executor, discord, tools |
| `{prefix}_setup_evaluations` | what a setup did after it CONFIRMED, under one frozen rule, measured by the same tracker the detections use (12, T-7). Separate from the setup because the setup is edited as it advances and an outcome on it would be future information | observer | discord, review, tools |
| `{prefix}_setups` | structures tracked over time, with their state machine and a per-setup `events` sub-collection holding how each one got there (12, T-6). Written only by the observer: Discord's card keeps its message id on the notification document instead, so this collection stays the observer's | observer | discord, review, tools |
| `{prefix}_symbol_specs` | broker metadata published for Discord to render. A convenience copy: the execution guard re-reads the live symbol at execution time (decision 79) | observer, tools | discord |
| `{prefix}_system_state` | the observer's current view, one document per symbol and timeframe (9A). A derived snapshot, never an authority | observer | discord |
| `{prefix}_trade_notes` | what a human wrote down about a trade or a detection | discord | discord, review |
| `{prefix}_trade_requests` | the only route to execution. Discord writes REQUESTED and CONFIRMED; the executor claims one with a lease and writes every state after that (§25, §41) | discord, executor, monitor | discord, monitor, review |
| `{prefix}_trades` | what the broker actually did, reconciled from its own deals. MT5 is the truth here; Aureon only records it (§58, §77) | monitor | discord, review |
| `{prefix}_weekly_reviews` | one aggregation per ISO week, including the scorecard that grades its own readouts | review | discord, tools |

<!-- END GENERATED: ownership -->

Two things are deliberately **not** in Firestore:

- **ticks**, anywhere, ever — far too voluminous, and never needed after the candle closes;
- **M1 candles**, which live in parquet. They exist for one job (reconstructing a position's
  excursions for a stretch the monitor was down, `§45`) and a day of them is 1440 bars per symbol.

---

## The broker-day cache

`market_days` holds one small document per symbol per broker day: its open, high, low, close, bar
count and whether it is finished. `market_day_frames` holds the bars, one document per timeframe,
capped at 600 with a `truncated` flag so an over-long day is a visible partial rather than a failed
write.

Only **M5, M15 and H1** are cached. M30, H4 and D1 are derivable from the stored M5 by
`aureon/engine/mtf.py`, and a second stored copy is a second thing that can disagree with the
first — store the source, derive the rest, never both.

The date is the **broker** date, not the UTC one: a candle at 22:00 UTC belongs to the next trading
day in Athens, and filing it under the UTC date would split one session across two documents so
that neither was the day anybody traded (`§8`).

A day is `complete` only when a bar belonging to a **later** broker day arrives. There is no fixed
bar count for a day — a holiday, an early close or a late open makes any number wrong — and the
flag matters because something downstream is about to build an EMA out of it. A day in progress is
never written as complete, including after a restart that lost the buffer.

---

## Higher-timeframe context

`aureon/engine/mtf.py` aggregates M15/M30/H1/H4/D1 from **closed M5 bars already in hand**, with no
additional broker call. One polled stream per symbol, everything above it derived, because replay
must feed the same engine the same inputs (`§82`) and a second fetched stream could not be replayed.

A bucket is emitted only when it is complete and contiguous: a partial hour's close is not its
close, and a bucket with a hole in it is not an hour.

`MtfContext` rides on `Detection.mtf` and on `system_state`, with
`mtf_alignment: aligned | mixed | against`. It is stored on the detection because it was true at
that candle's close and is not recoverable afterwards — an H1 EMA pair at 14:35 depends on every H1
bar before it.

**It is context, not a signal.** No agent reads it, nothing gates on it, and no guard refuses a
trade because H4 disagrees. Alignment is recorded so a review can ask later whether alignment
mattered; answering that is what the evaluation rules are for. The evidence that this is true
rather than merely intended: when `mtf` was added, every agent's version moved and
`docs/PHASE2_BASELINE.md` was regenerated — and only the version strings changed. Every agent's
detection count over the fixture is identical.

`mtf` is excluded from the live-vs-replay document comparison, with the exclusion pinned by a test:
the tool replays one archived day while a live `mtf` is built from a rolling multi-day buffer, so
every detection would mismatch on a field that says nothing about parity. `--mtf` re-derives it
from the archive and diffs it properly instead.

---

## Notifications: claim before posting

One document per message in `notifications`, keyed by what the message is **about**
(`{kind}__{ref_id}`). `claim` creates it only if it does not exist, so the caller that wins may
post and every later attempt — including one after a restart, which is exactly when a duplicate
would otherwise be sent — finds it there and stays quiet.

**The order is a deliberate tradeoff and it is not symmetric.** Claiming first can LOSE a message:
the process dies between the claim and the post, and nothing retries. Posting first can DUPLICATE
one: the process dies between the post and the record, restarts, and posts again.

Aureon claims first. A lost detection embed costs a human a glance at `/status`. A duplicated one
trains them to ignore the channel — and a duplicated *price alert* tells them twice that a level
they are watching has been crossed, which is a reason to act, twice. The crash window is real and
is accepted; if the post then fails, the failure is recorded on the document where an operator can
see it.

Do not change this direction quietly. It is the kind of change that looks like a robustness
improvement and is a behaviour change.

---

## Ops events

`ops_events` holds the named operational conditions, one document each, and each is announced
**once when it starts and once when it clears** — never in between. A condition that persisted for
six hours would otherwise produce six hours of identical messages, an operator would mute the
channel, and a muted channel is strictly worse than no channel because it looks like coverage.

The name set is closed and an unknown name raises. Every event is phrased as something that is
either true or false right now, including the ones that sound like moments: `mt5_reconnect` is "the
connection is currently being re-established", so it has a recovery edge.

The state is a stored document rather than a variable for two reasons: `/ops` runs in the Discord
process while the conditions are detected elsewhere, and reading the row is what makes "once"
survive a restart — a new process genuinely does not know whether the condition was already active,
so a deploy mid-outage does not re-alert.

Two facts about who writes these, because both are easy to assume wrongly. Today the register is
written by the **observer** and the **launcher**, and by nobody else: `executor_stale` and
`monitor_stale` are the observer's to report, because it is the process that reads the others'
heartbeats, and `supervisor_stack_down` is the launcher's. And Discord **only reads** them — the
repository is writable in its context object because a read-only wrapper would be ceremony, not
because Discord raises conditions of its own.

**Nothing gates on an ops event.** A condition that should stop execution does it through the guard
with a `FailureCode`, where it is testable.

---

## Setups

A detection is a fact about one closed candle. A **setup** is a claim about a sequence — that a
level was swept, reclaimed and confirmed — and it therefore has the one thing a detection must
never have: a state that changes. Everything below follows from that.

### Where it runs, and what it may touch

The setup engine (`aureon/services/setup_engine.py`) runs **inside the observer**, after the
agents and after the context, on each closed M5 candle. It reads detections, levels, indicators
and the volume profile from what the observer already computed for the detections — nothing is
recomputed and nothing is re-fetched, so the setup engine and the agents cannot disagree about
what EMA(20) was. It writes `setups` and their events through `SetupRepository`.

It imports nothing from `aureon.execution` and holds no broker handle: `services` is on the
observation side of `OBSERVER_SIDE` in the boundary suite, which enforces both. It never mutates
a detection — a setup *references* the detection that advanced it, one way, because detections
are immutable.

**A setup reaching CONFIRMED posts a card and nothing else happens.** There is no path from any
state of any setup to an order.

### The four families

Each is a shape with a beginning, a middle and a way of being wrong, which is what makes it
measurable. The vocabulary is closed for the same reason the ops register's is: an open one grows
near-duplicates no review can group by.

| family | the shape | how it fails |
| --- | --- | --- |
| `LIQUIDITY_REVERSAL` | price sweeps a level and comes back | a close beyond the sweep extreme |
| `BREAKOUT_ACCEPTANCE` | price breaks a level and stays | a close back inside — `FAKEOUT_RISK` first, `INVALIDATED` only if it holds |
| `TREND_PULLBACK` | an established trend pulls back into its EMA zone and resumes | a close through the slow EMA against the trend |
| `MOMENTUM_TRANSITION` | the EMA pair narrows, the fast slope turns, the cross lands | the gap widening again without one |

Every threshold lives in `aureon/config/symbol_tuning.py`'s setup section, is versioned, and is
expressed in **ATR multiples** rather than points: a distance in points is different money per
instrument (decision 141) and the same market distance in ATR on both. Every number there is a
placeholder — no real session has produced a setup.

### The document, its history, and the id

`{prefix}_setups/{setup_id}` holds where a setup is now.
`{prefix}_setups/{setup_id}/events/{event_id}` holds how it got there, one row per change, with
the context that was true at the time. The split is the one §21 makes between a detection and its
evaluations, for the same reason: a summary carrying its own history grows without bound, and an
unbounded array is how a document comes to fail its write at a size limit nobody was watching.
So every list on the summary is capped and `event_count` is a number.

`setup_id` is a hash over `account_scope | symbol | timeframe | family | direction_context |
market_date | anchor_kind | anchor_price_bin | setup_version`, with the same separator and 32-hex
convention as a detection's. **One open setup per that tuple is not a separate rule — it is what
the id means**: a new anchor opens a new setup, the same anchor re-derives the same id and finds
the setup already there. A level tested twice in one day is one setup with two histories of
events; `market_date` is what stops it being one setup across a week.

`event_id` is a hash over `(setup_id, candle_close, event_type)`, which makes every write
idempotent: a candle re-processed after a restart, or replayed from the archive, derives the same
ids and writes nothing.

### The state machine, and the one transaction

```
OBSERVING → WATCH → DEVELOPING → CONFIRMED → PULLBACK → CONTINUATION → COMPLETED
                                     ↘ FAKEOUT_RISK ↙
        any non-terminal state → INVALIDATED        COMPLETED / INVALIDATED are terminal
```

Every state write passes `assert_setup_transition`. Expiry is recorded as `INVALIDATED` with
reason `expired` rather than as a state of its own — a setup that ran out of candles is a setup
whose claim was not borne out, and a separate state would be a second answer to "did it work".

`SetupRepository.record` writes the state change **and** its event in **one Firestore
transaction**. That is the only reason the two can be trusted together: without it, two observers
on the same candle could both read `OBSERVING`, both write `WATCH`, and both increment a count
read before the other's write — leaving the document and its history disagreeing. The emulator
suite exercises exactly that race.

### The seventeen observations (T-8)

`aureon/engine/watch_events.py` emits `WATCH_*` events — proximity to a level, an EMA gap or
slope, an RSI turn, tick-volume expansion, a profile reclaim or rejection, breakout pressure.
They are **setup events with `from_state == to_state`**, not detections, and the model refuses any
other shape. "Price is near the previous day's high" is true on dozens of consecutive candles and
is not a finding: minting a detection for each would double the detections collection with rows
nothing measures, silently changing the meaning of every hit rate computed over it.

Each observation names the families it is relevant to, and one about a level carries the level's
price so it attaches only to setups anchored in the same bin the id uses.

The vocabulary is policed: `absorption`, `footprint` and `order flow` appear nowhere in the
models, the embeds or the docs, and `delta` is banned in its market sense (`volume delta`,
`cumulative delta`, …) but not as arithmetic. Volume is always `tick_volume` — MT5 reports the
number of price *changes* in a bar, and calling it anything else would be a claim this system
cannot support.

### Outcomes: measured by the machinery that measures detections

A setup's outcome is measured **from the CONFIRMED candle**. Before that there is no claim to be
right or wrong about: an OBSERVING setup is "a level is nearby", which is not a prediction.

`aureon/services/setup_evaluation.py` measures nothing itself. It feeds the existing
`OutcomeTracker` — the same class, the same frozen rule, the same `point` — an in-memory
`Detection` standing for the confirmation, and re-keys the result onto the setup in
`{prefix}_setup_evaluations/{setup_id}__{rule_id}`. **That subject is never stored**, and it
carries the setup's own id so that if it ever reached `detections` by accident it would be
obvious rather than plausible. One definition of an outcome in the system; a second measurement
here would differ from the first on the first gap over a weekend, with no way to tell which was
right.

`confirmed_at` is stored on the setup rather than derived, because it is not derivable: a setup
that confirmed and then invalidated and one that invalidated from WATCH both read `INVALIDATED`,
and only the first ever made a claim.

### The reference block (T-9)

Every setup carries a `reference`: a **measured cohort of what past setups of the same shape
did**, built by `aureon/services/setup_reference.py` from `setup_evaluations` rows on strictly
earlier broker days.

It reuses `/monitor`'s arithmetic rather than repeating it — the same `quantile` and
`wilson_interval`, the same `TP_QUANTILES`/`SL_QUANTILES` pairs, the same `MIN_COHORT` floor, the
same `MATURE_REAL_DAYS` maturity rule and the same `classify_dates`. What differs is the
population and the cohort dimensions: symbol, family and direction context are never mixed, and
`price_vs_va`, `mtf_alignment` and `volatility_regime` are given up one at a time, in that order,
when there is not enough history — and the block **says which** it gave up.

Four things keep it from reading as an instruction:

* it publishes **points and no prices**. A price would render as a level on a card beside a
  chart, at a plausible distance from the market, which is indistinguishable from a target;
* below `MIN_COHORT` it publishes **no percentages at all**, only `insufficient` and the n;
* every rendering carries `SetupReference.caption`, which is the constant "historical reference ·
  measured cohort · research context" plus the n, "immature history" where the cohort spans fewer
  than `MATURE_REAL_DAYS` verified broker days, and which dimensions were widened away;
* it is measured **at open and never rewritten**, so a screenshot can be reproduced and the
  block answers "what did the record say when this was first noticed".

Nothing reads it to decide anything. A boundary test walks the engine's AST and fails on a
`.reference` read off anything but `self`.

### What a week says about them

The weekly review — never the daily one — gains a setups section: setups opened, by family, state
and direction context; how many reached CONFIRMED; how many have a complete horizon and how many
are still unresolved; the reached count per family with its denominator; the measured excursions
per family; and how many carried an immature reference. A setup's sequences do not fit inside a
broker day, so a daily section would report a week's structures three times with a different
incomplete answer each time.

---

## The invariants

These are the rules that hold everywhere, restated here because a rule that lives only in one
module's docstring is a rule the next contributor will not find. Most are enforced by a test; where
one is, the test is named.

1. **A detection never creates a trade.** No code path calls broker execution from the observer,
   agents, context, engine, evaluations, setup engine, reviews or Discord.
   → `tests/boundary/test_architecture_boundaries.py`
2. **Execution only from a human CONFIRM**: Discord → `trade_requests` CONFIRMED → the executor
   claims it with a lease → the guard → exactly one order. An unknown broker outcome is reconciled,
   never retried. The executor never invents a request.
3. **MT5 is broker truth; Firestore is application truth.** Detections are immutable, and derived
   state (evaluations, assessments, setups) lives in its own documents. Never add a field to
   `Detection` that encodes future information.
4. **Every status write goes through `aureon.models.enums.assert_transition`.** Never write a
   status field without it.
5. **Money-moving logic depends on `BrokerInterface`, never on MetaTrader5.** Only
   `aureon/data/mt5_provider.py` and `aureon/execution/mt5_broker.py` may import it, lazily.
   → `tests/boundary/test_architecture_boundaries.py`
6. **Every collection name comes from `aureon/storage/paths.py`** with `AUREON_COLLECTION_PREFIX`.
   No collection literal anywhere else. → `tests/boundary/test_architecture_boundaries.py`,
   `tests/unit/test_paths.py`
7. **Every timestamp is tz-aware.** `MarketTime` for stored records; never `datetime.now()` without
   a timezone, and never local OS time.
8. **Firestore writes go through the repositories in `aureon/storage`.** No raw client calls
   elsewhere. Tick data never goes to Firestore.
9. **Discord never calls the broker and never computes an indicator.** It writes the short list
   above and nothing else.
10. **Real and synthetic history stay distinguishable.** Every assessment and every setup reference
    carries `history_source` and `real_days`, and "immature history" under the threshold is stated
    on the screen rather than inferred.
11. **Volume terminology is honest.** `tick_volume`, because that is what MT5 reports: the number
    of price changes in the bar, not contracts traded. The words for the things this system cannot
    see are banned outright. → `docs/DOCS_CHECK.md`, `tests/unit/test_docs_consistency.py`

---

## Where to look next

| question | file |
|---|---|
| what a document contains | `docs/CONTRACTS.md` (generated) |
| why a design decision went the way it did | `docs/PHASE1_DECISIONS.md` |
| how to run it, and what to do when it breaks | `docs/RUNBOOK.md` |
| what is proven and what is still owed | `docs/PHASES.md` |
| what a phrase in the docs must never claim | `docs/DOCS_CHECK.md` |
