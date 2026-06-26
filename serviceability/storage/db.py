"""Provider-agnostic result store.

SQLite for the POC because it needs zero setup and the schema is the same one a
Postgres deployment uses later. Every result is appended with its own timestamp,
so history is the table itself. We never overwrite; a new run is new rows.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

from ..models import CSV_COLUMNS, CheckResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address_key TEXT NOT NULL,
    address_line1 TEXT,
    unit TEXT,
    city TEXT,
    state TEXT,
    zip TEXT,
    provider TEXT NOT NULL,
    category TEXT NOT NULL,
    fiber_speed TEXT,
    technology TEXT,
    matched_address TEXT,
    raw_status TEXT,
    notes TEXT,
    final_url TEXT,
    checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_lookup
    ON results (address_key, provider, checked_at);
"""


class ResultStore:
    def __init__(self, db_path: str = "data/serviceability.db"):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # Add columns introduced after a database was first created.
            try:
                conn.execute("ALTER TABLE results ADD COLUMN final_url TEXT")
            except sqlite3.OperationalError:
                pass  # column already present

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def save(self, results: list[CheckResult]) -> int:
        rows = [r.to_row() for r in results]
        placeholders = ", ".join("?" for _ in CSV_COLUMNS)
        columns = ", ".join(CSV_COLUMNS)
        with self._conn() as conn:
            conn.executemany(
                f"INSERT INTO results ({columns}) VALUES ({placeholders})",
                [tuple(row[c] for c in CSV_COLUMNS) for row in rows],
            )
        return len(rows)

    def latest_per_address(self, provider: str, before: str | None = None) -> dict[str, dict]:
        """Most recent result per address for one provider.

        before, an ISO timestamp, restricts to results strictly older than it,
        which is how the comparison engine asks for "the previous run."
        """
        clause = "WHERE provider = ?"
        params: list = [provider]
        if before is not None:
            clause += " AND checked_at < ?"
            params.append(before)
        query = f"""
            SELECT * FROM results
            {clause}
            ORDER BY address_key, checked_at DESC
        """
        latest: dict[str, dict] = {}
        with self._conn() as conn:
            for row in conn.execute(query, params):
                key = row["address_key"]
                if key not in latest:
                    latest[key] = dict(row)
        return latest

    def all_rows(self, provider: str | None = None) -> list[dict]:
        query = "SELECT * FROM results"
        params: list = []
        if provider:
            query += " WHERE provider = ?"
            params.append(provider)
        query += " ORDER BY checked_at"
        with self._conn() as conn:
            return [dict(row) for row in conn.execute(query, params)]
