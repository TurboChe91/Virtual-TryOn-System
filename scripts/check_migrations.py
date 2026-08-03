#!/usr/bin/env python3
"""Verify migrations apply cleanly, are idempotent, and leave a sound schema.

The test suite only ever builds a database from scratch, so it cannot catch a
migration that breaks an EXISTING database — which is the only kind that matters
in production. This walks the versions forward one at a time with data present at
each step, the way a real upgrade does.

Run: python scripts/check_migrations.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lunelle.db import Database, _migration_files, migrate  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {label}{f' — {detail}' if detail else ''}")
    if not condition:
        FAILURES.append(label)


def seed_row(conn: sqlite3.Connection, suffix: str) -> None:
    """Insert a style + task so later migrations have data to carry forward."""
    from lunelle.db import transaction, utcnow

    now = utcnow()
    with transaction(conn):
        conn.execute(
            "INSERT INTO styles (style_id, sku, name, spec_json, source_type,"
            " source_input_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"st_{suffix}", f"sku-{suffix}", f"Style {suffix}", "{}", "structured",
             "{}", now, now),
        )
        conn.execute(
            "INSERT INTO tasks (task_id, style_id, sku, output_type, prompt,"
            " prompt_version, provider, model, status, estimated_cost_usd,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"tk_{suffix}", f"st_{suffix}", f"sku-{suffix}", "grid", "p", "v",
             "mock", "m", "success", 0.05, now, now),
        )


def main() -> int:
    versions = [number for number, _, _ in _migration_files()]
    print(f"migrations found: {versions}")
    if not versions:
        print("no migration files discovered")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        # --- 1. Stepwise upgrade with data present at every step -------------
        print("\n1. stepwise upgrade with data at each version")
        stepwise = Path(tmp) / "stepwise.db"
        database = Database(stepwise)
        conn = database.conn()
        applied_total: list[str] = []
        for index, number in enumerate(versions):
            applied = migrate(conn, target=number)
            applied_total.extend(applied)
            check(f"applied up to {number:04d}", bool(applied) or index == 0,
                  f"{len(applied)} file(s)")
            if number == 1:
                seed_row(conn, "one")
            else:
                seed_row(conn, f"v{number}")
        rows = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        check("data survived every migration", rows == len(versions), f"{rows} tasks")

        # --- 2. Idempotency ---------------------------------------------------
        print("\n2. re-running migrate is a no-op")
        again = migrate(conn)
        check("second run applies nothing", again == [], str(again))

        # --- 3. Integrity and foreign keys ------------------------------------
        print("\n3. schema soundness")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        check("integrity_check", integrity == "ok", integrity)
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        check("no foreign-key violations", not fk_violations,
              str([tuple(r) for r in fk_violations][:3]))

        # --- 4. Columns and tables the code depends on ------------------------
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        for column in ("qa_state", "review_state", "reviewed_at", "root_task_id"):
            check(f"tasks.{column} present", column in columns)
        tables = {
            row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for table in ("asset_reviews", "generation_lineages", "spend_reservations",
                      "qa_results", "attempts", "api_profiles", "app_settings"):
            check(f"table {table} present", table in tables)

        # --- 5. CHECK constraints are live -----------------------------------
        print("\n4. CHECK constraints enforced after migration")
        try:
            conn.execute("UPDATE tasks SET qa_state = 'bogus' WHERE task_id = 'tk_one'")
            check("qa_state CHECK enforced", False, "accepted an invalid value")
        except sqlite3.IntegrityError:
            check("qa_state CHECK enforced", True)
        try:
            conn.execute(
                "UPDATE tasks SET review_state = 'bogus' WHERE task_id = 'tk_one'")
            check("review_state CHECK enforced", False, "accepted an invalid value")
        except sqlite3.IntegrityError:
            check("review_state CHECK enforced", True)
        database.close_all()

        # --- 6. Fresh database reaches the same schema ------------------------
        print("\n5. fresh database matches the upgraded one")
        fresh_path = Path(tmp) / "fresh.db"
        fresh = Database(fresh_path)
        migrate(fresh.conn())

        def schema_of(connection: sqlite3.Connection) -> set[str]:
            return {
                f"{row['type']}:{row['name']}"
                for row in connection.execute(
                    "SELECT type, name FROM sqlite_master WHERE name NOT LIKE"
                    " 'sqlite_%'")
            }

        upgraded_db = Database(stepwise)
        difference = schema_of(upgraded_db.conn()) ^ schema_of(fresh.conn())
        check("upgraded and fresh schemas identical", not difference,
              str(sorted(difference)[:5]))
        fresh.close_all()
        upgraded_db.close_all()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("all migration checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
