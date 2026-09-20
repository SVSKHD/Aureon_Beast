# Aureon — runbook

For the person running it. What to type, in what order, what each thing refuses to do and
why, and what to do when something is wrong at an hour when reading source is not an
option.

Everything here assumes `docs/PHASES.md` for *what is proven* and
`docs/PHASE1_DECISIONS.md` for *why a thing behaves the way it does*. This document is
only the operating half.

## The shape of it

Five entry points. They do not talk to each other except through Firestore, which is why
any of them can be restarted alone.

| process | what it does | what it may never do |
|---|---|---|
| `main_observer.py` | reads candles, runs the agents, writes detections and outcomes | call the broker. A detection never creates a trade. |
| `main_executor.py` | executes **CONFIRMED** trade requests, exactly once each | invent a request. It only ever acts on one a human confirmed. |
| `main_monitor.py` | reconciles positions from MT5's own deals | decide a trade closed. MT5 is the truth; Aureon observes it. |
| `main_discord.py` | the human interface | call the broker, or compute an indicator. It reads Firestore and writes trade requests, `settings.trading_enabled` and audit rows. |
| `main_review.py` | batch aggregation over history, on demand or from cron | react to anything live. It is re-runnable for any past period, which is the point. |

If you remember one thing: **the observer cannot trade, and the executor cannot decide.**

Long-running: observer, executor, monitor, Discord. `main_review.py` is a command.

## Getting it running the first time

Python 3.11+, and:

    python -m venv .venv && source .venv/bin/activate
    pip install -e ".[dev]"       # add ".[mt5]" on Windows, for the terminal
    make emulator                 # Firestore emulator, no Docker, needs Node + Java
    make test                     # the whole suite, emulator included

`make test-fast` skips the emulator suite. If the emulator is unreachable behind a proxy
(`Expected SETTINGS frame as the first frame`), that is gRPC honouring its own proxy
variables — `no_grpc_proxy=127.0.0.1,localhost` is the targeted bypass and the Makefile
already sets it.

MetaTrader5 is **Windows-only** and is imported lazily inside exactly two modules, so the
suite runs anywhere with a fake. Anything that needs a terminal says so by failing the
`mt5_init` preflight check rather than by crashing somewhere further in.

## Before anything

    cp .env.example .env        # then fill it in

Three variables are worth more attention than the rest:

- **`AUREON_ACCOUNT_SCOPE`** — part of every `detection_id`. Set it once and never change
  it; changing it re-keys the whole of history (decision 4).
- **`AUREON_COLLECTION_PREFIX`** — every collection is `{prefix}_{name}`. One variable
  moves a whole deployment's data, and it is the only thing separating a test run from
  production in the same project. Use a distinct prefix for anything experimental.
- **`AUREON_MARKET_TZ`** — the **broker's** server clock, not yours. Every session
  boundary and every broker date derives from it.

`trading_enabled` is deliberately **not** an environment variable. It lives in Firestore,
defaults to false, and a fresh deploy therefore cannot trade until a human turns it on.

## Daily: an observation session

    python scripts/preflight.py          # the day before, with time to fix things
    python scripts/session_run.py        # preflight again, then the observer
    # ... the session ...
    python scripts/session_verify.py 2026-09-16
    git add docs/evidence/session_2026-09-16.md && git commit

`session_run.py` **refuses to start on a preflight FAIL**. `--force` overrides it and
records in the evidence file that it did. Do not get into the habit.

The full procedure, including what to watch during the day, is
`docs/MT5_SESSION_CHECKLIST.md`. Re-run `session_verify.py` the next day: horizons that
were `PENDING` at the close resolve overnight, and the second run's outcome table is more
complete. It never rewrites the half recorded before the open.

## Daily and weekly: the reviews

    python main_review.py daily                 # yesterday on the broker clock
    python main_review.py weekly --with-dailies # the most recently FINISHED trading week

Both default to the most recently **completed** period, so "run it after the close" is the
whole of the scheduling configuration — cron or a systemd timer, daily after the broker day
ends and weekly after Friday's close. Reviews are idempotent, so re-running one is
harmless, and re-running after a bug fix is the intended repair.

`weekly` means the week that has just *traded*, not seven days ago: on a Saturday that is
the week ending the previous evening (decision on §63). Getting that wrong would file last
week's numbers under this week's heading with nothing in the document to say so.

    python scripts/report_outcomes.py --rule XAU_OUTCOME_V2 --from 2026-09-14 --to 2026-09-18

Per agent, per session, per horizon: reached counts as a fraction of **COMPLETE horizons
only**, median MFE/MAE, median time to $5, and the MFE/MAE-first split. Dates are broker
dates and both ends are inclusive.

`/status` on a closed market renders the weekly review instead of the live panels —
deliberately, because after the close those numbers are a snapshot of whenever the market
shut, and rendering them in a live layout invites reading them as current.

## Enabling execution

Read `docs/DEMO_EXECUTION_CHECKLIST.md` first, in full, and run the drills:

    make drills                                        # rehearsal, FakeBroker + emulator
    python scripts/demo_drills.py --all --broker mt5 \
        --evidence docs/evidence/demo_drills_2026-09-16.md

Nine drills. Five of them prove that **no order reaches the broker** — the kill switch, a
stale confirmation, a wide spread, an oversized lot, and a CLOSED trade refusing to be
rewritten. Three need a broker that will misbehave on demand and therefore SKIP against a
real terminal; their manual equivalents are in that checklist and are worth doing once.

Then, and only then:

    /trading enable        # in Discord, by an authorised user, audited

## When something is wrong

### Stop trading, now

In Discord: `/trading disable`. It writes `settings.trading_enabled = false` and its audit
row **in one transaction**, and the executor reads settings on every request, so it takes
effect on the very next one. Nothing in flight is cancelled by it — an order already sent
is at the broker, and the kill switch is not a way to unsend it.

If Discord is down, the switch is one Firestore document: `{prefix}_settings/execution`,
field `trading_enabled`. Setting it by hand skips the audit row, so say so afterwards.

### A request is stuck in EXECUTING

That is the designed state for *"we do not know"*, not a bug. The executor will **never
retry** it — a retry after an unknown result is how one intended position becomes two.

Reconciliation resolves it: it searches the broker for the request's `comment_token`
(`AUR:` + 6 characters, stamped **before** the send) and adopts what it finds. The
executor runs it at **start-up, before serving**, so restarting the executor is the normal
repair — and it is why a request abandoned by a dead process is resolved before a new one
can claim anything. If it finds *several*
matches it refuses to guess and marks the request `FAILED_RECONCILIATION` — that one needs
a human to look at the terminal.

### There is a position Aureon does not know about

Expected, and handled: positions opened by hand or from the mobile app carry no Aureon
magic number and are recorded with `source = external_mt5`, imported and never managed
(§52). MT5 is the truth. Do not delete the document; the monitor reconciles it from the
deals, including a close you performed in the mobile app.

### The observer died mid-session

    python scripts/session_verify.py <date>

`observer_ran_to_the_close` compares the last heartbeat with the last archived candle and
fails when the archive extends past the heartbeat. That is the failure with no other
trace: the archive, the detections and the outcomes all look normal and simply stop.

Restart it. Detections already queued in the outbox are delivered on the next start
(`enqueue` is idempotent on the detection id), and the reach-back re-reads enough candles
to warm the indicators — measured in **candles**, not minutes, which is what decision 104
was about.

### The outbox is growing

`outbox.db` holds detections that have not reached Firestore. Pending rows mean every
stored count is a lower bound. Preflight warns about them before a session and
`session_verify.py` fails on them afterwards, both with the count. The cause is nearly
always Firestore credentials or connectivity; the queue is durable and drains itself once
that is fixed.

### live-vs-replay reports a difference

Read which kind before changing anything:

| kind | meaning | look at |
|---|---|---|
| **missing** | the replay produced a detection the live run never stored | the outbox, then whether the live engine's window differed |
| **extra** | Firestore holds one the replay does not produce | the `agent_version` on the extras — a leftover from an earlier build is expected, and is what §12 is for |
| **mismatched** | same id, different content | the most serious: the id is a pure function of the candle, so something wrote a detection the engine would not produce |

### The observer refuses to start on a symbol

    UnknownSymbolTuning: EURUSD has no entry in aureon/config/symbol_tuning.py …

Working as intended (P-6). The level and wick thresholds are **not dimensionless** — 5
points of penetration is $0.05 on gold and something else entirely on an instrument with a
different tick and daily range — so an unreviewed symbol would produce detections that look
like sweeps and are noise, with nothing downstream able to tell. Add an entry. An **empty**
entry is a valid answer: it means the defaults were reviewed and kept.

## Reviews are per symbol

    python main_review.py daily
    python main_review.py weekly --with-dailies
    python main_review.py daily --symbol XAGUSD

Without `--symbol` every configured symbol is generated, each with **its own** frozen rule,
into its own document (`daily_reviews/{date}_{symbol}`). One combined review would state a
single `evaluation_rule_id` over numbers produced by two rules, and add horizon counts
measured against thresholds in two instruments' money.

A symbol failing does not stop the others: a missing silver review is not a reason to have no
gold review. The exit code is non-zero if any symbol failed.

**After upgrading to per-symbol reviews**, regenerate the periods you care about. Reviews
generated before the split carry no symbol, so `/status symbol:` will not show them — and
regenerating is safe by design: a review is a pure function of the data it aggregates, and
re-running one overwrites the same document.

## The Discord commands

| command | what it does |
|---|---|
| `/status [symbol]` | the live panels on an open market, the completed review on a closed one. `symbol:` narrows every number — panels, open trades, pending requests — to one instrument, read from that symbol's own state document |
| `/execute symbol side lot [detection]` | the market-order shortcut. One embed, one CONFIRM. The lot is typed — there is no default — and the filling mode is named on the screen rather than left to the broker |
| `/execute-trade` | opens the confirmation wizard (order type, stops, execution mode). Nothing is sent until a human confirms, and only the requester may confirm |
| `/close symbol:` | closes the one open position on that symbol. Two open positions on it, or none, and it says what it found and stops — Aureon does not choose which of your trades to end |
| `/cancel-order ticket [symbol]` | cancels a resting pending order. Naming the symbol refuses unless the ticket really is that symbol, at Aureon's records **and** at the broker |
| `/close-trade position_id [volume] [symbol]` | closes an open position, with the same cross-check when a symbol is named |
| `/trading status` | is trading enabled, and who last changed it |
| `/trading enable` | requires a confirmation, and lists what to check first |
| `/trading disable` | immediate, audited, effective on the next request |

## What the tools refuse to do

Knowing this in advance is cheaper than fighting it at 02:00.

- **Preflight** exits non-zero only on FAIL. A `SKIP` is not a pass, and the summary line
  says how many checks did not run.
- **`session_run.py`** will not start the observer on a FAILED preflight without `--force`.
- **`session_verify.py`** preserves everything recorded before the open, byte for byte. A
  session that went wrong cannot be tidied up afterwards.
- **The drills** refuse a terminal whose account reports itself as real.
- **The evaluation rules** are frozen. A threshold change is a **new `rule_id`**, never an
  edit, because editing one silently redefines every result already stored under it.
- **A CLOSED trade** refuses any field write except `last_reconciled_at` and
  `last_synced_at`.
- **`update_phases.py`** will not write a Status cell. What is proven is a judgement; it
  only reports which files exist.

## Where the numbers come from

| document | what it is | regenerate with |
|---|---|---|
| `docs/CONTRACTS.md` | every stored model, generated from the code | `make contracts` |
| `docs/PHASE2_BASELINE.md` | a replay of the committed **synthetic** fixture | `make baseline` |
| `docs/evidence/session_*.md` | one real session, bracketed by two tools | `session_run.py` / `session_verify.py` |
| `docs/PHASES.md` Evidence column | derived from the files that exist | `make phases` |
| `daily_reviews` / `weekly_reviews` | aggregation over stored detections and trades | `python main_review.py daily` / `weekly` |

`make check` runs the three `--check`s and the whole suite. It is the thing to run before
believing a document.

## The five-minute version

    make emulator && make test              # once, to know it works
    python scripts/preflight.py             # the day before a session
    python scripts/session_run.py           # the session
    python scripts/session_verify.py <date> # after the close, then commit the evidence
    python main_review.py daily             # after the broker day ends
    make drills                             # before ever enabling execution
    /trading disable                        # when something is wrong

## What none of it proves

The baseline is a replay of a **synthetic** fixture (decision 28). It proves the pipeline
computes what it says it computes; it says nothing about gold. The `XAU_OUTCOME_V2`
thresholds are $3–$20 because that is a distance a gold trader holds for, not because a
study showed they discriminate. The level and wick parameters are placeholders researched
on nothing (decision 120), so every `liquidity`, `breakout`, `wick` and `session_trend`
count is a count of what those arbitrary numbers happened to select.

Until `docs/evidence/` holds a verified session, every parity claim here is a claim about
the **engine** given a fixture — not about the engine and a broker agreeing on what a
candle is.
