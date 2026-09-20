# Real MT5 session — validation checklist

The manual leg of §82. Everything in `tests/replay` proves the engine is deterministic
**given the same candles**; nothing in the repository has yet proven Aureon and a real
MT5 terminal agree on what those candles are. This is how that gets proven, once, before
any of the numbers are trusted.

It needs Windows and a running MT5 terminal, so it cannot be automated here. The tooling
it depends on **is** automated and tested: the observer archives the candles it processed,
and `scripts/compare_live_vs_replay.py` replays exactly those and diffs the result.

## The short version

    python scripts/session_run.py            # preflight, then run the observer
    # ... the session ...
    python scripts/session_verify.py 2026-09-16

Those two bracket the session and write `docs/evidence/session_{market_date}.md`, which is
what gets committed as the evidence for the Phase 2 gate. The rest of this document is
what the two scripts cannot do for you: decide whether the numbers they print are the ones
you meant, and read a failure.

`session_run.py` **refuses to start on a preflight FAIL**. `--force` overrides it and says
so in the evidence file — an override with a record is better than a gate people avoid by
not running it.

## What this is actually testing

Three things that only a live session can break, all of which the fixture cannot reach:

1. **Candle identity.** Does the broker's `copy_rates_from` return the same bars at the
   same instants Aureon assumes, in the timezone it assumes? A one-bar offset or a
   server-clock surprise changes every detection and would look like an indicator bug.
2. **Gaps that are not the weekend.** The fixture has one 49-hour weekend gap. A real
   session has rollover pauses, thin-liquidity minutes with no ticks, and the occasional
   broker hiccup. The restart reach-back was wrong about gaps for a whole phase
   (decision 104) and only the fixture's single gap caught it.
3. **The session boundaries against the broker's own clock.** `Europe/Athens` is a
   configured assumption (decision 6), not something the terminal told us.

## Before the session

Most of this list is now machine-checked:

    python scripts/preflight.py

`session_run.py` runs the same checks at start-up, so this is worth running **standalone
the day before** — a symbol missing from Market Watch or a prefix pointing at production
is much easier to fix with a day in hand than at 01:55.

Exit 0 with no `FAIL` row is the gate. **A `SKIP` is not a pass** — the summary line says
how many checks did not run, and a terminal check that did not run means the terminal was
not checked. Keep the output: it carries the terminal build, the broker server, the
resolved collection prefix and `trading_enabled`, which is most of what this session's
evidence needs to be re-readable a month later.

- [ ] **Set `AUREON_ACCOUNT_SCOPE` and never change it.** It is part of every
      `detection_id`. Changing it later re-keys all of history. *(preflight: `config`
      prints it; it cannot know whether you meant it.)*
- [ ] **Set `AUREON_COLLECTION_PREFIX`.** Use something other than `aureon_beast` for a
      first run — the validation data is worth keeping separate from whatever becomes
      production. *(preflight: `collection_prefix`, which WARNs on the production
      default.)*
- [ ] Confirm `AUREON_EMA_FAST` / `AUREON_EMA_SLOW` are the pair you mean (20 / 50). They
      are in `agent_params_snapshot`, and under §12 the agent version is in the detection
      id, so changing them after the fact forks the history rather than correcting it.
      *(preflight: `config` prints the pair.)*
- [ ] `AUREON_MARKET_TZ` matches the broker's server clock. Check it against the terminal,
      do not assume. *(preflight: `config` prints the zone AND its current UTC offset,
      which is the number to compare.)*
- [ ] **`trading_enabled` is false.** This is an observation session. `settings/execution`
      defaults to false; confirm it rather than trusting the default. *(preflight:
      `trading_enabled`, printed as INFO — it is not preflight's business to decide which
      session this is, only to make sure you know.)*
- [ ] The terminal is logged into the account and server the config names. *(preflight:
      `mt5_account`, a FAIL on mismatch. A session recorded from the wrong server looks
      completely normal afterwards.)*
- [ ] `data/live_candles/` is writable and has room. A day of M5 is a few hundred rows.
      *(preflight: `archive_dir`.)*
- [ ] The outbox has no undelivered rows from a previous run. *(preflight: `outbox` WARNs
      with the count. Two runs' delivery failures mixed together cannot be separated
      afterwards.)*
- [ ] This machine's clock agrees with the broker's. Candle boundaries are decided on the
      LOCAL clock, so a fast clock reads a bar as closed while the terminal is still
      writing it. *(preflight: `clock_drift`, which can only measure this while the symbol
      is ticking — re-run it near the open.)*
- [ ] Note the terminal's build number and the broker's server name. If a comparison fails
      later, "which terminal was this?" is the first question. *(preflight: `mt5_init`
      prints the build, `mt5_account` the server.)*

## Running it

- [ ] Start it with `python scripts/session_run.py`, not with `main_observer.py` directly.
      The wrapper runs the preflight, writes the evidence file **before** the observer
      starts, and stamps the stop time even if the observer crashes — which is the case
      where the record matters most.
- [ ] Start the observer **before the Asia open** (02:00 market time). Starting mid-session
      means the first candles come from the backfill rather than live, which is a different
      code path and not the one under test.
- [ ] Leave it running through the **New York close** (23:00 market time). A full day, so
      every session boundary and the day rollover are all crossed live.
- [ ] Leave it alone. No restarts, no config changes. A restart mid-session is worth
      testing, but as a *separate* run — mixing it into the first one makes a difference
      impossible to attribute.
- [ ] Watch `/status` a few times during the day and confirm the live panel is moving: the
      EMA pair changing, RSI changing, the cross counters incrementing, `session` rolling
      at the boundaries. A panel that has stopped updating is the failure to catch while
      the session is still running.

## After the close

- [ ] Stop the observer cleanly (SIGTERM / Ctrl-C, not a kill). Shutdown flushes the
      candle archive; a hard kill loses the day's tail, which the next flush would merge
      back but only if the observer runs again.
- [ ] Run the verification:

      python scripts/session_verify.py <market-date>

      Six checks, and it fills in the second half of the evidence file:

      | check | the silence it breaks |
      |---|---|
      | `archive` | the file the whole of §82 rests on is simply absent |
      | `archive_gaps` | an hour missing mid-session still produces a full-looking outcome table, with that hour's horizons quietly INVALID |
      | `live_vs_replay` | `compare_live_vs_replay.py`; the only check that can tell an engine difference from a broker one |
      | `detections_stored` | the observer can emit detections that never leave the outbox |
      | `outbox_drained` | and if they did not, every count in the document is a lower bound |
      | `observer_ran_to_the_close` | a process that died at lunchtime leaves an archive that looks normal and stops |

- [ ] **Exit code 0** with no `FAIL` row is the pass. A `SKIP` is not a pass: it means that
      check did not run, and the summary line says how many did not.
- [ ] Commit `docs/evidence/session_<market-date>.md`.
- [ ] **Re-run the verification the next day.** Evaluation horizons that were `PENDING` at
      the close resolve overnight, so the outcome table gets more complete. Re-running is
      safe and never touches the half written before the open.

## Reading a failure

The three difference kinds have different causes and different fixes. The script's own
docstring says this too, but it is worth having here before you start editing anything:

| result | what it means | where to look first |
|---|---|---|
| **missing** | the replay produced a detection the live run never stored | the outbox (did it drain?), then whether the live engine's window differed |
| **extra** | Firestore holds one the replay does not produce | `agent_version` on the extras — a leftover from an earlier build is expected and is exactly what §12 is for |
| **mismatched** | same id, different content | the most serious. The id is a pure function of the candle, so this means something wrote a detection the engine would not produce |

A **missing** result with a clean outbox and an identical window points at candle
identity: the broker served the live run something the archive did not capture, or the
archive's round trip lost precision. Compare a few rows of the parquet against the
terminal by hand before concluding anything about the engine.

## Recording the result

`scripts/session_run.py` and `scripts/session_verify.py` write it: one generated,
committed file at `docs/evidence/session_{market_date}.md`, carrying the market date,
symbol and timeframe, the terminal build and broker server, the collection prefix, the
commit **and whether the tree was dirty**, the preflight table, the comparison's full
output and the day's outcomes.

The commit matters most. A comparison is evidence about one build, and without the hash it
is an anecdote about an unknown one — which is why the tool records it rather than asking
for it, and why it records a dirty tree as loudly as it records the hash. A session run
from uncommitted changes is evidence about code that exists nowhere else.

Nothing in that file is written by hand. A hand-edited evidence file is indistinguishable
from a measured one, which devalues every other file beside it. `docs/evidence/README.md`
says the same thing where someone browsing the directory will see it.

## What this still does not prove

Worth stating so the evidence is not over-read:

- **One day is one day.** It proves the paths agreed on that session's candles. A month
  would say more, particularly across a DST change, which `Europe/Athens` has twice a year
  and which nothing here has ever exercised live.
- **Nothing about execution.** This is an observation session with trading disabled. The
  demo-account legs of Phases 4, 5 and 6 are still outstanding and are a different
  exercise entirely.
- **Nothing about the outcome rules.** `XAU_OUTCOME_V2`'s thresholds are $3–$20 because
  that is a distance a gold trader holds for, not because a live session showed they
  discriminate.
