#!/usr/bin/env python3
"""The review process (§61-§65).

Generates daily and weekly reviews. Reads detections, evaluations, trades and sessions;
writes only ``daily_reviews`` and ``weekly_reviews``.

## Why a separate process rather than a job inside the observer (decision 88)

The spec allows either. A separate process, because:

* a review is **batch aggregation over history**, not a reaction to a candle. It needs no
  live feed, so putting it in the observer would couple a minutes-long read of thousands of
  documents to the loop that must not miss a candle close;
* it must be **re-runnable for any past period** -- to backfill, or to regenerate after a
  bug fix. A scheduled job inside a long-running loop makes that awkward; a CLI makes it
  one command;
* reviews are idempotent, so running one twice is harmless, which removes the usual reason
  to keep a scheduler single-instance.

Scheduling itself is left to cron or a systemd timer: daily after the broker day closes,
weekly after Friday's close. The command computes the most recently *completed* period by
default, so "run it after close" is the whole configuration.

## One review per symbol (9A)

Every configured symbol gets its own document, generated with **that symbol's** frozen rule.
Aggregating them would produce one ``evaluation_rule_id`` over numbers produced by two rules,
and a horizon table adding reached-counts measured in two instruments' money. ``--symbol``
narrows a run to one instrument; without it every configured symbol is generated, and a
failure on one does not stop the others -- a missing silver review is not a reason to have no
gold review.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from aureon.config import AureonConfig
from aureon.evaluation.rules import get_rule
from aureon.models.base import utc_now
from aureon.reviews.aggregate import compare_by_tag, render_tag_comparisons
from aureon.reviews.periods import (
    previous_iso_week,
    previous_market_date,
    week_period,
)
from aureon.reviews.service import ReviewService

log = logging.getLogger("aureon.review")


def build_service(config: AureonConfig, symbol: str | None = None) -> ReviewService:
    from aureon.storage.firebase_service import get_client

    client = get_client(
        project_id=config.firebase_project_id,
        emulator_host=config.firestore_emulator_host,
    )
    # That symbol's rule, not the process's (decision 141): the rule id goes on the document
    # as a claim about how its numbers were produced.
    rule_id = config.rule_id_for(symbol) if symbol else config.evaluation_rule_id
    return ReviewService(
        client,
        get_rule(rule_id),
        market_tz=config.market_tz,
        infer_window_minutes=config.infer_window_minutes,
        account_scope=config.account_scope,
        symbol=symbol,
    )


def run_daily(service: ReviewService, market_date: str | None, *, now: datetime) -> int:
    date = market_date or previous_market_date(service.market_tz, now=now)
    review = service.generate_daily(date, generated_at=now)
    print(
        f"daily {review.market_date} [{review.symbol or 'all symbols'}]: "
        f"{review.detections_total} detections, "
        f"{review.trades_total} trades, P&L {review.realized_pnl:+.2f}, "
        f"{review.pending_horizons_excluded} pending horizons excluded"
    )
    print(f"  {review.notes}")
    return 0


def run_weekly(
    service: ReviewService,
    iso: tuple[int, int] | None,
    *,
    now: datetime,
    with_dailies: bool,
) -> int:
    year, week = iso or previous_iso_week(service.market_tz, now=now)
    if with_dailies:
        review, dailies = service.generate_week_with_dailies(year, week, generated_at=now)
        print(f"generated {len(dailies)} daily review(s)")
    else:
        review = service.generate_weekly(year, week, generated_at=now)
    print(
        f"weekly {review.iso_year}-W{review.iso_week:02d} "
        f"[{review.symbol or 'all symbols'}]: "
        f"{review.detections_total} detections, {review.trades_total} trades, "
        f"P&L {review.realized_pnl:+.2f}, "
        f"{review.pending_horizons_excluded} pending horizons excluded"
    )
    print(f"  {review.notes}")
    for outcome in review.horizons:
        cells = " ".join(
            f"{t.threshold:g}:{t.reached}/{t.evaluated}" for t in outcome.thresholds
        )
        print(f"  {outcome.horizon_id:<16} {cells}")
    if review.inferred_links:
        print(f"  {len(review.inferred_links)} inferred link(s) (analysis only, §50)")

    # §23: the same week split by what else the machine had seen. Printed rather than
    # stored on the review document -- it is a research view, and a week of detections
    # is far too few for any of these differences to be more than anecdote.
    period = week_period(review.iso_year, review.iso_week, service.market_tz)
    data = service.load(period)
    comparisons = compare_by_tag(data, service.rule)
    if any(c.with_tag_complete or c.without_tag_complete for c in comparisons):
        horizon = comparisons[0].horizon_id
        key = comparisons[0].threshold_key
        print(f"  context (horizon {horizon}, threshold {key}) — research only:")
        for line in render_tag_comparisons(comparisons).splitlines():
            print(f"    {line}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "period", choices=["daily", "weekly"], help="which review to generate"
    )
    parser.add_argument("--date", help="broker date YYYY-MM-DD (daily); default: yesterday")
    parser.add_argument("--iso-year", type=int, help="ISO year (weekly)")
    parser.add_argument("--iso-week", type=int, help="ISO week (weekly)")
    parser.add_argument(
        "--with-dailies",
        action="store_true",
        help="also generate each day of the week, before the weekly review",
    )
    parser.add_argument(
        "--symbol",
        help="one symbol; default: every configured symbol, each with its own rule",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    config = AureonConfig.from_env()
    now = utc_now()

    iso = None
    if args.iso_year and args.iso_week:
        iso = (args.iso_year, args.iso_week)
    elif args.iso_year or args.iso_week:
        parser.error("--iso-year and --iso-week must be given together")

    if args.symbol:
        symbol = args.symbol.upper()
        if symbol not in config.symbols:
            parser.error(
                f"{symbol} is not configured ({', '.join(config.symbols)}); its rule and "
                "tuning would both be guesses"
            )
        symbols: list[str | None] = [symbol]
    else:
        symbols = list(config.symbols)

    exit_code = 0
    for one in symbols:
        service = build_service(config, one)
        try:
            if args.period == "daily":
                exit_code |= run_daily(service, args.date, now=now)
            else:
                exit_code |= run_weekly(
                    service, iso, now=now, with_dailies=args.with_dailies
                )
        except Exception:  # noqa: BLE001 - one symbol failing must not lose the others
            log.exception("review failed for %s", one)
            exit_code |= 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
