"""Freeze the V1 baseline (item 12): model, config, thresholds, exit policy, results.

One directory per tag, immutable once written, with a SHA-256 manifest the health report
can verify. Secrets are never included; the config snapshot is the same safe subset the
monthly backup writes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aureon.services.backup_service import _atomic_write, sha256_file


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
) -> Path:
    """Write ``root/tag/`` with the Champion artifact and every result that defines V1."""
    target = Path(root) / tag
    if target.exists() and (target / "manifest.json").exists():
        raise FileExistsError(f"baseline {tag} already exists at {target}; baselines are immutable")
    target.mkdir(parents=True, exist_ok=True)
    symbol = symbol.upper()

    champion = storage.models.champion(symbol)
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
    for raw in extra_files or ():
        source = Path(raw)
        if not source.exists():
            continue
        destination = target / "results" / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        files[f"results/{source.name}"] = sha256_file(destination)
    summary = {
        key: payload[key] for key in ("schema", "tag", "symbol", "frozen_at", "training_examples")
    }
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
    return target


def _dump(value: Any) -> Any:
    return None if value is None else value.model_dump(mode="json")
