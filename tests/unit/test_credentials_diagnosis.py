"""Each way Firestore credentials can be wrong, told apart (11A F-2, extended in 12 T-3).

The whole value of this module is the distinction. All of these arrive at a session script as
one opaque exception from inside ``google.auth``, and the operator standing in front of it at
07:50 needs to be told which of five different things to do:

* there is no key, and nothing to fall back on;
* the path points at nothing, or at a directory;
* the file is JSON but is not a service-account key -- almost always an OAuth client secret;
* the key is valid and is for **a different project**, which is the one mistake that produces no
  error at all: it authenticates, it writes, and a whole session lands in somebody else's
  database;
* the key is valid, the project is right, and the identity may not write.

The last two are 12 T-3's additions. Every message must also name *Firestore*, because the same
``.env`` holds MT5 credentials and the wrong one is the first thing an operator reaches for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aureon.services import credentials

GOOD_KEY = {
    "type": "service_account",
    "project_id": "aureon-prod",
    "client_email": "aureon@aureon-prod.iam.gserviceaccount.com",
    "private_key": "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n",
}


def write_key(tmp_path: Path, payload: object, name: str = "key.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def env(path: Path | None = None) -> dict[str, str]:
    return {} if path is None else {credentials.CREDENTIALS_ENV: str(path)}


# ── the emulator says so loudly, and never passes ─────────────────────────────


def test_the_emulator_is_not_applicable_and_names_the_prefix() -> None:
    """A green row for a check that never ran is how an emulator run looks like evidence.

    T-3 adds the prefix and the sentence, because this row is the one that decides whether the
    PASSes above it are claims about production or about a throwaway database.
    """
    verdict = credentials.inspect(
        emulator_host="127.0.0.1:8080", collection_prefix="aureon_test"
    )
    assert verdict.not_applicable is True
    assert "EMULATOR" in verdict.detail
    assert "aureon_test_*" in verdict.detail
    assert "nothing here says anything about production" in verdict.detail


def test_the_emulator_verdict_survives_an_unknown_prefix() -> None:
    verdict = credentials.inspect(emulator_host="127.0.0.1:8080")
    assert verdict.not_applicable is True
    assert "EMULATOR" in verdict.detail


# ── nothing configured ────────────────────────────────────────────────────────


def test_an_unset_variable_is_reported_as_relying_on_defaults() -> None:
    """Not a failure: `gcloud auth application-default login` is a legitimate setup.

    But the operator is told which path is in play, because on an unattended VPS it is the
    wrong one and the round trip behind this is what will decide.
    """
    verdict = credentials.inspect(env={})
    assert verdict.ok is True
    assert verdict.remedy is not None, "the WARN shape: configured, but by the developer path"
    assert "application default credentials" in verdict.detail


# ── the path is wrong ─────────────────────────────────────────────────────────


def test_a_path_that_does_not_exist_is_named_as_such(tmp_path) -> None:
    verdict = credentials.inspect(env=env(tmp_path / "absent.json"))
    assert verdict.ok is False
    assert "no such file" in verdict.detail


def test_a_directory_is_not_a_key_file(tmp_path) -> None:
    (tmp_path / "keys").mkdir()
    verdict = credentials.inspect(env=env(tmp_path / "keys"))
    assert verdict.ok is False
    assert "is a directory" in verdict.detail


def test_a_file_that_is_not_json_says_so(tmp_path) -> None:
    path = tmp_path / "key.json"
    path.write_text("-----BEGIN PRIVATE KEY-----", encoding="utf-8")
    verdict = credentials.inspect(env=env(path))
    assert verdict.ok is False
    assert "not valid JSON" in verdict.detail


# ── the wrong KIND of file (12, T-3) ──────────────────────────────────────────


def test_an_authorized_user_file_is_named_by_its_type(tmp_path) -> None:
    """What `gcloud auth application-default login` writes, copied in by mistake.

    Before T-3 this was diagnosed only by its missing fields. Naming the ``type`` it has is a
    shorter route to the right file than listing the three it lacks.
    """
    path = write_key(
        tmp_path,
        {"type": "authorized_user", "client_id": "x", "refresh_token": "y"},
    )
    verdict = credentials.inspect(env=env(path))
    assert verdict.ok is False
    assert '"type": "authorized_user"' in verdict.detail
    assert "not a service-account key" in verdict.detail


@pytest.mark.parametrize(
    "block",
    ["installed", "web"],
    ids=["desktop-client", "web-client"],
)
def test_an_oauth_client_secret_without_a_type_is_named_by_its_block(
    tmp_path, block: str
) -> None:
    """The file the Cloud console offers next to the key, with no ``type`` at all.

    Asserted on "carries an OAuth client block", not merely on "OAuth client secret". A plant
    that disabled this branch SURVIVED the weaker assertion, because the missing-field branch
    below also mentions an OAuth client secret when ``client_email`` is absent -- so the test was
    passing on a different diagnosis than the one it was meant to be checking.

    The distinction is worth having: "this file carries an OAuth client block" points at the
    file, where "missing client_email, private_key, project_id" points at three fields and leaves
    the operator to infer which file they have.
    """
    path = write_key(
        tmp_path,
        {block: {"client_id": "x.apps.googleusercontent.com", "client_secret": "y"}},
    )
    verdict = credentials.inspect(env=env(path))
    assert verdict.ok is False
    assert "carries an OAuth client block" in verdict.detail
    assert "not a service-account key" in verdict.detail
    # And it must NOT fall through to the field list, which is the weaker message.
    assert "is missing" not in verdict.detail


def test_a_service_account_key_missing_a_field_names_the_field(tmp_path) -> None:
    payload = dict(GOOD_KEY)
    del payload["private_key"]
    verdict = credentials.inspect(env=env(write_key(tmp_path, payload)))
    assert verdict.ok is False
    assert "private_key" in verdict.detail


def test_json_that_is_not_an_object_is_refused(tmp_path) -> None:
    verdict = credentials.inspect(env=env(write_key(tmp_path, ["a", "list"])))
    assert verdict.ok is False
    assert "not an object" in verdict.detail


# ── the wrong PROJECT (12, T-3) ───────────────────────────────────────────────


def test_a_key_for_another_project_is_a_failure_not_a_warning(tmp_path) -> None:
    """The mistake that produces no error anywhere else.

    The key authenticates, the write succeeds, and the session lands in a different database.
    Nothing downstream would notice: the collections are created on demand, so the wrong project
    looks exactly like a first run.
    """
    path = write_key(tmp_path, GOOD_KEY)
    verdict = credentials.inspect(env=env(path), project_id="aureon-staging")
    assert verdict.ok is False
    assert "aureon-prod" in verdict.detail
    assert "aureon-staging" in verdict.detail
    assert credentials.PROJECT_ENV in verdict.detail or credentials.PROJECT_ENV in (
        verdict.remedy or ""
    )
    assert "writes the whole session somewhere else" in (verdict.remedy or "")


def test_a_matching_project_passes_and_names_the_identity(tmp_path) -> None:
    path = write_key(tmp_path, GOOD_KEY)
    verdict = credentials.inspect(env=env(path), project_id="aureon-prod")
    assert verdict.ok is True
    assert verdict.remedy is None
    assert GOOD_KEY["client_email"] in verdict.detail
    assert "Firestore project aureon-prod" in verdict.detail


def test_no_configured_project_means_nothing_to_compare_against(tmp_path) -> None:
    """Not a failure. ``AUREON_FIREBASE_PROJECT_ID`` is optional -- the key carries one."""
    path = write_key(tmp_path, GOOD_KEY)
    verdict = credentials.inspect(env=env(path), project_id=None)
    assert verdict.ok is True


# ── permission, told apart from everything above (12, T-3) ────────────────────


class Denied(Exception):
    pass


class PermissionDenied(Exception):
    pass


class Forbidden(Exception):
    pass


@pytest.mark.parametrize(
    "exc",
    [
        PermissionDenied("the caller does not have permission"),
        Forbidden("403 GET https://firestore.googleapis.com/..."),
        Denied("403 Missing or insufficient permissions."),
        Denied("PERMISSION_DENIED: Cloud Firestore API has not been used"),
    ],
    ids=["by-name-PermissionDenied", "by-name-Forbidden", "by-403", "by-PERMISSION_DENIED"],
)
def test_a_permission_failure_is_recognised(exc: Exception) -> None:
    """Matched by name AND by message, because it arrives both ways.

    A retry wrapper turns the typed exception into a ``RetryError`` whose message carries the
    403, which is how it usually surfaces when the project's rules refuse the write.
    """
    assert credentials.is_permission_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        Denied("failed to connect to all addresses"),
        Denied("404 database does not exist"),
        Denied("Deadline Exceeded"),
    ],
)
def test_an_unrelated_failure_is_not_called_a_permission_problem(exc: Exception) -> None:
    """A wrong diagnosis costs more than a vague one: it sends the operator to IAM for an
    outage that is a network problem."""
    assert credentials.is_permission_error(exc) is False


def test_a_missing_credentials_error_is_not_a_permission_error() -> None:
    class DefaultCredentialsError(Exception):
        pass

    exc = DefaultCredentialsError("Could not automatically determine credentials")
    assert credentials.is_default_credentials_error(exc) is True
    assert credentials.is_permission_error(exc) is False


def test_the_permission_remedy_names_the_role_to_grant() -> None:
    """Because "permission denied" without the role name is a search, not a remedy."""
    assert "roles/datastore.user" in credentials.PERMISSION_REMEDY


def test_a_chained_exception_is_walked() -> None:
    """google-api-core wraps; the cause is where the name usually is."""
    try:
        try:
            raise PermissionDenied("denied")
        except PermissionDenied as inner:
            raise RuntimeError("write failed") from inner
    except RuntimeError as outer:
        assert credentials.is_permission_error(outer) is True


# ── no message may confuse Firestore with MT5 ─────────────────────────────────


def test_no_remedy_in_this_module_mentions_mt5(tmp_path) -> None:
    """The two sets of credentials live in the same ``.env``.

    An operator told "check your credentials" reaches for whichever they touched last, and on
    this system that is usually the terminal login. Every message here says Firestore, or names
    the Firestore variable, and none of them says MT5.
    """
    verdicts = [
        credentials.inspect(env={}),
        credentials.inspect(env=env(tmp_path / "absent.json")),
        credentials.inspect(env=env(write_key(tmp_path, {"type": "authorized_user"}))),
        credentials.inspect(env=env(write_key(tmp_path, GOOD_KEY)), project_id="other"),
    ]
    for verdict in verdicts:
        text = f"{verdict.detail} {verdict.remedy or ''}"
        assert "MT5" not in text and "MetaTrader" not in text, text
    assert "MT5" not in credentials.PERMISSION_REMEDY
    assert "MT5" not in credentials.REMEDY
