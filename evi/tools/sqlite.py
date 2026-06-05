"""SQLite tools — read-only queries against a local DB file.

`sqlite3` is in the stdlib so there's no optional dep. We open every
connection in read-only mode via the SQLite URI form
(`file:<path>?mode=ro`) so even a model that goes off-script can't write
to a DB you didn't intend to mutate.

Statement gating: `sqlite_query` runs the first parsed token through a
small allowlist (`select`, `with`, `pragma`, `explain`). Anything else
is refused before the connection sees it. We deliberately disable
multi-statement execution (`;`-separated payloads) too.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from evi.tools.base import tool


_MAX_ROWS = 200
_MAX_CELL_BYTES = 4096
_ALLOWED_FIRST_TOKEN = {"select", "with", "pragma", "explain"}
_FIRST_TOKEN_RE = re.compile(r"^\s*--.*?$\s*|^\s*/\*.*?\*/\s*", re.M | re.S)


def _first_token(sql: str) -> str:
    """Best-effort first SQL keyword (strips leading comments)."""
    stripped = _FIRST_TOKEN_RE.sub("", sql, count=10).lstrip()
    return stripped.split(None, 1)[0].lower() if stripped else ""


def _open_ro(path: str) -> sqlite3.Connection:
    """Open `path` read-only. Raises if the file is missing."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(p)
    uri = f"file:{p.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


@tool(
    description=(
        "Return the schema of a SQLite file as JSON: a list of "
        "{table, columns: [{name, type, pk}, …]}. Use this before "
        "writing a query so you know what's there."
    ),
    category="sqlite",
)
def sqlite_schema(path: str) -> str:
    try:
        conn = _open_ro(path)
    except FileNotFoundError as exc:
        return f"ERROR: no such file: {exc}"
    except sqlite3.Error as exc:
        return f"ERROR: failed to open db: {exc}"

    try:
        tables = [
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]
        out: list[dict] = []
        for name in tables:
            cols = []
            for row in conn.execute(f"PRAGMA table_info({_quote_ident(name)})"):
                cols.append({
                    "name": row[1],
                    "type": row[2] or "",
                    "pk": bool(row[5]),
                })
            out.append({"table": name, "columns": cols})
        return json.dumps(out)
    except sqlite3.Error as exc:
        return f"ERROR: schema query failed: {exc}"
    finally:
        conn.close()


@tool(
    description=(
        "Run a read-only SQL query against a SQLite file. Allowed first "
        "keywords: SELECT, WITH, PRAGMA, EXPLAIN. Returns up to 200 rows "
        "as JSON `[{column: value, …}, …]`. Use `sqlite_schema` first to "
        "see what's available."
    ),
    category="sqlite",
)
def sqlite_query(path: str, sql: str, limit: int = 0) -> str:
    sql = (sql or "").strip().rstrip(";")
    if not sql:
        return "ERROR: empty query"
    if ";" in sql:
        return "ERROR: multi-statement queries are not allowed"
    head = _first_token(sql)
    if head not in _ALLOWED_FIRST_TOKEN:
        return (
            f"ERROR: query must start with one of "
            f"{sorted(_ALLOWED_FIRST_TOKEN)} (got {head!r})"
        )

    cap = max(1, min(int(limit) or _MAX_ROWS, _MAX_ROWS))
    try:
        conn = _open_ro(path)
    except FileNotFoundError as exc:
        return f"ERROR: no such file: {exc}"
    except sqlite3.Error as exc:
        return f"ERROR: failed to open db: {exc}"

    try:
        cur = conn.execute(sql)
        col_names = [d[0] for d in (cur.description or [])]
        rows = cur.fetchmany(cap)
    except sqlite3.Error as exc:
        return f"ERROR: query failed: {exc}"
    finally:
        conn.close()

    serialised: list[dict] = []
    for row in rows:
        d: dict = {}
        for col, val in zip(col_names, row):
            if isinstance(val, (bytes, bytearray)):
                val = f"<{len(val)}-byte blob>"
            elif isinstance(val, str) and len(val) > _MAX_CELL_BYTES:
                val = val[:_MAX_CELL_BYTES] + "…(truncated)"
            d[col] = val
        serialised.append(d)
    return json.dumps(serialised, default=str)


def _quote_ident(name: str) -> str:
    """Quote a SQLite identifier so `PRAGMA table_info(...)` is safe."""
    return '"' + name.replace('"', '""') + '"'
