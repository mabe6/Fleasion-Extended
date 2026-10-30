"""Offscreen UI tests for the simplified Local Database browser."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication

from fleasion.gui.local_database_editor import LocalDatabaseEditorWidget


_APP = None


def _qapp():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def test_local_database_browser_hides_sqlite_sidebar_and_keeps_advanced_tools(tmp_path: Path):
    app = _qapp()
    database_path = tmp_path / 'rbx-storage.db'
    asset_id = bytes.fromhex('01' * 16)
    payload = b'not-a-real-asset'
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            'CREATE TABLE files (id BLOB PRIMARY KEY, content BLOB, size INTEGER NOT NULL, atime INTEGER)'
        )
        connection.execute(
            'INSERT INTO files (id, content, size, atime) VALUES (?, ?, ?, ?)',
            (asset_id, payload, len(payload), 1),
        )
        connection.execute(
            'CREATE TABLE metadata (id INTEGER PRIMARY KEY, label TEXT NOT NULL)'
        )
        connection.execute(
            'INSERT INTO metadata (label) VALUES (?)',
            ('test row',),
        )

    widget = LocalDatabaseEditorWidget(
        config_manager=SimpleNamespace(settings={}),
        embedded=True,
    )
    try:
        widget._add_database(database_path, open_now=True)

        assert [widget.tabs.tabText(index) for index in range(widget.tabs.count())] == [
            'Data',
            'Schema',
            'SQL (read-only)',
        ]
        assert widget.advanced_tabs is widget.tabs
        assert not hasattr(widget, 'asset_explorer')
        assert widget.tabs.currentWidget() is widget.data_tab
        assert widget.database_combo.currentData() == str(database_path.resolve())
        assert widget.database_combo.count() == 1
        assert widget.database_list.isHidden()
        assert not hasattr(widget, 'table_list')
        assert widget.table_combo.currentData() == ('files', 'table')
        # The Asset Explorer is now the sole asset inventory; this tab is only SQLite.
        assert widget.tabs.currentWidget() is widget.data_tab
        metadata_index = next(
            index
            for index in range(widget.table_combo.count())
            if widget.table_combo.itemData(index) == ('metadata', 'table')
        )
        widget.table_combo.setCurrentIndex(metadata_index)
        assert widget.current_table == 'metadata'
        assert widget.data_grid.rowCount() == 1
    finally:
        widget.close()
        app.processEvents()
