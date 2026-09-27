"""Freeze the V1 baseline (item 12): model, config, thresholds, exit policy, results.

One directory per tag, immutable once written, with a SHA-256 manifest the health report
can verify. Secrets are never included; the config snapshot is the same safe subset the
monthly backup writes.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aureon.services.backup_service import _atomic_write, sha256_file
from aureon.services.v1_release import artifact_path, release_context, validate_ledger


def freeze_v1_baseline(
    *,
    root: str | Path,
    tag: str,
    symbol: str,
    storage: Any,
    config_snapshot: dict[str, Any],
    exit_policy: dict[str, Any],
    thresholds: dict[str, Any],
    extra_files: list[str | Path] | None = None,
    evidence: str | Path | None = None,
    commit: str | None = None,
) -> Path:
    """Write ``root/tag/`` with the Champion artifact and every result that defines V1."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", tag):
        raise ValueError("tag must be a single safe directory name")
    destination = Path(root) / tag
    if destination.exists():
        raise FileExistsError(f"baseline {tag} already exists; baselines are immutable")
    symbol = symbol.upper()
    champion = storage.models.champion(symbol)
    if champion is None or champion.status != "champion" or not champion.artifact:
        raise ValueError("a trained Champion is required to freeze V1")
    if evidence is None:
        raise ValueError("reviewed real-run evidence is required to freeze V1")
    context = release_context(
        commit=commit,
        champion=champion,
        config=config_snapshot,
        thresholds=thresholds,
        exit_policy=exit_policy,
    )
    evidence_path = Path(evidence)
    report = validate_ledger(evidence_path, expected_context=context)
    if not report["ready_for_freeze"]:
        raise ValueError("V1 evidence incomplete: " + "; ".join(report["problems"]))
    extra_sources = [Path(raw) for raw in extra_files or ()]
    if len({p.name for p in extra_sources}) != len(extra_sources):
        raise ValueError("included result filenames must be unique")
    for source in extra_sources:
        if not source.is_file():
            raise ValueError(f"missing included result: {source}")
    Path(root).mkdir(parents=True, exist_ok=True)
    # An incomplete write never appears at the final release path.
    staging = tempfile.TemporaryDirectory(prefix=".v1-freeze-", dir=root)
    target = Path(staging.name) / tag
    target.mkdir()
    payload: dict[str, Any] = {
        "schema": "AUREON_V1_BASELINE_V1",
        "tag": tag,
        "symbol": symbol,
        "frozen_at": datetime.now(UTC).isoformat(),
        "champion": None if champion is None else champion.model_dump(mode="json"),
        "shadow": _dump(storage.models.active_shadow(symbol)),
        "models": [m.model_dump(mode="json") for m in storage.models.models_for_symbol(symbol)],
        "exams": [e.model_dump(mode="json") for e in storage.models.exams_for(symbol)],
        "evolution": [
            d.model_dump(mode="json")
            for d in storage.models.evolution_for_symbol(symbol, limit=500)
        ],
        "latest_walk_forward": _dump(storage.models.latest_backtest(symbol)),
        "training_examples": len(
            storage.training_memory.canonical_between(symbol, "0001-01-01", "9999-12-31")
        ),
        "config": config_snapshot,
        "exit_policy": exit_policy,
        "thresholds": thresholds,
    }
    files: dict[str, str] = {}
    for name, block in (
        ("champion.json", payload["champion"]),
        ("models.json", payload["models"]),
        ("exams.json", payload["exams"]),
        ("evolution.json", payload["evolution"]),
        ("walk_forward.json", payload["latest_walk_forward"]),
        ("config.json", payload["config"]),
        ("exit_policy.json", payload["exit_policy"]),
        ("thresholds.json", payload["thresholds"]),
    ):
        _atomic_write(target / name, json.dumps(block, indent=2, sort_keys=True, default=str))
        files[name] = sha256_file(target / name)
    for source in extra_sources:
        copied = target / "results" / source.name
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, copied)
        files[f"results/{source.name}"] = sha256_file(copied)
    # Keep ledger-relative paths intact in the portable, frozen bundle.
    ledger = report["ledger"]
    for gate in ledger["gates"].values():
        for entry in gate["artifacts"]:
            source = artifact_path(evidence_path.parent, entry["path"])
            copied = target / "evidence" / entry["path"]
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, copied)
            if sha256_file(copied) != entry["sha256"]:
                raise ValueError("evidence changed during freeze")
            files[copied.relative_to(target).as_posix()] = entry["sha256"]
    ledger_file = target / "evidence" / "release.json"
    if ledger_file.exists():
        raise ValueError("artifact name release.json is reserved for the evidence ledger")
    _atomic_write(ledger_file, json.dumps(ledger, indent=2, sort_keys=True))
    files["evidence/release.json"] = sha256_file(ledger_file)
    summary = {
        key: payload[key] for key in ("schema", "tag", "symbol", "frozen_at", "training_examples")
    }
    summary["release_context"] = context
    summary["evidence_basis"] = "operator-reviewed real runs; checksums verified"
    summary["champion_model_id"] = None if champion is None else champion.model_id
    summary["champion_generation"] = None if champion is None else champion.generation
    _atomic_write(target / "baseline.json", json.dumps(summary, indent=2, sort_keys=True))
    files["baseline.json"] = sha256_file(target / "baseline.json")
    manifest = {
        "schema": "AUREON_BACKUP_MANIFEST_V1",
        "month": tag,
        "created_at": datetime.now(UTC).isoformat(),
        "files": sorted(
            (
                {
                    "source": name,
                    "path": name,
                    "sha256": digest,
                    "size": (target / name).stat().st_size,
                    "mtime_ns": (target / name).stat().st_mtime_ns,
                }
                for name, digest in files.items()
            ),
            key=lambda item: item["path"],
        ),
    }
    _atomic_write(target / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    target.rename(destination)
    staging.cleanup()
    return destination


def _dump(value: Any) -> Any:
    return None if value is None else value.model_dump(mode="json")
