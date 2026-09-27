"""Continuous, local-first Aureon V1 learning orchestration.

This service is deliberately outside the market-data/execution hot path. It never opens,
modifies, or closes a trade. A cycle may train candidates, run chronological walk-forward
validation, move one qualified challenger into shadow, evaluate existing shadow evidence,
and create the monthly local recovery snapshot.

All governance transitions still go through EvolutionAgent. Training never overwrites the
Champion and a candidate cannot skip shadow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aureon.models.learning_v1 import ModelLifecycleStatus
from aureon.services.backup_service import BackupService
from aureon.services.evolution_agent import EvolutionAgent
from aureon.services.model_backtest import V1WalkForwardBacktester
from aureon.services.v1_model_training import V1ModelTrainer

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LearningCyclePolicy:
    min_samples: int = 30
    min_new_examples: int = 10
    min_train_days: int = 20
    test_days: int = 5
    min_train_samples: int = 30


@dataclass(frozen=True)
class LearningCycleResult:
    symbol: str
    canonical_examples: int
    action: str
    detail: str
    model_id: str | None = None


class LearningCycleService:
    """One conservative continuous-learning cycle over local persistent memory."""

    def __init__(
        self,
        *,
        training_memory: Any,
        models: Any,
        symbols: tuple[str, ...] | list[str],
        policy: LearningCyclePolicy | None = None,
        backup: BackupService | None = None,
        backup_assets: tuple[str | Path, ...] = (),
        backup_config: dict[str, Any] | None = None,
        now: Any | None = None,
    ) -> None:
        self.training_memory = training_memory
        self.models = models
        self.symbols = tuple(symbol.upper() for symbol in symbols)
        self.policy = policy or LearningCyclePolicy()
        self.backup = backup
        self.backup_assets = tuple(Path(value) for value in backup_assets)
        self.backup_config = dict(backup_config or {})
        self._now = now or (lambda: datetime.now(UTC))
        self.trainer = V1ModelTrainer(
            training_memory=training_memory,
            models=models,
            now=self._now,
        )
        self.backtester = V1WalkForwardBacktester(
            training_memory=training_memory,
            models=models,
            now=self._now,
        )
        self.evolution = EvolutionAgent(models, now=self._now)
        self._last_backup_month: str | None = None

    def run_once(self) -> list[LearningCycleResult]:
        results: list[LearningCycleResult] = []
        for symbol in self.symbols:
            try:
                results.append(self._run_symbol(symbol))
            except Exception as exc:  # learning must never become an execution dependency
                log.exception("V1 learning cycle failed for %s", symbol)
                results.append(
                    LearningCycleResult(
                        symbol=symbol,
                        canonical_examples=0,
                        action="error",
                        detail=str(exc),
                    )
                )
        self._monthly_backup_best_effort()
        return results

    def _run_symbol(self, symbol: str) -> LearningCycleResult:
        examples = self.training_memory.canonical_between(
            symbol,
            "0001-01-01",
            "9999-12-31",
        )
        count = len(examples)

        # Shadow evaluation comes first. If enough real outcomes exist, EvolutionAgent
        # may promote/reject it. If it is still shadow afterwards, do not replace it with
        # another experiment in the same cycle.
        shadow = self.models.active_shadow(symbol)
        if shadow is not None:
            evaluated = self.evolution.evaluate_shadow(shadow.model_id)
            if evaluated.status == ModelLifecycleStatus.SHADOW.value:
                return LearningCycleResult(
                    symbol=symbol,
                    canonical_examples=count,
                    action="shadow_wait",
                    detail="active shadow still gathering reconciled outcomes",
                    model_id=evaluated.model_id,
                )

        if count < self.policy.min_samples:
            return LearningCycleResult(
                symbol=symbol,
                canonical_examples=count,
                action="wait_samples",
                detail=f"need at least {self.policy.min_samples} canonical examples",
            )

        latest = self.models.latest_model(symbol)
        champion = self.models.champion(symbol)
        baseline_samples = max(
            int(getattr(latest, "training_samples", 0) or 0),
            int(getattr(champion, "training_samples", 0) or 0),
        )
        new_examples = max(0, count - baseline_samples)
        if baseline_samples and new_examples < self.policy.min_new_examples:
            degradation = self.evolution.degradation_signal(symbol)
            return LearningCycleResult(
                symbol=symbol,
                canonical_examples=count,
                action="wait_new_data",
                detail=(
                    f"{new_examples} new examples < {self.policy.min_new_examples}; "
                    f"Champion degradation={degradation.get('degraded')}"
                ),
                model_id=champion.model_id if champion is not None else None,
            )

        candidates = self.trainer.train_candidates(
            symbol,
            min_samples=self.policy.min_samples,
        )
        qualified = []
        for candidate in candidates:
            governed = self.evolution.qualify_candidate(candidate.model_id)
            if governed.status != ModelLifecycleStatus.CHALLENGER.value:
                continue
            backtest = self.backtester.run(
                symbol,
                model_id=governed.model_id,
                min_train_days=self.policy.min_train_days,
                test_days=self.policy.test_days,
                min_train_samples=self.policy.min_train_samples,
            )
            clean = backtest.aggregate_metrics.get("clean_10")
            if backtest.status == "complete" and clean is not None:
                qualified.append((governed, clean))

        if not qualified:
            return LearningCycleResult(
                symbol=symbol,
                canonical_examples=count,
                action="no_qualified_challenger",
                detail="new candidates failed validation or walk-forward gates",
            )

        # Only one active shadow per symbol. Rank challengers using the V1 objective:
        # clean-win precision first, then lower Brier, then recall. The EvolutionAgent
        # still performs the final admission checks.
        def rank(item: tuple[Any, Any]) -> tuple[float, float, float]:
            _model, metric = item
            precision = -1.0 if metric.precision is None else float(metric.precision)
            brier = 1e9 if metric.brier is None else float(metric.brier)
            recall = -1.0 if metric.recall is None else float(metric.recall)
            return (precision, -brier, recall)

        best, _ = max(qualified, key=rank)
        admitted = self.evolution.admit_shadow(best.model_id)
        if admitted.status != ModelLifecycleStatus.SHADOW.value:
            return LearningCycleResult(
                symbol=symbol,
                canonical_examples=count,
                action="challenger_rejected",
                detail="best chronological challenger failed final shadow-admission policy",
                model_id=admitted.model_id,
            )
        return LearningCycleResult(
            symbol=symbol,
            canonical_examples=count,
            action="entered_shadow",
            detail=(
                f"{admitted.algorithm} entered shadow; Champion remains "
                f"{champion.model_id if champion is not None else 'unassigned'}"
            ),
            model_id=admitted.model_id,
        )

    def _monthly_backup_best_effort(self) -> None:
        if self.backup is None:
            return
        month = self._now().astimezone(UTC).strftime("%Y-%m")
        if month == self._last_backup_month:
            return
        self._last_backup_month = month
        snapshot_dir = self.backup.backup_root / month
        try:
            if snapshot_dir.exists() and (snapshot_dir / "manifest.json").exists():
                # The monthly local snapshot is immutable. A restart later in the month
                # must not try to replace the database bytes; only retry offsite sync.
                self.backup.sync_drive_async(snapshot_dir)
                return
            result = self.backup.monthly_snapshot(
                month=month,
                assets=self.backup_assets,
                config_snapshot=self.backup_config,
            )
            self.backup.sync_drive_async(result.snapshot_dir)
            log.info(
                "V1 monthly recovery snapshot created month=%s files=%s",
                month,
                result.files,
            )
        except Exception:  # Drive/local backup reporting is never a trade dependency
            log.warning("V1 monthly backup failed; Aureon continues locally", exc_info=True)
