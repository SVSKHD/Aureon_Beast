"""The client factory's two paths, and the one place a missing dependency is acceptable.

``get_client`` is the only place a Firestore client is built, so what it does when
``firebase-admin`` is absent decides whether the operator tools can be rehearsed at all.
The answer is deliberately asymmetric:

* against the **emulator**, ``google-cloud-firestore`` alone is enough -- there are no
  credentials to resolve, and a rehearsal nobody can run is not a rehearsal;
* against a **real project**, a missing ``firebase-admin`` is loud. That is the credential
  path the rest of the system is built on, and quietly resolving it through application
  default credentials instead is how a tool ends up writing somewhere nobody intended.
"""

from __future__ import annotations

import importlib.util

import pytest

from aureon.storage.firebase_service import get_client, reset_client

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("firebase_admin") is not None,
    reason=(
        "firebase-admin is installed, so the fallback these tests describe is not the "
        "path taken. It is the path on a machine that only rehearses."
    ),
)


@pytest.fixture(autouse=True)
def _forget_the_cached_client():
    """The client is process-wide and cached; these tests must not leak one."""
    reset_client()
    yield
    reset_client()


def test_a_real_project_without_firebase_admin_is_refused() -> None:
    with pytest.raises(RuntimeError, match="firebase-admin is not installed"):
        get_client(project_id="aureon-production")


def test_the_emulator_needs_only_google_cloud_firestore(monkeypatch) -> None:
    # setenv first: get_client assigns the same variable, and monkeypatch restores the
    # original state on teardown either way -- without this, one test would leave every
    # later one pointed at an emulator that is not there.
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:9")
    client = get_client(project_id="aureon-rehearsal", emulator_host="127.0.0.1:9")
    assert type(client).__module__.startswith("google.cloud.firestore")
    assert client.project == "aureon-rehearsal"


def test_the_emulator_client_is_cached_like_the_real_one(monkeypatch) -> None:
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:9")
    first = get_client(project_id="aureon-rehearsal", emulator_host="127.0.0.1:9")
    second = get_client(project_id="aureon-rehearsal", emulator_host="127.0.0.1:9")
    assert first is second


def test_a_project_id_falls_back_to_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:9")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "from-the-environment")
    client = get_client(emulator_host="127.0.0.1:9")
    assert client.project == "from-the-environment"
