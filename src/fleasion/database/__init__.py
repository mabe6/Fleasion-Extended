"""SQLite database inspection and guarded editing tools for Fleasion."""

from .sqlite_browser import (
    QueryResult,
    SQLiteBrowser,
    SQLiteBrowserError,
    TableInfo,
    TablePage,
    discover_databases,
    is_sqlite_file,
)

__all__ = [
    "QueryResult",
    "SQLiteBrowser",
    "SQLiteBrowserError",
    "TableInfo",
    "TablePage",
    "discover_databases",
    "is_sqlite_file",
]
