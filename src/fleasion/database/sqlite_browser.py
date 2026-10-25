"""Read-only-first browser and guarded editor for SQLite databases."""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..utils import CONFIG_DIR

SQLITE_HEADER = b"SQLite format 3\x00"
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}
MAX_QUERY_ROWS = 500


class SQLiteBrowserError(Exception):
    """A database operation failed for a reason suitable for display in the UI."""


@dataclass(frozen=True)
class TableInfo:
    """A SQLite table or view."""

    name: str
    kind: str
    sql: str


@dataclass(frozen=True)
class TablePage:
    """One paginated batch of data and the key values needed to locate each row."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    keys: tuple[tuple[tuple[str, ...], tuple[Any, ...]] | None, ...]
    total_rows: int
    writable: bool
    message: str = ""


@dataclass(frozen=True)
class QueryResult:
    """A bounded read-only query result."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    truncated: bool


@dataclass(frozen=True)
class WriteResult:
    """Summary of one backed-up SQL write statement."""

    backup_path: Path
    affected_rows: int
    last_row_id: int


def quote_identifier(identifier: str) -> str:
    """Quote a SQLite identifier; values must still be passed as parameters."""
    if "\x00" in identifier:
        raise SQLiteBrowserError("An SQLite identifier cannot contain a NUL character.")
    return '"' + identifier.replace('"', '""') + '"'


def is_sqlite_file(path: str | os.PathLike[str]) -> bool:
    """Return whether a file has the standard SQLite 3 header."""
    try:
        with Path(path).open("rb") as handle:
            return handle.read(16) == SQLITE_HEADER
    except OSError:
        return False


def discover_databases(root: str | os.PathLike[str], limit: int = 5000) -> list[Path]:
    """Find SQLite databases under a file or directory without traversing common build trees."""
    path = Path(root).expanduser()
    if path.is_file():
        return [path.resolve()] if is_sqlite_file(path) else []
    if not path.is_dir():
        return []

    found: list[Path] = []
    for directory, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for filename in filenames:
            candidate = Path(directory) / filename
            if candidate.suffix.lower() not in {".db", ".sqlite", ".sqlite3", ".db3"}:
                continue
            if is_sqlite_file(candidate):
                found.append(candidate.resolve())
                if len(found) >= limit:
                    return sorted(set(found), key=lambda item: str(item).casefold())
    return sorted(set(found), key=lambda item: str(item).casefold())


def _is_roblox_process_running() -> bool:
    """Detect Roblox Player/Studio processes on common desktop platforms."""
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist"],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
            text = result.stdout.casefold()
        else:
            result = subprocess.run(
                ["ps", "-A", "-o", "comm="],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            text = result.stdout.casefold()
    except (OSError, subprocess.TimeoutExpired):
        # Fail closed for a Roblox cache write if process detection is unavailable.
        return True
    names = ("robloxplayerbeta", "robloxstudiobeta", "roblox player", "roblox studio", "sober")
    return any(name in text for name in names)


class SQLiteBrowser:
    """Inspect SQLite databases in read-only mode and edit individual cells with backups."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser().resolve()

    def _require_file(self) -> None:
        if not self.path.is_file():
            raise SQLiteBrowserError(f"Database file does not exist:\n{self.path}")
        if not is_sqlite_file(self.path):
            raise SQLiteBrowserError(f"This file does not have a standard SQLite header:\n{self.path}")

    def connect_readonly(self) -> sqlite3.Connection:
        """Open a true SQLite read-only connection."""
        self._require_file()
        uri = self.path.as_uri() + "?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=3)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            return connection
        except sqlite3.Error as exc:
            raise SQLiteBrowserError(f"Could not open database read-only:\n{exc}") from exc

    def tables(self, *, include_internal: bool = True) -> list[TableInfo]:
        """Return tables and views, optionally excluding SQLite's internal tables."""
        try:
            with contextlib.closing(self.connect_readonly()) as connection:
                rows = connection.execute(
                    "SELECT name, type, COALESCE(sql, '') AS sql "
                    "FROM sqlite_master WHERE type IN ('table', 'view') "
                    "ORDER BY type, name COLLATE NOCASE"
                ).fetchall()
        except sqlite3.Error as exc:
            raise SQLiteBrowserError(f"Could not list database tables:\n{exc}") from exc
        result = []
        for row in rows:
            name = str(row["name"])
            if not include_internal and name.startswith("sqlite_"):
                continue
            result.append(TableInfo(name, str(row["type"]), str(row["sql"] or "")))
        return result

    def schema(self, table: str) -> tuple[list[dict[str, Any]], str, str]:
        """Return table_info rows, the CREATE statement and the object kind."""
        info = next((item for item in self.tables() if item.name == table), None)
        if info is None:
            raise SQLiteBrowserError(f"Table or view no longer exists: {table}")
        try:
            with contextlib.closing(self.connect_readonly()) as connection:
                rows = connection.execute(
                    f"PRAGMA table_info({quote_identifier(table)})"
                ).fetchall()
                columns = [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise SQLiteBrowserError(f"Could not read table schema:\n{exc}") from exc
        return columns, info.sql, info.kind

    def count_rows(self, table: str) -> int:
        info = next((item for item in self.tables() if item.name == table), None)
        if info is None:
            raise SQLiteBrowserError(f"Table or view no longer exists: {table}")
        try:
            with contextlib.closing(self.connect_readonly()) as connection:
                row = connection.execute(
                    f"SELECT COUNT(*) FROM {quote_identifier(table)}"
                ).fetchone()
                return int(row[0])
        except sqlite3.Error as exc:
            raise SQLiteBrowserError(f"Could not count rows:\n{exc}") from exc

    def fetch_page(self, table: str, limit: int = 100, offset: int = 0) -> TablePage:
        """Fetch one bounded page without materializing the entire table."""
        if not 1 <= limit <= 1000:
            raise SQLiteBrowserError("Page size must be between 1 and 1000.")
        if offset < 0:
            raise SQLiteBrowserError("Page offset cannot be negative.")
        columns_info, _ddl, kind = self.schema(table)
        column_names = tuple(str(column["name"]) for column in columns_info)
        pk_info = sorted(
            (column for column in columns_info if int(column["pk"] or 0) > 0),
            key=lambda column: int(column["pk"]),
        )
        pk_names = tuple(str(column["name"]) for column in pk_info)
        total = self.count_rows(table)
        writable = kind == "table" and not table.startswith("sqlite_")
        rows: list[tuple[Any, ...]] = []
        keys: list[tuple[tuple[str, ...], tuple[Any, ...]] | None] = []
        message = ""

        try:
            with contextlib.closing(self.connect_readonly()) as connection:
                quoted_table = quote_identifier(table)
                if pk_names:
                    query = (
                        f"SELECT * FROM {quoted_table} LIMIT ? OFFSET ?"
                    )
                    for row in connection.execute(query, (limit, offset)).fetchall():
                        values = tuple(row[name] for name in column_names)
                        key_values = tuple(row[name] for name in pk_names)
                        rows.append(values)
                        keys.append((pk_names, key_values) if writable else None)
                elif writable:
                    # Use an unshadowed hidden-rowid alias as a fallback key for ordinary tables.
                    hidden = next(
                        (candidate for candidate in ("_rowid_", "rowid", "oid")
                         if candidate.casefold() not in {name.casefold() for name in column_names}),
                        None,
                    )
                    if hidden is not None:
                        try:
                            fetched = connection.execute(
                                f"SELECT {quote_identifier(hidden)}, * FROM {quoted_table} "
                                "LIMIT ? OFFSET ?",
                                (limit, offset),
                            ).fetchall()
                            for row in fetched:
                                rows.append(tuple(row[name] for name in column_names))
                                keys.append(((hidden,), (row[0],)))
                        except sqlite3.Error:
                            hidden = None
                    if hidden is None:
                        fetched = connection.execute(
                            f"SELECT * FROM {quoted_table} LIMIT ? OFFSET ?",
                            (limit, offset),
                        ).fetchall()
                        rows.extend(tuple(row[name] for name in column_names) for row in fetched)
                        keys.extend(None for _row in fetched)
                        writable = False
                        message = "This table has no usable primary key or rowid; editing is disabled."
                else:
                    fetched = connection.execute(
                        f"SELECT * FROM {quoted_table} LIMIT ? OFFSET ?",
                        (limit, offset),
                    ).fetchall()
                    rows.extend(tuple(row[name] for name in column_names) for row in fetched)
                    keys.extend(None for _row in fetched)
                    message = "Views are shown read-only."
        except sqlite3.Error as exc:
            raise SQLiteBrowserError(f"Could not read rows from {table}:\n{exc}") from exc

        if kind == "view":
            writable = False
        return TablePage(column_names, tuple(rows), tuple(keys), total, writable, message)

    def execute_select(self, sql: str, limit: int = MAX_QUERY_ROWS) -> QueryResult:
        """Run a bounded SELECT/WITH/EXPLAIN query on a read-only connection."""
        normalized = sql.strip()
        if not normalized:
            raise SQLiteBrowserError("Enter a SELECT query first.")
        # The connection itself enforces read-only mode, regardless of SQL spelling.
        if re.match(r"(?is)^(select|with|explain)\b", normalized) is None:
            raise SQLiteBrowserError("Only SELECT, WITH (read-only) and EXPLAIN queries are allowed here.")
        try:
            with contextlib.closing(self.connect_readonly()) as connection:
                deadline = time.monotonic() + 3.0
                connection.set_progress_handler(
                    lambda: 1 if time.monotonic() > deadline else 0,
                    10_000,
                )
                cursor = connection.execute(normalized)
                if cursor.description is None:
                    raise SQLiteBrowserError("The query did not return a result set.")
                columns = tuple(str(item[0]) for item in cursor.description)
                fetched = cursor.fetchmany(max(1, min(limit, MAX_QUERY_ROWS)) + 1)
                truncated = len(fetched) > min(limit, MAX_QUERY_ROWS)
                rows = tuple(tuple(row) for row in fetched[: min(limit, MAX_QUERY_ROWS)])
                return QueryResult(columns, rows, truncated)
        except SQLiteBrowserError:
            raise
        except sqlite3.Error as exc:
            raise SQLiteBrowserError(f"Read-only query failed:\n{exc}") from exc

    def _require_roblox_cache_closed(self) -> None:
        """Refuse edits to known Roblox cache databases while their client is running."""
        is_roblox_cache = (
            self.path.name.casefold() == 'rbx-storage.db'
            or 'roblox' in {part.casefold() for part in self.path.parts}
            or 'sober' in {part.casefold() for part in self.path.parts}
        )
        if is_roblox_cache and _is_roblox_process_running():
            raise SQLiteBrowserError(
                'Roblox Player/Studio/Sober appears to be running. '
                'Close it before editing its cache database.'
            )

    def execute_write(self, sql: str) -> WriteResult:
        """Execute one explicit SQL write statement after creating a verified backup."""
        normalized = sql.strip()
        if not normalized:
            raise SQLiteBrowserError('Enter a write statement first.')
        if re.match(r'(?is)^(insert|update|delete|replace|create|alter|drop)\b', normalized) is None:
            raise SQLiteBrowserError(
                'Write mode accepts one INSERT, UPDATE, DELETE, REPLACE, CREATE, ALTER or DROP statement.'
            )
        if re.search(r'(?is)\breturning\b', normalized):
            raise SQLiteBrowserError(
                'RETURNING is not supported in write mode. Run a separate SELECT after the write.'
            )
        self._require_file()
        self._require_roblox_cache_closed()
        backup_path = self.backup()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=5)
            connection.execute('PRAGMA busy_timeout = 5000')
            connection.execute('BEGIN IMMEDIATE')
            deadline = time.monotonic() + 10.0
            connection.set_progress_handler(
                lambda: 1 if time.monotonic() > deadline else 0,
                10_000,
            )
            cursor = connection.execute(normalized)
            affected_rows = max(0, int(cursor.rowcount))
            last_row_id = int(cursor.lastrowid or 0)
            connection.commit()
            return WriteResult(backup_path, affected_rows, last_row_id)
        except sqlite3.Error as exc:
            if connection is not None:
                with contextlib.suppress(sqlite3.Error):
                    connection.rollback()
            raise SQLiteBrowserError(
                f'SQL write failed; the pre-write backup is saved at:\n{backup_path}\n\n{exc}'
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    def backup(self) -> Path:
        """Create a consistent SQLite backup with the SQLite backup API."""
        self._require_file()
        backup_dir = CONFIG_DIR / "FleasionNT" / "DatabaseBackups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
        destination = backup_dir / f"{self.path.stem}-{timestamp}{self.path.suffix or '.db'}"
        try:
            with contextlib.closing(self.connect_readonly()) as source:
                with contextlib.closing(sqlite3.connect(destination, timeout=5)) as target:
                    source.backup(target)
                    check = target.execute("PRAGMA integrity_check").fetchone()
                    if not check or check[0] != "ok":
                        raise SQLiteBrowserError(
                            f"Backup integrity check failed: {check[0] if check else 'no result'}"
                        )
        except (SQLiteBrowserError, sqlite3.Error, OSError) as exc:
            with contextlib.suppress(OSError):
                destination.unlink()
            if isinstance(exc, SQLiteBrowserError):
                raise
            raise SQLiteBrowserError(f"Could not back up the database:\n{exc}") from exc
        return destination

    def update_cell(
        self,
        table: str,
        column: str,
        key_columns: tuple[str, ...],
        key_values: tuple[Any, ...],
        value: Any,
    ) -> Path:
        """Update exactly one cell, after checking the target and creating a verified backup."""
        self._require_file()
        info = next((item for item in self.tables() if item.name == table), None)
        if info is None or info.kind != "table" or table.startswith("sqlite_"):
            raise SQLiteBrowserError("Only ordinary user tables can be edited.")
        schema, _ddl, _kind = self.schema(table)
        valid_columns = {str(item["name"]) for item in schema}
        if column not in valid_columns:
            raise SQLiteBrowserError(f"Column no longer exists: {column}")
        if not key_columns or len(key_columns) != len(key_values):
            raise SQLiteBrowserError("No safe row identifier is available for this record.")
        # A key can be one of SQLite's hidden rowid aliases only if it is not shadowed by a column.
        for key_column in key_columns:
            if key_column not in valid_columns and key_column not in {"rowid", "_rowid_", "oid"}:
                raise SQLiteBrowserError("The selected row identifier is no longer valid.")

        self._require_roblox_cache_closed()
        backup_path = self.backup()
        try:
            with contextlib.closing(sqlite3.connect(self.path, timeout=5)) as connection:
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute("BEGIN IMMEDIATE")
                where = " AND ".join(f"{quote_identifier(name)} IS ?" for name in key_columns)
                size_column = next(
                    (name for name in valid_columns if name.casefold() == "size"), None
                )
                if (
                    table.casefold() == "files"
                    and column.casefold() == "content"
                    and size_column is not None
                    and (value is None or isinstance(value, (bytes, bytearray, memoryview)))
                ):
                    blob_value = None if value is None else sqlite3.Binary(bytes(value))
                    blob_size = 0 if blob_value is None else len(blob_value)
                    cursor = connection.execute(
                        f"UPDATE {quote_identifier(table)} "
                        f"SET {quote_identifier(column)} = ?, {quote_identifier(size_column)} = ? "
                        f"WHERE {where}",
                        (blob_value, blob_size, *key_values),
                    )
                else:
                    cursor = connection.execute(
                        f"UPDATE {quote_identifier(table)} "
                        f"SET {quote_identifier(column)} = ? WHERE {where}",
                        (value, *key_values),
                    )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise SQLiteBrowserError(
                        f"Expected to update one row, but SQLite matched {cursor.rowcount}. "
                        "The database may have changed; refresh and retry."
                    )
                connection.commit()
        except SQLiteBrowserError:
            raise
        except sqlite3.Error as exc:
            raise SQLiteBrowserError(
                f"Database update failed. The backup is saved at:\n{backup_path}\n\n{exc}"
            ) from exc
        return backup_path
