"""Chronological M5 decision replay for Agent 20 research.

Historical results are reference evidence only. The runner freezes what Aureon knew at the
decision candle, then evaluates later candles separately. Training is chronological and never
randomly shuffles future periods into the past.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from aureon.agents.ema_rsi_eligibility_agent import EmaRsiEligibilityAgent
from aureon.config.sessions import session_for
from aureon.ml.logistic import binary_metrics, fit_logistic
from aureon.models.enums import Direction, Timeframe
from aureon.services.higher_timeframe_agent import HigherTimeframeAgent
from aureon.services.market_director import DirectorInputs, MarketDirector
from aureon.services.market_snapshot import MarketSnapshot


@dataclass
class DecisionReplayRow:
    at: str
    symbol: str
    timeframe: str
    event: str
    eligible: bool
    direction: str
    entry_price: float
    rsi: float | None
    ema_fast: float | None
    ema_slow: float | None
    ema_gap: float | None
    ema_gap_change: float | None
    htf_state: str
    director_state: str
    director_supporting: int
    director_opposing: int
    regime: str | None
    participation: str | None
    same_candle_agents: list[str]
    mfe_1: float
    mae_1: float
    mfe_3: float
    mae_3: float
    mfe_6: float
    mae_6: float
    mfe_12: float
    mae_12: float
    reached_10: bool
    bars_to_10: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_decision_replay(*, candles: list[Any], engine: Any, target_move: float = 10.0) -> list[DecisionReplayRow]:
    snapshot = MarketSnapshot(symbol=candles[0].symbol if candles else "UNKNOWN")
    htf_agent = HigherTimeframeAgent()
    director = MarketDirector(primary_target_move=target_move)
    pending: list[tuple[int, Any, Any, Any, Any, list[str]]] = []

    for index, candle in enumerate(candles):
        detections = engine.on_closed_candle(candle)
        snapshot.observe_candle(
            high=candle.high,
            low=candle.low,
            open=candle.open,
            close=candle.close,
            session=session_for(candle.open_time.market),
        )
        for detection in detections:
            snapshot.observe(detection)

        eligibility = next(
            (d for d in detections if d.agent_name == EmaRsiEligibilityAgent.agent_name),
            None,
        )
        if eligibility is None:
            continue

        mtf = engine.mtf_context(candle.symbol, candle.timeframe)
        htf = htf_agent.assess(mtf)
        read = engine.indicator_read(candle.symbol, candle.timeframe)
        current = snapshot.as_state()
        signals = tuple((d.agent_name, d.direction) for d in detections if d.direction is not None)
        decision = director.decide(
            DirectorInputs(
                price=candle.close,
                ema_fast=read.ema_fast,
                ema_slow=read.ema_slow,
                rsi=read.rsi,
                session_trend=current.get("session_trend"),
                journey=current.get("market_journey"),
                regime=current.get("market_regime"),
                participation=current.get("volume_participation"),
                htf=htf,
                candle_signals=signals,
                as_of=candle.close_time,
            )
        )
        pending.append(
            (
                index,
                eligibility,
                htf,
                decision,
                current,
                sorted({d.agent_name for d in detections}),
            )
        )

    rows: list[DecisionReplayRow] = []
    for index, detection, htf, decision, current, same_candle_agents in pending:
        direction = (
            Direction.BUY
            if detection.evidence.categorical.get("cross_direction") == "bullish"
            else Direction.SELL
        )
        metrics = {}
        for horizon in (1, 3, 6, 12):
            future = candles[index + 1 : index + 1 + horizon]
            metrics[horizon] = _excursions(future, detection.price, direction)

        future = candles[index + 1 : index + 1 + 12]
        reached_10 = False
        bars_to_10 = None
        for offset, bar in enumerate(future, start=1):
            favourable = (
                bar.high - detection.price
                if direction is Direction.BUY
                else detection.price - bar.low
            )
            if favourable >= target_move:
                reached_10 = True
                bars_to_10 = offset
                break

        ev = detection.evidence
        rows.append(
            DecisionReplayRow(
                at=detection.detected_at.utc.isoformat(),
                symbol=detection.symbol,
                timeframe=detection.timeframe.value,
                event=detection.event_key,
                eligible=bool(ev.flags.get("eligible")),
                direction=direction.value,
                entry_price=detection.price,
                rsi=detection.indicators.rsi,
                ema_fast=detection.indicators.ema.get("fast"),
                ema_slow=detection.indicators.ema.get("slow"),
                ema_gap=ev.numeric.get("ema_gap"),
                ema_gap_change=ev.numeric.get("ema_gap_change"),
                htf_state=htf.state.value,
                director_state=decision.state.value,
                director_supporting=decision.supporting,
                director_opposing=decision.opposing,
                regime=(current.get("market_regime") or {}).get("regime") if isinstance(current.get("market_regime"), dict) else None,
                participation=(current.get("volume_participation") or {}).get("state") if isinstance(current.get("volume_participation"), dict) else None,
                same_candle_agents=same_candle_agents,
                mfe_1=metrics[1][0], mae_1=metrics[1][1],
                mfe_3=metrics[3][0], mae_3=metrics[3][1],
                mfe_6=metrics[6][0], mae_6=metrics[6][1],
                mfe_12=metrics[12][0], mae_12=metrics[12][1],
                reached_10=reached_10,
                bars_to_10=bars_to_10,
            )
        )
    return rows


def train_reference(rows: list[DecisionReplayRow], *, train_fraction: float = 0.7) -> dict[str, Any]:
    eligible = [row for row in rows if row.eligible]
    if len(eligible) < 20:
        return {"status": "insufficient_data", "samples": len(eligible)}
    split = max(10, min(len(eligible) - 5, int(len(eligible) * train_fraction)))
    train, test = eligible[:split], eligible[split:]
    labels = [1 if row.reached_10 else 0 for row in train]
    if not any(labels) or all(labels):
        return {"status": "insufficient_classes", "samples": len(train)}

    names = (
        "direction_sign", "rsi", "ema_gap", "ema_gap_change",
        "director_supporting", "director_opposing",
        "htf_bullish", "htf_bearish", "director_ready",
    )
    raw_train = [_features(row) for row in train]
    means = [sum(v[i] for v in raw_train) / len(raw_train) for i in range(len(names))]
    scales = []
    for i, mean in enumerate(means):
        variance = sum((v[i] - mean) ** 2 for v in raw_train) / len(raw_train)
        scales.append(max(variance ** 0.5, 1e-9))

    def norm(values):
        return [(values[i] - means[i]) / scales[i] for i in range(len(names))]

    model = fit_logistic([norm(v) for v in raw_train], labels)
    test_labels = [1 if row.reached_10 else 0 for row in test]
    probabilities = [model.probability(norm(_features(row))) for row in test]
    return {
        "status": "reference_only",
        "historical_reference_only": True,
        "train_samples": len(train),
        "test_samples": len(test),
        "train_from": train[0].at,
        "train_through": train[-1].at,
        "test_from": test[0].at,
        "test_through": test[-1].at,
        "feature_names": list(names),
        "means": means,
        "scales": scales,
        "model": model.to_dict(),
        "test_metrics": binary_metrics(test_labels, probabilities),
        "note": "Historical replay is a reference match, not a promise that live regimes repeat.",
    }


def _features(row: DecisionReplayRow) -> list[float]:
    return [
        1.0 if row.direction == "buy" else -1.0,
        float(row.rsi or 50.0),
        float(row.ema_gap or 0.0),
        float(row.ema_gap_change or 0.0),
        float(row.director_supporting),
        float(row.director_opposing),
        1.0 if row.htf_state == "bullish" else 0.0,
        1.0 if row.htf_state == "bearish" else 0.0,
        1.0 if row.director_state == "ready" else 0.0,
    ]


def _excursions(future: list[Any], entry: float, direction: Direction) -> tuple[float, float]:
    if not future:
        return 0.0, 0.0
    if direction is Direction.BUY:
        mfe = max(max(0.0, bar.high - entry) for bar in future)
        mae = max(max(0.0, entry - bar.low) for bar in future)
    else:
        mfe = max(max(0.0, entry - bar.low) for bar in future)
        mae = max(max(0.0, bar.high - entry) for bar in future)
    return mfe, mae
