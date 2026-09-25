"""A test-only helper that counts real SQL statements issued against a
LocalPersistence instance while a page-render function (or any callable)
runs -- built for the P3/P4 perf gap review 2026-09-25 cold-load latency
pass (CLAUDE.md "Count statements per page without a workspace").

Uses sqlite3.Connection.set_trace_callback, the stdlib's own hook for this
exact purpose -- it fires once per statement actually executed on that
connection, including every statement inside an `executemany`, so it
counts the real round-trip cost a live Delta warehouse would also pay one
for one (CLAUDE.md: "Each Delta statement costs ~0.3-1.2s warm on the
serverless warehouse, so statement count ~= latency"). LocalPersistence's
own `_open()` is wrapped so every connection it ever opens (its shared
`:memory:` connection, or a fresh per-call connection in file-backed mode)
is traced -- the same counter works for both modes."""

from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def count_statements(persistence):
    """`with count_statements(persistence) as counter: ...; counter.count`
    -- `counter.statements` holds the raw SQL text of every statement
    issued through this persistence instance for the duration of the
    `with` block."""
    statements: list[str] = []
    original_open = persistence._open

    def _traced_open():
        conn = original_open()
        conn.set_trace_callback(lambda sql: statements.append(sql))
        return conn

    persistence._open = _traced_open
    # A `:memory:` LocalPersistence opens its one shared connection in
    # __init__, before this context manager ever runs -- trace it directly
    # too, so a caller using an in-memory db is still counted correctly.
    if getattr(persistence, "_is_memory", False) and persistence._conn is not None:
        persistence._conn.set_trace_callback(lambda sql: statements.append(sql))
    try:
        yield _Counter(statements)
    finally:
        persistence._open = original_open
        if getattr(persistence, "_is_memory", False) and persistence._conn is not None:
            persistence._conn.set_trace_callback(None)


class _Counter:
    def __init__(self, statements: list[str]):
        self.statements = statements

    @property
    def count(self) -> int:
        return len(self.statements)
