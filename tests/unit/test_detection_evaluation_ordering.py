from aureon.outbox.local_outbox import LocalOutbox
from aureon.outbox.outbox_worker import OutboxWorker
from aureon.storage.local_database import LocalDatabase
from aureon.storage.postgres.repositories.detections import DetectionRepository
from aureon.storage.postgres.repositories.evaluations import EvaluationRepository
from tests.postgres.factories import a_detection, an_evaluation


def test_detection_parent_is_established_before_evaluation_write(tmp_path) -> None:
    database = LocalDatabase(tmp_path / "aureon.db")
    database.ensure_schema()
    detections = DetectionRepository(database)
    evaluations = EvaluationRepository(database)
    outbox = LocalOutbox(tmp_path / "outbox.db")

    detection = a_detection("fk-order")
    evaluation = an_evaluation("fk-order")

    assert outbox.enqueue(detection) is True

    worker = OutboxWorker(outbox, detections.upsert_payload)
    ready = worker.ensure_delivered([detection.detection_id])

    assert ready == {detection.detection_id}
    assert detections.get(detection.detection_id) == detection
    assert outbox.get(detection.detection_id).delivered is True

    evaluations.upsert(evaluation)
    assert evaluations.get(detection.detection_id, evaluation.rule_id) == evaluation

    outbox.close()
    database.dispose()


def test_failed_parent_delivery_stays_pending_for_retry(tmp_path) -> None:
    outbox = LocalOutbox(tmp_path / "outbox.db")
    detection = a_detection("pending-parent")
    outbox.enqueue(detection)

    def down(_payload):
        raise RuntimeError("local sink unavailable")

    worker = OutboxWorker(outbox, down, initial_backoff=0.001)
    ready = worker.ensure_delivered([detection.detection_id])

    assert ready == set()
    row = outbox.get(detection.detection_id)
    assert row is not None
    assert row.delivered is False
    assert row.attempts == 1
    assert "RuntimeError" in (row.last_error or "")
    outbox.close()
