"""In-process protection against sending the same request twice (§32, §34).

Two layers guard against a double send, and they cover different failures:

* **Firestore's ``claim``** stops two *processes* from both acting. That is the durable
  guarantee.
* **``SendGuard`` here** stops one process from sending twice -- a retry loop, a
  duplicated listener callback, a listener and a poll both picking up the same request
  in the same instant. Firestore cannot see any of that, because the document is
  already legitimately ``EXECUTING`` under this instance's own lease.

Neither is sufficient alone.
"""

from __future__ import annotations

import threading

from aureon.models.identity import comment_token, execution_attempt_id

__all__ = ["SendGuard", "comment_token", "execution_attempt_id"]


class AlreadySent(RuntimeError):
    """This instance has already sent an order for this request.

    Raised rather than returning quietly, because reaching this point means a caller
    tried to re-send -- which is a bug worth surfacing, not a condition to absorb.
    """


class SendGuard:
    """Records which requests this process has already sent an order for."""

    def __init__(self) -> None:
        self._sent: dict[str, str] = {}
        self._lock = threading.Lock()

    def begin(self, request_id: str) -> str:
        """Claim the right to send, returning a fresh ``execution_attempt_id``.

        Registered **before** the send, not after. If it were recorded afterwards, a
        crash mid-send would leave no trace and the next attempt in the same process
        would be allowed through -- the exact double-send this exists to prevent.
        """
        with self._lock:
            if request_id in self._sent:
                raise AlreadySent(
                    f"this instance already sent an order for {request_id} "
                    f"(attempt {self._sent[request_id]}); an unknown outcome must be "
                    "reconciled, never re-sent"
                )
            attempt = execution_attempt_id()
            self._sent[request_id] = attempt
            return attempt

    def attempt_for(self, request_id: str) -> str | None:
        with self._lock:
            return self._sent.get(request_id)

    def has_sent(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self._sent

    def forget(self, request_id: str) -> None:
        """Drop a record. Used only by reconciliation after it has established the
        outcome, so a long-running process does not grow without bound."""
        with self._lock:
            self._sent.pop(request_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sent)
