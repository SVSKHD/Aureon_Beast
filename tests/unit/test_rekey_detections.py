"""``scripts/rekey_detections.py`` — the migration off the pre-§12 detection id.

Tested against an in-memory double rather than the emulator: what matters is the
arithmetic of "which id should this document be at", not Firestore's semantics. The
emulator suite already proves the repository layer round-trips.

The property that earns a test is **idempotence**. A re-key run that is not safe to
repeat is one nobody will dare run twice, which in practice means nobody runs it at
all — and the second run is exactly the one you want after fixing a mistake.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aureon.models.base import MarketTime
from aureon.models.detection import Detection, SessionContext
from aureon.models.enums import Direction, SessionName, Timeframe
from aureon.models.identity import detection_id
from aureon.storage import paths

REPO_ROOT = Path(__file__).resolve().parents[2]
TZ = "Europe/Athens"
CLOSE = datetime(2026, 9, 16, 10, 5, tzinfo=UTC)


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location(
        "rekey_detections", REPO_ROOT / "scripts" / "rekey_detections.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered before exec: @dataclass resolves annotations via
    # sys.modules[cls.__module__], which is None for an unregistered module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rekey_module = _load_script()


# ── An in-memory Firestore double, just enough for this script ────────────────


class FakeDoc:
    def __init__(self, store: dict[str, dict], doc_id: str) -> None:
        self._store = store
        self.id = doc_id

    @property
    def exists(self) -> bool:
        return self.id in self._store

    def to_dict(self) -> dict | None:
        return self._store.get(self.id)

    def set(self, payload: dict) -> None:
        self._store[self.id] = dict(payload)

    def get(self) -> FakeDoc:
        return self

    def delete(self) -> None:
        self._store.pop(self.id, None)


class FakeCollection:
    def __init__(self, store: dict[str, dict]) -> None:
        self._store = store

    def stream(self):
        # A list, not a generator over the live dict: the script writes while
        # iterating, and a real Firestore stream is a snapshot too.
        return [FakeDoc(self._store, key) for key in list(self._store)]


class FakeClient:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, dict]] = {}
        self.write_count = 0

    def _bucket(self, collection: str) -> dict[str, dict]:
        return self.docs.setdefault(collection, {})

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self._bucket(name))

    def document(self, path: str) -> FakeDoc:
        collection, _, doc_id = path.rpartition("/")
        bucket = self._bucket(collection)
        client = self

        class Counting(FakeDoc):
            def set(self, payload: dict) -> None:
                client.write_count += 1
                super().set(payload)

        return Counting(bucket, doc_id)


def detection(*, agent_version: str = "2.0.0", minutes: int = 0) -> Detection:
    close = CLOSE + timedelta(minutes=minutes)
    return Detection(
        detection_id=detection_id(
            account_scope="primary",
            symbol="XAUUSD",
            timeframe="M5",
            candle_close=close,
            agent_name="ema_cross",
            agent_version=agent_version,
            event_key="bullish",
        ),
        account_scope="primary",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        agent_name="ema_cross",
        agent_version=agent_version,
        event_key="bullish",
        direction=Direction.BUY,
        detected_at=MarketTime.from_utc(close, TZ),
        candle_open_time=MarketTime.from_utc(close - timedelta(minutes=5), TZ),
        price=2400.0,
        session=SessionContext(session=SessionName.LONDON, session_config_version=1),
        sequence_today=1,
        sequence_session=1,
    )


def old_style_id(det: Detection) -> str:
    """The pre-§12 recipe: candle OPEN, no agent_version, event_key before the time."""
    import hashlib

    parts = (
        det.account_scope,
        det.symbol,
        det.timeframe.value,
        det.agent_name,
        det.event_key,
        det.candle_open_time.utc.replace(microsecond=0).isoformat(),
    )
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


def seed_old(client: FakeClient, det: Detection) -> str:
    """Store a detection at its OLD id, as the previous code would have."""
    stale = det.model_dump(mode="json")
    stale["detection_id"] = old_style_id(det)
    # Straight into the store, bypassing the counting wrapper: seeding is setup, and
    # the tests below assert on writes the SCRIPT made.
    client.docs.setdefault(paths.DETECTIONS, {})[stale["detection_id"]] = stale
    return stale["detection_id"]


# ── What it does ──────────────────────────────────────────────────────────────


def test_a_mis_keyed_detection_is_copied_to_its_new_id(client: FakeClient) -> None:
    det = detection()
    old = seed_old(client, det)

    report = rekey_module.rekey(client, dry_run=False)

    assert report.scanned == 1
    assert report.rekeyed == 1
    assert report.already_correct == 0
    bucket = client.docs[paths.DETECTIONS]
    assert det.detection_id in bucket, "the new id was not written"
    assert old in bucket, "the old document must be left in place (copy, not move)"


def test_the_copy_carries_the_new_id_in_its_detection_id_field(
    client: FakeClient,
) -> None:
    """Otherwise the document would point readers back at the key it left.

    A reader that trusts the field over the path — the outbox does, and so does the
    review loader — would follow the stale value straight back to the old document.
    """
    det = detection()
    seed_old(client, det)
    rekey_module.rekey(client, dry_run=False)

    stored = client.docs[paths.DETECTIONS][det.detection_id]
    assert stored["detection_id"] == det.detection_id


def test_a_correctly_keyed_detection_is_left_alone(client: FakeClient) -> None:
    det = detection()
    client.document(paths.detection_path(det.detection_id)).set(
        det.model_dump(mode="json")
    )
    before = client.write_count

    report = rekey_module.rekey(client, dry_run=False)

    assert report.already_correct == 1
    assert report.rekeyed == 0
    assert client.write_count == before, "an already-correct detection was rewritten"


# ── Idempotence: the property that makes it safe to run ───────────────────────


def test_running_twice_writes_nothing_the_second_time(client: FakeClient) -> None:
    det = detection()
    seed_old(client, det)

    first = rekey_module.rekey(client, dry_run=False)
    writes_after_first = client.write_count
    second = rekey_module.rekey(client, dry_run=False)

    assert first.rekeyed == 1
    assert second.rekeyed == 0
    assert second.unchanged == 1, "the second pass should report the copy as unchanged"
    assert second.already_correct == 1, "the copy itself is now correctly keyed"
    assert client.write_count == writes_after_first, "the second run wrote to Firestore"


def test_a_dry_run_writes_nothing_but_still_reports(client: FakeClient) -> None:
    seed_old(client, detection())

    report = rekey_module.rekey(client, dry_run=True)

    assert report.rekeyed == 1
    assert client.write_count == 0
    assert len(client.docs[paths.DETECTIONS]) == 1, "a dry run created a document"


def test_delete_old_removes_the_stale_document(client: FakeClient) -> None:
    det = detection()
    old = seed_old(client, det)

    report = rekey_module.rekey(client, dry_run=False, delete_old=True)

    assert report.deleted_old == 1
    bucket = client.docs[paths.DETECTIONS]
    assert det.detection_id in bucket
    assert old not in bucket


# ── Robustness and reporting ──────────────────────────────────────────────────


def test_one_unreadable_document_does_not_lose_the_rest(client: FakeClient) -> None:
    """A single corrupt document must not abort a migration."""
    seed_old(client, detection())
    client.document(paths.detection_path("garbage")).set({"not": "a detection"})

    report = rekey_module.rekey(client, dry_run=False)

    assert report.unreadable == 1
    assert "garbage" in report.unreadable_ids
    assert report.rekeyed == 1


def test_re_keying_cannot_recover_a_collision_the_old_recipe_already_caused() -> None:
    """The limitation, pinned so nobody plans a migration around the opposite.

    The old recipe left agent_version out of the id, so two versions of an agent
    observing the SAME candle wrote to the same document — the second silently
    overwrote the first. §12 separates them going forward, but the information
    needed to split the old document in two is already gone: only one version's
    data survived, and re-keying faithfully moves that one.

    So the 9/21 -> 20/50 history does not come from this script. It comes from
    replaying the fixture with the new agent, which mints genuinely new detections.
    """
    client = FakeClient()
    v1 = detection(agent_version="1.0.0")
    v2 = detection(agent_version="2.0.0")
    assert v1.detection_id != v2.detection_id, "§12 must separate the two versions"
    assert old_style_id(v1) == old_style_id(v2), (
        "this test is meaningless unless the old recipe really did collide"
    )

    seed_old(client, v1)
    seed_old(client, v2)  # overwrites v1, exactly as the old code would have
    assert len(client.docs[paths.DETECTIONS]) == 1

    report = rekey_module.rekey(client, dry_run=False)

    assert report.scanned == 1
    assert report.rekeyed == 1
    bucket = client.docs[paths.DETECTIONS]
    assert v2.detection_id in bucket, "the surviving version must be re-keyed"
    assert v1.detection_id not in bucket, (
        "the overwritten version cannot be recovered and must not be invented"
    )


def test_the_report_counts_by_agent(client: FakeClient) -> None:
    seed_old(client, detection())
    seed_old(client, detection(minutes=5))

    report = rekey_module.rekey(client, dry_run=False)

    assert report.by_agent == {"ema_cross": 2}
    rendered = report.render()
    assert "scanned          2" in rendered
    assert "ema_cross" in rendered


# ── The production guard ──────────────────────────────────────────────────────


def test_writing_without_the_emulator_is_refused(monkeypatch) -> None:
    """The guard exists because a wrong recipe produces a second wrong dataset."""
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    args = rekey_module.argparse.Namespace(
        dry_run=False, i_know_there_is_no_production_data=False
    )
    refusal = rekey_module._guard(args)
    assert refusal is not None
    assert "FIRESTORE_EMULATOR_HOST" in refusal


def test_the_emulator_needs_no_acknowledgement(monkeypatch) -> None:
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8080")
    args = rekey_module.argparse.Namespace(
        dry_run=False, i_know_there_is_no_production_data=False
    )
    assert rekey_module._guard(args) is None


def test_a_dry_run_is_always_allowed(monkeypatch) -> None:
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    args = rekey_module.argparse.Namespace(
        dry_run=True, i_know_there_is_no_production_data=False
    )
    assert rekey_module._guard(args) is None
