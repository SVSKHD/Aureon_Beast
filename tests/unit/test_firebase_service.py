"""Firebase is intentionally absent from the Aureon runtime."""

from __future__ import annotations

import pytest

from aureon.storage.firebase_service import (
    FirebaseRemovedError,
    get_client,
    reset_client,
    set_client,
)


def test_get_client_refuses_with_the_local_storage_remedy() -> None:
    with pytest.raises(FirebaseRemovedError) as raised:
        get_client()
    message = str(raised.value)
    assert "removed" in message.lower()
    assert "build_storage" in message


def test_no_firebase_client_can_be_injected() -> None:
    with pytest.raises(FirebaseRemovedError):
        set_client(object())


def test_reset_is_a_safe_noop() -> None:
    assert reset_client() is None
