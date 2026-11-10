"""Compare a database's schema with what a migration revision should produce.

Used before stamping an existing database (production was built by the old startup code,
not by Alembic), and by the tests that prove the baseline migration matches that code.

    python -m app.schema_check                         # DATABASE_URL vs 0001_baseline
    python -m app.schema_check --url <url> --expect 0001_baseline
    python -m app.schema_check --dump                  # print the live snapshot as JSON

Exit code 0 means identical, 1 means differences (listed), 2 means a usage error.
The snapshot reads only the Postgres catalog; it never changes the database. Column order
is ignored (a column added later with ALTER sits at the end), and so is the Alembic
bookkeeping table.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from app.database import sqlalchemy_url

SNAPSHOTS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "snapshots"
IGNORED_TABLES = {"alembic_version"}

_COLUMNS_SQL = text(
    """
    SELECT c.relname AS table_name, a.attname AS column_name,
           format_type(a.atttypid, a.atttypmod) AS data_type,
           a.attnotnull AS not_null,
           CASE WHEN a.attgenerated = '' THEN pg_get_expr(d.adbin, d.adrelid) END AS default,
           CASE WHEN a.attgenerated <> '' THEN pg_get_expr(d.adbin, d.adrelid) END AS generated
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE n.nspname = 'public' AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
    """
)

_CONSTRAINTS_SQL = text(
    """
    SELECT c.relname AS table_name, con.conname AS name, pg_get_constraintdef(con.oid) AS definition
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
    """
)

_INDEXES_SQL = text(
    "SELECT tablename AS table_name, indexname AS name, indexdef AS definition "
    "FROM pg_indexes WHERE schemaname = 'public'"
)

_SEQUENCES_SQL = text(
    """
    SELECT s.relname AS name, format_type(seq.seqtypid, NULL) AS data_type,
           t.relname AS owned_by_table, a.attname AS owned_by_column
    FROM pg_class s
    JOIN pg_namespace n ON n.oid = s.relnamespace
    JOIN pg_sequence seq ON seq.seqrelid = s.oid
    LEFT JOIN pg_depend dep ON dep.objid = s.oid AND dep.deptype = 'a'
    LEFT JOIN pg_class t ON t.oid = dep.refobjid
    LEFT JOIN pg_attribute a ON a.attrelid = dep.refobjid AND a.attnum = dep.refobjsubid
    WHERE n.nspname = 'public' AND s.relkind = 'S'
    """
)

_EXTENSIONS_SQL = text("SELECT extname FROM pg_extension WHERE extname <> 'plpgsql'")


def snapshot(connection: Connection) -> dict[str, Any]:
    """The schema of the `public` schema as plain, sorted data (JSON-friendly)."""
    tables: dict[str, dict[str, dict]] = {}

    def table(name: str) -> dict[str, dict]:
        return tables.setdefault(name, {"columns": {}, "constraints": {}, "indexes": {}})

    for row in connection.execute(_COLUMNS_SQL).mappings():
        if row["table_name"] in IGNORED_TABLES:
            continue
        table(row["table_name"])["columns"][row["column_name"]] = {
            "type": row["data_type"],
            "not_null": row["not_null"],
            "default": row["default"],
            "generated": row["generated"],
        }
    for row in connection.execute(_CONSTRAINTS_SQL).mappings():
        if row["table_name"] not in IGNORED_TABLES:
            table(row["table_name"])["constraints"][row["name"]] = row["definition"]
    for row in connection.execute(_INDEXES_SQL).mappings():
        if row["table_name"] not in IGNORED_TABLES:
            table(row["table_name"])["indexes"][row["name"]] = row["definition"]

    sequences = {
        row["name"]: {
            "type": row["data_type"],
            "owned_by": f"{row['owned_by_table']}.{row['owned_by_column']}"
            if row["owned_by_table"] else None,
        }
        for row in connection.execute(_SEQUENCES_SQL).mappings()
    }
    extensions = sorted(connection.execute(_EXTENSIONS_SQL).scalars())
    return json.loads(json.dumps(
        {"extensions": extensions, "sequences": sequences, "tables": tables}, sort_keys=True
    ))


def _flatten(value: Any, path: str = "") -> dict[str, Any]:
    # A column or sequence ({"type": ..., ...}) is compared attribute by attribute, but a
    # whole missing or unexpected one is reported once (see differences()).
    if isinstance(value, dict):
        flat: dict[str, Any] = {}
        for key, item in value.items():
            flat.update(_flatten(item, f"{path}.{key}" if path else key))
        return flat
    return {path: value}


def differences(expected: dict, actual: dict) -> list[str]:
    """Human-readable differences between two snapshots; empty when identical."""
    want, have = _flatten(expected), _flatten(actual)
    lines = []
    # Columns and sequences present on one side only: one line each, not one per attribute.
    whole = {
        key.rsplit(".", 1)[0]
        for side, other in ((want, have), (have, want))
        for key in side
        if key.endswith(".type") and key not in other
    }
    for prefix in sorted(whole):
        attributes = {
            key.rsplit(".", 1)[1]: (want.get(key), have.get(key))
            for key in want.keys() | have.keys()
            if key.rsplit(".", 1)[0] == prefix
        }
        if f"{prefix}.type" in want:
            lines.append(f"missing:    {prefix} ({_describe(attributes, 0)})")
        else:
            lines.append(f"unexpected: {prefix} ({_describe(attributes, 1)})")
        for key in list(want):
            if key.rsplit(".", 1)[0] == prefix:
                want.pop(key)
        for key in list(have):
            if key.rsplit(".", 1)[0] == prefix:
                have.pop(key)
    for key in sorted(want.keys() | have.keys()):
        if key not in have:
            lines.append(f"missing:    {key} = {want[key]!r}")
        elif key not in want:
            lines.append(f"unexpected: {key} = {have[key]!r}")
        elif want[key] != have[key]:
            lines.append(f"different:  {key}: expected {want[key]!r}, found {have[key]!r}")
    return lines


def _describe(attributes: dict[str, tuple], side: int) -> str:
    return ", ".join(
        f"{name}={values[side]!r}" for name, values in sorted(attributes.items())
        if values[side] is not None
    )


def expected_snapshot(revision: str) -> dict:
    path = SNAPSHOTS_DIR / f"{revision}.json"
    if not path.exists():
        raise SystemExit(f"No reference snapshot for revision {revision!r} ({path}).")
    return json.loads(path.read_text(encoding="utf-8"))


def snapshot_of(url: str) -> dict:
    engine = create_engine(sqlalchemy_url(url), connect_args={"prepare_threshold": None})
    try:
        with engine.connect() as connection:
            return snapshot(connection)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", help="database URL (default: DATABASE_URL)")
    parser.add_argument("--expect", default="0001_baseline",
                        help="revision whose reference snapshot to compare with")
    parser.add_argument("--dump", action="store_true", help="print the live snapshot as JSON")
    args = parser.parse_args(argv)

    url = (args.url or os.getenv("DATABASE_URL", "")).strip()
    if not url:
        print("Set DATABASE_URL or pass --url.", file=sys.stderr)
        return 2
    actual = snapshot_of(url)
    if args.dump:
        print(json.dumps(actual, indent=2, sort_keys=True))
        return 0

    found = differences(expected_snapshot(args.expect), actual)
    if found:
        print(f"The schema differs from {args.expect} ({len(found)} differences):")
        for line in found:
            print("  " + line)
        return 1
    print(f"The schema matches {args.expect}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
