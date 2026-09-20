# Real MT5 session — validation checklist

The manual leg of §82. Everything in `tests/replay` proves the engine is deterministic
**given the same candles**; nothing in the repository has yet proven Aureon and a real
MT5 terminal agree on what those candles are. This is how that gets proven, once, before
any of the numbers are trusted.

It needs Windows and a running MT5 terminal, so it cannot be automated here. The tooling
it depends on **is** automated and tested: the observer archives the candles it processed,
and `scripts/compare_live_vs_replay.py` replays exactly those and diffs the result.

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

Most of this list is now machine-checked. Run it first, and read the table rather than
skimming for green:

    python scripts/preflight.py

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
- [ ] Confirm the archive exists and covers the day:

      ls -la data/live_candles/
      # expect XAUUSD_M5_<market-date>.parquet

- [ ] Run the comparison:

      python scripts/compare_live_vs_replay.py <market-date>

- [ ] **Exit code 0 and `IDENTICAL`** is the pass. Anything else is a finding, not a
      formality.

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

Paste the comparison's full output into `docs/PHASES.md` under **Phase 2 real-session
evidence**, with:

- the market date, symbol and timeframe;
- the terminal build and broker server;
- the commit the observer ran at (`git rev-parse HEAD`);
- the exit code.

The commit matters most. A comparison is evidence about one build, and without the hash it
is an anecdote about an unknown one.

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
