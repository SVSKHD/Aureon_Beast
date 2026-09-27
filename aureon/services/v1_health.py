"""One consolidated V1 health report (GAP 13).

A single read of everything an operator asks about, assembled from the repositories the
services already write. It computes nothing that gates anything: every value is a copy of
state some process owns, labelled with where it came from.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import Field

from aureon.models.base import AureonModel, UtcDatetime, to_utc, utc_now
from aureon.models.learning_v1 import FEATURE_SCHEMA_V1, LABEL_SCHEMA_V1
from aureon.services.training_coverage import COVERAGE_SCHEMA

log = logging.getLogger(__name__)


class MarketHealth(AureonModel):
    symbol: str
    market_status: str = "unknown"
    current_session: str | None = None
    daily_bias: str | None = None
    daily_bias_strength: float | None = None
    session_bias: str | None = None
    session_strength: float | None = None
    regime: str | None = None
    volatility: str | None = None
    session_transition_state: str | None = None
    last_closed_candle_time: UtcDatetime | None = None


class AgentHealthLine(AureonModel):
    name: str
    state: str = "unknown"
    stance: str | None = None


class LearningHealth(AureonModel):
    feature_schema: str = FEATURE_SCHEMA_V1
    label_schema: str = LABEL_SCHEMA_V1
    canonical_example_count: int = 0
    training_from: str | None = None
    training_through: str | None = None
    pending_learning_setups: int = 0
    coverage_status: str = "unknown"
    coverage_total_samples: int | None = None
    open_exams: tuple[str, ...] = ()


class ModelHealth(AureonModel):
    champion: str | None = None
    champion_generation: int | None = None
    challengers: tuple[str, ...] = ()
    shadow: str | None = None
    latest_training_run: str | None = None
    latest_walk_forward: str | None = None
    latest_walk_forward_status: str | None = None


class ModelMetricsHealth(AureonModel):
    source: str = "none"
    clean_10_precision: float | None = None
    recall: float | None = None
    false_positive_rate: float | None = None
    brier: float | None = None
    log_loss: float | None = None
    average_mae: float | None = None
    average_mfe: float | None = None
    experience: dict[str, int] = Field(default_factory=dict)


class LivePositionHealth(AureonModel):
    aureon_position: str | None = None
    symbol: str | None = None
    entry: float | None = None
    mfe: float | None = None
    mae: float | None = None
    protection_state: str | None = None
    trailing_state: str | None = None
    current_stop: float | None = None
    reached: dict[str, bool] = Field(default_factory=dict)
    manual_positions_observed: int = 0
    aureon_positions_open: int = 0


class EvolutionHealth(AureonModel):
    latest_decision: str | None = None
    latest_decision_reason: str | None = None
    latest_decision_at: UtcDatetime | None = None
    previous_champion: str | None = None
    current_champion: str | None = None
    latest_generation_comparison: dict[str, Any] | None = None


class BackupHealth(AureonModel):
    latest_local_snapshot: str | None = None
    manifest_verified: bool | None = None
    manifest_files: int | None = None
    manifest_problems: tuple[str, ...] = ()
    drive_sync_status: str = "unconfigured"
    drive_last_synced_at: UtcDatetime | None = None


class SystemHealth(AureonModel):
    mt5_health: str = "unknown"
    storage_health: str = "unknown"
    heartbeats: dict[str, UtcDatetime] = Field(default_factory=dict)
    last_restart_recovery: dict[str, Any] | None = None
    autonomous_management_enabled: bool = False


class V1HealthReport(AureonModel):
    """Everything in one object. Render with ``render_health``."""

    generated_at: UtcDatetime
    market: tuple[MarketHealth, ...] = ()
    agents: tuple[AgentHealthLine, ...] = ()
    learning: dict[str, LearningHealth] = Field(default_factory=dict)
    model: dict[str, ModelHealth] = Field(default_factory=dict)
    model_metrics: dict[str, ModelMetricsHealth] = Field(default_factory=dict)
    live_position: LivePositionHealth = Field(default_factory=LivePositionHealth)
    evolution: dict[str, EvolutionHealth] = Field(default_factory=dict)
    backup: BackupHealth = Field(default_factory=BackupHealth)
    system: SystemHealth = Field(default_factory=SystemHealth)


def build_v1_health_report(
    storage: Any,
    *,
    symbols: tuple[str, ...] | list[str],
    timeframe: Any,
    backup_root: str | Path = "backups",
    autonomous_management_enabled: bool = False,
    restart_notice_path: str | Path | None = None,
    now: datetime | None = None,
) -> V1HealthReport:
    moment = to_utc(now or utc_now())
    timeframe_value = getattr(timeframe, "value", str(timeframe))

    market: list[MarketHealth] = []
    agents: list[AgentHealthLine] = []
    heartbeats: dict[str, datetime] = {}
    mt5 = "unknown"
    storage_health = "ok"
    try:
        storage.database.probe()
    except Exception as exc:  # noqa: BLE001
        storage_health = f"unavailable: {exc}"

    state = _safe(lambda: storage.system_state.read())
    if state is not None:
        heartbeats = dict(state.heartbeats or {})
        for name, health in sorted((state.agent_health or {}).items()):
            agents.append(
                AgentHealthLine(
                    name=name,
                    state=str(getattr(getattr(health, "state", None), "value", "unknown")),
                )
            )
        for symbol_state in state.symbols:
            bias = symbol_state.daily_bias
            regime = symbol_state.market_regime or {}
            market.append(
                MarketHealth(
                    symbol=symbol_state.symbol,
                    market_status=symbol_state.market_state.value,
                    current_session=(bias.current_session.value if bias is not None else None),
                    daily_bias=None if bias is None else bias.daily_bias.value,
                    daily_bias_strength=None if bias is None else bias.daily_bias_strength,
                    session_bias=None if bias is None else bias.session_bias.value,
                    session_strength=None if bias is None else bias.session_bias_strength,
                    regime=regime.get("regime") if isinstance(regime, dict) else None,
                    volatility=(
                        regime.get("volatility_state") if isinstance(regime, dict) else None
                    ),
                    session_transition_state=(
                        None if bias is None else bias.session_transition_state
                    ),
                    last_closed_candle_time=symbol_state.last_closed_candle_time,
                )
            )
            director = symbol_state.market_director
            if director is not None:
                agents.append(
                    AgentHealthLine(
                        name=f"market_director:{symbol_state.symbol}",
                        state=str(director.state.value),
                        stance=None if director.direction is None else director.direction.value,
                    )
                )
            if bias is not None:
                agents.append(
                    AgentHealthLine(
                        name=f"daily_market_bias:{symbol_state.symbol}",
                        state="healthy",
                        stance=bias.daily_bias.value,
                    )
                )
        states = {one.market_state.value for one in state.symbols}
        mt5 = (
            "open"
            if "open" in states
            else ("closed" if states == {"closed"} else ",".join(sorted(states)) or "unknown")
        )
    try:
        beats = storage.heartbeats.read_all()
        for name, beat in beats.items():
            heartbeats[name] = beat.updated_at
    except Exception:  # noqa: BLE001
        pass

    learning: dict[str, LearningHealth] = {}
    model: dict[str, ModelHealth] = {}
    metrics: dict[str, ModelMetricsHealth] = {}
    evolution: dict[str, EvolutionHealth] = {}
    for symbol in symbols:
        examples = (
            _safe(
                lambda s=symbol: storage.training_memory.canonical_between(
                    s, "0001-01-01", "9999-12-31"
                )
            )
            or []
        )
        pending = (
            _safe(lambda s=symbol: storage.training_memory.pending_for_stream(s, timeframe_value))
            or []
        )
        champion = _safe(lambda s=symbol: storage.models.champion(s))
        coverage = (
            (champion.validation_metrics or {}).get("training_coverage") if champion else None
        )
        exams = _safe(lambda s=symbol: storage.models.unreleased_exams(s)) or []
        learning[symbol] = LearningHealth(
            canonical_example_count=len(examples),
            training_from=min((e.market_date for e in examples), default=None),
            training_through=max((e.market_date for e in examples), default=None),
            pending_learning_setups=len(pending),
            coverage_status=(
                "reported"
                if isinstance(coverage, dict) and coverage.get("schema") == COVERAGE_SCHEMA
                else "no_report"
            ),
            coverage_total_samples=(coverage or {}).get("total_samples")
            if isinstance(coverage, dict)
            else None,
            open_exams=tuple(f"{e.exam_id}:{e.status.value}" for e in exams),
        )
        shadow = _safe(lambda s=symbol: storage.models.active_shadow(s))
        challengers = _safe(lambda s=symbol: storage.models.challengers(s)) or []
        run = _safe(lambda s=symbol: storage.models.latest_training_run(s))
        backtest = _safe(lambda s=symbol: storage.models.latest_backtest(s))
        model[symbol] = ModelHealth(
            champion=None if champion is None else champion.model_id,
            champion_generation=None if champion is None else champion.generation,
            challengers=tuple(one.model_id for one in challengers if one.status == "challenger"),
            shadow=None if shadow is None else shadow.model_id,
            latest_training_run=None if run is None else run.run_id,
            latest_walk_forward=None if backtest is None else backtest.backtest_id,
            latest_walk_forward_status=None if backtest is None else backtest.status,
        )
        metric_source = "none"
        clean = None
        if champion is not None:
            shadow_clean = (champion.shadow_metrics or {}).get("clean_10")
            if isinstance(shadow_clean, dict) and shadow_clean.get("samples"):
                clean = shadow_clean
                metric_source = "champion_live_reconciled"
            elif champion.target_metrics.get("clean_10") is not None:
                clean = champion.target_metrics["clean_10"].model_dump(mode="json")
                metric_source = "champion_validation"
        champion_id = None if champion is None else champion.model_id
        experience = (
            _safe(lambda cid=champion_id: storage.models.experience_summary(cid))
            if champion_id
            else None
        )
        metrics[symbol] = ModelMetricsHealth(
            source=metric_source,
            clean_10_precision=(clean or {}).get("precision"),
            recall=(clean or {}).get("recall"),
            false_positive_rate=(clean or {}).get("false_positive_rate"),
            brier=(clean or {}).get("brier"),
            log_loss=(clean or {}).get("log_loss"),
            average_mae=(clean or {}).get("average_mae"),
            average_mfe=(clean or {}).get("average_mfe"),
            experience=experience or {},
        )
        decisions = _safe(lambda s=symbol: storage.models.evolution_for_symbol(s, limit=50)) or []
        latest = decisions[0] if decisions else None
        promoted = [d for d in decisions if d.action == "promoted_champion"]
        comparison = next((d.metrics for d in decisions if d.action == "generation_report"), None)
        evolution[symbol] = EvolutionHealth(
            latest_decision=None if latest is None else latest.action,
            latest_decision_reason=None if latest is None else latest.reason,
            latest_decision_at=None if latest is None else latest.created_at,
            previous_champion=(promoted[0].champion_model_id if promoted else None),
            current_champion=None if champion is None else champion.model_id,
            latest_generation_comparison=comparison,
        )

    live = LivePositionHealth()
    open_trades = _safe(lambda: storage.trades.open_trades()) or []
    aureon_open = [t for t in open_trades if t.aureon_managed]
    live.manual_positions_observed = len(open_trades) - len(aureon_open)
    live.aureon_positions_open = len(aureon_open)
    if aureon_open:
        trade = aureon_open[0]
        ms = trade.management_state
        live.aureon_position = trade.trade_id
        live.symbol = trade.symbol
        live.entry = trade.open_price
        if ms is not None:
            live.mfe = ms.mfe
            live.mae = ms.mae
            live.protection_state = "active" if ms.protection_activated else "pending"
            live.trailing_state = ms.phase.value
            live.current_stop = ms.current_stop
            live.reached = {
                f"reached_{n}": getattr(ms, f"reached_{n}") for n in (5, 10, 20, 30, 40)
            }
        else:
            live.mfe = trade.excursion.mfe
            live.mae = trade.excursion.mae
            live.current_stop = trade.sl

    backup = _backup_health(Path(backup_root))

    recovery = None
    if restart_notice_path is not None:
        recovery = _safe(lambda: json.loads(Path(restart_notice_path).read_text(encoding="utf-8")))

    return V1HealthReport(
        generated_at=moment,
        market=tuple(market),
        agents=tuple(agents),
        learning=learning,
        model=model,
        model_metrics=metrics,
        live_position=live,
        evolution=evolution,
        backup=backup,
        system=SystemHealth(
            mt5_health=mt5,
            storage_health=storage_health,
            heartbeats=heartbeats,
            last_restart_recovery=recovery if isinstance(recovery, dict) else None,
            autonomous_management_enabled=autonomous_management_enabled,
        ),
    )


def _backup_health(root: Path) -> BackupHealth:
    from aureon.services.backup_service import verify_manifest

    if not root.exists():
        return BackupHealth()
    months = sorted(p for p in root.iterdir() if p.is_dir() and (p / "manifest.json").exists())
    if not months:
        return BackupHealth()
    latest = months[-1]
    verification = verify_manifest(latest)
    drive_status = "unconfigured"
    drive_at = None
    status_file = latest / "drive_sync_status.json"
    if status_file.exists():
        try:
            payload = json.loads(status_file.read_text(encoding="utf-8"))
            drive_status = str(payload.get("status", "unknown"))
            raw = payload.get("synced_at")
            drive_at = to_utc(datetime.fromisoformat(raw)) if raw else None
        except (OSError, ValueError):
            drive_status = "unreadable"
    return BackupHealth(
        latest_local_snapshot=latest.name,
        manifest_verified=verification.ok,
        manifest_files=verification.files,
        manifest_problems=tuple(verification.problems[:10]),
        drive_sync_status=drive_status,
        drive_last_synced_at=drive_at,
    )


def _safe(reader: Any) -> Any:
    try:
        return reader()
    except Exception:  # noqa: BLE001 - one broken source must not blank the whole report
        log.debug("health source failed", exc_info=True)
        return None


def render_health(report: V1HealthReport) -> str:
    lines = [f"AUREON V1 HEALTH  {report.generated_at.isoformat()}", ""]
    lines.append("MARKET")
    for m in report.market:
        lines.append(
            f"  {m.symbol:<8} {m.market_status:<8} session={m.current_session or '-':<9} "
            f"daily={m.daily_bias or '-'}({_f(m.daily_bias_strength)}) "
            f"session_bias={m.session_bias or '-'}({_f(m.session_strength)}) "
            f"regime={m.regime or '-'} vol={m.volatility or '-'}"
        )
        if m.session_transition_state:
            lines.append(f"           transitions: {m.session_transition_state}")
    lines.append("AGENTS")
    for a in report.agents:
        lines.append(f"  {a.name:<48} {a.state:<10} {a.stance or ''}")
    for symbol, learn in report.learning.items():
        lines.append(f"LEARNING {symbol}")
        lines.append(
            f"  schemas={learn.feature_schema}/{learn.label_schema} "
            f"examples={learn.canonical_example_count} "
            f"range={learn.training_from or '-'}..{learn.training_through or '-'} "
            f"pending={learn.pending_learning_setups} coverage={learn.coverage_status} "
            f"exams={list(learn.open_exams) or '-'}"
        )
        mdl = report.model[symbol]
        lines.append(f"MODEL {symbol}")
        lines.append(
            f"  champion={mdl.champion or '-'} gen={mdl.champion_generation} "
            f"shadow={mdl.shadow or '-'} challengers={list(mdl.challengers) or '-'} "
            f"run={mdl.latest_training_run or '-'} "
            f"walk_forward={mdl.latest_walk_forward or '-'}"
            f"({mdl.latest_walk_forward_status or '-'})"
        )
        met = report.model_metrics[symbol]
        lines.append(
            f"  metrics[{met.source}] precision={_f(met.clean_10_precision)} "
            f"recall={_f(met.recall)} fpr={_f(met.false_positive_rate)} "
            f"brier={_f(met.brier)} logloss={_f(met.log_loss)} "
            f"mae={_f(met.average_mae)} mfe={_f(met.average_mfe)} "
            f"experience={met.experience or '-'}"
        )
        evo = report.evolution[symbol]
        lines.append(f"EVOLUTION {symbol}")
        lines.append(
            f"  latest={evo.latest_decision or '-'} "
            f"previous_champion={evo.previous_champion or '-'} "
            f"current={evo.current_champion or '-'} "
            f"comparison={'yes' if evo.latest_generation_comparison else 'no'}"
        )
    lp = report.live_position
    lines.append("LIVE POSITION")
    lines.append(
        f"  aureon={lp.aureon_position or '-'} symbol={lp.symbol or '-'} "
        f"entry={_f(lp.entry)} mfe={_f(lp.mfe)} mae={_f(lp.mae)} "
        f"protection={lp.protection_state or '-'} phase={lp.trailing_state or '-'} "
        f"stop={_f(lp.current_stop)} manual_observed={lp.manual_positions_observed} "
        f"aureon_open={lp.aureon_positions_open}"
    )
    b = report.backup
    lines.append("BACKUP")
    lines.append(
        f"  snapshot={b.latest_local_snapshot or '-'} manifest_verified={b.manifest_verified} "
        f"files={b.manifest_files} drive={b.drive_sync_status} "
        f"problems={list(b.manifest_problems) or '-'}"
    )
    s = report.system
    beats = ", ".join(f"{k}:{v.isoformat()}" for k, v in sorted(s.heartbeats.items()))
    lines.append("SYSTEM")
    lines.append(
        f"  mt5={s.mt5_health} storage={s.storage_health} "
        f"autonomous_management={s.autonomous_management_enabled} heartbeats={{{beats}}}"
    )
    if s.last_restart_recovery:
        lines.append(f"  last_restart={s.last_restart_recovery}")
    return "\n".join(lines)


def _f(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"
