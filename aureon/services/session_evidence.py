"""The record one session leaves behind (P-3).

A session is a one-off experiment on a market that will never be in that state again, so
what survives it is a document. ``docs/evidence/session_{market_date}.md`` is that
document, and it is written by two tools at two moments:

* ``scripts/session_run.py``, **before** the observer starts, records what was true going
  in -- the preflight table, the commit, the terminal build, the collection prefix.
* ``scripts/session_verify.py``, **after** the close, replaces the second half with what
  actually happened: the live-vs-replay comparison, the stored counts, the outcomes.

## Why the halves are written at different times and never merged

Because the first half must survive a session that goes wrong. If the document were
written once at the end, a crashed observer, a lost terminal or an operator who gave up
would leave no record at all -- and "we tried on the 16th and something went wrong" is
the single most useless sentence in a research log. Writing the opening half before
anything starts means the failure itself is evidence, filed under the right date, with
the commit and the configuration that produced it.

So ``scripts/session_verify.py`` **preserves everything above the marker verbatim** and
only replaces what is below it. It can be re-run any number of times; it cannot
retroactively improve the before.

## It is generated, and it is committed

Generated, so nothing in it is a claim someone typed from memory. Committed, because the
whole point is to be readable a month later next to the commit it describes -- and
because ``docs/PHASES.md`` links to it as the evidence for a phase gate (P-5).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from aureon.services.checks import CheckReport

#: Where session evidence lives. Relative, so it resolves against the repository the
#: tool is run from rather than one baked in at import time.
DEFAULT_EVIDENCE_DIR = Path("docs") / "evidence"

#: The boundary between "what was true going in" and "what happened". Everything above
#: it is written once and never rewritten; see the module docstring.
AFTER_MARKER = "## After the close"

NOT_YET_VERIFIED = "_Not verified yet._"

UNKNOWN = "—"


def evidence_path(
    market_date: str, symbol: str | None = None, *, root: Path | None = None
) -> Path:
    """``docs/evidence/session_{market_date}_{symbol}.md``, on the BROKER date.

    Broker date, not UTC: a New York session's last three hours are already the next day
    in Athens, so a UTC-named file would split one session across two and neither would
    be the session anybody ran.

    Per symbol (9A), because one session of one deployment now produces one record **per
    instrument**: the checks inside are per symbol (its detections, its archive, its
    live-vs-replay comparison, its rule), so two symbols sharing a file would mean the second
    verification silently replacing the first's verdict, under a header naming one of them.

    ``symbol`` is optional only so the helper can still address a pre-9A file by name.
    """
    stem = f"session_{market_date}"
    if symbol:
        stem = f"{stem}_{symbol.upper()}"
    return (root or DEFAULT_EVIDENCE_DIR) / f"{stem}.md"


#: The line ``session_verify.py`` writes when every check passed. Matched, rather than a
#: field parsed out of the file, because it is the same string the PHASES evidence column
#: greps for: one marker, one meaning.
VERIFIED_MARKER = "SESSION VERIFIED"


def verified_market_dates(
    symbol: str, *, root: Path | None = None
) -> tuple[str, ...]:
    """Every broker date this symbol has a VERIFIED session for (11C, F-9).

    This is the system's answer to "which days really happened". It reads the evidence
    directory rather than Firestore, and that is the point: a replay run writes detections
    into the same collection a live session does, so nothing in Firestore can distinguish
    a day the broker served from a day a fixture generated. The evidence file can, because
    it is written by ``session_verify.py`` after six checks against a real terminal and
    carries the commit it was verified at.

    A file that exists without the marker does not count. One is written when a session
    STARTS, so "there is a file" and "the session was verified" are different claims and
    only the second one is evidence.
    """
    directory = root or DEFAULT_EVIDENCE_DIR
    if not directory.is_dir():
        return ()
    wanted = symbol.upper()
    found: set[str] = set()
    for path in directory.glob(f"session_*_{wanted}.md"):
        if VERIFIED_MARKER not in path.read_text(encoding="utf-8"):
            continue
        # ``session_{market_date}_{symbol}.md``
        stem = path.stem[len("session_") : -(len(wanted) + 1)]
        if stem:
            found.add(stem)
    return tuple(sorted(found))


def git_commit(*, cwd: Path | None = None) -> str | None:
    """The commit the tool is running at, or ``None`` if that cannot be determined.

    It is the most important line in the document. A comparison is evidence about one
    build; without the hash it is an anecdote about an unknown one. ``None`` rather than
    a guess or a crash: a missing hash is a gap a reader can see, and a wrong one is not.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    commit = result.stdout.strip()
    return commit or None


def git_dirty(*, cwd: Path | None = None) -> bool | None:
    """Whether the working tree has uncommitted changes. ``None`` if unknown.

    Recorded because a session run from a dirty tree is evidence about code that exists
    nowhere else. The hash alone would claim otherwise.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


@dataclass(frozen=True)
class Block:
    """One titled chunk of evidence: a command's output, a table, a note.

    ``command`` is rendered above the body. It is what makes the block reproducible: a
    wall of numbers with no record of what produced them is not evidence, because nobody
    can run it again.
    """

    title: str
    body: str
    command: str | None = None
    #: True when the body is already markdown (a table) rather than terminal output.
    markdown: bool = False

    def render(self) -> list[str]:
        lines = [f"### {self.title}", ""]
        if self.command:
            lines += [f"    $ {self.command}", ""]
        if self.markdown:
            lines += [self.body.rstrip(), ""]
        else:
            lines += ["```", self.body.rstrip() or "(no output)", "```", ""]
        return lines


@dataclass
class SessionMeta:
    """What identifies a session, and what nobody can reconstruct afterwards."""

    market_date: str
    symbol: str
    timeframe: str
    market_tz: str
    account_scope: str
    collection_prefix: str
    evaluation_rule_id: str
    ema_fast: int
    ema_slow: int
    commit: str | None = None
    dirty: bool | None = None
    terminal: str | None = None
    server: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None

    def rows(self) -> list[tuple[str, str]]:
        def stamp(moment: datetime | None) -> str:
            return moment.isoformat() if moment else UNKNOWN

        commit = f"`{self.commit}`" if self.commit else UNKNOWN
        if self.dirty:
            commit += " **+ uncommitted changes**"
        elif self.dirty is None:
            commit += " (tree state unknown)"
        return [
            ("market date", f"`{self.market_date}`"),
            ("symbol / timeframe", f"{self.symbol} {self.timeframe}"),
            ("market timezone", f"`{self.market_tz}`"),
            ("account scope", f"`{self.account_scope}`"),
            ("collection prefix", f"`{self.collection_prefix}`"),
            ("evaluation rule", f"`{self.evaluation_rule_id}`"),
            ("EMA pair", f"{self.ema_fast} / {self.ema_slow}"),
            ("commit", commit),
            ("terminal", self.terminal or UNKNOWN),
            ("broker server", self.server or UNKNOWN),
            ("observer started", stamp(self.started_at)),
            ("observer stopped", stamp(self.ended_at)),
        ]


@dataclass
class SessionDocument:
    """The opening half of the evidence file: what was true going in."""

    meta: SessionMeta
    blocks: list[Block] = field(default_factory=list)
    note: str | None = None

    def render(self) -> str:
        meta = self.meta
        lines = [
            f"# Session evidence — {meta.symbol} {meta.timeframe} {meta.market_date}",
            "",
            "**Generated.** The half above `## After the close` is written by",
            "`scripts/session_run.py` before the observer starts and is never rewritten;",
            "the half below is written by `scripts/session_verify.py` after the close and",
            "may be regenerated. Do not edit either by hand — a hand-edited evidence file",
            "is indistinguishable from a measured one.",
            "",
            "| property | value |",
            "|---|---|",
        ]
        lines += [f"| {name} | {value} |" for name, value in meta.rows()]
        lines += ["", "## Before the open", ""]
        if self.note:
            lines += [self.note, ""]
        for block in self.blocks:
            lines += block.render()
        lines += [AFTER_MARKER, "", NOT_YET_VERIFIED, ""]
        return "\n".join(lines).rstrip() + "\n"

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render(), encoding="utf-8")
        return path


def render_closing(
    report: CheckReport,
    blocks: list[Block],
    *,
    verified_at: datetime | None = None,
) -> str:
    """The second half: the verdict table, then the outputs behind it."""
    lines = [
        "",
        report.summary(ready="SESSION VERIFIED", not_ready="SESSION NOT VERIFIED"),
        "",
    ]
    if verified_at is not None:
        lines += [f"Verified at `{verified_at.isoformat()}`.", ""]
    lines += report.markdown()
    lines.append("")
    remedies = [r for r in report.results if r.remedy]
    if remedies:
        lines += ["**What to look at:**", ""]
        lines += [f"- `{r.name}`: {r.remedy}" for r in remedies]
        lines.append("")
    for block in blocks:
        lines += block.render()
    return "\n".join(lines).rstrip() + "\n"


def split_at_marker(existing: str) -> tuple[str, str]:
    """``(everything above the marker line, everything from it down)``.

    Split on a **whole line**, not on the substring. The document's own header explains
    what the marker separates and therefore contains its text, so a substring split finds
    that sentence first and silently truncates the file at it -- which is how a
    verification would appear to preserve the opening half while deleting most of it.
    """
    lines = existing.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == AFTER_MARKER:
            return "\n".join(lines[:index]), "\n".join(lines[index:])
    raise ValueError(
        f"the evidence file has no {AFTER_MARKER!r} line; it was not written by "
        "scripts/session_run.py"
    )


def replace_after(existing: str, closing: str) -> str:
    """Swap the second half, keeping every line above the marker byte for byte.

    A session_verify re-run must not be able to improve the record of what was true
    before the session, so the split is textual and the opening half is never re-rendered.
    """
    head, _tail = split_at_marker(existing)
    return head + "\n" + AFTER_MARKER + "\n" + closing.rstrip() + "\n"


def orphan_document(meta: SessionMeta) -> SessionDocument:
    """An evidence file for a session that was never recorded going in.

    Verification can run against an archive whose session nobody bracketed with
    ``session_run.py``. The result is worth keeping, and it is NOT the same artefact:
    nothing here attests to the preflight, and the commit is the one verification ran at
    rather than the one the observer ran at. Said in the document rather than inferred
    from an absence.
    """
    return SessionDocument(
        meta=meta,
        note=(
            "**No pre-session record.** `scripts/session_run.py` did not write this "
            "file, so nothing here attests to what was true before the open: no "
            "preflight table, and the commit below is the one **verification** ran at, "
            "which is not necessarily the one the observer ran at."
        ),
    )
