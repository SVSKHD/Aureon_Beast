"""Operator-reviewed V1 evidence ledger; integrity checks are not market verification."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aureon.services.backup_service import sha256_file

SCHEMA = "AUREON_V1_RELEASE_EVIDENCE_V1"
# Each item requires its own reviewed claim and raw evidence. No fixture can close a gate.
REQUIREMENTS = {
    "xauusd_session": ("real_mt5", ("session_verified", "nonempty_observations")),
    "xagusd_session": ("real_mt5", ("session_verified", "nonempty_observations")),
    "demo_execution": (
        "mt5_demo",
        (
            "market_order",
            "pending_order",
            "sl_modification",
            "rejection",
            "duplicate_protection",
            "reconnect",
            "unknown_result_recovery",
        ),
    ),
    "discord_confirm": (
        "mt5_demo",
        (
            "authorized_confirm",
            "expired_confirm_refused",
            "request_executor_ticket_trace",
        ),
    ),
    "windows_stack": (
        "windows_vps",
        (
            "main_aureon_startup",
            "all_services_healthy",
            "supervised_recovery",
        ),
    ),
    "calibration": (
        "real_history",
        (
            "dataset_provenance",
            "daily_bias",
            "liquidity",
            "wick",
            "breakout",
            "session_setup",
            "versioned_thresholds",
            "held_out_evaluation",
        ),
    ),
    "foundation_training": (
        "real_history",
        (
            "xauusd_provenance",
            "dataset_checksums",
            "no_synthetic_rows",
            "chronological_walk_forward",
            "champion_lineage",
        ),
    ),
    "unseen_months": (
        "real_history",
        (
            "january_adaptation",
            "february_frozen_predictions",
            "february_blind_score",
            "february_release_after_score",
            "challenger_learns_february",
            "march_frozen_predictions",
            "march_blind_score",
            "no_future_label_leakage",
        ),
    ),
    "exit_policy": (
        "real_history",
        (
            "fixed_plus_10",
            "plus_5_protection",
            "trailing_comparison",
            "costs_and_same_bar_ambiguity",
            "held_out_selection",
            "selected_policy_matches_context",
        ),
    ),
    "live_replay_mtf": (
        "real_mt5",
        (
            "xauusd_parity",
            "xagusd_parity",
            "mtf_parity",
            "nonempty_comparisons",
        ),
    ),
    "weekend_sleep_wake": (
        "windows_vps",
        (
            "market_close",
            "sleep_heartbeats",
            "preopen_wake",
            "resumed_closed_candles",
            "no_duplicate_requests",
            "no_crash",
        ),
    ),
}


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def release_context(*, commit, champion, config, thresholds, exit_policy) -> dict:
    return {
        "commit": commit,
        "champion_model_id": champion.model_id if champion else None,
        "champion_sha256": fingerprint(champion.model_dump(mode="json")) if champion else None,
        "configuration_sha256": fingerprint(
            {
                "config": config,
                "thresholds": thresholds,
                "exit_policy": exit_policy,
            }
        ),
    }


def new_ledger(context: dict) -> dict:
    return {
        "schema": SCHEMA,
        "context": context,
        "notice": "Operator attestations plus file integrity, not automatic proof of real runs.",
        "gates": {
            name: {
                "source": source,
                "status": "pending",
                "checks": dict.fromkeys(checks, False),
                "reviewer": "",
                "reviewed_at": None,
                "notes": "",
                "artifacts": [],
            }
            for name, (source, checks) in REQUIREMENTS.items()
        },
    }


def artifact_path(root: Path, raw: str) -> Path:
    """Evidence is portable and confined to the ledger directory, including symlinks."""
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
        raise ValueError("artifact path must be relative to the evidence ledger")
    path = (root / raw).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"missing or outside evidence directory: {raw}")
    return path


def review_gate(
    ledger: dict,
    *,
    root: Path,
    gate: str,
    reviewer: str,
    notes: str,
    artifacts: list[str],
    checks: list[str],
) -> None:
    """Explicit human attestation; never infer success from process exit code alone."""
    source, required = REQUIREMENTS[gate]
    if set(checks) != set(required) or not reviewer.strip() or not notes.strip() or not artifacts:
        raise ValueError("all named checks, reviewer, notes and raw artifacts are required")
    entries = []
    for raw in artifacts:
        path = artifact_path(root, raw)
        if path.stat().st_size == 0:
            raise ValueError(f"empty evidence: {raw}")
        entries.append({"path": raw, "sha256": sha256_file(path)})
    ledger["gates"][gate] = {
        "source": source,
        "status": "reviewed",
        "checks": dict.fromkeys(required, True),
        "reviewer": reviewer.strip(),
        "notes": notes.strip(),
        "reviewed_at": datetime.now(UTC).isoformat(),
        "artifacts": entries,
    }


def validate_ledger(path: Path, *, expected_context: dict | None = None) -> dict:
    """Fail closed on missing reviews, changed files or a different release identity."""
    ledger = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(ledger, dict) or ledger.get("schema") != SCHEMA:
        raise ValueError("unsupported release evidence schema")
    context = ledger.get("context")
    if not isinstance(context, dict) or not all(
        context.get(k)
        for k in (
            "commit",
            "champion_model_id",
            "champion_sha256",
            "configuration_sha256",
        )
    ):
        raise ValueError("release context requires commit, Champion and configuration fingerprints")
    if expected_context is not None and context != expected_context:
        raise ValueError("evidence belongs to a different code/model/configuration")
    gates = ledger.get("gates")
    if not isinstance(gates, dict) or set(gates) != set(REQUIREMENTS):
        raise ValueError("release evidence must contain exactly the eleven V1 gates")
    problems = []
    for name, (source, checks) in REQUIREMENTS.items():
        gate = gates[name]
        try:
            if not isinstance(gate, dict) or gate.get("status") != "reviewed":
                raise ValueError("pending review")
            if gate.get("source") != source:
                raise ValueError(f"requires {source} evidence")
            if not isinstance(gate.get("checks"), dict) or any(
                gate["checks"].get(check) is not True for check in checks
            ):
                raise ValueError("incomplete acceptance checks")
            if (
                not str(gate.get("reviewer") or "").strip()
                or not str(gate.get("notes") or "").strip()
            ):
                raise ValueError("missing reviewer/notes")
            reviewed = datetime.fromisoformat(gate.get("reviewed_at") or "")
            if reviewed.tzinfo is None or reviewed > datetime.now(UTC):
                raise ValueError("review time must be timezone-aware and not in the future")
            artifacts = gate.get("artifacts")
            if not isinstance(artifacts, list) or not artifacts:
                raise ValueError("missing raw evidence")
            for artifact in artifacts:
                source_path = artifact_path(path.parent, artifact["path"])
                if (
                    source_path.stat().st_size == 0
                    or sha256_file(source_path) != artifact["sha256"]
                ):
                    raise ValueError(f"empty or changed evidence: {artifact['path']}")
        except (ValueError, TypeError, KeyError, OSError) as exc:
            problems.append(f"{name}: {exc}")
    return {"ready_for_freeze": not problems, "problems": problems, "ledger": ledger}
