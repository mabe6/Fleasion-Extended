"""Tests for the local database asset explorer."""

from __future__ import annotations

import os
import subprocess
import sys

from fleasion.gui.database_asset_explorer import (
    _asset_id_from_url,
    _detect_kind,
    _embedded_name,
    _kind_group,
)


def test_asset_kind_recognizes_readable_animation_and_image_formats() -> None:
    kind, url = _detect_kind(
        b'<roblox version="4"><Item class="KeyframeSequence"></Item></roblox>'
    )
    assert kind == 'anim'
    assert url == ''

    kind, url = _detect_kind(b'\x89PNG\r\n\x1a\n' + b'payload')
    assert kind == 'png'
    assert url == ''


def test_embedded_roblox_xml_name_is_readable() -> None:
    payload = (
        b'<roblox version="4"><Item class="KeyframeSequence"><Properties>'
        b'<string name="Name">wave</string></Properties></Item></roblox>'
    )
    assert _embedded_name(payload) == 'wave'


def test_asset_url_extraction_and_type_groups() -> None:
    assert _asset_id_from_url('https://www.roblox.com/asset/?id=123456') == '123456'
    assert _asset_id_from_url('https://example.invalid/assets/789') == '789'
    assert _kind_group('anim') == 'Animations'
    assert _kind_group('curve-anim') == 'Animations'
    assert _kind_group('png') == 'Images'
    assert _kind_group('other') == 'Other'


def test_asset_explorer_scans_filters_and_previews_database_rows_offscreen() -> None:
    code = r"""
import sqlite3
import tempfile
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from fleasion.gui.database_asset_explorer import DatabaseAssetExplorer

app = QApplication([])
with tempfile.TemporaryDirectory() as folder:
    path = Path(folder) / 'rbx-storage.db'
    payload = b'\x89PNG\r\n\x1a\n' + bytes(64)
    with sqlite3.connect(path) as connection:
        connection.execute(
            'CREATE TABLE files (id BLOB PRIMARY KEY, content BLOB, size INTEGER NOT NULL, category INTEGER, hits INTEGER, atime INTEGER)'
        )
        connection.execute(
            'INSERT INTO files (id, content, size, category, hits, atime) VALUES (?, ?, ?, ?, ?, ?)',
            (bytes.fromhex('01' * 16), payload, len(payload), 1, 3, 100),
        )
        connection.execute(
            'INSERT INTO files (id, content, size, category, hits, atime) VALUES (?, ?, ?, ?, ?, ?)',
            (bytes.fromhex('02' * 16), b'unknown-payload', 15, 2, 1, 50),
        )

    widget = DatabaseAssetExplorer()
    assert widget.set_database(path) is True
    assert len(widget.items) == 2
    assert widget.items_by_id['01' * 16].kind == 'png'
    widget._active_kinds = {'png'}
    widget._render_items()
    assert widget.asset_table.rowCount() == 1
    assert widget.asset_table.item(0, 4).text() == 'PNG image'
    assert widget.asset_table.horizontalHeaderItem(1).text() == 'Name / hash'
    assert widget.preview_panel.isHidden()
    widget.search_edit.setText('01010101')
    assert widget.asset_table.rowCount() == 1
    widget.search_edit.setText('no-such-asset')
    assert widget.asset_table.rowCount() == 0
    widget.close()
"""
    env = os.environ.copy()
    env['QT_QPA_PLATFORM'] = 'offscreen'
    subprocess.run([sys.executable, '-c', code], check=True, env=env)


def test_asset_explorer_reads_file_backed_entries_offscreen() -> None:
    code = r"""
import sqlite3
import struct
import tempfile
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from fleasion.gui.database_asset_explorer import DatabaseAssetExplorer

app = QApplication([])
with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    path = root / 'rbx-storage.db'
    url = b'https://assets.example/asset/?id=120350549417076'
    payload = b'<roblox version="4"><Item class="KeyframeSequence"></Item></roblox>'
    entry = b'RBXH' + bytes(4) + struct.pack('<I', len(url)) + url + bytes(25) + payload
    entry_id = 'ab' + '12' * 15
    physical = root / 'rbx-storage' / entry_id[:2] / entry_id
    physical.parent.mkdir(parents=True)
    physical.write_bytes(entry)
    with sqlite3.connect(path) as connection:
        connection.execute(
            'CREATE TABLE files (id BLOB PRIMARY KEY, content BLOB, size INTEGER NOT NULL, atime INTEGER)'
        )
        connection.execute(
            'INSERT INTO files (id, content, size, atime) VALUES (?, NULL, ?, 1)',
            (bytes.fromhex(entry_id), len(entry)),
        )
        # A metadata row with neither a BLOB nor a file must be skipped
        connection.execute(
            'INSERT INTO files (id, content, size, atime) VALUES (?, NULL, 10, 2)',
            (bytes.fromhex('cd' * 16),),
        )

    widget = DatabaseAssetExplorer()
    assert widget.set_database(path) is True
    assert list(widget.items_by_id) == [entry_id]
    item = widget.items_by_id[entry_id]
    assert item.kind == 'anim'
    assert item.asset_id == '120350549417076'
    assert item.content_size == len(entry)
    assert widget._read_entry_bytes(entry_id) == entry
    widget.close()
"""
    env = os.environ.copy()
    env['QT_QPA_PLATFORM'] = 'offscreen'
    subprocess.run([sys.executable, '-c', code], check=True, env=env)
