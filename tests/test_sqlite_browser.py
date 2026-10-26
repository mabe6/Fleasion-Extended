"""Tests for the read-only-first SQLite database browser and editor."""

import sqlite3
from pathlib import Path

import pytest

from fleasion.database import SQLiteBrowser, SQLiteBrowserError, discover_databases
from fleasion.database import sqlite_browser as sqlite_browser_module


def _make_database(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE files (id BLOB PRIMARY KEY, size INTEGER NOT NULL, note TEXT)"
        )
        connection.executemany(
            "INSERT INTO files (id, size, note) VALUES (?, ?, ?)",
            [(bytes.fromhex("01" * 16), 10, "first"), (bytes.fromhex("02" * 16), 20, None)],
        )
        connection.execute(
            "CREATE TABLE no_primary_key (category TEXT, value INTEGER)"
        )
        connection.execute(
            "INSERT INTO no_primary_key (category, value) VALUES ('test', 3)"
        )
        connection.execute(
            "CREATE VIEW files_view AS SELECT id, size, note FROM files"
        )
    return path


def test_discover_databases_only_returns_sqlite_files(tmp_path):
    root = tmp_path / "scan"
    db_path = _make_database(root / "rbx-storage.db")
    invalid = root / "not-a-database.db"
    invalid.write_text("not sqlite", encoding="utf-8")
    hidden_build_db = _make_database(root / ".venv" / "ignored.db")

    results = discover_databases(root)

    assert db_path.resolve() in results
    assert invalid.resolve() not in results
    assert hidden_build_db.resolve() not in results


def test_tables_schema_pagination_and_views_are_read_only(tmp_path):
    path = _make_database(tmp_path / "sample.db")
    browser = SQLiteBrowser(path)

    tables = {table.name: table.kind for table in browser.tables()}
    assert tables["files"] == "table"
    assert tables["files_view"] == "view"

    schema, ddl, kind = browser.schema("files")
    assert kind == "table"
    assert "CREATE TABLE files" in ddl
    assert [column["name"] for column in schema] == ["id", "size", "note"]

    page = browser.fetch_page("files", limit=1, offset=1)
    assert page.total_rows == 2
    assert page.columns == ("id", "size", "note")
    assert page.rows[0][1:] == (20, None)
    assert page.writable is True
    assert page.keys[0] == (("id",), (bytes.fromhex("02" * 16),))

    view_page = browser.fetch_page("files_view")
    assert view_page.writable is False
    assert all(key is None for key in view_page.keys)


def test_rowid_fallback_supports_tables_without_primary_key(tmp_path):
    browser = SQLiteBrowser(_make_database(tmp_path / "sample.db"))

    page = browser.fetch_page("no_primary_key")
    assert page.writable is True
    assert page.keys[0] is not None
    assert page.keys[0][0] in (("_rowid_",), ("rowid",), ("oid",))


def test_read_only_query_is_bounded_and_writes_are_rejected(tmp_path):
    browser = SQLiteBrowser(_make_database(tmp_path / "sample.db"))

    result = browser.execute_select("SELECT size, note FROM files ORDER BY size")
    assert result.columns == ("size", "note")
    assert result.rows == ((10, "first"), (20, None))
    assert result.truncated is False

    with pytest.raises(SQLiteBrowserError, match="Only SELECT"):
        browser.execute_select("DELETE FROM files")

    # WITH-prefixed DML passes the initial keyword check but is still rejected by SQLite's read-only URI.
    with pytest.raises(SQLiteBrowserError, match="Read-only query failed"):
        browser.execute_select(
            "WITH selected AS (SELECT id FROM files) "
            "DELETE FROM files WHERE id IN (SELECT id FROM selected)"
        )

    with pytest.raises(sqlite3.OperationalError):
        with sqlite3.connect(browser.path.as_uri() + "?mode=ro", uri=True) as connection:
            connection.execute("UPDATE files SET size = 99")


def test_update_cell_makes_verified_backup_before_writing(tmp_path, monkeypatch):
    path = _make_database(tmp_path / "Roblox" / "rbx-storage.db")
    monkeypatch.setattr(sqlite_browser_module, "CONFIG_DIR", tmp_path / "app-config")
    monkeypatch.setattr(sqlite_browser_module, "_is_roblox_process_running", lambda: False)
    browser = SQLiteBrowser(path)

    page = browser.fetch_page("files", limit=1)
    key_columns, key_values = page.keys[0]
    backup = browser.update_cell("files", "size", key_columns, key_values, 777)

    assert backup.is_file()
    assert backup.parent == tmp_path / "app-config" / "FleasionNT" / "DatabaseBackups"
    with sqlite3.connect(backup) as connection:
        assert connection.execute(
            "SELECT size FROM files WHERE id = ?", (key_values[0],)
        ).fetchone()[0] == 10
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT size FROM files WHERE id = ?", (key_values[0],)
        ).fetchone()[0] == 777


def test_sql_write_makes_verified_backup_and_commits_one_statement(tmp_path, monkeypatch):
    path = _make_database(tmp_path / "sample.db")
    monkeypatch.setattr(sqlite_browser_module, "CONFIG_DIR", tmp_path / "app-config")
    browser = SQLiteBrowser(path)

    result = browser.execute_write("UPDATE files SET size = 88 WHERE size = 10")

    assert result.affected_rows == 1
    assert result.backup_path.is_file()
    with sqlite3.connect(result.backup_path) as connection:
        assert connection.execute("SELECT size FROM files ORDER BY size LIMIT 1").fetchone()[0] == 10
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT size FROM files WHERE size = 88").fetchone()[0] == 88


def test_sql_write_rejects_multiple_statements_and_non_write_statements(tmp_path, monkeypatch):
    browser = SQLiteBrowser(_make_database(tmp_path / "sample.db"))
    monkeypatch.setattr(sqlite_browser_module, "CONFIG_DIR", tmp_path / "app-config")

    with pytest.raises(SQLiteBrowserError, match="Write mode accepts"):
        browser.execute_write("PRAGMA integrity_check")

    with pytest.raises(SQLiteBrowserError, match="not supported"):
        browser.execute_write("UPDATE files SET size = 12 RETURNING size")

    with pytest.raises(SQLiteBrowserError, match="SQL write failed"):
        browser.execute_write("UPDATE files SET size = 12; DELETE FROM files")

    with sqlite3.connect(browser.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2


def test_sql_write_is_blocked_for_running_roblox_cache(tmp_path, monkeypatch):
    path = _make_database(tmp_path / "Roblox" / "rbx-storage.db")
    monkeypatch.setattr(sqlite_browser_module, "CONFIG_DIR", tmp_path / "app-config")
    monkeypatch.setattr(sqlite_browser_module, "_is_roblox_process_running", lambda: True)
    browser = SQLiteBrowser(path)

    with pytest.raises(SQLiteBrowserError, match="Close it before editing"):
        browser.execute_write("UPDATE files SET size = 100")

    assert not (tmp_path / "app-config" / "FleasionNT" / "DatabaseBackups").exists()


def test_readonly_browser_and_backup_include_committed_wal_frames(tmp_path, monkeypatch):
    path = tmp_path / "Roblox" / "rbx-storage.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sqlite_browser_module, "CONFIG_DIR", tmp_path / "app-config")

    writer = sqlite3.connect(path, timeout=5)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower() == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "CREATE TABLE files (id BLOB PRIMARY KEY, content BLOB, size INTEGER NOT NULL)"
        )
        ident = bytes.fromhex("cd" * 16)
        payload = b"committed-in-wal-not-yet-checkpointed"
        writer.execute(
            "INSERT INTO files (id, content, size) VALUES (?, ?, ?)",
            (ident, payload, len(payload)),
        )
        writer.commit()

        wal_path = path.with_name(path.name + "-wal")
        assert wal_path.is_file() and wal_path.stat().st_size > 0

        browser = SQLiteBrowser(path)
        page = browser.fetch_page("files")
        assert page.rows == ((ident, payload, len(payload)),)

        backup = browser.backup()
        with sqlite3.connect(backup) as connection:
            assert connection.execute(
                "SELECT content, size FROM files WHERE id = ?", (ident,)
            ).fetchone() == (payload, len(payload))
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        writer.close()


def test_replacing_files_content_blob_keeps_size_column_in_sync(tmp_path, monkeypatch):
    path = tmp_path / "Roblox" / "rbx-storage.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    ident = bytes.fromhex("ab" * 16)
    original = b"RBXH-original-cache-payload"
    replacement = b"RBXH-new-cache-payload-with-a-different-length"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE files (id BLOB PRIMARY KEY, content BLOB, size INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO files (id, content, size) VALUES (?, ?, ?)",
            (ident, original, len(original)),
        )
    monkeypatch.setattr(sqlite_browser_module, "CONFIG_DIR", tmp_path / "app-config")
    monkeypatch.setattr(sqlite_browser_module, "_is_roblox_process_running", lambda: False)

    backup = SQLiteBrowser(path).update_cell("files", "content", ("id",), (ident,), replacement)

    assert backup.is_file()
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT content, size FROM files WHERE id = ?", (ident,)
        ).fetchone() == (replacement, len(replacement))
    with sqlite3.connect(backup) as connection:
        assert connection.execute(
            "SELECT content, size FROM files WHERE id = ?", (ident,)
        ).fetchone() == (original, len(original))


def test_roblox_cache_edit_is_blocked_while_client_is_running(tmp_path, monkeypatch):
    path = _make_database(tmp_path / "Roblox" / "rbx-storage.db")
    monkeypatch.setattr(sqlite_browser_module, "CONFIG_DIR", tmp_path / "app-config")
    monkeypatch.setattr(sqlite_browser_module, "_is_roblox_process_running", lambda: True)
    browser = SQLiteBrowser(path)
    page = browser.fetch_page("files", limit=1)
    key_columns, key_values = page.keys[0]

    with pytest.raises(SQLiteBrowserError, match="Close it before editing"):
        browser.update_cell("files", "size", key_columns, key_values, 100)

    backups_dir = tmp_path / "app-config" / "FleasionNT" / "DatabaseBackups"
    assert not backups_dir.exists()
