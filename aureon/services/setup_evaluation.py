"""What a setup did after it confirmed, measured by the machinery that measures detections
(12, T-7).

A setup's outcome has to be measured from the CONFIRMED candle. Before that there is no claim to
be right or wrong about -- an OBSERVING setup is "a level is nearby", which is not a prediction.

## One definition of an outcome, not two

The thresholds, the horizons, the MFE/MAE excursions and the tz-aware gap tolerance all live in
``aureon.evaluation.outcome_tracker`` and are frozen by §21. Writing a second measurement here
would give the system two answers to "did it work", and the two would differ on the first edge
case -- a gap over a weekend, a horizon that expires mid-candle -- with no way to tell which was
right.

So this module measures nothing. It feeds the existing tracker a **subject**: an in-memory
``Detection`` standing for the setup's confirmation, with the setup's direction and the confirming
candle's close. The tracker does what it always does, and the result is re-keyed onto the setup.

**The subject is never stored.** It exists for the duration of one call, it is not written to
``detections``, and its id is the setup's own so that nothing can mistake it for an observation.
A test asserts the detections collection stays untouched.

## Why a separate collection rather than a field

``setup_evaluations/{setup_id}__{rule_id}``, for the reason §21 gives for
``detection_evaluations``: the setup document is edited as it advances, and an outcome written
onto it would be future information sitting on a record of the present. Keeping them apart means
a setup can be read as "what was believed at the time" and its evaluation as "what happened
next", which is the distinction every review in this system rests on.
"""

from __future__ import annotations

import logging
from typing import Any

from aureon.evaluation.rules import EvaluationRule
from aureon.models.base import MarketTime
from aureon.models.detection import Detection, SessionContext
from aureon.models.enums import Direction, DirectionContext, SetupState
from aureon.models.evaluation import SetupEvaluation
from aureon.models.market import Candle
from aureon.models.setup import Setup

log = logging.getLogger(__name__)

#: The session config version stamped on the subject. It is never read -- the subject is not
#: stored -- but the model requires one, and a magic number at the call site would look like a
#: claim about sessions.
SUBJECT_SESSION_VERSION = 1


def direction_of(setup: Setup) -> Direction | None:
    """A setup's direction as the evaluation rules understand it, or ``None``.

    ``NEUTRAL`` maps to ``None`` rather than to a guess, and the consequence is deliberate: a
    MOMENTUM_TRANSITION that confirmed without resolving its direction is **not evaluated**. The
    alternative -- picking a side from the candle -- would put a measurement in the population
    that answers a question nobody asked, and it would be indistinguishable from the rest.
    """
    if setup.direction_context is DirectionContext.BULLISH:
        return Direction.BULLISH
    if setup.direction_context is DirectionContext.BEARISH:
        return Direction.BEARISH
    return None


def subject_for(setup: Setup, candle: Candle) -> Detection | None:
    """The in-memory stand-in the tracker measures. Never stored -- see the module docstring."""
    direction = direction_of(setup)
    if direction is None:
        return None
    return Detection(
        # The SETUP's id, deliberately. If this ever reached the detections collection by
        # accident it would be obvious rather than plausible.
        detection_id=setup.setup_id,
        account_scope=setup.account_scope,
        symbol=setup.symbol,
        timeframe=setup.timeframe,
        agent_name=f"setup:{setup.family.value}",
        agent_version=setup.setup_version,
        event_key=setup.direction_context.value,
        direction=direction,
        detected_at=MarketTime.from_utc(candle.close_time, candle.open_time.market_tz),
        candle_open_time=candle.open_time,
        price=candle.close,
        session=SessionContext(
            session=_session_of(setup),
            session_config_version=SUBJECT_SESSION_VERSION,
        ),
        sequence_today=1,
        sequence_session=1,
    )


def _session_of(setup: Setup):
    from aureon.models.enums import SessionName

    return setup.context_summary.session or SessionName.OFF


class SetupEvaluator:
    """Tracks confirmed setups through the frozen rule and writes their outcomes.

    One per symbol, beside the setup engine, holding one ``OutcomeTracker``. The tracker is the
    stateful part -- a horizon spans many candles -- and it is the same class the detections use,
    constructed with the same rule and the same ``point``.
    """

    def __init__(
        self,
        *,
        rule: EvaluationRule,
        market_tz: str,
        point: float,
        repository: Any,
        tracker: Any | None = None,
    ) -> None:
        from aureon.evaluation.outcome_tracker import OutcomeTracker

        self.rule = rule
        self.repository = repository
        self.tracker = tracker or OutcomeTracker(
            rule, market_tz=market_tz, point=point
        )
        #: setup_id -> the setup, so a completed evaluation can carry its family and context
        #: without a read.
        self._confirmed: dict[str, Setup] = {}

    def on_confirmed(self, setup: Setup, candle: Candle) -> SetupEvaluation | None:
        """Begin measuring a setup that has just confirmed.

        Idempotent through the tracker: ``track`` on an id it already holds returns the existing
        evaluation rather than restarting it, which is what makes a re-processed candle harmless.
        """
        if setup.state is not SetupState.CONFIRMED:
            return None
        subject = subject_for(setup, candle)
        if subject is None:
            log.debug(
                "setup %s confirmed with no direction; not evaluated", setup.setup_id
            )
            return None
        self._confirmed[setup.setup_id] = setup
        evaluation = self.tracker.track(subject)
        if evaluation is None:
            return None
        return self._write(setup, evaluation)

    def on_closed_candle(self, candle: Candle) -> list[SetupEvaluation]:
        """Advance every open measurement. Returns the ones that changed."""
        written: list[SetupEvaluation] = []
        for evaluation in self.tracker.on_closed_candle(candle):
            setup = self._confirmed.get(evaluation.detection_id)
            if setup is None:
                continue
            stored = self._write(setup, evaluation)
            if stored is not None:
                written.append(stored)
        return written

    def _write(self, setup: Setup, evaluation: Any) -> SetupEvaluation | None:
        record = SetupEvaluation(
            setup_id=setup.setup_id,
            rule_id=evaluation.rule_id,
            evaluation_rule_id=evaluation.rule_id,
            family=setup.family,
            direction_context=setup.direction_context,
            symbol=setup.symbol,
            timeframe=setup.timeframe,
            market_date=setup.market_date,
            setup_version=setup.setup_version,
            reference_price=evaluation.reference_price,
            reference_value=evaluation.reference_value,
            horizons=evaluation.horizons,
            context_summary=setup.context_summary,
        )
        try:
            return self.repository.write(record)
        except Exception:  # noqa: BLE001 - a storage failure must not stop observation
            log.exception("could not write the evaluation for setup %s", setup.setup_id)
            return None
