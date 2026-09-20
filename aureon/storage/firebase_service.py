"""The single Firestore client factory (§71, §72).

The only place a Firestore client is constructed. A boundary test enforces that no
module outside ``aureon/storage`` imports ``firebase_admin`` or
``google.cloud.firestore``, so every write in the system funnels through the
repositories here.

Imports are **lazy**, inside the factory, for the same reason the MT5 modules defer
theirs: the test suite imports this module to exercise the repositories against an
in-memory double, and must not require the Google libraries -- or credentials -- to
be present.

Credentials come from ``GOOGLE_APPLICATION_CREDENTIALS`` and are never hard-coded or
logged (CLAUDE.md).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

_client: Any | None = None
_lock = threading.Lock()


@runtime_checkable
class FirestoreLike(Protocol):
    """The slice of the Firestore client the repositories actually use.

    Declared as a Protocol so tests can pass an in-memory double and so the
    repositories are not coupled to the vendor SDK's full surface.
    """

    def document(self, path: str) -> Any: ...

    def collection(self, path: str) -> Any: ...


def get_client(*, project_id: str | None = None, emulator_host: str | None = None) -> Any:
    """Return the process-wide Firestore client, creating it once.

    Cached because ``firebase_admin.initialize_app`` raises if called twice, and
    because a client holds connection state worth reusing across repositories.
    """
    global _client
    with _lock:
        if _client is not None:
            return _client

        if emulator_host:
            # Set before the client is built: the SDK reads this at construction.
            # An emulator host pointed at a real project would otherwise write to
            # production, so this is logged loudly.
            os.environ["FIRESTORE_EMULATOR_HOST"] = emulator_host
            log.warning("Firestore client using EMULATOR at %s", emulator_host)

        try:
            import firebase_admin
            from firebase_admin import credentials, firestore
        except ImportError as exc:
            if emulator_host:
                # The emulator needs no credentials, so google-cloud-firestore alone is
                # enough to reach it -- and that is the whole dependency set on a machine
                # that only ever rehearses. Restricted to the emulator ON PURPOSE:
                # against a real project the firebase-admin credential path is the one
                # the rest of the system is built on, and a missing dependency there
                # should be loud rather than quietly resolved through ADC.
                _client = _emulator_client(project_id)
                return _client
            raise RuntimeError(
                "firebase-admin is not installed. Install the project dependencies, "
                "or pass an in-memory client to the repositories in tests."
            ) from exc

        if not firebase_admin._apps:  # noqa: SLF001 - the SDK's own registry
            creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            options = {"projectId": project_id} if project_id else None
            # Application-default credentials when no explicit file is given, so a
            # GCP-hosted process needs no key material on disk.
            cred = (
                credentials.Certificate(creds_path)
                if creds_path
                else credentials.ApplicationDefault()
            )
            firebase_admin.initialize_app(cred, options)

        _client = firestore.client()
        return _client


def _emulator_client(project_id: str | None) -> Any:
    """A plain ``google.cloud.firestore`` client, for the emulator only.

    Exists so the operator tools -- preflight, session verification, the demo drills --
    can be rehearsed against the emulator on a machine with no service account and no
    firebase-admin. A rehearsal nobody can run is not a rehearsal.
    """
    try:
        from google.cloud import firestore as gcloud_firestore
    except ImportError as exc:  # pragma: no cover - both packages absent
        raise RuntimeError(
            "neither firebase-admin nor google-cloud-firestore is installed; "
            "install the project dependencies."
        ) from exc

    project = project_id or os.environ.get("GOOGLE_CLOUD_PROJECT") or "aureon-emulator"
    log.warning(
        "firebase-admin is absent; using google-cloud-firestore against the emulator "
        "with project %s",
        project,
    )
    return gcloud_firestore.Client(project=project)


def set_client(client: Any | None) -> None:
    """Install a client explicitly. Used by tests and by the emulator harness."""
    global _client
    with _lock:
        _client = client


def reset_client() -> None:
    """Forget the cached client, so the next call rebuilds it."""
    set_client(None)
