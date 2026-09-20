"""One vocabulary for "did this pass?", shared by every operator tool (P-2, P-3).

Preflight asks it before a session; session verification asks it afterwards; the demo
drills ask it of each drill. They are different questions with the same shape, and
giving them one set of statuses means an operator learns the table once and that a
verdict means the same thing in all three places.

## Five statuses, and why two would be a lie

* **PASS** -- verified, now, against the real thing.
* **FAIL** -- verified to be wrong. The only status that makes an exit code non-zero.
* **WARN** -- true, but probably not what was wanted. Proceed knowingly.
* **SKIP** -- **not checked**. Never a pass. A dependency failed, or the market was
  closed, or a flag turned it off. A skipped check rendered like a passing one is how a
  tool ends up certifying something it never looked at, which is worse than having no
  tool: it converts an unknown into a false assurance.
* **INFO** -- a value that must be in front of a human, with no pass/fail meaning.
  ``trading_enabled`` is the example: whether it should be on depends on which session
  this is, so the tool refuses to have an opinion and refuses to let it go unseen.

``CheckReport.summary`` names the skipped count in the verdict line for the same reason.
"Four passed" under a table of eleven rows reads as a clean run to anyone scanning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"
    INFO = "INFO"


#: Rendered marks. Words rather than symbols: this output is pasted into evidence files
#: and read in terminals with no colour, where a tick and a cross are one glyph apart.
MARKS: dict[Status, str] = {
    Status.PASS: "PASS",
    Status.FAIL: "FAIL",
    Status.WARN: "WARN",
    Status.SKIP: "SKIP",
    Status.INFO: "INFO",
}


@dataclass(frozen=True)
class CheckResult:
    """One check's verdict, with what to do about it when it is not a pass."""

    name: str
    status: Status
    detail: str
    remedy: str | None = None

    @property
    def blocking(self) -> bool:
        return self.status is Status.FAIL


@dataclass
class CheckReport:
    """A list of results, a verdict and an exit code."""

    results: list[CheckResult] = field(default_factory=list)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.blocking]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is Status.WARN]

    @property
    def skipped(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is Status.SKIP]

    @property
    def ok(self) -> bool:
        """No FAIL. Deliberately silent about SKIP -- see ``summary``."""
        return not self.failures

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0

    def get(self, name: str) -> CheckResult | None:
        return next((r for r in self.results if r.name == name), None)

    def render(self, *, ready: str = "READY", not_ready: str = "NOT READY") -> str:
        width = max((len(r.name) for r in self.results), default=10)
        lines = [
            f"{'status':<7}{'check':<{width + 2}}detail",
            "-" * (7 + width + 2 + 40),
        ]
        for result in self.results:
            lines.append(
                f"{MARKS[result.status]:<7}{result.name:<{width + 2}}{result.detail}"
            )

        remedies = [r for r in self.results if r.remedy]
        if remedies:
            lines += ["", "What to do:"]
            for result in remedies:
                lines.append(f"  {result.name}: {result.remedy}")

        lines += ["", self.summary(ready=ready, not_ready=not_ready)]
        return "\n".join(lines)

    def markdown(self) -> list[str]:
        """The same table for an evidence document."""
        lines = ["| status | check | detail |", "|---|---|---|"]
        for result in self.results:
            # Pipes inside a detail would break the row; a detail is free text from a
            # broker error message, so it cannot be trusted not to contain one.
            detail = result.detail.replace("|", "\\|")
            lines.append(f"| {MARKS[result.status]} | `{result.name}` | {detail} |")
        return lines

    def summary(self, *, ready: str = "READY", not_ready: str = "NOT READY") -> str:
        counts = {
            status: sum(1 for r in self.results if r.status is status)
            for status in Status
        }
        parts = [f"{counts[s]} {s.value.lower()}" for s in Status if counts[s]]
        verdict = ready if self.ok else not_ready
        if self.skipped:
            verdict += f" ({len(self.skipped)} checks not run)"
        return f"{verdict} — " + ", ".join(parts)
