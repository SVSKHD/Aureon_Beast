"""V4 Gold placement and trade-management research (TODO 085-092).

Research only: these helpers compare historical observation points and hypothetical
management rules. They never create, modify or close a live position.
"""
from __future__ import annotations
from dataclasses import dataclass
from statistics import mean
from typing import Any
from aureon.models.ema_journey_v4 import V4ReentryObservation, V4RemainingMovementExample

@dataclass(frozen=True)
class V4PlacementOutcome:
    journey_id: str
    placement: str
    movement_consumed: float
    mfe: float
    mae: float
    reached_3: bool
    reached_5: bool
    reached_10: bool

def placement_outcome(row: V4RemainingMovementExample, placement: str | None = None) -> V4PlacementOutcome:
    return V4PlacementOutcome(
        journey_id=row.journey_id,
        placement=placement or row.features.anchor_type,
        movement_consumed=float(row.features.movement_consumed),
        mfe=float(row.labels.remaining_mfe),
        mae=float(row.labels.remaining_mae),
        reached_3=row.labels.reached_3,
        reached_5=row.labels.reached_5,
        reached_10=row.labels.reached_10,
    )

def _summary(rows: list[V4PlacementOutcome]) -> dict[str, float | int]:
    if not rows:
        return {"samples": 0}
    n=len(rows)
    return {
        "samples": n,
        "mean_movement_consumed": mean(r.movement_consumed for r in rows),
        "mean_remaining_mfe": mean(r.mfe for r in rows),
        "mean_remaining_mae": mean(r.mae for r in rows),
        "reach_3_rate": sum(r.reached_3 for r in rows)/n,
        "reach_5_rate": sum(r.reached_5 for r in rows)/n,
        "reach_10_rate": sum(r.reached_10 for r in rows)/n,
    }

def compare_pre_cross_vs_cross(pre_cross: list[V4RemainingMovementExample], cross: list[V4RemainingMovementExample]) -> dict[str, Any]:
    """TODO 085: compare observable pre-cross and confirmed-cross anchors."""
    return {
        "pre_cross": _summary([placement_outcome(r, "pre_cross") for r in pre_cross]),
        "confirmed_cross": _summary([placement_outcome(r, "confirmed_cross") for r in cross]),
    }

def compare_cross_vs_pullback_reentry(cross: list[V4RemainingMovementExample], reentries: list[V4ReentryObservation]) -> dict[str, Any]:
    """TODO 086: compare cross outcomes with resolved pullback/re-entry observations."""
    cross_rows=[placement_outcome(r, "confirmed_cross") for r in cross]
    reentry_rows=[
        V4PlacementOutcome(r.journey_id, "pullback_reentry", r.pullback_depth, r.outcome.mfe, r.outcome.mae, r.outcome.reached_3, r.outcome.reached_5, r.outcome.reached_10)
        for r in reentries if r.outcome.completed
    ]
    return {"confirmed_cross": _summary(cross_rows), "pullback_reentry": _summary(reentry_rows)}

def missed_large_movements(rows: list[V4RemainingMovementExample], *, large_move: float = 20.0, consumed_threshold: float = 10.0) -> dict[str, Any]:
    """TODO 087: movements where substantial opportunity existed but anchor arrived late."""
    missed=[r for r in rows if r.labels.remaining_mfe >= large_move and r.features.movement_consumed >= consumed_threshold]
    return {
        "samples": len(rows),
        "missed_large_movements": len(missed),
        "rate": len(missed)/len(rows) if rows else 0.0,
        "journey_ids": [r.journey_id for r in missed],
    }

def opportunity_cost(earlier: list[V4RemainingMovementExample], later: list[V4RemainingMovementExample]) -> dict[str, Any]:
    """TODO 088: paired loss of remaining MFE between earlier and later anchors."""
    early={r.journey_id:r for r in earlier}
    late={r.journey_id:r for r in later}
    shared=sorted(set(early)&set(late))
    costs=[max(0.0, early[j].labels.remaining_mfe-late[j].labels.remaining_mfe) for j in shared]
    return {
        "paired_journeys": len(shared),
        "mean_opportunity_cost": mean(costs) if costs else 0.0,
        "max_opportunity_cost": max(costs) if costs else 0.0,
        "costs": {j: c for j,c in zip(shared,costs,strict=True)},
    }

@dataclass(frozen=True)
class V4ManagementOutcome:
    rule: str
    gross_capture: float
    runner_capture: float
    completed: bool

def fixed_target_research(rows: list[V4RemainingMovementExample], target: float) -> dict[str, Any]:
    """TODO 089/090: hypothetical fixed-target capture from resolved MFE."""
    captures=[target if r.labels.remaining_mfe >= target else 0.0 for r in rows]
    return {
        "target": target,
        "samples": len(rows),
        "hit_rate": sum(c>0 for c in captures)/len(rows) if rows else 0.0,
        "mean_capture": mean(captures) if captures else 0.0,
        "total_capture": sum(captures),
    }

def partial_runner_research(rows: list[V4RemainingMovementExample], *, partial_target: float = 5.0, partial_fraction: float = 0.5, runner_cap: float = 20.0) -> dict[str, Any]:
    """TODO 091: partial at target, remainder follows observed favourable excursion."""
    if not 0.0 < partial_fraction < 1.0:
        raise ValueError("partial_fraction must be between zero and one")
    captures=[]
    for r in rows:
        mfe=float(r.labels.remaining_mfe)
        if mfe < partial_target:
            captures.append(0.0)
            continue
        runner=min(mfe, runner_cap)
        captures.append(partial_fraction*partial_target+(1.0-partial_fraction)*runner)
    return {
        "samples": len(rows),
        "partial_target": partial_target,
        "partial_fraction": partial_fraction,
        "runner_cap": runner_cap,
        "mean_capture": mean(captures) if captures else 0.0,
        "total_capture": sum(captures),
    }

def pullback_continuation_management(reentries: list[V4ReentryObservation], *, continuation_target: float = 10.0) -> dict[str, Any]:
    """TODO 092: hypothetical continuation management after valid pullback observation."""
    resolved=[r for r in reentries if r.outcome.completed]
    captures=[min(float(r.outcome.mfe), continuation_target) if r.outcome.reached_3 and not r.outcome.failed else 0.0 for r in resolved]
    return {
        "samples": len(resolved),
        "continuation_target": continuation_target,
        "success_rate": sum(c>0 for c in captures)/len(resolved) if resolved else 0.0,
        "mean_capture": mean(captures) if captures else 0.0,
        "mean_mae": mean(float(r.outcome.mae) for r in resolved) if resolved else 0.0,
        "failed_reentries": sum(r.outcome.failed for r in resolved),
    }
