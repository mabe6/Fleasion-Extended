"""GUI for safely browsing and editing local SQLite databases."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..database.sqlite_browser import (
    MAX_QUERY_ROWS,
    SQLiteBrowser,
    SQLiteBrowserError,
    TablePage,
    discover_databases,
)
from ..utils import CONFIG_DIR


def _display_value(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return f"<BLOB: {len(value):,} bytes>"
    return str(value)


def _coerce_value(text: str, original: Any) -> Any:
    """Preserve a cell's basic SQLite value type where its prior value is known."""
    if original is None:
        return None if text.strip().upper() == "NULL" else text
    if isinstance(original, bool):
        return bool(int(text.strip()))
    if isinstance(original, int):
        return int(text.strip(), 10)
    if isinstance(original, float):
        return float(text.strip())
    if isinstance(original, bytes):
        raise SQLiteBrowserError("Use the BLOB export/replace action for binary values.")
    return text


class LocalDatabaseEditorWidget(QWidget):
    """Browse tables and edit individual cells with a verified backup before each write."""

    PAGE_SIZES = (25, 50, 100, 250, 500, 1000)

    def __init__(self, parent: QWidget | None = None, *, embedded: bool = False):
        super().__init__(parent)
        self.embedded = embedded
        self.setWindowFlag(Qt.WindowType.Window, not embedded)
        self.setWindowTitle("Local SQLite Database Editor — Fleasion Extended")
        self.resize(1180, 760)
        self.browser: SQLiteBrowser | None = None
        self.current_table = ""
        self.current_kind = ""
        self.page_offset = 0
        self.page: TablePage | None = None
        self._loading_data = False
        self._editing_enabled = False
        self._schema_ddl = ""

        self._build_ui()
        self._find_default_database()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        file_row = QHBoxLayout()
        file_row.addWidget(QLabel("Database file:"))
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Choose an SQLite .db / .sqlite file…")
        self.path_edit.returnPressed.connect(self._open_path_from_line)
        file_row.addWidget(self.path_edit, 1)
        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self._browse_database)
        file_row.addWidget(browse_button)
        open_button = QPushButton("Open")
        open_button.clicked.connect(self._open_path_from_line)
        file_row.addWidget(open_button)
        root.addLayout(file_row)

        scan_row = QHBoxLayout()
        scan_row.addWidget(QLabel("Search folder:"))
        self.scan_path_edit = QLineEdit()
        default_root = os.environ.get("LOCALAPPDATA", "")
        self.default_scan_root = str(Path(default_root) / "Roblox") if default_root else str(Path.home())
        self.scan_path_edit.setText(self.default_scan_root)
        scan_row.addWidget(self.scan_path_edit, 1)
        folder_button = QPushButton("Choose folder…")
        folder_button.clicked.connect(self._browse_scan_folder)
        scan_row.addWidget(folder_button)
        scan_button = QPushButton("Find databases")
        scan_button.clicked.connect(self._scan_folder)
        scan_row.addWidget(scan_button)
        root.addLayout(scan_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("SQLite databases"))
        self.database_list = QListWidget()
        self.database_list.currentItemChanged.connect(self._database_item_changed)
        left_layout.addWidget(self.database_list, 3)
        left_layout.addWidget(QLabel("Tables and views"))
        self.table_list = QListWidget()
        self.table_list.currentItemChanged.connect(self._table_item_changed)
        left_layout.addWidget(self.table_list, 2)
        splitter.addWidget(left)

        self.tabs = QTabWidget()
        splitter.addWidget(self.tabs)
        splitter.setSizes([290, 890])

        self.data_tab = QWidget()
        data_layout = QVBoxLayout(self.data_tab)
        data_toolbar = QHBoxLayout()
        self.table_title = QLabel("Choose a table")
        data_toolbar.addWidget(self.table_title, 1)
        data_toolbar.addWidget(QLabel("Rows/page:"))
        self.page_size = QComboBox()
        for size in self.PAGE_SIZES:
            self.page_size.addItem(str(size), size)
        self.page_size.setCurrentText("100")
        self.page_size.currentIndexChanged.connect(self._page_size_changed)
        data_toolbar.addWidget(self.page_size)
        self.prev_button = QPushButton("← Previous")
        self.prev_button.clicked.connect(self._previous_page)
        data_toolbar.addWidget(self.prev_button)
        self.page_label = QLabel("Page 0 / 0")
        data_toolbar.addWidget(self.page_label)
        self.next_button = QPushButton("Next →")
        self.next_button.clicked.connect(self._next_page)
        data_toolbar.addWidget(self.next_button)
        data_layout.addLayout(data_toolbar)

        self.data_grid = QTableWidget()
        self.data_grid.setAlternatingRowColors(True)
        self.data_grid.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.data_grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.data_grid.horizontalHeader().setStretchLastSection(True)
        self.data_grid.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self.data_grid.cellChanged.connect(self._cell_changed)
        self.data_grid.cellDoubleClicked.connect(self._cell_double_clicked)
        data_layout.addWidget(self.data_grid, 1)

        self.edit_button = QPushButton("Enable cell editing…")
        self.edit_button.setCheckable(True)
        self.edit_button.toggled.connect(self._toggle_editing)
        data_layout.addWidget(self.edit_button)
        self.tabs.addTab(self.data_tab, "Data")

        self.schema_tab = QWidget()
        schema_layout = QVBoxLayout(self.schema_tab)
        self.schema_grid = QTableWidget(0, 6)
        self.schema_grid.setHorizontalHeaderLabels(
            ["Column", "Type", "Not NULL", "Default", "Primary key", "CID"]
        )
        self.schema_grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.schema_grid.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.schema_grid.horizontalHeader().setStretchLastSection(True)
        schema_layout.addWidget(self.schema_grid, 2)
        schema_layout.addWidget(QLabel("CREATE statement"))
        self.ddl_edit = QPlainTextEdit()
        self.ddl_edit.setReadOnly(True)
        self.ddl_edit.setMaximumBlockCount(1000)
        schema_layout.addWidget(self.ddl_edit, 1)
        self.tabs.addTab(self.schema_tab, "Schema")

        self.sql_tab = QWidget()
        sql_layout = QVBoxLayout(self.sql_tab)
        sql_layout.addWidget(QLabel(
            "Read-only SQL console. Only SELECT, read-only WITH, and EXPLAIN statements are accepted."
        ))
        self.sql_edit = QPlainTextEdit(
            "SELECT name, type, sql FROM sqlite_master "
            "WHERE type IN ('table', 'view') ORDER BY type, name;"
        )
        self.sql_edit.setMinimumHeight(110)
        sql_layout.addWidget(self.sql_edit)
        sql_toolbar = QHBoxLayout()
        run_query = QPushButton("Run read-only query")
        run_query.clicked.connect(self._run_query)
        sql_toolbar.addWidget(run_query)
        self.query_status = QLabel("Results are limited to 500 rows.")
        sql_toolbar.addWidget(self.query_status, 1)
        sql_layout.addLayout(sql_toolbar)
        self.query_grid = QTableWidget()
        self.query_grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.query_grid.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.query_grid.horizontalHeader().setStretchLastSection(True)
        sql_layout.addWidget(self.query_grid, 1)
        self.tabs.addTab(self.sql_tab, "SQL (read-only)")

        root.addWidget(splitter, 1)
        footer = QHBoxLayout()
        self.status_label = QLabel("Read-only mode. No database has been opened.")
        self.status_label.setWordWrap(True)
        footer.addWidget(self.status_label, 1)
        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self._refresh_current_table)
        footer.addWidget(refresh_button)
        if not self.embedded:
            close_button = QPushButton("Close")
            close_button.clicked.connect(self.close)
            footer.addWidget(close_button)
        root.addLayout(footer)

    def _find_default_database(self) -> None:
        """Open the standard Roblox cache DB if it exists; do not scan the entire drive at startup."""
        candidate = Path(self.default_scan_root) / "rbx-storage.db"
        if candidate.is_file():
            self._add_database(candidate, open_now=True)
            return
        self.status_label.setText(
            "Choose a database or scan a folder. Databases open read-only by default."
        )

    def _browse_database(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Open SQLite database",
            self.path_edit.text() or self.default_scan_root,
            "SQLite databases (*.db *.sqlite *.sqlite3 *.db3);;All files (*.*)",
        )
        if filename:
            self._add_database(Path(filename), open_now=True)

    def _open_path_from_line(self) -> None:
        value = self.path_edit.text().strip().strip('"')
        if value:
            self._add_database(Path(value), open_now=True)

    def _browse_scan_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose folder to scan", self.scan_path_edit.text()
        )
        if folder:
            self.scan_path_edit.setText(folder)

    def _scan_folder(self) -> None:
        root = self.scan_path_edit.text().strip().strip('"')
        if not root:
            return
        self.status_label.setText("Scanning for SQLite database files…")
        databases = discover_databases(root)
        self.database_list.blockSignals(True)
        for path in databases:
            if not self._contains_database(path):
                item = QListWidgetItem(str(path))
                item.setToolTip(str(path))
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                self.database_list.addItem(item)
        self.database_list.blockSignals(False)
        if databases:
            self.status_label.setText(f"Found {len(databases)} SQLite database(s) under {root}.")
        else:
            self.status_label.setText(
                f"No standard SQLite database files were found under {root}."
            )

    def _contains_database(self, path: Path) -> bool:
        expected = str(path.resolve())
        for index in range(self.database_list.count()):
            item = self.database_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == expected:
                return True
        return False

    def _add_database(self, path: Path, *, open_now: bool) -> None:
        try:
            path = path.expanduser().resolve()
        except OSError:
            pass
        if self._contains_database(path):
            for index in range(self.database_list.count()):
                item = self.database_list.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == str(path):
                    self.database_list.setCurrentItem(item)
                    break
        else:
            item = QListWidgetItem(str(path))
            item.setToolTip(str(path))
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.database_list.addItem(item)
            self.database_list.setCurrentItem(item)
        if open_now:
            self._load_database(path)

    def _database_item_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if current is not None:
            self._load_database(Path(str(current.data(Qt.ItemDataRole.UserRole))))

    def _load_database(self, path: Path) -> None:
        try:
            browser = SQLiteBrowser(path)
            tables = browser.tables()
        except SQLiteBrowserError as exc:
            self.browser = None
            self.table_list.clear()
            self.data_grid.clear()
            self.data_grid.setRowCount(0)
            self.data_grid.setColumnCount(0)
            self.path_edit.setText(str(path))
            self.status_label.setText(str(exc))
            return
        self.browser = browser
        self.path_edit.setText(str(browser.path))
        self.table_list.blockSignals(True)
        self.table_list.clear()
        for table in tables:
            item = QListWidgetItem(f"{table.name}  [{table.kind}]")
            item.setData(Qt.ItemDataRole.UserRole, table.name)
            item.setData(Qt.ItemDataRole.UserRole + 1, table.kind)
            item.setToolTip(table.sql or table.name)
            self.table_list.addItem(item)
        self.table_list.blockSignals(False)
        if tables:
            self.table_list.setCurrentRow(0)
            self._load_table(tables[0].name, tables[0].kind)
        else:
            self.current_table = ""
            self.data_grid.clear()
            self.data_grid.setRowCount(0)
            self.data_grid.setColumnCount(0)
            self.status_label.setText(f"Opened {browser.path}; no tables or views found.")
        self.status_label.setText(
            f"Opened read-only: {browser.path} — {len(tables)} table(s)/view(s)."
        )

    def _table_item_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if current is not None:
            self._load_table(
                str(current.data(Qt.ItemDataRole.UserRole)),
                str(current.data(Qt.ItemDataRole.UserRole + 1)),
            )

    def _load_table(self, table: str, kind: str | None = None) -> None:
        if self.browser is None:
            return
        self.current_table = table
        self.current_kind = kind or ""
        self.page_offset = 0
        self._refresh_current_table()

    def _page_size(self) -> int:
        return int(self.page_size.currentData() or 100)

    def _refresh_current_table(self) -> None:
        if self.browser is None or not self.current_table:
            return
        try:
            self.page = self.browser.fetch_page(
                self.current_table, self._page_size(), self.page_offset
            )
            columns, ddl, kind = self.browser.schema(self.current_table)
        except SQLiteBrowserError as exc:
            self.status_label.setText(str(exc))
            return

        page = self.page
        self.current_kind = kind
        self.table_title.setText(
            f"{self.current_table} — {page.total_rows:,} row(s)"
        )
        self.prev_button.setEnabled(self.page_offset > 0)
        self.next_button.setEnabled(self.page_offset + self._page_size() < page.total_rows)
        start = self.page_offset + 1 if page.total_rows else 0
        end = min(self.page_offset + len(page.rows), page.total_rows)
        self.page_label.setText(f"{start:,}–{end:,} / {page.total_rows:,}")

        self._loading_data = True
        self.data_grid.blockSignals(True)
        self.data_grid.clear()
        self.data_grid.setColumnCount(len(page.columns))
        self.data_grid.setHorizontalHeaderLabels(list(page.columns))
        self.data_grid.setRowCount(len(page.rows))
        for row_index, row in enumerate(page.rows):
            for column_index, value in enumerate(row):
                item = QTableWidgetItem(_display_value(value))
                item.setData(Qt.ItemDataRole.UserRole, value)
                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if (
                    self._editing_enabled
                    and page.writable
                    and page.keys[row_index] is not None
                    and not isinstance(value, bytes)
                ):
                    flags |= Qt.ItemFlag.ItemIsEditable
                item.setFlags(flags)
                if isinstance(value, bytes):
                    item.setToolTip("Binary BLOB. Double-click to export or replace.")
                elif value is None:
                    item.setToolTip("SQL NULL. Edit and enter NULL to keep it NULL.")
                self.data_grid.setItem(row_index, column_index, item)
        self.data_grid.blockSignals(False)
        self._loading_data = False
        self.data_grid.resizeColumnsToContents()
        self._render_schema(columns, ddl)
        self.edit_button.setEnabled(page.writable)
        if not page.writable and self.edit_button.isChecked():
            self.edit_button.blockSignals(True)
            self.edit_button.setChecked(False)
            self.edit_button.blockSignals(False)
            self._editing_enabled = False
        if page.message:
            self.status_label.setText(page.message)
        elif self._editing_enabled:
            self.status_label.setText(
                "Editing enabled. Every saved cell creates a SQLite backup first."
            )
        else:
            self.status_label.setText(
                f"Read-only: {self.browser.path} — {page.total_rows:,} row(s) in {self.current_table}."
            )

    def _render_schema(self, columns: list[dict[str, Any]], ddl: str) -> None:
        self.schema_grid.setRowCount(len(columns))
        for row_index, column in enumerate(columns):
            values = [
                column.get("name"),
                column.get("type"),
                column.get("notnull"),
                column.get("dflt_value"),
                column.get("pk"),
                column.get("cid"),
            ]
            for col_index, value in enumerate(values):
                self.schema_grid.setItem(
                    row_index, col_index, QTableWidgetItem(_display_value(value))
                )
        self.schema_grid.resizeColumnsToContents()
        self.ddl_edit.setPlainText(ddl or "(No CREATE statement; this may be a virtual table or view.)")

    def _toggle_editing(self, enabled: bool) -> None:
        if enabled and not self._editing_enabled:
            result = QMessageBox.warning(
                self,
                "Enable database editing?",
                "Editing writes directly to the selected SQLite database.\n\n"
                "Before each changed cell, Fleasion will create and integrity-check a backup in "
                "the FleasionNT/DatabaseBackups folder. Editing the Roblox cache is blocked while "
                "Roblox Player, Studio, or Sober is detected running.\n\n"
                "Only enable editing when you understand the consequences.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if result != QMessageBox.StandardButton.Yes:
                self.edit_button.blockSignals(True)
                self.edit_button.setChecked(False)
                self.edit_button.blockSignals(False)
                return
        self._editing_enabled = enabled
        self.edit_button.setText("Disable cell editing" if enabled else "Enable cell editing…")
        self._refresh_current_table()

    def _cell_changed(self, row: int, column: int) -> None:
        if self._loading_data or not self._editing_enabled or self.page is None or self.browser is None:
            return
        if row >= len(self.page.rows) or column >= len(self.page.columns):
            return
        key = self.page.keys[row]
        if key is None:
            self._refresh_current_table()
            return
        original = self.page.rows[row][column]
        item = self.data_grid.item(row, column)
        if item is None:
            return
        try:
            value = _coerce_value(item.text(), original)
        except (ValueError, TypeError, SQLiteBrowserError) as exc:
            QMessageBox.warning(self, "Invalid value", str(exc))
            self._refresh_current_table()
            return
        if value == original:
            self._refresh_current_table()
            return
        column_name = self.page.columns[column]
        answer = QMessageBox.question(
            self,
            "Confirm database write",
            f"Write this value to {self.current_table}.{column_name}?\n\n"
            f"Old: {_display_value(original)[:300]}\n"
            f"New: {_display_value(value)[:300]}\n\n"
            "A backup will be created first.",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Save:
            self._refresh_current_table()
            return
        try:
            backup = self.browser.update_cell(
                self.current_table, column_name, key[0], key[1], value
            )
        except (SQLiteBrowserError, sqlite3.Error, OSError) as exc:
            QMessageBox.critical(self, "Database update failed", str(exc))
            self._refresh_current_table()
            return
        self.status_label.setText(f"Saved. Backup: {backup}")
        self._refresh_current_table()

    def _cell_double_clicked(self, row: int, column: int) -> None:
        if self.page is None or self.browser is None:
            return
        if row >= len(self.page.rows) or column >= len(self.page.columns):
            return
        value = self.page.rows[row][column]
        if not isinstance(value, bytes):
            return
        if self._editing_enabled and self.page.writable and self.page.keys[row] is not None:
            filename, _ = QFileDialog.getOpenFileName(
                self, "Replace BLOB from file", "", "All files (*.*)"
            )
            if not filename:
                return
            try:
                replacement = Path(filename).read_bytes()
            except OSError as exc:
                QMessageBox.warning(self, "Could not read file", str(exc))
                return
            answer = QMessageBox.question(
                self,
                "Replace BLOB?",
                f"Replace the {len(value):,}-byte BLOB in {self.current_table}."
                f"{self.page.columns[column]} with {len(replacement):,} bytes?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Save:
                return
            key = self.page.keys[row]
            try:
                backup = self.browser.update_cell(
                    self.current_table, self.page.columns[column], key[0], key[1], replacement
                )
            except (SQLiteBrowserError, sqlite3.Error, OSError) as exc:
                QMessageBox.critical(self, "BLOB replacement failed", str(exc))
                return
            self.status_label.setText(f"BLOB replaced. Backup: {backup}")
            self._refresh_current_table()
            return

        filename, _ = QFileDialog.getSaveFileName(
            self, "Export BLOB", f"{self.current_table}-{self.page.columns[column]}.bin",
            "Binary files (*.bin);;All files (*.*)",
        )
        if filename:
            try:
                Path(filename).write_bytes(value)
                self.status_label.setText(f"Exported {len(value):,} bytes to {filename}")
            except OSError as exc:
                QMessageBox.warning(self, "Could not export BLOB", str(exc))

    def _previous_page(self) -> None:
        self.page_offset = max(0, self.page_offset - self._page_size())
        self._refresh_current_table()

    def _next_page(self) -> None:
        if self.page is not None and self.page_offset + self._page_size() < self.page.total_rows:
            self.page_offset += self._page_size()
            self._refresh_current_table()

    def _page_size_changed(self, _index: int) -> None:
        self.page_offset = 0
        self._refresh_current_table()

    def _run_query(self) -> None:
        if self.browser is None:
            self.query_status.setText("Open a database first.")
            return
        try:
            result = self.browser.execute_select(self.sql_edit.toPlainText(), MAX_QUERY_ROWS)
        except SQLiteBrowserError as exc:
            self.query_status.setText(str(exc))
            return
        self.query_grid.setUpdatesEnabled(False)
        self.query_grid.clear()
        self.query_grid.setColumnCount(len(result.columns))
        self.query_grid.setHorizontalHeaderLabels(list(result.columns))
        self.query_grid.setRowCount(len(result.rows))
        for row_index, row in enumerate(result.rows):
            for column_index, value in enumerate(row):
                item = QTableWidgetItem(_display_value(value))
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if isinstance(value, bytes):
                    item.setToolTip("Binary BLOB value.")
                self.query_grid.setItem(row_index, column_index, item)
        self.query_grid.setUpdatesEnabled(True)
        self.query_grid.resizeColumnsToContents()
        suffix = " (truncated at 500 rows)" if result.truncated else ""
        self.query_status.setText(f"{len(result.rows):,} row(s) returned{suffix}.")

