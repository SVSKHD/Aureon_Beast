# Session evidence

One file per session **per symbol**: `session_{market_date}_{symbol}.md`, on the **broker**
date. Every check inside is about one instrument — its detections, its archive, its
live-vs-replay comparison, its outcome rule — so two symbols are two records (9A).

Every file here is **generated** by two tools and committed:

| when | tool | what it writes |
|---|---|---|
| before the open | `python scripts/session_run.py` | the header, the preflight table, the commit, the terminal build — everything above `## After the close` |
| after the close | `python scripts/session_verify.py {market_date}` | the checks, the live-vs-replay comparison, the stored counts and the day's outcomes — everything below it |

## Read the two halves differently

The **opening half** is a record of what was true going in, written before anything
started. It cannot be rewritten: `session_verify.py` preserves every line above the
marker byte for byte and replaces only what is below. That is deliberate. If the whole
document were written at the end, a crashed observer or an abandoned session would leave
no record at all, and *"we tried on the 16th and something went wrong"* is the least
useful sentence a research log can contain.

The **closing half** is regenerated on every verification, and re-verifying the next day
is normal rather than suspicious: evaluation horizons that were `PENDING` at the close
resolve overnight, so the outcome table the day after is more complete than the one from
five minutes after the bell.

## Nothing here is written by hand

A hand-edited evidence file is indistinguishable from a measured one, which makes every
file here worth less. If something is wrong, fix the tool and re-run it.

An unbracketed session — one verified without `session_run.py` having recorded it — gets a
document that says so in its first paragraph. It is still worth keeping; it is simply not
the same artefact, because nothing in it attests to the preflight and its commit is the
one *verification* ran at.

## The Evidence column reads this directory

`docs/PHASES.md` carries an **Evidence** column, and it is derived rather than typed:
`python scripts/update_phases.py` rewrites it from what is actually here. A phase says
`⬜ missing` until a file exists that contains `SESSION VERIFIED`, and goes back to
`⬜ missing` the moment that file is removed. `--check` fails if the column is stale, so
the table cannot drift into claiming evidence the repository does not hold.

It never touches the Status column. Whether a partial pass counts is a judgement, and a
script inferring that from file existence would be making it silently.

## What a file does and does not prove

It proves that on one broker date, at one commit, against one terminal, the live path and
a replay of the same candles agreed (or did not), and what was stored. It says nothing
about any other day, and nothing about whether the thresholds those outcomes are measured
against discriminate — `docs/PHASE2_BASELINE.md` is explicit about that, and so is
`docs/MT5_SESSION_CHECKLIST.md` under *What this still does not prove*.
