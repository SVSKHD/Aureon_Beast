"""Every audit row is written inside the transaction it describes (§71).

Structural, not behavioural, and deliberately so. A behavioural test needs a failure that
happens AFTER the audit write and before the commit, and no caller has one -- every guard in
the money path runs before the audit. So a plant that moved the audit write into its own
transaction survived every test in this package: the row still appeared, just not atomically.

What it would cost in production is the log's whole value. An audit row committed separately
can survive a rollback, and then the log says a thing happened that did not -- which is worse
than no log, because the log is what an investigation trusts.

The rule is therefore checked where it is written: every ``audit.append`` call in a money
repository must pass ``connection=``. ``AuditRepository.append`` itself may not, because its
own signature is what offers the parameter (decision 354).
"""

from __future__ import annotations

import ast
import inspect

import pytest

from aureon.storage.postgres.repositories import (
    control_requests,
    setups,
    trade_requests,
    trades,
)

#: The modules whose writes are audited. Named as a literal for the reason ``OBSERVER_SIDE``
#: is: a module dropped from this list is a rule switched off with every test still green.
AUDITED = (trade_requests, control_requests, trades)


def test_every_audited_module_is_named_in_the_list() -> None:
    """The list IS the check, so it is pinned.

    ``setups`` is deliberately absent and imported only to be asserted about: its transaction
    writes an event, which is its own history, so it has no audit call to constrain. If it
    grows one, this test fails and the module has to be added on purpose.
    """
    assert [module.__name__.rsplit(".", 1)[-1] for module in AUDITED] == [
        "trade_requests",
        "control_requests",
        "trades",
    ]
    assert "audit" not in inspect.getsource(setups), (
        "setups.py grew an audit call; add it to AUDITED and give it a connection"
    )


@pytest.mark.parametrize("module", AUDITED, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_every_audit_append_passes_the_connection(module) -> None:
    """Otherwise the row commits in a transaction of its own."""
    tree = ast.parse(inspect.getsource(module))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "append":
            continue
        # ``self.audit.append(...)`` -- the only append this rule is about.
        if not (isinstance(func.value, ast.Attribute) and func.value.attr == "audit"):
            continue
        if not any(keyword.arg == "connection" for keyword in node.keywords):
            offenders.append(f"{module.__name__}:{node.lineno}")
    assert not offenders, (
        "an audit row would be written outside the transaction it describes:\n"
        + "\n".join(offenders)
    )


def test_an_audit_row_written_in_a_transaction_dies_with_it(schema) -> None:
    """The mechanism the structural rule relies on, asserted once.

    If ``connection=`` were ignored -- if ``append`` opened its own transaction regardless --
    the rule above would be checking a parameter that does nothing.
    """
    from aureon.models.audit import AuditRecord
    from aureon.storage.postgres.repositories.audit import AuditRepository

    audit = AuditRepository(schema)
    record = AuditRecord(audit_id="doomed", actor="trader", action="trade_request.create")

    class Refused(RuntimeError):
        pass

    with pytest.raises(Refused):
        with schema.transaction() as connection:
            audit.append(record, connection=connection)
            raise Refused("the guard said no")

    assert audit.get("doomed") is None, "the audit row outlived the rolled-back transaction"
