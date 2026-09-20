# Demo execution drills — checklist

The demo-account leg of Phases 4, 5 and 6. Those phases each ask for a human on a demo
account, watching real orders, confirming that the things which must not happen do not
happen — and the reason it stayed outstanding is that *"try to make it place two orders"* is
not a procedure anybody can repeat identically.

`scripts/demo_drills.py` is the procedure. Each drill sets up one dangerous situation,
drives the real `ExecutionWorker` through it, and states in advance exactly what must be
true afterwards — including, for five of the nine, that **no order reached the broker at
all**.

    python scripts/demo_drills.py --list
    python scripts/demo_drills.py --all --broker fake     # rehearsal, against the emulator
    python scripts/demo_drills.py --all --broker mt5 \
        --evidence docs/evidence/demo_drills_2026-09-16.md

Exit 0 with no `FAIL` row is the pass. **A `SKIP` is not a pass** — the summary line says
how many did not run.

## The nine

| # | drill | proves | invariant | places orders | needs a scriptable broker |
|---|---|---|---|---|---|
| 1 | `confirmed_request_fills_once` | A CONFIRMED market request reaches the broker exactly once, with Aureon's comment token and magic number, and the fill is recorded | §29–§35 | yes | no |
| 2 | `kill_switch_stops_execution` | With `trading_enabled` false, a CONFIRMED request FAILS with `trading_disabled` and the broker is never asked to send | §57 | no | no |
| 3 | `two_executors_place_one_order` | Two executors racing one request produce exactly one order | §32 | yes | no |
| 4 | `expired_confirmation_never_sends` | A confirmation past its TTL is never claimed: `FAILED_STALE` + `confirmation_expired`, written inside the claim's own transaction | §42, §56 | no | no |
| 5 | `wide_spread_is_refused` | A spread that widened *after* the confirmation is refused with `spread_limit` | §41, §56 | no | **yes** |
| 6 | `lot_over_the_limit_is_refused` | A volume above `max_lot` is refused with `max_lot_exceeded` | §56 | no | no |
| 7 | `unknown_outcome_reconciles_never_retries` | A connection lost after the send leaves the request `EXECUTING` with its comment token, sends nothing more, and is repaired by reconciliation finding the order that already exists | §35 | yes | **yes** |
| 8 | `cancel_that_loses_to_a_fill` | A cancel arriving after the order filled is recorded FAILED saying the order was not resting — never as a completed cancel over a live position | §46, §47 | yes | **yes** |
| 9 | `closed_trade_refuses_to_be_rewritten` | A CLOSED trade refuses a write to `realized_pnl` and still accepts `last_reconciled_at` | §58 | no | no |

Three of them need a broker that will **misbehave on demand** — widen a spread, drop a
connection mid-send, fill a resting order because it was asked to. A real terminal does none
of those, so against `--broker mt5` those three report `SKIP` rather than quietly testing
something weaker. Their manual equivalents are at the bottom of this document.

## Rehearse first, and not as a formality

- [ ] Start the emulator (`make emulator`) and run every drill against `FakeBroker`:

      make drills          # = demo_drills.py --all --broker fake, against the emulator

- [ ] **Nine PASS.** A drill that is wrong about what should happen is worse than no drill:
      it produces a green table beside a broken guard, and the operator who reads it is
      worse off than if the drill had never existed.

This leg is also a committed test — `tests/failure_injection/test_demo_drills.py` — so a
change that breaks a drill's expectation is caught before anybody books a demo session.

## Before the demo run

- [ ] **A DEMO account.** Four drills place real orders; one deliberately loses a
      connection mid-send and leaves an order the reconciler then has to find. The script
      refuses a terminal whose account reports itself as real unless
      `--i-know-this-is-real-money` is passed — which exists so that the refusal reads as a
      decision rather than as an inability.
- [ ] `python scripts/preflight.py` — exit 0. The drills need the same terminal, prefix and
      clock every other tool does.
- [ ] **`trading_enabled` must be `true`.** This is the opposite of an observation session:
      drill 1 cannot fill anything with the kill switch on. Preflight prints the value; it
      is not preflight's business to decide which session this is.
- [ ] **A separate `AUREON_COLLECTION_PREFIX`.** The drills write trade requests, trades,
      control requests and audit rows. Mixing them into the prefix a real session uses
      leaves documents that look like trading decisions and were not.
- [ ] Note the demo account's balance. Drill 1 and drill 3 open positions and drill 8
      leaves one open; you will want to close them afterwards.

## Running it

- [ ] One drill at a time is fine and often better:

      python scripts/demo_drills.py --drill 1 --broker mt5

- [ ] Watch the terminal as each one runs. The drills assert on the stored documents and on
      what the broker accepted; **you** are the check on whether the terminal agrees with
      both. A drill that passes while the terminal shows two positions is the single most
      important thing this exercise could find.
- [ ] Write the evidence file: `--evidence docs/evidence/demo_drills_<date>.md`. It records
      the commit, whether the tree was dirty, the broker, the prefix, and every
      expectation with its observed value. Commit it.

## After the run

- [ ] Close every position the drills opened, and cancel anything resting.
- [ ] Confirm the audit trail: every `trade_request.*` and `control_request.*` action for
      the drill ids is in `audit_logs`. A drill that executed without an audit row is a
      §60 finding, not a tidiness issue.
- [ ] Re-read the `FAIL` and `SKIP` rows. A `SKIP` for a scriptable-broker drill is
      expected; anything else means that drill did not run and nothing about its invariant
      was shown.

## The three manual equivalents

These cannot be automated against a real terminal, and they are the ones worth doing by
hand exactly once.

**Drill 5 — a spread that widens.** Wait for a spread-widening moment the broker produces
on its own: the daily rollover, or the first seconds after a major release. Set
`max_spread_points` just under the normal spread, confirm a request, and watch it fail with
`spread_limit`. The point is that the guard compares against the price *you would trade at*,
not the one the confirmation carries.

**Drill 7 — an unknown outcome.** The honest version: confirm a request, and **pull the
network** (or kill the terminal) in the second after the executor logs its send. Then:

- the request must be `EXECUTING` with a `comment_token`;
- the terminal, once reconnected, must show **one** order carrying that token in its
  comment;
- `ReconciliationService` must adopt that order rather than sending another.

If you see two positions, stop and do not proceed to Phase 6. That is the failure this whole
design exists to prevent.

**Drill 8 — a cancel that loses to a fill.** Place a buy limit a few cents below the market
and wait for price to reach it. In the moment it fills, submit a cancel through Discord. The
control request must end `FAILED` saying the order was not resting — never `COMPLETED`,
because a human reading "cancelled" while holding a position is how a small loss becomes a
large one.

## What this still does not prove

- **Nothing about a live account.** A demo server fills differently, requotes differently
  and has different latency. Everything here is about Aureon's behaviour, not the broker's.
- **Nothing about sizing or strategy.** Every drill uses one small volume and cares only
  about what happened to the order.
- **Nothing that was skipped.** Read the summary line, not the colour of the table.
