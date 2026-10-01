"""Deterministic provenance helpers for Aureon V3 model training."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess

from aureon.services.v3_ema_learning import CanonicalEMAExampleV3

DEFAULT_RANDOM_SEED = 0


def dataset_snapshot_hash(examples: list[CanonicalEMAExampleV3]) -> str:
    canonical = [
        example.model_dump(mode="json")
        for example in sorted(
            examples,
            key=lambda row: (row.market_date, row.generated_at, row.example_id),
        )
    ]
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def current_code_commit() -> str:
    env = os.getenv("AUREON_GIT_COMMIT")
    if env:
        return env.strip()
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            timeout=3,
        ).strip()
    except Exception:
        return "unknown"


def stable_payload_hash(payload: object) -> str:
    data = (
        payload.model_dump(mode="json")
        if hasattr(payload, "model_dump")
        else payload
    )
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
