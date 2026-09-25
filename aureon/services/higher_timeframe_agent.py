"""Logical Agent 12: higher-timeframe assessment.

Consumes the MTF context already aggregated by AnalysisEngine. It never fetches candles
and never creates a second EMA implementation.
"""

from __future__ import annotations

from aureon.models.agent_decision import HigherTimeframeAssessment, HtfState
from aureon.models.enums import Timeframe, TrendBias
from aureon.models.mtf import MtfContext


class HigherTimeframeAgent:
    agent_name = "higher_timeframe"
    agent_version = "1.0.0"

    #: The reads that matter for the trading view. M30 and D1 remain available in the raw
    #: context but do not get to outvote the requested M15/H1/H4 structure.
    watched = (Timeframe.M15, Timeframe.H1, Timeframe.H4)

    def assess(self, context: MtfContext | None) -> HigherTimeframeAssessment:
        if context is None:
            return HigherTimeframeAssessment(
                state=HtfState.NEUTRAL,
                warnings=("higher-timeframe context unavailable",),
            )

        reads: dict[str, str] = {}
        bullish = bearish = sideways = 0
        available: list[Timeframe] = []
        evidence: list[str] = []

        for timeframe in self.watched:
            read = context.by_timeframe.get(timeframe)
            if read is None:
                reads[timeframe.value] = "unavailable"
                continue
            available.append(timeframe)
            reads[timeframe.value] = read.bias.value
            evidence.append(
                f"{timeframe.value} {read.bias.value} @ {read.close:g}"
            )
            if read.bias is TrendBias.BULLISH:
                bullish += 1
            elif read.bias is TrendBias.BEARISH:
                bearish += 1
            else:
                sideways += 1

        if bullish and not bearish:
            state, dominant = HtfState.BULLISH, TrendBias.BULLISH
        elif bearish and not bullish:
            state, dominant = HtfState.BEARISH, TrendBias.BEARISH
        elif bullish and bearish:
            state, dominant = HtfState.MIXED, TrendBias.SIDEWAYS
        else:
            state, dominant = HtfState.NEUTRAL, TrendBias.SIDEWAYS

        warnings = []
        missing = [tf.value for tf in self.watched if tf not in available]
        if missing:
            warnings.append("missing " + ", ".join(missing))

        return HigherTimeframeAssessment(
            state=state,
            dominant_bias=dominant,
            reads=reads,
            bullish_count=bullish,
            bearish_count=bearish,
            sideways_count=sideways,
            available_count=len(available),
            strongest_available=max(available, key=lambda tf: tf.minutes) if available else None,
            evidence=tuple(evidence),
            warnings=tuple(warnings),
        )
