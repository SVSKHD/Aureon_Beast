# Aureon — runbook

For the person running it. What to type, in what order, what each thing refuses to do and
why, and what to do when something is wrong at an hour when reading source is not an
option.

Everything here assumes `docs/PHASES.md` for *what is proven* and
`docs/PHASE1_DECISIONS.md` for *why a thing behaves the way it does*. This document is
only the operating half.

## The shape of it

`docs/ARCHITECTURE.md` is the as-built description: what the processes are, what writes what, and
which rules a test enforces rather than merely stating. It answers "which process writes `trades`?"
and "what stops a detection from trading?". This file answers "what do I type, and what do I do
when it breaks". Regenerate its ownership table with `make architecture` after adding a collection.

Five entry points, and a launcher that starts them. They do not talk to each other except through Firestore, which is why
any of them can be restarted alone.

| process | what it does | what it may never do |
|---|---|---|
| `main_observer.py` | reads candles, runs the agents, writes detections and outcomes | call the broker. A detection never creates a trade. |
| `main_executor.py` | executes **CONFIRMED** trade requests, exactly once each | invent a request. It only ever acts on one a human confirmed. |
| `main_monitor.py` | reconciles positions from MT5's own deals | decide a trade closed. MT5 is the truth; Aureon observes it. |
| `main_discord.py` | the human interface | call the broker, or compute an indicator. It reads Firestore and writes trade requests, `settings.trading_enabled` and audit rows. |
| `main_review.py` | batch aggregation over history, on demand or from cron | react to anything live. It is re-runnable for any past period, which is the point. |

If you remember one thing: **the observer cannot trade, and the executor cannot decide.**

Long-running: observer, executor, monitor, Discord, and `main_review.py watch`. The same
`main_review.py` also runs one-off daily and weekly reports as a command.

## Starting and stopping the whole stack

    python main_aureon.py

That is the documented way to run Aureon. It loads `.env` once, runs the preflight, and starts
the five processes in order — **observer → monitor → executor → Discord → review watcher** — as
separate children of one launcher. It is not a sixth service: there is no shared event loop and
no runtime object, only five `Popen` handles.

| flag | what it does |
|---|---|
| `--env-file PATH` | load that instead of `./.env` |
| `--no-observer` `--no-monitor` `--no-executor` `--no-discord` `--no-review` | leave that service out |
| `--no-preflight` | start without checking anything. Logs a warning. Development only. |

**Starting it never enables trading.** `settings/execution.trading_enabled` lives in Firestore,
defaults to false, and the launcher does not touch it. The executor may well start on a
real-money account; `AUREON_ALLOW_LIVE_EXECUTION` and the guard's first rule are what stop it
acting.

**A preflight FAIL starts nothing** and exits non-zero with the table printed. Fix the red rows.

**One child dying takes the whole stack down.** The launcher logs `child_exited` with the service
and its code, records the `supervisor_stack_down` condition in `ops_events` so it outlives the
terminal scrollback, stops the rest in reverse order and exits non-zero. There is no automatic
restart, deliberately: a half-running stack is the dangerous state — an executor with no monitor
means positions nobody reconciles, and it looks healthy from the one screen you have. If you want
it back up, that is systemd's job or yours, after reading why it went down.

Discord with no token is one of those deaths, on purpose: a bot that cannot answer anybody is not
a service. `--no-discord` is how you run without it.

**Ctrl+C is cooperative.** The launcher interrupts the children in reverse start order and waits
`AUREON_SHUTDOWN_GRACE_SECONDS` (default 20) before terminating anything, because the observer's
shutdown drains the outbox, flushes the parquet archive and saves its cursor. Each service logs
`shutdown_flushed` when it has finished; grep for it if you want to know whether the queue got
out. A service that ignores the interrupt is terminated after the grace, then killed.

Do not press Ctrl+C twice expecting it to hurry. The second one lands during the outbox drain,
which is the one part of shutdown that loses data if abandoned; the grace period is already the
bound on how long you wait.

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

## A local PostgreSQL for the tests

From Phase 13, PostgreSQL is the application truth. The tests that touch it are in
`tests/postgres/` and they **skip** when no server is configured, so `make test` still works
on a machine with no database — the skip message names both variables below. Nothing else in
the suite needs one.

There are two ways to give it a server, and the suite picks whichever it finds (C-10).

**A server already running, which is what CI does.** Bring up PostgreSQL however you like — a
service container in the workflow, a local install, whatever the machine has — and export the
URL:

    export AUREON_DATABASE_URL='postgresql+psycopg://aureon:PASSWORD@127.0.0.1:5432/aureon_test'

The suite uses that database as it finds it and never drops it, because dropping something it
did not create is how a harness deletes a scratch database somebody was using.

**No container, a local server you can create databases on.** Give the suite an admin URL
instead and it creates `aureon_test` at the start of the session and drops it at the end:

    sudo -u postgres psql -c "CREATE ROLE aureon LOGIN PASSWORD 'PASSWORD' CREATEDB"
    export AUREON_TEST_ADMIN_URL='postgresql+psycopg://aureon:PASSWORD@127.0.0.1:5432/postgres'

The role needs `CREATEDB` and nothing more.

### Applying the schema

The tables come from one Alembic revision, `0001_initial`, generated from
`aureon/storage/postgres/tables.py`. Three commands, and a deployment should run the third
before starting anything:

    python scripts/migrate.py upgrade    # bring the database to the head revision
    python scripts/migrate.py current    # what it is at, and what this build wants
    python scripts/migrate.py check      # exit non-zero unless they agree AND match the models

`check` exits **0** when the schema is current, **1** when it is wrong, and **2** when the
database could not be reached — three outcomes because the remedies differ, and a deploy
script that treated a stopped database as a schema problem would try to migrate it.

It fails in **both** directions, which is the part worth understanding:

- **behind** — somebody deployed without migrating. A query will hit a missing column.
  Run the migration.
- **ahead** — an *older* build has started against a schema a newer one wrote. It reads the
  tables it knows, writes the columns it knows, and leaves every column added since
  silently NULL. On `trade_requests` that is a broker ticket or a failure code that never
  gets recorded, in a run that reports success. **Do not migrate again** — it would do
  nothing and look like a fix. Run the build that matches the schema.
- **drifted** — the revision matches and the tables do not, because a model changed without
  a migration or a migration was hand-edited. Everything looks healthy until a query
  mentions the column that is not there.

**There is no `alembic downgrade`.** `0001_initial` raises instead. Dropping
`trade_requests`, `trades`, `audit_logs` and `control_requests` would not be a rollback —
it would be indistinguishable from the trades never having happened, while MT5 still held
the positions.

To tear down a **development** database, drop it by name — `DROP DATABASE aureon_test`.
Naming it out loud is the point: `alembic downgrade` does not, which is exactly why it is
the wrong tool here.

A **real** database is not torn down. It is restored from a backup, and the backup and
restore tooling does not exist yet — it arrives with C-3 in S-6, together with the
`backup_age` preflight row and a tested restore drill. Until then there is no supported way
to roll a real Aureon database back, which is the honest position: nothing has written to
one, because no service reads PostgreSQL until S-4.

After changing a model, regenerate both the migration and the contract:

    python -m alembic revision --autogenerate -m "what changed"
    python scripts/gen_contracts.py

`python scripts/gen_contracts.py --check` fails when `docs/CONTRACTS.md` is stale, and the
tables section is generated from the models, so a table added without regenerating is
caught rather than silently undocumented.

### The name is the safety rule

`AUREON_DATABASE_URL` for a test run must name a database called **`aureon_test`**. Anything
else is refused at conftest import, before a single fixture runs, and a URL naming `aureon`
is refused by name with its own message (C-9). This replaces the Firestore collection prefix,
which had nothing to prefix in a relational database. The suite creates, truncates and drops
tables; pointed at `aureon` it would do that to the only copy of every detection, evaluation
and trade the system has.

To check a machine is set up without running anything:

    python -c "import sys; sys.path.insert(0, '.'); from tests.postgres_support import resolve_test_url; print(resolve_test_url())"

A `(url, None)` means ready. A `(None, reason)` prints what is missing.

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
    git add docs/evidence/session_2026-09-16_XAUUSD.md && git commit

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

Since 11B there is a fourth way to run these, and it is the one to prefer:

    python main_review.py watch

It stays up and generates the week's last daily and every symbol's weekly **at the close**,
on the market's word rather than a cron line's. A cron line holding a UTC time is a fourth
opinion about when the week ends, and it cannot notice a broker that closed early for a
holiday or stayed open late after an outage. A closure that is *not* the week's end sleeps
the watcher and generates nothing: a "weekly review" of three days would be published with
the same field names as a real one, and nothing downstream could tell them apart.

## The weekend: what the services do when nothing is open

Nobody has to stop or start anything at the close. The four processes stay up, park what
they cannot usefully do, and bring themselves back before the open.

    AUREON_CLOSE_CONFIRM_SECONDS=300     # CLOSED must hold this long, on every symbol
    AUREON_PREOPEN_SECONDS=900           # how early they wake
    AUREON_SLEEP_HEARTBEAT_SECONDS=300   # heartbeat cadence while asleep
    AUREON_SLEEP_POLL_SECONDS=60         # how often a sleeping service asks

What each one does is different, and the differences are deliberate:

| service | at the close | while asleep |
|---------|--------------|--------------|
| observer | drains the outbox, flushes the candle archive, saves the cursor | stops polling candles and answering alerts; keeps the terminal connected |
| executor | nothing | **keeps serving requests**, slowly. A trade confirmed on a Saturday is refused with `MARKET_CLOSED`, not left waiting |
| monitor | one full reconciliation | stops polling: every number it records comes from a quote |
| review | the week's last daily and every symbol's weekly | waits |
| Discord | — | slows its heartbeat and its announcement sweep; `/status` says "closed — next open …"; `/execute` is refused; `/remind` still arms |

Three things to expect, so none of them reads as a fault:

* **`/status` on a Saturday shows every service LIVE**, not STALE, even though they are
  beating once every five minutes. The freshness threshold widens to twice the sleep cadence
  while the services are asleep. Two missed sleep beats is still reported, so a process that
  really died over the weekend still shows up.
* **`/execute` is refused outright**, where a merely stale feed is only a warning. A
  confirmation lives for a minute and the market will not open for two days, so the only
  thing it could do is expire.
* **a bar straddling the close is thrown away**, with a WARNING naming it. Its open is the
  last price before the weekend and its close the first price after, so its range is the
  weekend gap; a cross found in it would be a two-day move that involved no trading.

Only CLOSED sleeps anything. A STALE feed inside trading hours, or a provider that cannot
say what a symbol is, keeps everything awake and the heartbeats fast — those are faults, and
they are exactly when the system must stay loud. One symbol closing is not the market
closing: every configured symbol has to read CLOSED, or the observer would stop watching
gold because silver was disabled.

If you need a service to behave as it did before 11B, set `AUREON_CLOSE_CONFIRM_SECONDS` to
something longer than a weekend. Nothing will ever confirm a close, and everything stays
awake.

## Higher timeframes, and where they come from

The observer polls **one** stream per symbol: M5. M15, M30, H1, H4 and D1 are aggregated from
those bars — no second poll loop, no extra broker call, no second cursor. Parity is the reason:
replay feeds the same engine the same M5 bars, so an H1 bias derived from them is reproducible
from the archive, where a broker's own H1 is its own aggregation and a difference between the
two would be indistinguishable from a bug in either.

A higher bar exists only when **every M5 bar inside it has closed, with none missing**. An hour
holding eleven of its twelve bars has a high that may still be exceeded and a close that is not
the close, so it is simply absent — the same rule the M5 grace period applies, one level up. A
daily bar is complete when a bar from the NEXT broker day arrives, because there is no fixed bar
count for a day and a holiday makes any number wrong.

Every detection now carries `mtf`: one read per timeframe (the EMA pair, the close and the
bias) plus an `alignment` of `aligned` / `mixed` / `against`. Three things to know when reading
it:

* **nothing gates on it.** No agent reads it, no guard refuses a trade because H4 disagrees.
  Whether alignment predicts anything is a question for the evaluation rules, and
  `scripts/report_outcomes.py` is where it will be answered.
* **`mtf` being absent is not "flat".** A detection with no context came from a process with too
  little history; a timeframe that was read and had no view records `sideways`. `mixed` covers
  both "some agree and some do not" and "nobody has a view" — a sideways timeframe is an
  absence of evidence, and counting it as agreement would make `aligned` mean "we could not
  tell".
* **the agent versions moved.** ema_cross is 2.2.0 and the other five are 1.2.0, because a
  detection carrying the context and one without it would otherwise sit at the same
  `detection_id` — indistinguishable and incomparable.

    AUREON_MTF_M5_BARS=2880   # ten 24-hour days; enough for an H4 EMA(50). 0 disables it

That buffer is deliberately separate from the analysis window, which is a **correctness**
parameter fixed at the hungriest agent's requirement: widening it to reach H4 would silently
change every agent's detections.

D1 needs fifty trading days, which no live buffer will hold. That is what the day cache is for.

## The broker-day cache

At each day rollover the observer writes two documents per symbol:

* `{prefix}_market_days/{SYMBOL}_{date}` — the day's open, high, low, close, bar count and tick
  count. One small read for "what did Tuesday do".
* `{prefix}_market_day_frames/{SYMBOL}_{date}_{M5|M15|H1}` — the bars, so a process with no
  broker connection can rebuild a chart or a multi-week H4 bias.

M30, H4 and D1 are **not** cached: each is derivable from the stored M5 or H1, and a second
stored copy is a second thing that can disagree with the first. M1 is not cached either — tick
data never goes to Firestore, and the M1 archive is parquet, where it is read for exactly one
job (rebuilding a position's excursions for a stretch the monitor was down).

Two things to expect:

* **the day in progress is not stored.** Only finished days. A document flagged incomplete is
  one mistake away from an aggregation reading it as finished, which produces a daily bar whose
  close is not the close. Use the parquet archive for a live view.
* **a truncated frame says so.** The bar list is capped at 600 and sets `truncated`; the day's
  first bars are kept. Hitting the cap means the aggregation is wrong, not that the market was
  busy.

## Choosing a threshold, and why no tool applies one

    python scripts/tune_report.py --symbol XAGUSD --days 10

Writes `docs/TUNING_{SYMBOL}.md`: one section per threshold, each with the distribution of
the quantity the agent compares against it and a table of candidate values against how many
bars each would admit. Read it for the two extremes rather than for a recommendation — a
value above every observed measure produces no detections and looks exactly like a quiet
market, and one below the first quartile produces a detection on most bars and looks exactly
like a busy one. Neither is visible from the detections.

It reads the parquet archive the observer writes, so it needs a recorded session and refuses
without one. `--fixture` reads the generated fixture instead and stamps **SYNTHETIC** on
every page: a random walk has the distribution its generator was given, so a threshold
chosen from one is a threshold chosen from `scripts/gen_fixtures.py`.

**There is no `--apply`, and there will not be.** Changing a threshold is an `agent_version`
bump: edit `OVERRIDES` in `aureon/config/symbol_tuning.py` and bump the agent's version in
the same commit. Without the bump the detections from before and after sit at ids produced by
the same version — indistinguishable and incomparable — and the whole point of D-1 putting
`agent_version` back into `detection_id` was to make a retune safe to do and safe to undo.

The first thing the tool said, on gold: `min_range_points = 20` admits about 99% of
five-minute bars. As a filter it does almost nothing.

## Reading a `/monitor` history line

Since 11C every stored readout records where its cohort came from, and the screen says so
above the numbers whenever it is anything other than a mature real one:

| line | what it means |
|------|---------------|
| (nothing) | real bars, ten or more verified broker days. The only case that is evidence about the instrument |
| `history: SYNTHETIC` | replayed fixture bars. Every rate describes `gen_fixtures.py` |
| `history: MIXED` | some of each. The rate is a weighted average of a real frequency and a generated one |
| `history: N verified broker day(s) … immature` | real, but too few days. Two hundred detections from one Tuesday are not two hundred independent observations |
| `history: not recorded` | written before the field existed. Treat the rates as unverified |

"Verified" means a `docs/evidence/session_{date}_{SYMBOL}.md` containing `SESSION VERIFIED`
— written by `scripts/session_verify.py` after six checks against a real terminal. A replay
writes detections into the same collection a live session does, so the evidence file is the
only thing that can tell the two apart. **Today this repository has none**, so every cohort
it can build is synthetic and every `/monitor` screen says so.

The weekly review keeps its single `assessment_hit_rate` and adds `assessment_hit_by_source`
beside it. Read the split before quoting the rate.

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
| `/status [symbol]` | the live panels on an open market, the completed review on a closed one. `symbol:` narrows every number — panels, open trades, pending requests — to one instrument, read from that symbol's own state document. Each panel ends with the §19 block: the tick-volume POC and value area for the current session and for Asia, the HVN and LVN nearest price, and ATR14 with its volatility regime (9B) |
| `/execute symbol side lot [detection]` | the market-order shortcut. One embed, one CONFIRM. The lot is typed — there is no default — and the filling mode is named on the screen rather than left to the broker |
| `/execute-trade` | opens the confirmation wizard (order type, stops, execution mode). Nothing is sent until a human confirms, and only the requester may confirm |
| `/close symbol:` | closes the one open position on that symbol. Two open positions on it, or none, and it says what it found and stops — Aureon does not choose which of your trades to end |
| `/cancel-order ticket [symbol]` | cancels a resting pending order. Naming the symbol refuses unless the ticket really is that symbol, at Aureon's records **and** at the broker |
| `/close-trade position_id [volume] [symbol]` | closes an open position, with the same cross-check when a symbol is named |
| `/remind price symbol: level: side: [note]` | arms a one-shot price alert. Aureon tells **you** once when a quote it already reads crosses the level; it expires in 24 hours, 20 armed per person. A level already behind the market is refused with the current price, because it would fire on the next quote |
| `/remind list` | your alerts, armed ones first with their remaining time |
| `/remind cancel id:` | disarms one of your own alerts |
| `/monitor symbol: [detection:]` | what the **measured record** says about a detection: the observer's published trend read with its evidence, the cohort of prior detections that matched it and what had to be dropped to reach thirty, how often each threshold was reached with n and a 95% interval, and target/adverse quantiles of measured excursions. Below thirty prior detections it says "insufficient history (n=…)" and shows **no percentage at all**. Nothing here is a forecast and nothing prefills an order |
| `/note trade:<id\|last> text:` | your own words about a trade, stored **beside** it so a CLOSED trade stays untouched (§45). `#tags` group the week in the weekly review. Nothing automated ever reads a note |
| `/setups symbol:` | what structures are being tracked on that instrument right now: one line each with the family, the direction **context**, the state and the anchor. Open setups only — a completed one is history and belongs to the weekly review |
| `/setup id:` | one setup's card, the same card the channel posts: state badge, anchor, invalidation price (labelled **not a stop**), its last events, the detections that advanced it, and its measured historical reference with the caption that says what the numbers are. Takes the full id or the short prefix `/setups` lists; an ambiguous prefix is refused by name rather than resolved to the first match |
| `/trading status` | is trading enabled, and who last changed it |
| `/trading enable` | requires a confirmation, and lists what to check first |
| `/trading disable` | immediate, audited, effective on the next request |

## The channel announces; it never decides

Set `AUREON_ALERT_CHANNEL_ID` and the bot posts each enabled detection there, once, with
`[Monitor]` and `[Execute]` under it. Leave it unset and it announces **nothing** and says
so in the log on startup — the alternative is a bot that picks a channel it can see and
posts market calls into it.

- **What is announced** is decided by `settings/notifications`: `detections_enabled`, and
  `enabled_kinds` (the agent names). Read fresh on every sweep, so silencing a noisy agent
  takes effect on the next detection rather than the next deploy. The defaults are the four
  an eye watching a chart would notice — `ema_cross`, `liquidity`, `breakout` and
  `wick` — and turning detections off keeps the list, so turning them back on restores what
  you had.
- **A fired `/remind` alert goes to you directly**, not to the channel: an alert is one
  person's question, and the channel is readable by more people than armed it.
- **Once each.** The record is a document (`{prefix}_notifications`), written with a
  `create` that Firestore refuses over an existing one — so a restart, a second bot, or two
  overlapping sweeps produce one message. A post that **fails** is recorded as FAILED for
  the operator and not retried: a retry over a channel that is rejecting messages either
  double-posts or hides the outage.
- **A bot that was down does not catch up.** Each sweep reads only the last
  `AUREON_NOTIFY_WINDOW_SECONDS` (default 120), so an hour of downtime produces a gap
  rather than an hour of stale detections arriving at once. The gap is visible; the stale
  flood reads as live.
- **`[Execute]` is a shortcut through the typing, not through the authorisation.** It opens
  a modal asking for the lot — no default, no "same as last time" — and the typed lot then
  goes through the same confirmation, the same quote check and the same CONFIRM as
  `/execute`. The symbol and side are prefilled because they are what the embed is about;
  a detection with no direction has the button removed rather than defaulting to buy.
- **Every embed is the same neutral colour** and carries `research only · not a
  recommendation`. A green buy and a red sell would read as approval, and the eye reaches
  the colour before the words.

If the channel goes quiet, check in this order: is `AUREON_ALERT_CHANNEL_ID` set (the log
line on startup says); is the observer writing detections at all (`/status`); is the agent
in `enabled_kinds`; and is there a FAILED row in `{prefix}_notifications` naming a
permissions error.

## Reading a `/monitor` screen

Every number on it was measured. Nothing on it is a prediction, and the four things worth
knowing before you act on one:

- **The n and the interval are the number.** "Reached +$5 in 60%" from thirty-five prior
  detections and from three hundred are the same four characters. The count and the 95%
  interval are printed next to every rate for that reason; if the interval is wide, the
  rate is not telling you much.
- **The cohort may not be the question you asked.** It matches on symbol, agent, direction
  and session always, and on volatility regime, value-area position and wick tag when there
  is enough history. When there is not, it drops them one at a time — wick, then value area,
  then regime — and the screen says `widened by dropping: …`. A cohort that dropped the
  regime is answering "what followed this signal" rather than "what followed this signal in
  a session this size".
- **"Insufficient history" is the honest answer, not a failure.** Below thirty COMPLETE
  evaluations it publishes no rate rather than a small one, because a rate from eleven
  detections reads exactly like a rate from three hundred.
- **The target and adverse figures are quantiles, not levels.** `p50 400pt (2404.00)` means
  half of the matched prior detections got at least that far before the horizon ended. It is
  a description of the past, not a target, and nothing in Aureon will ever put it on an
  order: `/execute` still asks for the lot and still requires CONFIRM, and no SL or TP is
  prefilled anywhere.

If the bias reads `sideways` with the single evidence line "no trend read published", the
observer is not writing state — check `/status`. Discord cannot compute the trend read
itself; it holds no data provider at all.

## The weekly review scores its own readouts

From 9D the weekly review carries two things it did not before.

**`assessment_hit_rate`** asks, of every `/monitor` readout produced that week, whether
price reached the target quantile it published before the stop quantile it published. Four
outcomes, and the split matters more than the rate:

- **hit** — the target's distance was covered and the stop's was not;
- **miss** — the stop's was and the target's was not;
- **neither** — neither distance was covered inside the horizon. Counted against the rate,
  because the question is "did the target come first" and the answer is no, but kept apart:
  a target nobody got near is a different lesson from one price ran away from;
- **unresolved** — both were covered, and candle data cannot say which came first. Excluded
  from the rate entirely rather than guessed at.

The rate is also broken out **per cohort**, because "it holds when the volatility regime
matched" and "it holds in general" are different claims — a cohort that had to widen is the
weaker one, and a single blended number would hide which is which. `assessments_not_scored`
counts the readouts that published nothing because there was not enough history; that number
should fall as the record grows.

Nothing writes the outcome back onto the assessment. The assessment says what was believed
and from what evidence; the review says how it turned out.

**Your notes** are printed under each trade beside its outcome, and the week is grouped by
the `#tags` you used. They are copied into the review document rather than linked, so a
review read next year still says what you said at the time. No number in the review, and
nothing in `/monitor`, ever reads one.

## Firebase key rotation

The key is a service-account JSON that lives **outside the repo** — on the VPS at
`/etc/aureon/firebase-key.json`, readable only by the service user — and `.env` records its
path, never its contents.

To rotate:

1. Create a new key for the same service account in the Google console. Do not delete the
   old one yet.
2. Copy it to the box beside the current one (`firebase-key.new.json`), `chown` and `chmod`
   it to match.
3. `GOOGLE_APPLICATION_CREDENTIALS=/etc/aureon/firebase-key.new.json python
   scripts/preflight.py --skip-mt5`. The `credentials` row names the service account and
   project it resolved to, and the `firestore` row does a real write and read-back. Both
   green, or stop.
4. Move the new key over the old path, restart the four services, run preflight again.
5. Only then delete the old key in the console.

**What preflight tells you when it is wrong.** The `credentials` row is a diagnosis and the
`firestore` row is the proof, and they are separate on purpose:

- `no such file` — the path in `.env` does not exist. Check the path, not the key.
- `"type": "authorized_user", not "service_account"` — what `gcloud auth
  application-default login` writes, copied in by mistake. Not a key.
- `carries an OAuth client block — this is an OAuth client secret` — the single most common
  mistake. Both files are JSON and both come from the same console; only one of them works.
  Downloading the same file again will not help.
- `missing private_key` (or another field) — a service-account key with something removed,
  usually by a copy that went through an editor.
- `not valid JSON` — usually a truncated copy or a PEM pasted over the JSON.
- `the key is for Firestore project X and AUREON_FIREBASE_PROJECT_ID says Y` — **the one
  credential mistake that produces no error at all.** The key authenticates, the write
  succeeds, and the whole session lands in another project's database, which looks exactly
  like a first run because collections are created on demand. Fix one or the other; do not
  start a session on a mismatch.
- `GOOGLE_APPLICATION_CREDENTIALS is unset` — a WARN, not a failure: application default
  credentials from `gcloud auth application-default login` are a legitimate developer setup.
  On a box that runs unattended for a week, set the variable.
- `no usable credentials` on the `firestore` row — the library found nothing at all.
- `permission denied writing to this prefix` on the `firestore` row — the credentials are
  **fine** and the identity they name may not write. Grant that service account
  `roles/datastore.user` on the database; the remedy on the row names it. Handed the generic
  advice you would go looking for a better key file and find nothing wrong with the one you
  have, which is why this is a separate diagnosis.
- an emulator host is set — `credentials` reports `EMULATOR at …` and SKIP, never PASS, and
  names the prefix it is writing to. A green row for a check that never ran is how an
  emulator-only run comes to look like evidence about production.

Every message on these two rows says Firestore, or names a Firestore variable. None of them
says "credentials" unqualified: the same `.env` holds the MT5 login, and that is the one an
operator reaches for first.

The `config` row prints `env_file=` — the file the launcher actually loaded, or `none`. That
is the row to read first when a value looks wrong, because the commonest surprise is not a
wrong value but a file that was never read: a `--env-file` typo, or a service started from
another directory. It says `none` rather than `.env`, deliberately — printing a plausible
filename for a file nobody read is the failure the line exists to prevent.

## Which account is the terminal on?

**Aureon attaches to the terminal you logged in.** That is the normal setup and the intended
one: start MT5, log in, and every service attaches to that account. Leave
`AUREON_MT5_LOGIN`, `AUREON_MT5_PASSWORD`, `AUREON_MT5_SERVER` and
`AUREON_MT5_TERMINAL_PATH` blank. The `mt5_account` row then reads

    PASS   mt5_account   using attached terminal account: login=… server=… mode=DEMO currency=USD trade_allowed=true

Those four variables are **overrides**, for two situations: more than one terminal on the box
(pin the path), or a machine where somebody could plausibly leave the wrong account logged in
(pin the login and/or the server, and the row FAILS on a mismatch instead of reporting what it
found). One of them is enough — setting only `AUREON_MT5_SERVER` verifies the server. A
password is needed only if Aureon must perform the login itself; prefer logging in by hand and
leaving it blank, because it is a secret in a file.

Until 12 T-3 this row WARNed when no login was configured, with a remedy telling you to set the
variables. That was backwards: it made the normal configuration look incomplete and asked for a
password nothing needed.

`preflight` prints `mode=DEMO`, `mode=REAL`, `mode=CONTEST` or `mode=UNKNOWN` on the
`mt5_account` row, from MT5's own `account_info().trade_mode`. A real or unidentified account
is a **WARN, never a silent pass** — an operator scanning a green table would not notice, and
that outranks the attach case: no login configured must never turn a real-money terminal into a
PASS.

What each part of the system does about it:

- **Observer and position monitor** run on a live account quite happily. They are read-only,
  and the observer publishes `system_state.account_mode` so `/status` and the session
  evidence file record which account the candles came from.
- **Executor** starts in **reconcile-only** mode on a real-money account unless
  `AUREON_ALLOW_LIVE_EXECUTION=true` is set on that box: startup reconciliation runs as
  usual, every confirmed request is refused with `live_execution_not_allowed`, and one ops
  message says which terminal is open. The refusal is the guard's *first* rule, ahead of
  `trading_enabled` — when both are wrong at once you need to hear about the terminal, not
  about a switch you turned off on purpose.
- **`/trading enable`** refuses outright on a live account without that variable, rather
  than arming a switch the executor is going to ignore. With the variable set it shows a
  second screen naming the account and the word LIVE before offering the button.
- **`demo_drills.py`** refuses any account that is not `DEMO` — contest included, since a
  contest account is somebody's competition entry and one drill drops a connection
  mid-send. `--i-know-this-is-real-money` overrides it, and is spelled that way on purpose.

`UNKNOWN` is treated as real money everywhere. A terminal that will not say what it is
logged into has not said it is a demo, and the asymmetry is not close: being wrong in that
direction costs a refused order, being wrong the other way costs somebody's savings.

## The ops register: `/ops`

Aureon runs four processes on a box nobody is watching. The failure that costs money is almost
never a crash — the supervisor handles those. It is a service that is **still running and no
longer doing its job**: an observer whose candle loop stopped while the market is open, an
outbox whose deliveries have been failing for a minute, a reconciliation that ended ambiguous
and left a trade nobody has looked at.

Ten named conditions now cover those, each posted **once when it starts and once when it
clears**:

| condition | what it means | threshold |
|---|---|---|
| `observer_stale` | no candle has closed while the market is OPEN — running, not observing | `AUREON_OPS_OBSERVER_STALE_INTERVALS` × timeframe (2) |
| `executor_stale` | the executor's heartbeat stopped; confirmed requests will sit | heartbeat freshness |
| `monitor_stale` | positions are not being reconciled against the broker | heartbeat freshness |
| `firestore_unavailable` | outbox delivery failing; detections are queued on disk | `AUREON_OPS_FIRESTORE_UNAVAILABLE_SECONDS` (60) |
| `outbox_backlog` | deliveries are slower than detections arrive | `AUREON_OPS_OUTBOX_BACKLOG` (50) |
| `reconciliation_ambiguous` | a request is FAILED_RECONCILIATION — whether an order exists is UNKNOWN | any |
| `archive_write_failed` | a candle did not reach the parquet archive; parity cannot be checked for that session | any |
| `symbol_feed_stale` | **per symbol**: no tick while OPEN — the feed is dark | `AUREON_OPS_FEED_STALE_SECONDS` (30) |
| `mt5_reconnect` | the terminal connection dropped and is being re-established | any |
| `live_account_detected` | this process is on a real-money account | any |

**Once, not every poll.** A condition that persists for six hours produces two lines, not six
hours of identical ones. An operator who gets the latter mutes the channel, and a muted channel
is strictly worse than no channel because it looks like coverage.

**`/ops` distinguishes three states**, which is why cleared rows are kept rather than deleted:

- 🔴 **active**, with how long — `since` records when the state *began*, so a three-hour-old
  onset reads as three hours old.
- 🟢 **clear**, with a count — "this flapped twice this morning and cleared" and "this fired
  once and cleared" are different problems, and both read as fine without it.
- **no row at all** — it has never happened since this deployment started. The footer says how
  many of the ten are in that state, because a register with two rows could mean eight are
  healthy or that eight are not wired up.

`symbol_feed_stale` is the only per-symbol condition. One event covering both instruments would
clear the moment either recovered — reporting healthy while silver was still dark.

Nothing gates on an ops event. A condition that should stop execution stops it through the
execution guard with a `FailureCode`, where it is testable; the register only reports.

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
| `docs/evidence/session_*_{symbol}.md` | one real session of one symbol, bracketed by two tools | `session_run.py` / `session_verify.py` |
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
