"""Tests for the read-only SQLite tools."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from evi.tools.base import REGISTRY
import evi.tools.sqlite  # noqa: F401  register tools


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = tmp_path / "test.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT
            );
            CREATE TABLE notes (
                id INTEGER PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                body TEXT
            );
            INSERT INTO users (id, name, email) VALUES
                (1, 'Alice', 'a@example.com'),
                (2, 'Bob',   'b@example.com');
            INSERT INTO notes (user_id, body) VALUES
                (1, 'hello world'),
                (2, 'how are you');
            """
        )
        conn.commit()
    finally:
        conn.close()
    return str(path)


def test_schema_lists_tables(db: str) -> None:
    out = json.loads(REGISTRY["sqlite_schema"].call(json.dumps({"path": db})))
    table_names = [t["table"] for t in out]
    assert "users" in table_names
    assert "notes" in table_names
    users = next(t for t in out if t["table"] == "users")
    col_names = [c["name"] for c in users["columns"]]
    assert col_names == ["id", "name", "email"]


def test_select_returns_rows(db: str) -> None:
    out = REGISTRY["sqlite_query"].call(json.dumps({
        "path": db, "sql": "SELECT name FROM users ORDER BY id"
    }))
    rows = json.loads(out)
    assert rows == [{"name": "Alice"}, {"name": "Bob"}]


def test_join_query(db: str) -> None:
    out = REGISTRY["sqlite_query"].call(json.dumps({
        "path": db,
        "sql": (
            "SELECT u.name, n.body FROM users u "
            "JOIN notes n ON n.user_id = u.id "
            "ORDER BY u.id"
        ),
    }))
    rows = json.loads(out)
    assert rows[0]["name"] == "Alice"
    assert rows[1]["body"] == "how are you"


def test_rejects_ddl(db: str) -> None:
    out = REGISTRY["sqlite_query"].call(json.dumps({
        "path": db, "sql": "DROP TABLE users"
    }))
    assert out.startswith("ERROR:")
    assert "must start with" in out


def test_rejects_dml(db: str) -> None:
    for stmt in ("INSERT INTO users (name) VALUES ('x')",
                 "UPDATE users SET name='x'",
                 "DELETE FROM users"):
        out = REGISTRY["sqlite_query"].call(json.dumps({"path": db, "sql": stmt}))
        assert out.startswith("ERROR:")


def test_rejects_multistatement(db: str) -> None:
    out = REGISTRY["sqlite_query"].call(json.dumps({
        "path": db,
        "sql": "SELECT 1; SELECT 2",
    }))
    assert "multi-statement" in out


def test_missing_file_clean_error(tmp_path: Path) -> None:
    out = REGISTRY["sqlite_query"].call(json.dumps({
        "path": str(tmp_path / "nope.db"), "sql": "SELECT 1",
    }))
    assert "no such file" in out


def test_limit_caps_rows(db: str) -> None:
    out = REGISTRY["sqlite_query"].call(json.dumps({
        "path": db, "sql": "SELECT * FROM users", "limit": 1,
    }))
    rows = json.loads(out)
    assert len(rows) == 1
