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
# ``services`` joined the list with P-2: the observer's own services (market state,
# heartbeats, preflight) read the broker's DATA through a provider, which is what `data`
# is for, and must never acquire the ability to act on it. Preflight in particular holds
# a live terminal handle and runs beside a human who is about to enable trading.
OBSERVER_SIDE = (
    "agents",
    "engine",
    "outbox",
    "data",
    "evaluation",
    "reviews",
    "services",
    # ``visuals`` joined with T-10. A chart is a picture of observations, and the package that
    # draws it has no business holding anything that can act on them -- least of all because its
    # main caller is Discord, which is forbidden the broker outright (§71).
    "visuals",
)

# Only these two modules may touch MetaTrader5 at all (CLAUDE.md).
MT5_PERMITTED = {
    AUREON / "data" / "mt5_provider.py",
    AUREON / "execution" / "mt5_broker.py",
}


def test_every_observation_package_is_named_in_the_list() -> None:
    """Pinned as a literal, because THE LIST IS THE GUARD.

    Every rule below iterates ``OBSERVER_SIDE``. A package quietly dropped from the tuple is not
    a failing test -- it is a guard switched off, silently, with every test in this file still
    green. A plant removing ``visuals`` survived every other check here, which is how this test
    came to exist.

    Adding a package is a one-line change to this literal, made on purpose. Removing one should
    be the same.
    """
    assert set(OBSERVER_SIDE) == {
        "agents",
        "engine",
        "outbox",
        "data",
        "evaluation",
        "reviews",
        "services",
        "visuals",
    }
    # And every name must be a package that actually exists, or the rule iterates nothing.
    for package in OBSERVER_SIDE:
        assert (AUREON / package).is_dir(), f"{package} is not a package under aureon/"


def test_observer_side_never_imports_execution() -> None:
    """The observation packages must not import execution.

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


def test_no_firestore_access_outside_storage() -> None:
    """CLAUDE.md: Firestore goes through ``aureon/storage``, with no exceptions.

    The older guard only checked that nobody outside storage IMPORTED a Firestore SDK.
    That missed the shape the code actually took: an injected client, with
    ``.collection(...)`` and ``.document(...)`` called on it. Six such call sites existed
    -- four in the review service, one in the Discord listener, and one in the executor
    that reached into ``repository._client``, a private attribute.

    All six were reads, so no client-side money write ever existed. That is exactly why
    this guard matters: the rule that was supposed to prevent one did not constrain this
    shape of code at all, so the next raw call could have been a ``.set()`` and the suite
    would have stayed green.

    AST-based, so a docstring or a comment mentioning ``.collection(`` does not fail the
    suite while a real call does.
    """
    forbidden_methods = {"collection", "document", "transaction"}
    offenders: list[str] = []
    for path in sorted(AUREON.rglob("*.py")):
        if path.is_relative_to(AUREON / "storage"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in forbidden_methods:
                continue
            offenders.append(
                f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                f"{ast.unparse(func)}(...)"
            )
    assert not offenders, (
        "only aureon/storage may call Firestore's .collection()/.document()/"
        ".transaction(); move the read into a repository method:\n" + "\n".join(offenders)
    )


def test_nothing_reaches_into_a_repositorys_private_client() -> None:
    """``repository._client`` puts the write discipline one attribute access away.

    Checked separately from the call guard because the access alone is the problem: a
    caller holding the raw client can do anything the repository was written to prevent,
    and the next reader has no reason to think the repository is authoritative.
    """
    offenders: list[str] = []
    for path in sorted(AUREON.rglob("*.py")):
        if path.is_relative_to(AUREON / "storage"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr != "_client":
                continue
            # ``self._client`` is a class's own attribute, not a reach into someone
            # else's repository. The offence is holding ANOTHER object's client.
            if isinstance(node.value, ast.Name) and node.value.id == "self":
                continue
            offenders.append(
                f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {ast.unparse(node)}"
            )
    assert not offenders, (
        "a repository's private _client must not be reached into from outside "
        "aureon/storage:\n" + "\n".join(offenders)
    )


#: The collection names, unprefixed. A bare occurrence of any of these as a string
#: literal outside paths.py is almost certainly a forgotten prefix.
def _bare_collection_names() -> frozenset[str]:
    """Every collection's UNPREFIXED name, derived from the registry (11A, F-10).

    This used to be a hand-written set, and it had drifted by two entries -- ``assessments``
    and ``trade_notes``, added in 9D -- which means the bare-literal check below had silently
    stopped covering the two newest collections. A boundary test that quietly narrows its own
    scope is worse than no boundary test: it reports green over exactly the ground it stopped
    looking at.

    Derived, so the scope cannot narrow again. ``paths.ALL_COLLECTIONS`` is itself generated
    from ``paths.py`` and cross-checked against the source by ``tests/unit/test_paths.py``.
    """
    from aureon.storage import paths

    prefix = f"{paths.PREFIX}_"
    return frozenset(name[len(prefix) :] for name in paths.ALL_COLLECTIONS)


BARE_COLLECTION_NAMES = _bare_collection_names()


def _innocent_string_nodes(tree: ast.AST) -> set[int]:
    """String constants that cannot be a collection reference, by their POSITION.

    Needed because a collection name and a field name legitimately collide. Widening the scan
    to the full registry immediately produced four hits that were all innocent:
    ``"trade_notes"`` and ``"trades_by_tag"`` are keys on a review DOCUMENT, and
    ``getattr(context, "assessments", None)`` names a ``BotContext`` attribute.

    Excluded by CONTEXT rather than by value, and the difference matters: a first attempt
    excluded any string equal to a model field name, and that suppressed a genuine
    ``client.collection("assessments")`` as well -- the check went quiet on exactly the kind of
    bug it exists to find. Planting that call is what caught it.

    Three positions are innocent, and nothing else:

    * a **dict key** -- ``{"trade_notes": ...}`` builds a document, it does not name a
      collection;
    * a **string subscript** -- ``fields["trade_notes"]`` reads one back out of a dict; there
      is no API in this codebase where a collection is reached by indexing;
    * the **attribute-name argument** of ``getattr``/``setattr``/``hasattr``.

    An argument to ``.collection(...)`` or ``.document(...)`` is never innocent, whatever the
    string says.
    """
    innocent: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    innocent.add(id(key))
        elif isinstance(node, ast.Subscript):
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                innocent.add(id(node.slice))
        elif isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name in {"getattr", "setattr", "hasattr"} and len(node.args) >= 2:
                second = node.args[1]
                if isinstance(second, ast.Constant) and isinstance(second.value, str):
                    innocent.add(id(second))
    return innocent


def test_no_bare_collection_literal_outside_paths() -> None:
    """Every collection name is built by ``paths.collection()`` (§83, decision 111).

    A bare literal is the worst kind of bug because it does not fail. Reading or writing
    ``"detections"`` when everything else uses ``"aureon_beast_detections"`` silently
    touches a second, empty collection -- which looks exactly like "no data yet".

    Checked as string CONSTANTS via the AST, so a docstring or a dict key derived from a
    path does not trip it while a real ``client.collection("trades")`` does.
    """
    paths_module = AUREON / "storage" / "paths.py"
    # tests/ is scanned too. A stale literal in a test HELPER is worse than one in
    # production code: it matches nothing, so the negative assertions pass vacuously and
    # only a positive one fails -- if there happens to be one.
    scanned = [*AUREON.rglob("*.py"), *(REPO_ROOT / "tests").rglob("*.py")]
    # paths.py builds the names; its own test has to pass bare ones to test the builder;
    # this file lists them to check for them. Those three, and nothing else.
    exempt = {
        paths_module,
        Path(__file__).resolve(),
        (REPO_ROOT / "tests" / "unit" / "test_paths.py").resolve(),
    }
    offenders: list[str] = []
    for path in sorted(scanned):
        if path.resolve() in exempt:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        innocent = _innocent_string_nodes(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in BARE_COLLECTION_NAMES
                and id(node) not in innocent
            ):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {node.value!r}"
                )
    assert not offenders, (
        "collection names must come from aureon.storage.paths, never a bare literal:\n"
        + "\n".join(offenders)
    )


def test_every_collection_constant_carries_the_prefix() -> None:
    """The constants hold prefixed values, so existing callers are already correct."""
    from aureon.storage import paths

    assert paths.PREFIX, "a prefix must always resolve to something"
    for name in paths.ALL_COLLECTIONS:
        assert name.startswith(f"{paths.PREFIX}_"), f"{name} is not prefixed"
    # Round-tripping through ``collection()`` must be the identity. Not a tautology: it is
    # what catches a constant built by string concatenation instead of the helper, which would
    # be prefixed and still bypass the one place §83 says the name is assembled.
    assert set(paths.ALL_COLLECTIONS) == {
        paths.collection(bare) for bare in BARE_COLLECTION_NAMES
    }
    assert len(BARE_COLLECTION_NAMES) == len(paths.ALL_COLLECTIONS), (
        "two collections share an unprefixed name, so one of them is unreachable"
    )


# ── Reviews may only read COMPLETE horizons ───────────────────────────────────


def test_reviews_never_read_horizons_directly() -> None:
    """Phase 3: ``complete_horizons`` is the only accessor review code may use.

    Reading ``DetectionEvaluation.horizons`` in a review would silently fold PENDING and
    INVALID horizons into a reached-N count, turning "we do not know yet" into "it did not
    happen" and making every statistic in the review pessimistically wrong -- with nothing
    in the output to reveal it.

    AST-based, not a text grep. A grep cannot tell these apart, and all four of the last
    four are legitimate:

    * ``evaluation.horizons``          -- the violation this guard exists for;
    * ``rule.horizons``                -- the EvaluationRule's horizon *definitions*, which
      a review must read to know which horizons exist at all;
    * ``totals.horizons`` / ``result.horizons`` -- the aggregation's own output;
    * ``horizons=...``                 -- a keyword argument building a review document.

    So the rule is: ``<name>.horizons`` is forbidden unless ``<name>`` is in the documented
    safe set below. A new receiver trips it by default, which is the right way round -- a
    developer reaching for ``evaluation.horizons`` is caught, and one adding a genuinely
    safe container has to say so deliberately.
    """
    # Receivers whose `.horizons` is NOT a DetectionEvaluation's unfiltered tuple.
    safe_receivers = {
        "rule",  # EvaluationRule: the horizon definitions
        "result",  # Aggregates: this module's own output
        "totals",  # Aggregates, at the call site
        "self",  # a review object's own field
        "review",  # a built DailyReview/WeeklyReview
    }
    safe_attributes = {"complete_horizons", "pending_horizons", "invalid_horizons"}

    offenders: list[str] = []
    for path in _python_files("reviews"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr in safe_attributes or node.attr != "horizons":
                continue
            receiver = node.value
            name = receiver.id if isinstance(receiver, ast.Name) else None
            if name in safe_receivers:
                continue
            rendered = name or type(receiver).__name__
            offenders.append(
                f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {rendered}.horizons"
            )
    assert not offenders, (
        "review code must aggregate via DetectionEvaluation.complete_horizons, never the "
        "unfiltered .horizons:\n" + "\n".join(offenders)
    )


def test_reviews_never_write_the_records_they_analyse() -> None:
    """§50: an inferred link is analysis, and analysis must not edit its subject.

    ``aureon/reviews`` reads detections, evaluations, trades and sessions, and writes only
    its own two collections. A behavioural test already proves an inferred link does not
    mutate the trade it points at, but that only covers the path it exercises. This covers
    every path, by denying the package the ability: it may not import a repository that
    writes anything else.

    The failure this prevents is quiet. A review that "helpfully" stamped its best guess
    onto the trade would turn a guess into a permanent record, indistinguishable from a
    human's own statement, and every statistic built on those trades afterwards would
    inherit it without being able to identify it as inferred.
    """
    forbidden_writers = {
        "aureon.storage.trade_repository",
        "aureon.storage.trade_request_repository",
        "aureon.storage.detection_repository",
        "aureon.storage.evaluation_repository",
        "aureon.storage.session_repository",
        "aureon.storage.control_request_repository",
        "aureon.storage.settings_repository",
        "aureon.outbox.local_outbox",
        "aureon.outbox.outbox_worker",
    }
    offenders: list[str] = []
    for path in _python_files("reviews"):
        for module in _imported_modules(path):
            if module in forbidden_writers:
                offenders.append(f"{path.relative_to(REPO_ROOT)} imports {module}")
    assert not offenders, (
        "reviews may read detections, evaluations, trades and sessions but must write "
        "only daily_reviews and weekly_reviews (§50):\n" + "\n".join(offenders)
    )


# ── Discord's write scope ─────────────────────────────────────────────────────


def test_discord_writes_only_what_it_is_permitted_to() -> None:
    """CLAUDE.md: Discord writes ``trade_requests``, ``settings.trading_enabled`` and
    ``audit_logs`` -- plus ``control_requests`` from Phase 6, which is how §46/§47 ask for
    cancels and closes to be requested.

    It must never write a detection, a trade, an evaluation or a review. Those are
    observations and outcomes: a human interface that could write them could rewrite
    history, and every statistic built on that history would become unfalsifiable.

    Enforced by which repositories the Discord package is allowed to import. The
    read-only ones are fine -- Discord reads freely; it is writing that is constrained --
    so this checks for the repositories whose whole purpose is to write those collections.
    """
    # Modules whose purpose is to WRITE those collections. Read-only counterparts are
    # fine and exist precisely so Discord can read without being able to write:
    # ``review_reader`` is the sanctioned way to show a review on /status (§61-§63).
    forbidden_writers = {
        "aureon.storage.evaluation_repository",
        "aureon.storage.session_repository",
        "aureon.storage.review_repository",
        "aureon.outbox.local_outbox",
        "aureon.outbox.outbox_worker",
    }
    offenders: list[str] = []
    for path in _python_files("discord"):
        for module in _imported_modules(path):
            if module in forbidden_writers:
                offenders.append(f"{path.relative_to(REPO_ROOT)} imports {module}")
    assert not offenders, (
        "Discord may not write detections, evaluations, sessions or reviews:\n"
        + "\n".join(offenders)
    )


def test_discord_holds_no_broker_or_data_provider() -> None:
    """The BotContext is the whole surface Discord can reach.

    A broker or provider field there would make the import-level guards moot: the object
    would arrive at runtime, handed over by whoever built the context. Checking the
    dataclass's own annotations closes that.
    """
    from aureon.discord.context import BotContext

    fields = set(BotContext.__dataclass_fields__)
    assert "broker" not in fields
    assert "provider" not in fields
    annotations = " ".join(
        str(f.type) for f in BotContext.__dataclass_fields__.values()
    ).lower()
    assert "broker" not in annotations
    assert "provider" not in annotations


# ── The assessment is arithmetic over stored outcomes, and nothing else ───────


def test_the_assessment_service_cannot_reach_execution_or_a_provider() -> None:
    """A readout that could touch either would stop being a description (9D).

    ``aureon.execution`` is how an order is placed; a data provider is how a live price is
    read. The assessment describes what already happened to detections of one shape, so it
    needs neither -- and a module that imported one would be a plausible place for
    "…and then act on it" to appear later.

    Checked by import rather than by intent: intent is not enforceable, and the whole reason
    this file exists is that the rules which matter are the ones a future change cannot
    quietly break.
    """
    path = AUREON / "services" / "assessment_service.py"
    assert path.exists(), "9D's assessment service is missing"

    modules = _imported_modules(path)
    forbidden = [
        module
        for module in modules
        if module == "aureon.execution"
        or module.startswith("aureon.execution.")
        or module == "aureon.data"
        or module.startswith("aureon.data.")
        or module == "MetaTrader5"
    ]
    assert not forbidden, (
        f"assessment_service imports {forbidden}; it may read stored outcomes and candles "
        "it is handed, nothing else"
    )


def test_the_assessment_service_never_reads_a_trader_note() -> None:
    """Notes are for people (9D).

    The moment a note moved a number, the number would stop measuring the market and start
    measuring the trader's mood when they typed it -- and nothing downstream could tell the
    two apart, because both arrive as a float.

    ``aureon/reviews`` is deliberately absent from the scan: printing notes beside each
    trade is exactly what the weekly review is for. Everything that computes a number is in
    scope.
    """
    offenders: list[str] = []
    for path in _python_files("services", "evaluation", "engine", "agents"):
        text = path.read_text(encoding="utf-8")
        modules = _imported_modules(path)
        if "aureon.storage.note_repository" in modules or "TradeNote" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "these would let a trader's own note move a measured number:\n" + "\n".join(offenders)
    )


# ── T-9: the measured reference is context, never a gate ──────────────────────


def test_the_setup_engine_never_reads_a_reference_to_decide() -> None:
    """A setup's ``reference`` block must not reach any decision.

    It is a measured cohort of past outcomes, attached at open and rendered on a card. The moment
    something branches on it, the system has started preferring one structure over another on the
    strength of its own history -- which is a model, and a model is the one thing this codebase
    does not have. Worse, the preference would be invisible: every number in the block is
    genuinely measured, so the branch would look like arithmetic.

    AST rather than a grep, so the legitimate uses are nameable. ``self.reference`` is the hook
    the observer installs, and ``reference=`` is the keyword that attaches the block.
    """
    import ast

    engine = AUREON / "services" / "setup_engine.py"
    offenders: list[str] = []
    for node in ast.walk(ast.parse(engine.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Attribute) or node.attr != "reference":
            continue
        receiver = node.value
        if isinstance(receiver, ast.Name) and receiver.id == "self":
            continue
        offenders.append(f"{engine.name}:{node.lineno} reads .reference off something else")
    assert not offenders, (
        "the setup engine must not read a measured reference:\n" + "\n".join(offenders)
    )


def test_the_reference_service_measures_and_never_writes() -> None:
    """``setup_reference`` is arithmetic over rows somebody else read.

    It is handed a repository and calls exactly one read method on it. A write from here would
    put a second writer on ``setups`` or ``setup_evaluations`` -- and the transaction in
    ``SetupRepository.record`` is safe precisely because the observer is the only one.
    """
    text = (AUREON / "services" / "setup_reference.py").read_text(encoding="utf-8")
    for forbidden in (".set(", ".create(", ".update(", ".delete(", ".write(", ".open("):
        assert forbidden not in text, f"setup_reference must not call {forbidden}"


def test_no_surface_renders_a_reference_without_its_label() -> None:
    """Every rendering of the block goes through ``SetupReference.caption``.

    The caption is the smallest place a measured number can quietly become advice, so it is not
    left to a caller: a card that printed the quantiles under a heading of its own choosing would
    be publishing a target with a measurement's authority.

    The rule is narrow on purpose -- it applies to modules that WORK with the block, found by
    their import of it, rather than to everything mentioning ``mfe``. ``assessment_service``
    reads ``result.mfe`` off a horizon and is not rendering anything.
    """
    offenders: list[str] = []
    for path in _python_files("discord", "reviews", "services", "engine"):
        if path.name == "setup_reference.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "SetupReference" not in text and "setup_reference" not in text:
            continue
        if ".mfe" not in text and ".mae" not in text:
            continue
        if "caption" not in text and "render_reference" not in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "these modules read a reference's quantiles without its caption:\n"
        + "\n".join(offenders)
    )
