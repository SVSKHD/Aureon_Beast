"""Chronological M5 decision replay for Agent 20 research.

Historical results are reference evidence only. The runner freezes what Aureon knew at the
decision candle, then evaluates later candles separately. Training is chronological and never
randomly shuffles future periods into the past.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Callable

from aureon.agents.ema_rsi_eligibility_agent import EmaRsiEligibilityAgent
from aureon.config.sessions import session_for
from aureon.ml.logistic import LogisticModel, binary_metrics, fit_logistic
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


def run_decision_replay(
    *,
    candles: list[Any],
    engine: Any,
    target_move: float = 10.0,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> list[DecisionReplayRow]:
    snapshot = MarketSnapshot(symbol=candles[0].symbol if candles else "UNKNOWN")
    htf_agent = HigherTimeframeAgent()
    director = MarketDirector(primary_target_move=target_move)
    pending: list[tuple[int, Any, Any, Any, Any, list[str]]] = []
    total = len(candles)
    progress_every = max(1, total // 20)

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
            if on_progress is not None and (
                (index + 1) % progress_every == 0 or index + 1 == total
            ):
                on_progress(index + 1, total, len(pending))
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
        if on_progress is not None and (
            (index + 1) % progress_every == 0 or index + 1 == total
        ):
            on_progress(index + 1, total, len(pending))

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


def test_reference_model(
    rows: list[DecisionReplayRow],
    reference_model: dict[str, Any],
) -> dict[str, Any]:
    """Score unseen eligible rows with a previously saved reference model.

    The model is never refit here. This is the strict holdout path used to test a
    July-trained artifact on August data.
    """
    eligible = [row for row in rows if row.eligible]
    if not eligible:
        return {"status": "no_eligible_rows", "samples": 0}

    if reference_model.get("status") != "reference_only":
        raise ValueError(
            "saved artifact does not contain a completed reference_only model"
        )

    names = tuple(reference_model.get("feature_names") or ())
    expected = (
        "direction_sign", "rsi", "ema_gap", "ema_gap_change",
        "director_supporting", "director_opposing",
        "htf_bullish", "htf_bearish", "director_ready",
    )
    if names != expected:
        raise ValueError(
            f"saved model feature contract mismatch: expected {expected}, got {names}"
        )

    means = [float(value) for value in reference_model.get("means") or ()]
    scales = [float(value) for value in reference_model.get("scales") or ()]
    if len(means) != len(expected) or len(scales) != len(expected):
        raise ValueError("saved model normalization metadata is incomplete")

    model = LogisticModel.from_dict(reference_model["model"])

    def norm(values: list[float]) -> list[float]:
        return [
            (values[i] - means[i]) / max(scales[i], 1e-9)
            for i in range(len(expected))
        ]

    probabilities = [model.probability(norm(_features(row))) for row in eligible]
    labels = [1 if row.reached_10 else 0 for row in eligible]
    metrics = binary_metrics(labels, probabilities)

    scored = []
    for row, probability in zip(eligible, probabilities, strict=True):
        scored.append(
            {
                "at": row.at,
                "direction": row.direction,
                "event": row.event,
                "probability_reach_10": probability,
                "predicted_positive_50": probability >= 0.5,
                "actual_reached_10": row.reached_10,
                "bars_to_10": row.bars_to_10,
                "mfe_12": row.mfe_12,
                "mae_12": row.mae_12,
            }
        )

    selected = [item for item in scored if item["predicted_positive_50"]]
    selected_hits = sum(bool(item["actual_reached_10"]) for item in selected)
    return {
        "status": "tested_saved_model",
        "historical_reference_only": True,
        "samples": len(eligible),
        "metrics": metrics,
        "predicted_positive_50": len(selected),
        "predicted_positive_hits": selected_hits,
        "predicted_positive_misses": len(selected) - selected_hits,
        "predicted_positive_hit_rate": (
            selected_hits / len(selected) if selected else None
        ),
        "actual_positive_rate": sum(labels) / len(labels),
        "scored_rows": scored,
    }


def simulate_money_outcomes(
    rows: list[DecisionReplayRow],
    candles: list[Any],
    *,
    target_move: float,
    stop_move: float,
    hold_bars: int,
    lot_size: float,
    contract_size: float,
    selected_times: set[str] | None = None,
) -> dict[str, Any]:
    """Simulate first-touch TP/SL outcomes and translate price moves into money."""
    if target_move <= 0 or stop_move <= 0:
        raise ValueError("target_move and stop_move must be > 0")
    if hold_bars < 1:
        raise ValueError("hold_bars must be >= 1")
    if lot_size <= 0 or contract_size <= 0:
        raise ValueError("lot_size and contract_size must be > 0")

    ordered_times = [candle.open_time.utc for candle in candles]

    def first_future_index(at: str) -> int | None:
        moment = datetime.fromisoformat(at)
        for i, opened in enumerate(ordered_times):
            if opened >= moment:
                return i
        return None

    trades: list[dict[str, Any]] = []
    for row in rows:
        if not row.eligible:
            continue
        if selected_times is not None and row.at not in selected_times:
            continue
        start_index = first_future_index(row.at)
        if start_index is None:
            continue
        direction = Direction.BUY if row.direction == "buy" else Direction.SELL
        future = candles[start_index : start_index + hold_bars]
        if not future:
            continue

        result = "timeout"
        exit_move = 0.0
        exit_bar = None
        max_adverse_before_exit = 0.0

        for offset, bar in enumerate(future, start=1):
            if direction is Direction.BUY:
                adverse = max(0.0, row.entry_price - bar.low)
                hit_tp = bar.high >= row.entry_price + target_move
                hit_sl = bar.low <= row.entry_price - stop_move
            else:
                adverse = max(0.0, bar.high - row.entry_price)
                hit_tp = bar.low <= row.entry_price - target_move
                hit_sl = bar.high >= row.entry_price + stop_move
            max_adverse_before_exit = max(max_adverse_before_exit, adverse)

            if hit_tp and hit_sl:
                result = "ambiguous"
                exit_bar = offset
                break
            if hit_tp:
                result = "win"
                exit_move = target_move
                exit_bar = offset
                break
            if hit_sl:
                result = "loss"
                exit_move = -stop_move
                exit_bar = offset
                break

        if result == "timeout":
            last = future[-1]
            exit_move = (
                last.close - row.entry_price
                if direction is Direction.BUY
                else row.entry_price - last.close
            )
            exit_bar = len(future)

        usd_pnl = None if result == "ambiguous" else exit_move * contract_size * lot_size
        trades.append({
            "at": row.at,
            "direction": row.direction,
            "entry_price": row.entry_price,
            "result": result,
            "exit_bar": exit_bar,
            "price_move": None if result == "ambiguous" else exit_move,
            "usd_pnl": usd_pnl,
            "mae_before_exit": max_adverse_before_exit,
        })

    resolved = [trade for trade in trades if trade["usd_pnl"] is not None]
    wins = [trade for trade in resolved if trade["result"] == "win"]
    losses = [trade for trade in resolved if trade["result"] == "loss"]
    timeouts = [trade for trade in resolved if trade["result"] == "timeout"]
    ambiguous = [trade for trade in trades if trade["result"] == "ambiguous"]
    profitable_exits = [trade for trade in resolved if float(trade["usd_pnl"]) > 0]
    losing_exits = [trade for trade in resolved if float(trade["usd_pnl"]) < 0]
    flat_exits = [trade for trade in resolved if float(trade["usd_pnl"]) == 0]
    usd_made = sum(max(0.0, float(trade["usd_pnl"])) for trade in resolved)
    usd_lost = -sum(min(0.0, float(trade["usd_pnl"])) for trade in resolved)
    net = sum(float(trade["usd_pnl"]) for trade in resolved)

    return {
        "trades": len(trades),
        "resolved_trades": len(resolved),
        "wins": len(wins),
        "losses": len(losses),
        "timeouts": len(timeouts),
        "ambiguous": len(ambiguous),
        "profitable_exits": len(profitable_exits),
        "losing_exits": len(losing_exits),
        "flat_exits": len(flat_exits),
        "usd_made": usd_made,
        "usd_lost": usd_lost,
        "net_usd": net,
        "win_rate": (len(wins) / len(resolved)) if resolved else None,
        "average_usd_per_resolved_trade": (net / len(resolved)) if resolved else None,
        "max_mae_before_exit": max((float(t["mae_before_exit"]) for t in trades), default=0.0),
        "target_move": target_move,
        "stop_move": stop_move,
        "hold_bars": hold_bars,
        "lot_size": lot_size,
        "contract_size": contract_size,
        "trade_rows": trades,
        "note": "Ambiguous M5 bars touch TP and SL in the same candle and are excluded from USD P&L.",
    }