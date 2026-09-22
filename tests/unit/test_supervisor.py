from __future__ import annotations

from pathlib import Path

from aureon.services.supervisor import DEFAULT_SERVICES, load_env_file


def test_default_supervisor_services_are_complete() -> None:
    assert [(s.name, s.argv) for s in DEFAULT_SERVICES] == [
        ("observer", ("main_observer.py",)),
        ("monitor", ("main_monitor.py",)),
        ("executor", ("main_executor.py",)),
        ("discord", ("main_discord.py",)),
        ("review", ("main_review.py", "watch")),
    ]


def test_load_env_file_applies_values_without_overwriting_existing(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n"
        "AUREON_ONE=one\n"
        "export AUREON_TWO=\"two words\"\n"
        "AUREON_KEEP=file-value\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AUREON_KEEP", "process-value")
    monkeypatch.delenv("AUREON_ONE", raising=False)
    monkeypatch.delenv("AUREON_TWO", raising=False)

    assert load_env_file(path) == 2
    assert __import__("os").environ["AUREON_ONE"] == "one"
    assert __import__("os").environ["AUREON_TWO"] == "two words"
    assert __import__("os").environ["AUREON_KEEP"] == "process-value"


def test_missing_env_file_is_allowed(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "missing.env") == 0
