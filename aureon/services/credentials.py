"""Whether Firestore credentials will actually resolve, and what to do when they will not
(11A, F-2).

Separate from ``preflight`` because the diagnosis is the whole value here. "Permission
denied" and "could not find credentials" and "the key file is for a different project" all
arrive at a session script as one opaque exception, and the operator standing in front of it
at 07:50 needs to be told which of the four things to do — not handed a stack trace whose
top frame is inside ``google.auth``.

## Why this is a separate check from the round trip

``preflight.check_firestore`` writes a document and reads it back, which is the only proof
that matters. But when it fails, it fails for one of several reasons that need different
remedies, and a round-trip check cannot tell them apart before it runs. This inspects the
credential *configuration* first, so a missing file is reported as a missing file rather than
as a failed write.

Both checks stay: configuration that looks right and a round trip that works are different
claims, and only the second one is what the observer needs in ten minutes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

#: The variable Google's libraries read. Named here so the remedy text and the check cannot
#: drift apart, and so a grep for it finds both.
CREDENTIALS_ENV = "GOOGLE_APPLICATION_CREDENTIALS"

#: What a service-account JSON must contain to be one. ``client_email`` is the field that
#: distinguishes a service-account key from an OAuth client secret, which is the single most
#: common wrong file to point this variable at -- both are JSON, both come from the same
#: console, and only one of them works.
REQUIRED_KEY_FIELDS = ("client_email", "private_key", "project_id")

#: The value Google puts in a service-account key's ``type``. Checked explicitly (12, T-3)
#: rather than inferred from a missing ``client_email``: an OAuth client file says
#: ``"type": "authorized_user"`` or carries an ``installed``/``web`` block, and naming the type
#: it actually has is a shorter route to the right file than listing the fields it lacks.
SERVICE_ACCOUNT_TYPE = "service_account"

REMEDY = (
    f"Set {CREDENTIALS_ENV}=<path> to a service-account JSON key, or run "
    "`gcloud auth application-default login` for a developer machine. On the VPS the key "
    "path is in .env; the key itself is never in the repo."
)

#: Named separately because the action is different: the key is fine and the identity it names
#: cannot write. An operator handed the generic remedy would go looking for a better key file
#: (12, T-3).
PERMISSION_REMEDY = (
    "The credentials resolved and the identity they name may not write. Grant that service "
    "account `roles/datastore.user` on the Firestore database (or `roles/datastore.owner` if "
    "it must also manage indexes), then re-run. Check the Firestore security rules too if the "
    "project uses them in Native mode."
)

#: The Firestore variable, named here so a message can be explicit about WHICH project it means.
#: Every message in this module says Firestore or says MT5, never "credentials" unqualified: the
#: two live in the same .env and the wrong one is the first thing an operator reaches for.
PROJECT_ENV = "AUREON_FIREBASE_PROJECT_ID"


@dataclass(frozen=True)
class CredentialVerdict:
    """What the credential configuration looks like, before anything is attempted."""

    ok: bool
    detail: str
    remedy: str | None = None
    #: True when no credentials are needed at all, because the emulator is in use. Separate
    #: from ``ok`` so a caller can report "not applicable" rather than "passed" -- a green
    #: row for a check that never ran is how an emulator-only test suite comes to look like
    #: evidence about production.
    not_applicable: bool = False


def is_default_credentials_error(exc: BaseException) -> bool:
    """Whether this exception means "no credentials could be found".

    Matched by NAME rather than by importing ``google.auth.exceptions``, so this module and
    everything that tests it stay importable without the Google libraries installed -- the
    same reason the notification repository matches ``AlreadyExists`` by name.
    """
    seen: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(seen) < 10:
        seen.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return any(
        name in {"DefaultCredentialsError", "RefreshError"} for name in seen
    )


def is_permission_error(exc: BaseException) -> bool:
    """Whether this exception means "the credentials are fine and may not write" (12, T-3).

    Matched by NAME for the same reason as above -- this module must stay importable without the
    Google libraries. ``PermissionDenied`` is the google-api-core name; ``Forbidden`` is the
    google-cloud-core HTTP one; ``403`` appears in the message of both when they arrive wrapped
    in a retry error, which is how the emulator-less path usually surfaces it.
    """
    seen: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(seen) < 10:
        seen.append(type(current).__name__)
        if any(
            marker in str(current)
            for marker in ("403", "PERMISSION_DENIED", "Missing or insufficient permissions")
        ):
            return True
        current = current.__cause__ or current.__context__
    return any(name in {"PermissionDenied", "Forbidden"} for name in seen)


def inspect(
    *,
    emulator_host: str | None = None,
    env: dict[str, str] | None = None,
    project_id: str | None = None,
    collection_prefix: str | None = None,
) -> CredentialVerdict:
    """Look at the FIRESTORE credential configuration and say what is wrong with it.

    Deliberately does not call Google's own resolution: that either succeeds or raises the
    one opaque error this module exists to translate. Reading the variable and the file it
    points at answers the common cases with a remedy attached, and the round-trip check
    behind it catches everything else.

    ``project_id`` is the project the deployment is configured for. When both it and the key's
    own ``project_id`` are known and they differ, that is reported as a failure: the key would
    authenticate perfectly and write a whole session into somebody else's database, which is the
    one credential mistake that produces no error at all (12, T-3).
    """
    environ = os.environ if env is None else env

    if emulator_host:
        # Said loudly, and with the prefix, because this is the row that decides whether a green
        # table is evidence about production or about a throwaway database (12, T-3). A run that
        # skipped this line and read the PASSes above it would conclude the wrong thing.
        where = f" writing to {collection_prefix}_*" if collection_prefix else ""
        return CredentialVerdict(
            ok=True,
            detail=(
                f"EMULATOR at {emulator_host}{where} — no Firestore credentials are used, and "
                "nothing here says anything about production"
            ),
            not_applicable=True,
        )

    raw = (environ.get(CREDENTIALS_ENV) or "").strip()
    if not raw:
        # Not a failure on its own: Application Default Credentials from `gcloud auth` are a
        # legitimate setup on a developer machine, and the round trip behind this is what
        # decides. Reported as a WARN-shaped verdict so the operator knows which path is in
        # play before the round trip either works or does not.
        return CredentialVerdict(
            ok=True,
            detail=f"{CREDENTIALS_ENV} is unset — relying on application default credentials",
            remedy=REMEDY,
        )

    path = Path(raw).expanduser()
    if not path.exists():
        return CredentialVerdict(
            ok=False,
            detail=f"{CREDENTIALS_ENV}={raw} — no such file",
            remedy=REMEDY,
        )
    if path.is_dir():
        return CredentialVerdict(
            ok=False,
            detail=f"{CREDENTIALS_ENV}={raw} — is a directory, not a key file",
            remedy=REMEDY,
        )

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return CredentialVerdict(
            ok=False,
            detail=f"{raw} is not valid JSON: {exc}",
            remedy=REMEDY,
        )
    except OSError as exc:
        return CredentialVerdict(
            ok=False,
            detail=f"{raw} could not be read: {exc}",
            remedy=REMEDY,
        )

    if not isinstance(payload, dict):
        return CredentialVerdict(
            ok=False, detail=f"{raw} is JSON but not an object", remedy=REMEDY
        )

    declared_type = str(payload.get("type") or "").strip()
    if declared_type and declared_type != SERVICE_ACCOUNT_TYPE:
        # Checked before the field list, because the type NAMES the wrong file where the field
        # list only implies it. "authorized_user" is what `gcloud auth ... login` writes and what
        # people copy in by mistake (12, T-3).
        return CredentialVerdict(
            ok=False,
            detail=(
                f'{raw} has "type": "{declared_type}", not "{SERVICE_ACCOUNT_TYPE}" — this is '
                "not a service-account key"
            ),
            remedy=REMEDY,
        )
    if not declared_type and ("installed" in payload or "web" in payload):
        return CredentialVerdict(
            ok=False,
            detail=(
                f"{raw} has no \"type\" and carries an OAuth client block — this is an OAuth "
                "client secret, not a service-account key"
            ),
            remedy=REMEDY,
        )

    missing = [field for field in REQUIRED_KEY_FIELDS if not payload.get(field)]
    if missing:
        # Named individually, because which field is missing says which wrong file this is:
        # no `client_email` is almost always an OAuth client secret rather than a
        # service-account key.
        hint = (
            " — this looks like an OAuth client secret rather than a service-account key"
            if "client_email" in missing
            else ""
        )
        return CredentialVerdict(
            ok=False,
            detail=f"{raw} is missing {', '.join(missing)}{hint}",
            remedy=REMEDY,
        )

    key_project = str(payload["project_id"])
    if project_id and key_project != project_id:
        # The one credential mistake that produces no error anywhere: the key is valid, the
        # write succeeds, and a session lands in a different project's database (12, T-3).
        return CredentialVerdict(
            ok=False,
            detail=(
                f"the key is for Firestore project {key_project!r} and {PROJECT_ENV} says "
                f"{project_id!r}"
            ),
            remedy=(
                f"These must match. Either point {CREDENTIALS_ENV} at a key for "
                f"{project_id}, or set {PROJECT_ENV}={key_project} if that is the intended "
                "database. A mismatch does not fail: it writes the whole session somewhere else."
            ),
        )

    return CredentialVerdict(
        ok=True,
        detail=f"{payload['client_email']} (Firestore project {key_project})",
    )
