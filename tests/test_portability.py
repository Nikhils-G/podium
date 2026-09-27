"""The Postgres path: the aggregate queries compile for PostgreSQL, and no SQLite-only SQL
function (iif) or dialect-specific cast creeps back into the services."""

import re
from pathlib import Path

from sqlalchemy.dialects import postgresql

from podium.services import judges, voting

SRC = Path(__file__).resolve().parents[1] / "src" / "podium"


def _pg_sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_tally_compiles_for_postgres():
    sql = _pg_sql(voting._tally_statement(1))
    assert "CASE WHEN" in sql
    assert "iif" not in sql.lower()


def test_assignment_counts_compile_for_postgres():
    sql = _pg_sql(judges._assignment_counts_statement(1))
    assert "CASE WHEN" in sql
    assert "iif" not in sql.lower()


def test_no_sqlite_only_functions_in_source():
    banned = re.compile(r"func\.iif\b|func\.cast\(")
    offenders = [
        f"{path.relative_to(SRC)}:{n}"
        for path in SRC.rglob("*.py")
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if banned.search(line)
    ]
    assert offenders == []
