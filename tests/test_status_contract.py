"""Every status the pipeline can write must be one the database will accept.

This is the ``stale`` incident, generalised.

``mirror_pipeline.STATUS_STALE`` is produced whenever a signal is older than the
age limit, but ``mirror_events.status`` carries a CHECK constraint that did not
list it. Postgres answered 400, the row was never written, the message was never
claimed, and ``gap_fill`` held the cursor below it and retried every poll
forever. The signal did not appear as "stale" - it did not appear at all.

Nothing caught it, because the constraint exists only in the Supabase schema.
The local SQLite ledger accepts any string, so the entire suite passed while
production dropped every stale event.

These tests compare the code against the schema. Adding a new ``STATUS_*``
constant without a migration now fails here, in one second, instead of becoming
a silent hole in production.
"""
from __future__ import annotations

import re
from pathlib import Path

from rade.observer import mirror_pipeline as mp

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"

ADD_CONSTRAINT = re.compile(
    r"add constraint mirror_events_status_valid\s+check\s*\((.*?)\);",
    re.S | re.I)


def code_statuses() -> set[str]:
    """Every value a ``STATUS_*`` constant in the pipeline can hold."""
    return {value for name, value in vars(mp).items()
            if name.startswith("STATUS_") and isinstance(value, str)}


def stored_statuses() -> set[str]:
    """The statuses the current CHECK constraint accepts.

    Later migrations redefine the constraint, so the highest-numbered one that
    adds it wins. Reading 0001 alone would test a schema that may no longer
    exist - 0006 exists precisely because 0001 was out of date.
    """
    current: set[str] | None = None
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        if "mirror_events_status_valid" not in text:
            continue
        block = ADD_CONSTRAINT.search(text)
        if block:
            current = set(re.findall(r"'([a-z_]+)'", block.group(1)))
    assert current is not None, "no migration defines mirror_events_status_valid"
    return current


def test_every_status_the_pipeline_writes_can_be_stored():
    """The direction that broke production: a status Postgres refuses."""
    missing = code_statuses() - stored_statuses()
    assert not missing, (
        f"the pipeline can write {sorted(missing)}, but the mirror_events CHECK "
        "constraint rejects them. Postgres returns 400, the row is never "
        "written, the cursor wedges below the message and retries forever - "
        "the signal does not appear as anything, it does not appear. Add a "
        "migration, as 0006 did for 'stale'.")


def test_the_constraint_does_not_describe_a_different_program():
    """The other direction: a status the schema allows that nothing produces."""
    extra = stored_statuses() - code_statuses()
    assert not extra, (
        f"the schema accepts {sorted(extra)}, which no STATUS_* constant "
        "produces. The constraint has drifted from the code, so it no longer "
        "documents what the table can contain.")


def test_the_parser_reads_a_real_constraint():
    """A guard that parses nothing passes every test above."""
    stored = stored_statuses()
    assert len(stored) >= 5, f"only parsed {stored} - the parser is broken"
    assert "paper_intent" in stored
    assert "stale" in stored
