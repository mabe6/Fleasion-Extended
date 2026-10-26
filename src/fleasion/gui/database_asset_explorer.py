"""Visual asset browser for Roblox assets embedded in an SQLite files table."""

from __future__ import annotations

import contextlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QImage, QPixmap, QResizeEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..database.roblox_cache_store import (
    RobloxCacheError,
    detect_roblox_payload_kind,
    split_rbxh_entry,
)
from ..database.sqlite_browser import SQLiteBrowser, SQLiteBrowserError, quote_identifier


_SAMPLE_BYTES = 2048
_MAX_PREVIEW_BYTES = 50 * 1024 * 1024
_MAX_ROWS = 100_000
_ASSET_ID_RE = re.compile(r'(?:[?&](?:id|assetId)=|/assets/)(\d+)', re.IGNORECASE)


@dataclass(frozen=True)
class _AssetItem:
    entry_id: str
    db_size: int
    content_size: int
    kind: str
    url: str
    asset_id: str
    category: int | None
    hits: int | None
    atime: int | None


_KIND_LABELS = {
    'anim': 'Animation',
    'curve-anim': 'Curve animation',
    'png': 'PNG image',
    'jpg': 'JPEG image',
    'bmp': 'BMP image',
    'gif': 'GIF image',
    'webp': 'WebP image',
    'tiff': 'TIFF image',
    'ico': 'Icon',
    'dds': 'DDS texture',
    'ktx': 'KTX texture',
    'psd': 'Photoshop image',
    'rbxm': 'Roblox model',
    'xml': 'Roblox XML',
    'mesh': 'Mesh',
    'font': 'Font',
    'ogg': 'OGG audio',
    'mp3': 'MP3 audio',
    'json': 'JSON',
    'other': 'Unknown format',
}
_IMAGE_KINDS = {'png', 'jpg', 'bmp', 'gif', 'webp', 'tiff', 'ico', 'dds', 'ktx', 'psd'}
_MODEL_KINDS = {'rbxm', 'xml', 'mesh'}
_AUDIO_KINDS = {'ogg', 'mp3'}


def _detect_kind(sample: bytes) -> tuple[str, str]:
    url, payload, has_header = split_rbxh_entry(sample)
    if not has_header:
        payload = sample
    kind = detect_roblox_payload_kind(payload)
    # Some XML assets contain animations but have no binary RBXM signature.
    if kind == 'xml' and b'CurveAnimation' in payload:
        kind = 'curve-anim'
    elif kind == 'xml' and b'KeyframeSequence' in payload:
        kind = 'anim'
    return kind, url


def _asset_id_from_url(url: str) -> str:
    match = _ASSET_ID_RE.search(url)
    if match is None:
        return ''
    return next((value for value in match.groups() if value), '')


def _kind_group(kind: str) -> str:
    if kind in {'anim', 'curve-anim'}:
        return 'Animations'
    if kind in _IMAGE_KINDS:
        return 'Images'
    if kind in _MODEL_KINDS:
        return 'Models'
    if kind in _AUDIO_KINDS:
        return 'Audio'
    if kind == 'font':
        return 'Fonts'
    return 'Other'


def _format_size(value: int) -> str:
    size = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if size < 1024 or unit == 'GiB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return str(value)


class DatabaseAssetExplorer(QWidget):
    """Read-only visual browser for the embedded Roblox files.content BLOBs."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.database_path: Path | None = None
        self.items: list[_AssetItem] = []
        self.items_by_id: dict[str, _AssetItem] = {}
        self._current_payload: bytes | None = None
        self._current_entry_id = ''
        self._current_image: QImage | None = None
        self._animation_panel: Any = None
        self._model_panel: Any = None
        self._scan_note = ''
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel('Find assets:'))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('Asset ID, cache hash, URL or format…')
        self.search_edit.textChanged.connect(self._render_items)
        toolbar.addWidget(self.search_edit, 1)

        toolbar.addWidget(QLabel('Type:'))
        self.kind_filter = QComboBox()
        self.kind_filter.addItem('All types', 'All')
        for label in ('Animations', 'Images', 'Models', 'Audio', 'Fonts', 'Other'):
            self.kind_filter.addItem(label, label)
        self.kind_filter.currentIndexChanged.connect(self._render_items)
        toolbar.addWidget(self.kind_filter)

        refresh_button = QPushButton('Refresh')
        refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(refresh_button)
        root.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.asset_table = QTableWidget(0, 5)
        self.asset_table.setHorizontalHeaderLabels(
            ['Type', 'Asset ID', 'Cache entry', 'Size', 'Category']
        )
        self.asset_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.asset_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.asset_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.asset_table.setAlternatingRowColors(True)
        self.asset_table.setSortingEnabled(False)
        self.asset_table.horizontalHeader().setStretchLastSection(True)
        self.asset_table.itemSelectionChanged.connect(self._selection_changed)
        splitter.addWidget(self.asset_table)

        preview = QWidget()
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(8, 0, 0, 0)
        self.preview_title = QLabel('Select an asset to preview')
        self.preview_title.setStyleSheet('font-weight: 600;')
        preview_layout.addWidget(self.preview_title)
        self.preview_details = QLabel(
            'Images and supported animations will appear here. '
            'The database only stores cache IDs/URLs and payload bytes, not always a friendly asset name.'
        )
        self.preview_details.setWordWrap(True)
        self.preview_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        preview_layout.addWidget(self.preview_details)

        self.preview_stack = QStackedWidget()
        self.info_preview = QLabel(
            'Choose an asset from the list.\n\n'
            'Use the type filter to show only animations, images, models, or audio.'
        )
        self.info_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.info_preview.setWordWrap(True)
        self.preview_stack.addWidget(self.info_preview)

        self.image_preview = QLabel()
        self.image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_preview.setMinimumSize(QSize(220, 180))
        self.preview_stack.addWidget(self.image_preview)

        self.preview_stack.setCurrentWidget(self.info_preview)
        preview_layout.addWidget(self.preview_stack, 1)

        actions = QHBoxLayout()
        self.copy_id_button = QPushButton('Copy cache ID')
        self.copy_id_button.setEnabled(False)
        self.copy_id_button.clicked.connect(self._copy_selected_id)
        actions.addWidget(self.copy_id_button)
        self.export_button = QPushButton('Export payload…')
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export_payload)
        actions.addWidget(self.export_button)
        actions.addStretch()
        preview_layout.addLayout(actions)
        splitter.addWidget(preview)
        splitter.setSizes([560, 520])
        root.addWidget(splitter, 1)

        self.status_label = QLabel(
            'Open an rbx-storage.db database to browse embedded assets. This view is read-only.'
        )
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

    def set_database(self, path: str | Path) -> bool:
        """Set the database and return whether it has the standard files.content schema."""
        self.database_path = Path(path).expanduser().resolve()
        self.items = []
        self.items_by_id = {}
        self._current_payload = None
        self._current_entry_id = ''
        self.asset_table.setRowCount(0)
        self._clear_preview()
        try:
            browser = SQLiteBrowser(self.database_path)
            tables = {table.name.casefold(): table for table in browser.tables()}
            if 'files' not in tables:
                self.status_label.setText('This database has no Roblox files table. Use Data or SQL for ordinary SQLite browsing.')
                return False
            columns, _ddl, kind = browser.schema(tables['files'].name)
            names = {str(column['name']).casefold() for column in columns}
            if kind != 'table' or not {'id', 'content', 'size'}.issubset(names):
                self.status_label.setText(
                    'This database has no compatible files.content BLOB column. '
                    'Older file-backed entries can be inspected from Cache Slots.'
                )
                return False
        except (SQLiteBrowserError, OSError) as exc:
            self.status_label.setText(str(exc))
            return False
        self.refresh()
        return True

    def refresh(self) -> None:
        if self.database_path is None:
            return
        try:
            browser = SQLiteBrowser(self.database_path)
            columns, _ddl, _kind = browser.schema('files')
            names = {str(column['name']).casefold() for column in columns}
            select = [
                f'hex({quote_identifier("id")}) AS entry_id',
                f'{quote_identifier("size")} AS db_size',
                f'length({quote_identifier("content")}) AS content_size',
                f'substr({quote_identifier("content")}, 1, ?) AS sample',
            ]
            params: list[Any] = [_SAMPLE_BYTES]
            for name in ('category', 'hits', 'atime'):
                if name in names:
                    select.append(f'{quote_identifier(name)} AS {name}')
                else:
                    select.append(f'NULL AS {name}')
            order = f' ORDER BY {quote_identifier("atime")} DESC' if 'atime' in names else ''
            with contextlib.closing(browser.connect_readonly()) as connection:
                total_row = connection.execute(
                    f'SELECT COUNT(*) FROM {quote_identifier("files")} '
                    f'WHERE {quote_identifier("content")} IS NOT NULL'
                ).fetchone()
                total = int(total_row[0] if total_row else 0)
                query = (
                    f'SELECT {", ".join(select)} FROM {quote_identifier("files")} '
                    f'WHERE {quote_identifier("content")} IS NOT NULL{order} LIMIT ?'
                )
                params.append(_MAX_ROWS)
                rows = connection.execute(query, params)
                loaded: list[_AssetItem] = []
                for row in rows:
                    sample = row['sample']
                    if isinstance(sample, memoryview):
                        sample = sample.tobytes()
                    if not isinstance(sample, bytes):
                        sample = b''
                    kind, url = _detect_kind(sample)
                    entry_id = str(row['entry_id'] or '').casefold()
                    asset_id = _asset_id_from_url(url)
                    loaded.append(
                        _AssetItem(
                            entry_id=entry_id,
                            db_size=int(row['db_size'] or 0),
                            content_size=int(row['content_size'] or 0),
                            kind=kind,
                            url=url,
                            asset_id=asset_id,
                            category=int(row['category']) if row['category'] is not None else None,
                            hits=int(row['hits']) if row['hits'] is not None else None,
                            atime=int(row['atime']) if row['atime'] is not None else None,
                        )
                    )
            self.items = loaded
            self.items_by_id = {item.entry_id: item for item in loaded}
            if total > _MAX_ROWS:
                self._scan_note = f'Loaded {_MAX_ROWS:,} of {total:,} embedded assets; older rows may be omitted.'
            else:
                self._scan_note = f'{total:,} embedded assets loaded.'
            self._render_items()
        except (SQLiteBrowserError, sqlite3.Error, OSError) as exc:
            self.items = []
            self.items_by_id = {}
            self._render_items()
            self._scan_note = f'Could not scan database assets: {exc}'
            self.status_label.setText(self._scan_note)

    def _visible_items(self) -> list[_AssetItem]:
        query = self.search_edit.text().strip().casefold()
        group = str(self.kind_filter.currentData() or 'All')
        result = []
        for item in self.items:
            if group != 'All' and _kind_group(item.kind) != group:
                continue
            searchable = ' '.join(
                (item.entry_id, item.asset_id, item.url, item.kind, _KIND_LABELS.get(item.kind, item.kind))
            ).casefold()
            if query and query not in searchable:
                continue
            result.append(item)
        return result

    def _render_items(self, *_args: Any) -> None:
        selected_id = self._current_entry_id
        visible = self._visible_items()
        self.asset_table.setUpdatesEnabled(False)
        self.asset_table.setRowCount(len(visible))
        selected_row = -1
        for row_index, item in enumerate(visible):
            display_id = item.asset_id or '—'
            short_id = item.entry_id[:12] + '…' if len(item.entry_id) > 12 else item.entry_id
            values = [
                _KIND_LABELS.get(item.kind, item.kind.upper()),
                display_id,
                short_id,
                _format_size(item.content_size),
                str(item.category) if item.category is not None else '—',
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(
                    f'URL: {item.url or "(not stored in an RBXH header)"}\n'
                    f'Cache entry ID: {item.entry_id}\n'
                    f'Format: {_KIND_LABELS.get(item.kind, item.kind)}\n'
                    f'Size in database: {_format_size(item.db_size)}'
                )
                cell.setData(Qt.ItemDataRole.UserRole, item.entry_id)
                cell.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.asset_table.setItem(row_index, column, cell)
            if item.entry_id == selected_id:
                selected_row = row_index
        self.asset_table.setUpdatesEnabled(True)
        self.asset_table.resizeColumnsToContents()
        self.asset_table.setColumnWidth(0, 145)
        self.asset_table.setColumnWidth(2, 130)
        if selected_row >= 0:
            self.asset_table.selectRow(selected_row)
        elif visible:
            self.asset_table.selectRow(0)
        else:
            self._current_entry_id = ''
            self._current_payload = None
            self.copy_id_button.setEnabled(False)
            self.export_button.setEnabled(False)
            self._clear_preview()
            self.preview_title.setText('No matching assets')
            self.preview_details.setText('Change the search text or type filter.')
        note = f'{len(visible):,} asset(s) shown from {len(self.items):,} loaded.'
        if self._scan_note:
            note += f'  {self._scan_note}'
        self.status_label.setText(note)

    def _selected_item(self) -> _AssetItem | None:
        row = self.asset_table.currentRow()
        if row < 0:
            return None
        cell = self.asset_table.item(row, 0)
        entry_id = str(cell.data(Qt.ItemDataRole.UserRole) or '') if cell else ''
        return self.items_by_id.get(entry_id)

    def _selection_changed(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        self._current_entry_id = item.entry_id
        self.copy_id_button.setEnabled(True)
        self.export_button.setEnabled(True)
        self.preview_title.setText(
            f'{_KIND_LABELS.get(item.kind, item.kind.upper())}'
            + (f' · Asset ID {item.asset_id}' if item.asset_id else '')
        )
        detail = [
            f'Cache ID: {item.entry_id}',
            f'Format: {_KIND_LABELS.get(item.kind, item.kind)}',
            f'Payload: {_format_size(item.content_size)}  ·  DB size: {_format_size(item.db_size)}',
        ]
        if item.url:
            detail.append(f'URL: {item.url}')
        if item.category is not None:
            detail.append(f'Category: {item.category}')
        if item.hits is not None:
            detail.append(f'Cache hits: {item.hits}')
        self.preview_details.setText('\n'.join(detail))
        self._load_preview(item)

    def _load_preview(self, item: _AssetItem) -> None:
        self._current_payload = None
        self._current_image = None
        if self._animation_panel is not None:
            self._animation_panel.clear_animation()
        try:
            if item.content_size > _MAX_PREVIEW_BYTES:
                self._clear_preview()
                self.info_preview.setText(
                    f'This payload is {_format_size(item.content_size)}. '
                    'Preview is skipped for safety; use Cache Slots to export or inspect it.'
                )
                self.preview_stack.setCurrentWidget(self.info_preview)
                return
            if self.database_path is None:
                return
            browser = SQLiteBrowser(self.database_path)
            with contextlib.closing(browser.connect_readonly()) as connection:
                row = connection.execute(
                    f'SELECT {quote_identifier("content")} FROM {quote_identifier("files")} '
                    f'WHERE {quote_identifier("id")} = ?',
                    (bytes.fromhex(item.entry_id),),
                ).fetchone()
            if row is None:
                raise SQLiteBrowserError('The selected cache row no longer exists.')
            data = row[0]
            if isinstance(data, memoryview):
                data = data.tobytes()
            if not isinstance(data, bytes):
                raise SQLiteBrowserError('The selected row has no embedded content BLOB.')
            _url, payload, _has_header = split_rbxh_entry(data)
            self._current_payload = payload
            if item.kind in _IMAGE_KINDS:
                image = QImage()
                if image.loadFromData(payload):
                    self._current_image = image
                    self._show_image()
                else:
                    self._show_info(
                        f'{_KIND_LABELS.get(item.kind, item.kind)} detected, but Qt cannot decode this texture format. '
                        'You can export the raw payload for a specialized viewer.'
                    )
            elif item.kind in {'anim', 'curve-anim'}:
                from ..cache.animation_viewer import AnimationViewerPanel

                if self._animation_panel is None:
                    self._animation_panel = AnimationViewerPanel()
                    self.preview_stack.addWidget(self._animation_panel)
                if self._animation_panel.load_animation(payload):
                    self.preview_stack.setCurrentWidget(self._animation_panel)
                else:
                    self._show_info('Animation data was detected, but no playable keyframes could be loaded.')
            elif item.kind in {'rbxm', 'xml'} and (
                payload.startswith(b'<roblox!') or payload.lstrip().startswith(b'<roblox')
            ):
                from ..cache.rbxm_preview import RbxmPreviewWidget

                if self._model_panel is None:
                    self._model_panel = RbxmPreviewWidget()
                    self._model_panel.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
                    self._model_panel.properties_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
                    self._model_panel.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
                    self._model_panel.properties_table.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
                    self._model_panel.add_child_btn.setEnabled(False)
                    self._model_panel.add_property_btn.setEnabled(False)
                    self.preview_stack.addWidget(self._model_panel)
                self._model_panel.load_bytes(payload, item.asset_id or item.entry_id[:12])
                self.preview_stack.setCurrentWidget(self._model_panel)
            else:
                self._show_info(
                    'No built-in preview is available for this format. '
                    'Use Export payload… to save the raw asset bytes.'
                )
        except (SQLiteBrowserError, RobloxCacheError, sqlite3.Error, OSError, ValueError) as exc:
            self._show_info(f'Preview failed: {exc}')

    def _show_image(self) -> None:
        if self._current_image is None:
            return
        self.preview_stack.setCurrentWidget(self.image_preview)
        self._scale_image()

    def _scale_image(self) -> None:
        if self._current_image is None:
            return
        target = self.image_preview.size()
        if target.width() < 20 or target.height() < 20:
            target = QSize(600, 420)
        pixmap = QPixmap.fromImage(self._current_image).scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_preview.setPixmap(pixmap)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self.preview_stack.currentWidget() is self.image_preview:
            self._scale_image()

    def _show_info(self, text: str) -> None:
        if self._animation_panel is not None:
            self._animation_panel.clear_animation()
        self.info_preview.setText(text)
        self.preview_stack.setCurrentWidget(self.info_preview)

    def _clear_preview(self) -> None:
        self._current_payload = None
        self._current_image = None
        self.image_preview.clear()
        if self._animation_panel is not None:
            self._animation_panel.clear_animation()
        self.preview_stack.setCurrentWidget(self.info_preview)
        self.info_preview.setText('Choose an asset from the list to preview it.')

    def _copy_selected_id(self) -> None:
        if self._current_entry_id:
            QApplication.clipboard().setText(self._current_entry_id)
            self.status_label.setText(f'Copied cache entry ID {self._current_entry_id}.')

    def _export_payload(self) -> None:
        item = self._selected_item()
        if item is None or self.database_path is None:
            return
        suffixes = {
            'anim': '.rbxm',
            'curve-anim': '.rbxm',
            'rbxm': '.rbxm',
            'xml': '.rbxmx',
            'png': '.png',
            'jpg': '.jpg',
            'bmp': '.bmp',
            'gif': '.gif',
            'webp': '.webp',
            'ogg': '.ogg',
            'mp3': '.mp3',
            'mesh': '.mesh',
        }
        default_name = (item.asset_id or item.entry_id) + suffixes.get(item.kind, '.bin')
        target, _filter = QFileDialog.getSaveFileName(self, 'Export raw payload', str(Path.home() / default_name))
        if not target:
            return
        try:
            if self._current_payload is None or self._current_entry_id != item.entry_id:
                self._load_preview(item)
            if self._current_payload is None:
                raise SQLiteBrowserError('Payload could not be read from the database.')
            Path(target).write_bytes(self._current_payload)
            self.status_label.setText(f'Exported raw payload to {target}')
        except (OSError, SQLiteBrowserError) as exc:
            QMessageBox.warning(self, 'Export failed', str(exc))
