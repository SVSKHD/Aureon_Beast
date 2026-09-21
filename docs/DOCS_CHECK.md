# Documentation drift register

Docs rot silently. A stale sentence does not fail anything — it simply keeps telling a reader
something that stopped being true, and the reader has no way to know which sentences are
current. `tests/unit/test_docs_consistency.py` turns the ones we can state mechanically into a
failing test.

This file is that test's input, and it is meant to be edited by hand. Add a row whenever a
correction lands that a doc might still contradict.

## What the test enforces

1. **Every collection is documented.** Each name in `paths.ALL_COLLECTIONS` must appear in
   `docs/CONTRACTS.md`. The registry is generated from `paths.py` itself (11A, F-10), so a new
   collection makes the contracts file stale and the test says which one.
2. **No banned phrase appears anywhere in `docs/`.** The list below. Each row says what the
   phrase used to mean, why it is wrong now, and what to say instead — so a hit is a repair
   instruction rather than a puzzle.
3. **`CLAUDE.md`'s reading list exists.** Every file that the standing rules tell a
   contributor to read before changing anything must actually be in the repo. This is how a
   four-phase-old instruction to read a missing spec gets caught.

## Banned phrases

| phrase | why it is wrong | say instead |
|---|---|---|
| `agent_version` excluded from | It was put BACK into `detection_id` in the corrections slice (decision 120). The caveat outlived the thing it described by four phases. | `agent_version` is part of `detection_id` (`DETECTION_ID_COMPONENTS`) |
| `total_volume` | Renamed in 11A F-5. The field is MT5 tick volume — price changes, not contracts — and the name now says so. | `total_tick_volume` |
| exchange volume | Aureon has never had exchange volume and cannot get it from MT5's M5 candles. | tick volume (MT5) |
| `weekday in (5, 6)` | 11B drives sleep/wake from `MarketStateService`, never from a weekday. A holiday is not a weekend and a Friday close is not midnight. | market state CLOSED, confirmed over `AUREON_CLOSE_CONFIRM_SECONDS` |
| `is_demo` | Never a field on `AccountInfo`; the `getattr` that read it was dead code for the whole of its life (decision 208). | `account.mode is AccountMode.DEMO` |
| restarted at the open | 11B: nothing restarts. The processes stay up and wake themselves, because a restart is the one moment a stateful service loses its cursor, its lease or its queue. | wakes itself before the open |
| services are stopped at the close | 11B: they park their loops and slow their heartbeats. A stopped service and a sleeping one look identical to an operator and only one of them is fine. | parks its loops |
| fetches the H1 | 11D: every timeframe above M5 is AGGREGATED from the M5 stream, so a claim that one is fetched describes a design this repository rejected for parity (§82). | aggregates the H1 from closed M5 |
| M1 in Firestore | Tick-scale data never goes to Firestore (CLAUDE.md). The M1 archive is parquet, and 11D's day cache deliberately stores M5/M15/H1 only. | the M1 parquet archive |

Phrases are matched case-insensitively as substrings, so keep them specific enough not to fire
on prose that happens to contain the words. A row that starts producing false positives should
be made narrower, never deleted — deleting it is how the drift comes back.

## Deliberate exemptions

- `docs/PHASE1_DECISIONS.md` is **append-only history**. A decision records what was believed
  and why at the time it was made; rewriting one to match the present would destroy the only
  record of the change. The phrase check skips it, and rows above that supersede an earlier
  decision say so in the newer decision's own text.
- `docs/PHASE2_BASELINE.md` and `docs/evidence/` are generated. They are regenerated rather
  than edited, so a stale phrase in them is a code fix, not a docs fix — but they are still
  scanned, because a generator that emits a stale phrase is exactly the bug worth finding.
