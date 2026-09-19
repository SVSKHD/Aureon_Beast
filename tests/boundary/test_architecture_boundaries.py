"""Architectural boundary guards.

These encode the CLAUDE.md non-negotiables as executable tests. They are the
"cross-phase checklist" greps, and they must stay green after every phase.

They are deliberately structural (AST-based for imports) rather than plain text
greps, so that a comment or a docstring mentioning ``MetaTrader5`` does not fail
the suite while a real ``import MetaTrader5`` inside a forbidden module does.

While a package is still empty the corresponding check has nothing to look at
and passes trivially; it starts biting the moment code lands there. That is the
intended behaviour -- the guard is installed before the code it guards.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AUREON = REPO_ROOT / "aureon"

pytestmark = pytest.mark.boundary


def _python_files(*relative_dirs: str) -> list[Path]:
    """Every .py file under the given aureon-relative directories."""
    files: list[Path] = []
    for rel in relative_dirs:
        root = AUREON / rel
        if root.is_dir():
            files.extend(sorted(p for p in root.rglob("*.py") if p.name != "__init__.py"))
    return files


def _imported_modules(path: Path) -> set[str]:
    """Top-level module names imported by a file, including lazy imports.

    Both ``import x.y`` and ``from x.y import z`` contribute ``x`` and ``x.y``,
    so a check can match either a package or an exact module.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # Relative imports (level > 0) carry no absolute module name.
            if node.module and node.level == 0:
                modules.add(node.module)
                modules.add(node.module.split(".")[0])
    return modules


# ── A detection never creates a trade ─────────────────────────────────────────

# The observation half of the system. Nothing here may reach the broker.
OBSERVER_SIDE = ("agents", "engine", "outbox", "data", "evaluation", "reviews")

# Only these two modules may touch MetaTrader5 at all (CLAUDE.md).
MT5_PERMITTED = {
    AUREON / "data" / "mt5_provider.py",
    AUREON / "execution" / "mt5_broker.py",
}


def test_observer_side_never_imports_execution() -> None:
    """agents/engine/outbox/data/evaluation/reviews must not import execution.

    This is the structural half of "a detection never creates a trade": if the
    observation packages cannot even name the execution package, no code path
    through them can place an order.
    """
    offenders: list[str] = []
    for path in _python_files(*OBSERVER_SIDE):
        for module in _imported_modules(path):
            if module == "aureon.execution" or module.startswith("aureon.execution."):
                offenders.append(f"{path.relative_to(REPO_ROOT)} imports {module}")
    assert not offenders, "observer-side code must never import aureon.execution:\n" + "\n".join(
        offenders
    )


def test_observer_side_has_no_order_placing_calls() -> None:
    """No order_send / BrokerInterface references on the observation side.

    Catches a broker reached by something other than an import of
    aureon.execution -- a duck-typed handle passed in, or MetaTrader5 used
    directly.
    """
    forbidden = ("order_send", "BrokerInterface", "send_market_order", "send_pending_order")
    offenders: list[str] = []
    for path in _python_files(*OBSERVER_SIDE):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)} mentions {token}")
    assert not offenders, "observer-side code must not reference the broker:\n" + "\n".join(
        offenders
    )


# ── MetaTrader5 stays behind the two permitted modules ────────────────────────


def test_metatrader5_imported_only_in_the_two_permitted_modules() -> None:
    """Money-moving logic depends on BrokerInterface, never on MetaTrader5.

    Keeping the dependency in exactly two files is what lets the whole suite run
    on Linux/macOS against a fake, since both import it lazily inside a function.
    """
    offenders: list[str] = []
    for path in sorted(AUREON.rglob("*.py")):
        if path in MT5_PERMITTED:
            continue
        if "MetaTrader5" in _imported_modules(path):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    permitted = ", ".join(sorted(str(p.relative_to(REPO_ROOT)) for p in MT5_PERMITTED))
    assert not offenders, (
        f"only {permitted} may import MetaTrader5; offenders:\n" + "\n".join(offenders)
    )


def test_permitted_mt5_modules_import_it_lazily() -> None:
    """The two permitted modules must not import MetaTrader5 at module scope.

    A top-level import would break collection of the entire test suite on any
    non-Windows machine.
    """
    offenders: list[str] = []
    for path in sorted(MT5_PERMITTED):
        if not path.exists():
            continue  # module not written yet
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:  # module scope only, not ast.walk
            if isinstance(node, ast.Import) and any(
                a.name == "MetaTrader5" for a in node.names
            ):
                offenders.append(str(path.relative_to(REPO_ROOT)))
            elif isinstance(node, ast.ImportFrom) and node.module == "MetaTrader5":
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "MetaTrader5 must be imported lazily inside a function, not at module scope:\n"
        + "\n".join(offenders)
    )


# ── Discord reads Firestore; it never trades or computes indicators ───────────


def test_discord_never_touches_broker_or_indicators() -> None:
    """Discord is a human interface over an already-safe backend."""
    offenders: list[str] = []
    for path in _python_files("discord"):
        modules = _imported_modules(path)
        for module in modules:
            if module == "MetaTrader5":
                offenders.append(f"{path.relative_to(REPO_ROOT)} imports MetaTrader5")
            if module in {"aureon.execution.mt5_broker", "aureon.engine.indicators"}:
                offenders.append(f"{path.relative_to(REPO_ROOT)} imports {module}")
        text = path.read_text(encoding="utf-8")
        for token in ("order_send", "send_market_order", "send_pending_order"):
            if token in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)} mentions {token}")
    assert not offenders, "Discord must not reach the broker or compute indicators:\n" + "\n".join(
        offenders
    )


# ── Firestore access is funnelled through aureon/storage ─────────────────────


def test_firestore_clients_are_built_only_in_storage() -> None:
    """Firestore writes go through aureon/storage repositories only."""
    offenders: list[str] = []
    for path in sorted(AUREON.rglob("*.py")):
        if path.is_relative_to(AUREON / "storage"):
            continue
        for module in _imported_modules(path):
            if module.startswith(("google.cloud.firestore", "firebase_admin")):
                offenders.append(f"{path.relative_to(REPO_ROOT)} imports {module}")
    assert not offenders, (
        "only aureon/storage may construct Firestore clients:\n" + "\n".join(offenders)
    )
