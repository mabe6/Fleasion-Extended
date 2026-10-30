"""Scraper-style visual browser for assets stored in a local Roblox SQLite database."""

from __future__ import annotations

import contextlib
import gzip
import html
import json
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, QSize, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap, QResizeEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from ..database.roblox_cache_store import (
    RobloxCacheError,
    RobloxCacheStore,
    decompress_roblox_payload_file,
    detect_roblox_payload_kind,
    extract_cached_http_response_asset_payload,
    format_csgphs_preview,
    format_cached_http_response_preview,
    is_roblox_compressed_payload,
    parse_cached_http_response,
    split_rbxh_entry,
)
from ..database.sqlite_browser import SQLiteBrowser, SQLiteBrowserError, quote_identifier
from ..utils.roblox_auth import get_roblosecurity as _get_roblosecurity


_SAMPLE_BYTES = 8192
_MAX_ROWS = 100_000
_NAME_RESOLUTION_BATCH = 50
_MAX_NAME_RESOLUTION = 20_000
_ASSET_ID_RE = re.compile(r'(?:[?&](?:id|assetId)=|/assets/)(\d+)', re.IGNORECASE)
_EMBEDDED_NAME_RE = re.compile(
    r'<(?:string|Content)\s+name=["\']Name["\']\s*>(.*?)</(?:string|Content)>',
    re.IGNORECASE | re.DOTALL,
)


@dataclass
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
    name: str = ''
    creator_name: str = ''
    created_at: str = ''
    updated_at: str = ''


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
    'rbxm': 'Roblox model (RBXM)',
    'xml': 'Roblox XML / model',
    'mesh': 'Mesh',
    'csgphs': 'Roblox collision mesh (CSGPHS)',
    'font': 'Font',
    'ogg': 'OGG audio',
    'mp3': 'MP3 audio',
    'json': 'JSON data',
    'asset-metadata': 'Roblox asset metadata',
    'roblox-settings': 'Roblox client settings (DCZ)',
    'video-metadata': 'Video resource metadata',
    'video-manifest': 'HLS video playlist (M3U8)',
    'http-response': 'Cached HTTP response',
    'compressed': 'Compressed payload (type determined on open)',
    'other': 'Unknown format',
}
_IMAGE_KINDS = {'png', 'jpg', 'bmp', 'gif', 'webp', 'tiff', 'ico', 'dds', 'ktx', 'psd'}
_MODEL_KINDS = {'rbxm', 'xml', 'mesh'}
_AUDIO_KINDS = {'ogg', 'mp3'}
_KIND_ORDER = (
    'anim',
    'curve-anim',
    'mesh',
    'csgphs',
    'rbxm',
    'xml',
    'png',
    'jpg',
    'bmp',
    'gif',
    'webp',
    'tiff',
    'ico',
    'dds',
    'ktx',
    'psd',
    'ogg',
    'mp3',
    'font',
    'json',
    'asset-metadata',
    'roblox-settings',
    'video-metadata',
    'video-manifest',
    'http-response',
    'compressed',
    'other',
)
_SEARCH_FIELD_LABELS = (
    ('name', 'Name'),
    ('creator', 'Creator'),
    ('asset_id', 'Asset ID'),
    ('type', 'Type'),
    ('hash', 'Cache entry / hash'),
    ('cached_at', 'Cached at'),
    ('url', 'URL'),
)
_DEFAULT_SEARCH_FIELDS = {'name', 'asset_id', 'type', 'hash', 'url'}


def _detect_kind(sample: bytes) -> tuple[str, str]:
    """Classify from the stored prefix without decompressing any cache payload."""
    url, payload, has_header = split_rbxh_entry(sample)
    if not has_header:
        payload = sample
    if is_roblox_compressed_payload(payload):
        return 'compressed', url
    kind = detect_roblox_payload_kind(payload, url=url)
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
    """Return a human-readable high-level group for a detected payload."""
    if kind in {'anim', 'curve-anim'}:
        return 'Animations'
    if kind == 'csgphs':
        return 'Physics'
    if kind in _IMAGE_KINDS:
        return 'Images'
    if kind in _MODEL_KINDS:
        return 'Models'
    if kind in _AUDIO_KINDS:
        return 'Audio'
    if kind == 'font':
        return 'Fonts'
    if kind in {'video-metadata', 'video-manifest'}:
        return 'Video'
    if kind == 'roblox-settings':
        return 'Settings'
    return 'Other'


def _format_size(value: int | None) -> str:
    if value is None:
        return '—'
    size = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if size < 1024 or unit == 'GiB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return str(value)


_SIZE_QUERY_RE = re.compile(r'^(<=|>=|<|>|=)?\s*(\d+(?:\.\d+)?)\s*(b|kb|kib|mb|mib|gb|gib)?$', re.I)
_SIZE_QUERY_MULTIPLIERS = {
    'b': 1, 'kb': 1000, 'kib': 1024, 'mb': 1000**2, 'mib': 1024**2,
    'gb': 1000**3, 'gib': 1024**3, '': 1,
}


def _size_query_matches(value: int | None, expression: str) -> bool:
    if value is None:
        return False
    match = _SIZE_QUERY_RE.fullmatch(expression.strip())
    if not match:
        return False
    operator, amount, unit = match.groups()
    target = int(float(amount) * _SIZE_QUERY_MULTIPLIERS[(unit or '').casefold()])
    operator = operator or '='
    return {
        '>': value > target,
        '>=': value >= target,
        '<': value < target,
        '<=': value <= target,
        '=': value == target,
    }[operator]


def _format_atime(value: int | None) -> str:
    if not value:
        return '—'
    try:
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
    except (OSError, OverflowError, TypeError, ValueError):
        return str(value)


def _embedded_name(payload: bytes) -> str:
    """Extract a Name property only when it is already present in the raw sample."""
    if not payload:
        return ''
    text = payload.decode('utf-8', errors='ignore')
    match = _EMBEDDED_NAME_RE.search(text)
    if match:
        value = html.unescape(match.group(1)).strip()
        if value and len(value) <= 160:
            return value
    return ''


def _display_name(item: _AssetItem) -> str:
    if item.name:
        return item.name
    label = _KIND_LABELS.get(item.kind, item.kind.upper())
    suffix = item.asset_id or item.entry_id[:12]
    return f'{label} · {suffix}'


class _CheckListPopup(QMenu):
    """Searchable, scrollable multi-select popup for long filter lists."""

    changed = pyqtSignal(set)

    def __init__(
        self,
        options: list[tuple[str, str]],
        selected: set[str],
        *,
        all_label: str = 'All',
        clear_label: str = 'Clear selection',
        parent=None,
    ):
        super().__init__(parent)
        self.active = set(selected)
        self.checkboxes: dict[str, QCheckBox] = {}
        self.setStyleSheet(
            'QMenu { background: palette(window); border: 1px solid palette(mid); }'
            'QCheckBox { padding: 2px 4px; }'
        )
        self.setMinimumWidth(260)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText('Filter types…')
        self.filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_edit)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setFixedHeight(250)
        checkbox_container = QWidget()
        checkbox_layout = QVBoxLayout(checkbox_container)
        checkbox_layout.setContentsMargins(0, 0, 0, 0)
        checkbox_layout.setSpacing(2)

        for key, label in options:
            checkbox = QCheckBox(label)
            checkbox.setChecked(key in self.active)
            checkbox.stateChanged.connect(
                lambda state, filter_key=key: self._toggle(filter_key, bool(state))
            )
            self.checkboxes[key] = checkbox
            checkbox_layout.addWidget(checkbox)
        checkbox_layout.addStretch(1)
        self.scroll_area.setWidget(checkbox_container)
        layout.addWidget(self.scroll_area)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(separator)

        buttons = QHBoxLayout()
        self.all_button = QPushButton(all_label)
        self.all_button.setFixedHeight(26)
        self.all_button.clicked.connect(self._select_all)
        self.clear_button = QPushButton(clear_label)
        self.clear_button.setFixedHeight(26)
        self.clear_button.clicked.connect(self._clear)
        buttons.addWidget(self.all_button)
        buttons.addWidget(self.clear_button)
        layout.addLayout(buttons)

        action = QWidgetAction(self)
        action.setDefaultWidget(container)
        self.addAction(action)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for checkbox in self.checkboxes.values():
            checkbox.setVisible(not needle or needle in checkbox.text().casefold())

    def _toggle(self, key: str, checked: bool) -> None:
        if checked:
            self.active.add(key)
        else:
            self.active.discard(key)
        self.changed.emit(set(self.active))

    def _select_all(self) -> None:
        self.active = set(self.checkboxes)
        for key, checkbox in self.checkboxes.items():
            checkbox.blockSignals(True)
            checkbox.setChecked(True)
            checkbox.blockSignals(False)
        self.changed.emit(set(self.active))

    def _clear(self) -> None:
        self.active.clear()
        for checkbox in self.checkboxes.values():
            checkbox.blockSignals(True)
            checkbox.setChecked(False)
            checkbox.blockSignals(False)
        self.changed.emit(set())


class _AssetNameResolver(QThread):
    """Resolve public Roblox asset names in the background, never on the UI thread."""

    names_ready = pyqtSignal(dict, list, bool)

    def __init__(self, asset_ids: list[str], parent=None):
        super().__init__(parent)
        self.asset_ids = list(dict.fromkeys(asset_ids))
        self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        found: dict[str, dict[str, str]] = {}
        failed = False
        if not self.asset_ids or self._stop_requested:
            self.names_ready.emit(found, self.asset_ids, False)
            return

        try:
            import requests

            session = requests.Session()
            session.trust_env = False
            session.proxies = {}
            session.headers.update(
                {
                    'User-Agent': 'Roblox/WinInet',
                    'Accept': 'application/json',
                    'Referer': 'https://www.roblox.com/',
                    'Origin': 'https://www.roblox.com',
                }
            )
            try:
                cookie = _get_roblosecurity()
                if cookie:
                    session.cookies.set('.ROBLOSECURITY', cookie, domain='.roblox.com')
            except Exception:
                # Names are optional; never make the local browser depend on browser-cookie access.
                pass

            for start in range(0, len(self.asset_ids), _NAME_RESOLUTION_BATCH):
                if self._stop_requested:
                    break
                batch = self.asset_ids[start:start + _NAME_RESOLUTION_BATCH]
                try:
                    response = session.get(
                        'https://develop.roblox.com/v1/assets',
                        params={'assetIds': ','.join(batch)},
                        timeout=3,
                    )
                    response.raise_for_status()
                    body = response.json()
                except Exception:
                    failed = True
                    break
                for entry in body.get('data', []):
                    asset_id = str(entry.get('id') or '')
                    name = str(entry.get('name') or '').strip()
                    if not asset_id or not name:
                        continue
                    creator = entry.get('creator') or {}
                    creator_name = ''
                    if isinstance(creator, dict):
                        creator_name = str(creator.get('name') or creator.get('displayName') or '')
                    if not creator_name and entry.get('creatorName'):
                        creator_name = str(entry.get('creatorName'))
                    found[asset_id] = {
                        'name': name,
                        'creator_name': creator_name,
                        'created_at': str(entry.get('created') or ''),
                        'updated_at': str(entry.get('updated') or ''),
                    }
                if start + _NAME_RESOLUTION_BATCH < len(self.asset_ids):
                    self.msleep(120)
        except Exception:
            # Many cache entries have no public asset ID or Roblox may reject name lookup.
            failed = True
        self.names_ready.emit(found, self.asset_ids, failed)



class _AssetPreviewWorker(QThread):
    """Read and decompress a selected database asset away from the GUI thread."""

    payload_ready = pyqtSignal(str, bytes, str)
    preview_failed = pyqtSignal(str, str)

    def __init__(self, database_path: Path, entry_id: str, parent=None):
        super().__init__(parent)
        self.database_path = database_path
        self.entry_id = entry_id

    def run(self) -> None:
        try:
            if self.isInterruptionRequested():
                return
            store = RobloxCacheStore(self.database_path.parent)
            with tempfile.TemporaryDirectory(prefix='fleasion-asset-preview-') as folder:
                raw_path = Path(folder) / 'payload.raw'
                decoded_path = Path(folder) / 'payload.decoded'
                source_url, _raw_size = store.copy_entry_payload_to_file(self.entry_id, raw_path)
                if self.isInterruptionRequested():
                    return
                decompress_roblox_payload_file(raw_path, decoded_path)
                if self.isInterruptionRequested():
                    return
                with decoded_path.open('rb') as handle:
                    prefix = handle.read(8192)
                kind = detect_roblox_payload_kind(prefix, url=source_url)
                # Animation names may appear after the RBXM/XML signature.
                payload = decoded_path.read_bytes()
                response_asset = extract_cached_http_response_asset_payload(
                    payload, url=source_url
                )
                if response_asset is not None:
                    payload, kind, _response_headers = response_asset
                if kind == 'xml' and b'CurveAnimation' in payload:
                    kind = 'curve-anim'
                elif kind == 'xml' and b'KeyframeSequence' in payload:
                    kind = 'anim'
                if self.isInterruptionRequested():
                    return
                self.payload_ready.emit(self.entry_id, payload, kind)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.preview_failed.emit(
                    self.entry_id, f'{type(exc).__name__}: {exc}'
                )


class DatabaseAssetExplorer(QWidget):
    """Scraper-style visual browser for Roblox cache entries (content BLOBs or rbx-storage files)."""

    def __init__(self, parent: QWidget | None = None, *, config_manager=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self.database_path: Path | None = None
        self.items: list[_AssetItem] = []
        self.items_by_id: dict[str, _AssetItem] = {}
        self._current_entry_id = ''
        self._current_image: QImage | None = None
        self._current_request_id = ''
        self._preview_workers: set[_AssetPreviewWorker] = set()
        self._decoder_workers: set[QThread] = set()
        self._preview_stopped = False
        self._preview_enabled = bool(self._settings().get('asset_database_preview_enabled', True))
        self._page_offset = 0
        self._scan_note = ''
        self._active_kinds = self._load_setting_set('local_db_asset_kinds', set()) & set(_KIND_ORDER)
        self._active_search_fields = self._load_setting_set(
            'local_db_search_fields', _DEFAULT_SEARCH_FIELDS
        )
        self._name_cache = self._load_name_cache()
        self._name_resolution_attempted: set[str] = set()
        self._name_resolution_inflight: set[str] = set()
        self._name_resolution_disabled = False
        self._name_resolver: _AssetNameResolver | None = None
        self._image_loader: QThread | None = None
        self._mesh_loader: QThread | None = None
        self._animation_panel: Any = None
        self._model_panel: Any = None
        self._model_preview_tabs: QTabWidget | None = None
        self._mesh_panel: Any = None
        self._font_widget: Any = None
        self._audio_player: Any = None
        self._build_ui()

        self._settings_save_timer = QTimer(self)
        self._settings_save_timer.setSingleShot(True)
        self._settings_save_timer.setInterval(350)
        self._settings_save_timer.timeout.connect(self._save_settings)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown_workers)

    def _settings(self) -> dict:
        settings = getattr(self.config_manager, 'settings', None)
        return settings if isinstance(settings, dict) else {}

    def _load_setting_set(self, key: str, default: set[str]) -> set[str]:
        saved = self._settings().get(key)
        if isinstance(saved, list):
            valid = {str(value) for value in saved}
            return valid
        return set(default)

    def _load_name_cache(self) -> dict[str, dict[str, str]]:
        raw = self._settings().get('local_db_asset_names', {})
        if not isinstance(raw, dict):
            return {}
        result: dict[str, dict[str, str]] = {}
        for asset_id, info in raw.items():
            if not isinstance(info, dict) or not info.get('name'):
                continue
            result[str(asset_id)] = {
                key: str(info.get(key) or '')
                for key in (
                    'name',
                    'creator_name',
                    'created_at',
                    'updated_at',
                    'description',
                    'creator_type',
                    'creator_id',
                    'asset_type',
                    'price',
                    'price_status',
                    'favorite_count',
                    'source',
                )
            }
        return result

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        filters_group = QGroupBox('Filters')
        filters_layout = QHBoxLayout(filters_group)
        filters_layout.setContentsMargins(8, 8, 8, 8)
        filters_layout.addWidget(QLabel('Search'))

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('Name, creator, asset ID, cache hash, type or URL…')
        self.search_edit.setText(str(self._settings().get('local_db_search_query', '') or ''))
        self.search_edit.textChanged.connect(self._filters_changed)
        filters_layout.addWidget(self.search_edit, 1)

        self.kind_filter_button = QPushButton('All types')
        self.kind_filter_button.clicked.connect(self._show_kind_filter)
        filters_layout.addWidget(self.kind_filter_button)

        self.search_fields_button = QPushButton('Search fields')
        self.search_fields_button.clicked.connect(self._show_search_fields)
        filters_layout.addWidget(self.search_fields_button)

        self.refresh_button = QPushButton('Refresh')
        self.refresh_button.clicked.connect(self.refresh)
        filters_layout.addWidget(self.refresh_button)

        self.stats_total_label = QLabel('0 assets')
        filters_layout.addWidget(self.stats_total_label)
        self.stats_size_label = QLabel('0 B')
        filters_layout.addWidget(self.stats_size_label)
        root.addWidget(filters_group)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        table_widget = QWidget()
        table_layout = QVBoxLayout(table_widget)
        table_layout.setContentsMargins(0, 0, 0, 0)

        self.asset_table = QTableWidget(0, 9)
        self.asset_table.setHorizontalHeaderLabels(
            ['#', 'Name / hash', 'Creator', 'Asset ID', 'Type', 'Size', 'Cached at', 'URL', 'Cache entry']
        )
        self.asset_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.asset_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.asset_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.asset_table.setAlternatingRowColors(True)
        self.asset_table.setSortingEnabled(False)
        self.asset_table.verticalHeader().hide()
        self.asset_table.horizontalHeader().setStretchLastSection(False)
        self.asset_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.asset_table.horizontalHeader().setSortIndicator(6, Qt.SortOrder.DescendingOrder)
        self.asset_table.setColumnWidth(0, 42)
        self.asset_table.setColumnWidth(1, 230)
        self.asset_table.setColumnWidth(2, 130)
        self.asset_table.setColumnWidth(3, 95)
        self.asset_table.setColumnWidth(4, 140)
        self.asset_table.setColumnWidth(5, 75)
        self.asset_table.setColumnWidth(6, 145)
        self.asset_table.setColumnHidden(2, True)
        self.asset_table.setColumnHidden(7, True)
        self.asset_table.setColumnHidden(8, True)
        self.asset_table.itemSelectionChanged.connect(self._selection_changed)
        self.asset_table.itemClicked.connect(self._asset_row_clicked)
        table_layout.addWidget(self.asset_table, 1)

        paging = QHBoxLayout()
        paging.addWidget(QLabel('Rows/page:'))
        self.page_size = QComboBox()
        for size in (100, 250, 500, 1000):
            self.page_size.addItem(str(size), size)
        page_size = int(self._settings().get('local_db_page_size', 250) or 250)
        index = self.page_size.findData(page_size)
        self.page_size.setCurrentIndex(index if index >= 0 else self.page_size.findData(250))
        self.page_size.currentIndexChanged.connect(self._page_size_changed)
        paging.addWidget(self.page_size)

        self.prev_page_button = QPushButton('← Previous')
        self.prev_page_button.clicked.connect(self._previous_page)
        paging.addWidget(self.prev_page_button)
        self.page_label = QLabel('Page 0 / 0')
        paging.addWidget(self.page_label)
        self.next_page_button = QPushButton('Next →')
        self.next_page_button.clicked.connect(self._next_page)
        paging.addWidget(self.next_page_button)
        paging.addStretch()
        table_layout.addLayout(paging)
        self.splitter.addWidget(table_widget)

        self.preview_panel = QWidget()
        preview_layout = QVBoxLayout(self.preview_panel)
        preview_layout.setContentsMargins(8, 0, 0, 0)
        preview_layout.setSpacing(5)

        preview_title_row = QHBoxLayout()
        self.preview_title = QLabel('Preview')
        self.preview_title.setStyleSheet('font-weight: 600;')
        preview_title_row.addWidget(self.preview_title, 1)
        self.stop_preview_button = QPushButton('Stop preview')
        self.stop_preview_button.setToolTip('Stop decoding the selected cache entry')
        self.stop_preview_button.setVisible(False)
        self.stop_preview_button.clicked.connect(self._stop_preview)
        preview_title_row.addWidget(self.stop_preview_button)
        preview_layout.addLayout(preview_title_row)

        self.preview_details = QLabel(
            'Select a row to preview an image, animation, mesh, Roblox model, audio, font or data payload.'
        )
        self.preview_details.setWordWrap(True)
        self.preview_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        preview_layout.addWidget(self.preview_details)

        self.preview_stack = QStackedWidget()
        self.info_preview = QLabel(
            'Select an asset to preview it.\n\n'
            'Use the type and search filters to find animations, meshes, images and other cached resources.'
        )
        self.info_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.info_preview.setWordWrap(True)
        self.preview_stack.addWidget(self.info_preview)

        self.image_preview = QLabel()
        self.image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_preview.setMinimumSize(QSize(240, 220))
        self.preview_stack.addWidget(self.image_preview)

        self.text_preview = QPlainTextEdit()
        self.text_preview.setReadOnly(True)
        self.text_preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.preview_stack.addWidget(self.text_preview)

        self.json_preview: Any = None
        self.audio_panel = QWidget()
        self.audio_layout = QVBoxLayout(self.audio_panel)
        self.audio_layout.setContentsMargins(0, 0, 0, 0)
        self.preview_stack.addWidget(self.audio_panel)

        self.font_panel = QWidget()
        self.font_layout = QVBoxLayout(self.font_panel)
        self.font_layout.setContentsMargins(0, 0, 0, 0)
        self.preview_stack.addWidget(self.font_panel)

        self.mesh_panel = QWidget()
        self.mesh_layout = QVBoxLayout(self.mesh_panel)
        self.mesh_layout.setContentsMargins(0, 0, 0, 0)
        self.preview_stack.addWidget(self.mesh_panel)

        preview_layout.addWidget(self.preview_stack, 1)

        actions = QHBoxLayout()
        self.copy_id_button = QPushButton('Copy cache ID')
        self.copy_id_button.setEnabled(False)
        self.copy_id_button.clicked.connect(self._copy_selected_id)
        actions.addWidget(self.copy_id_button)

        self.copy_asset_button = QPushButton('Copy asset ID')
        self.copy_asset_button.setEnabled(False)
        self.copy_asset_button.clicked.connect(self._copy_asset_id)
        actions.addWidget(self.copy_asset_button)

        self.export_button = QPushButton('Export payload…')
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export_payload)
        actions.addWidget(self.export_button)
        actions.addStretch()
        root.addLayout(actions)

        self.splitter.addWidget(self.preview_panel)
        self.splitter.setSizes([650, 300])
        self.preview_panel.setMinimumWidth(240)
        if not self._preview_enabled:
            self.preview_panel.hide()
        root.addWidget(self.splitter, 1)

        self.status_label = QLabel(
            'Open an rbx-storage.db database to browse embedded assets. This view does not modify the database.'
        )
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self._update_filter_buttons()

    def _show_kind_filter(self) -> None:
        options = [(kind, _KIND_LABELS.get(kind, kind)) for kind in _KIND_ORDER]
        popup = _CheckListPopup(options, self._active_kinds, parent=self)
        popup.changed.connect(self._on_kind_filters_changed)
        popup.aboutToHide.connect(popup.deleteLater)
        popup.exec(self.kind_filter_button.mapToGlobal(self.kind_filter_button.rect().bottomLeft()))

    def _show_search_fields(self) -> None:
        popup = _CheckListPopup(
            list(_SEARCH_FIELD_LABELS),
            self._active_search_fields,
            all_label='All',
            clear_label='None',
            parent=self,
        )
        popup.changed.connect(self._on_search_fields_changed)
        popup.aboutToHide.connect(popup.deleteLater)
        popup.exec(self.search_fields_button.mapToGlobal(self.search_fields_button.rect().bottomLeft()))

    def _on_kind_filters_changed(self, kinds: set[str]) -> None:
        self._active_kinds = set(kinds)
        self._update_filter_buttons()
        self._persist_settings_later()
        self._page_offset = 0
        self._render_items()

    def _on_search_fields_changed(self, fields: set[str]) -> None:
        self._active_search_fields = set(fields)
        self._update_filter_buttons()
        self._persist_settings_later()
        self._page_offset = 0
        self._render_items()

    def _update_filter_buttons(self) -> None:
        if not self._active_kinds or self._active_kinds.issuperset(_KIND_ORDER):
            kind_label = 'All types'
        elif len(self._active_kinds) == 1:
            kind_label = _KIND_LABELS.get(next(iter(self._active_kinds)), '1 type')
        else:
            kind_label = f'{len(self._active_kinds)} types'
        self.kind_filter_button.setText(kind_label)

        if self._active_search_fields >= {key for key, _label in _SEARCH_FIELD_LABELS}:
            self.search_fields_button.setText('Search fields: All')
        elif not self._active_search_fields:
            self.search_fields_button.setText('Search fields: None')
        else:
            self.search_fields_button.setText(f'Search fields: {len(self._active_search_fields)}')

    def _persist_settings_later(self) -> None:
        self._settings_save_timer.start()

    def _save_settings(self) -> None:
        if self.config_manager is None:
            return
        settings = getattr(self.config_manager, 'settings', None)
        if not isinstance(settings, dict):
            return
        settings['local_db_asset_kinds'] = sorted(self._active_kinds)
        settings['local_db_search_fields'] = sorted(self._active_search_fields)
        settings['local_db_search_query'] = self.search_edit.text()
        settings['local_db_page_size'] = self._page_size()
        settings['local_db_asset_names'] = dict(list(self._name_cache.items())[-5000:])
        try:
            self.config_manager.save()
        except Exception:
            pass

    def set_database(self, path: str | Path) -> bool:
        """Set the database and return whether it has the standard files.content schema."""
        self.database_path = Path(path).expanduser().resolve()
        self.items = []
        self.items_by_id = {}
        self._current_entry_id = ''
        self._scan_note = ''
        self.copy_id_button.setEnabled(False)
        self.copy_asset_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.asset_table.setRowCount(0)
        self._clear_preview()
        try:
            browser = SQLiteBrowser(self.database_path)
            tables = {table.name.casefold(): table for table in browser.tables()}
            if 'files' not in tables:
                self._scan_note = (
                    'This database has no Roblox files table. Use Data or SQL for ordinary SQLite browsing.'
                )
                self.status_label.setText(self._scan_note)
                return False
            columns, _ddl, kind = browser.schema(tables['files'].name)
            names = {str(column['name']).casefold() for column in columns}
            file_backed = self._assets_path().is_dir()
            if (
                kind != 'table'
                or not {'id', 'size'}.issubset(names)
                or ('content' not in names and not file_backed)
            ):
                self._scan_note = (
                    'This database has no files.content BLOB column and no rbx-storage '
                    'folder beside it, so there are no entries to read.'
                )
                self.status_label.setText(self._scan_note)
                return False
        except (SQLiteBrowserError, OSError) as exc:
            self.status_label.setText(str(exc))
            return False
        self.refresh()
        return True

    def _assets_path(self) -> Path:
        """Return the external rbx-storage folder that sits beside the database."""
        if self.database_path is None:
            return Path()
        return self.database_path.parent / 'rbx-storage'

    def _read_entry_bytes(self, entry_id: str) -> bytes:
        """Read a cache entry from its content BLOB or, failing that, its physical file."""
        if self.database_path is None:
            raise SQLiteBrowserError('No database is open.')
        browser = SQLiteBrowser(self.database_path)
        columns, _ddl, _kind = browser.schema('files')
        names = {str(column['name']).casefold() for column in columns}
        with contextlib.closing(browser.connect_readonly()) as connection:
            select = quote_identifier('content') if 'content' in names else 'NULL'
            row = connection.execute(
                f'SELECT {select} FROM {quote_identifier("files")} '
                f'WHERE {quote_identifier("id")} = ?',
                (bytes.fromhex(entry_id),),
            ).fetchone()
        if row is None:
            raise SQLiteBrowserError('The selected cache row no longer exists.')
        data = row[0]
        if isinstance(data, memoryview):
            data = data.tobytes()
        if isinstance(data, bytes):
            return data
        physical = self._assets_path() / entry_id[:2] / entry_id
        if not physical.is_file():
            raise SQLiteBrowserError(
                'The selected row has no content BLOB and no cache file was found at '
                f'{physical}.'
            )
        return physical.read_bytes()

    def refresh(self) -> None:
        if self.database_path is None:
            return
        try:
            browser = SQLiteBrowser(self.database_path)
            columns, _ddl, _kind = browser.schema('files')
            names = {str(column['name']).casefold() for column in columns}
            has_content = 'content' in names
            select = [
                f'hex({quote_identifier("id")}) AS entry_id',
                f'{quote_identifier("size")} AS db_size',
            ]
            params: list[Any] = []
            if has_content:
                select.append(f'length({quote_identifier("content")}) AS content_size')
                select.append(f'substr({quote_identifier("content")}, 1, ?) AS sample')
                params.append(_SAMPLE_BYTES)
            else:
                select.append('NULL AS content_size')
                select.append('NULL AS sample')
            for name in ('category', 'hits', 'atime'):
                if name in names:
                    select.append(f'{quote_identifier(name)} AS {name}')
                else:
                    select.append(f'NULL AS {name}')
            order = f' ORDER BY {quote_identifier("atime")} DESC' if 'atime' in names else ''
            assets_path = self._assets_path()
            with contextlib.closing(browser.connect_readonly()) as connection:
                # Rows may hold their bytes in a BLOB or in a file under rbx-storage
                query = f'SELECT {", ".join(select)} FROM {quote_identifier("files")}{order}'
                rows = connection.execute(query, params)
                loaded: list[_AssetItem] = []
                total = 0
                total_size = 0
                for row in rows:
                    entry_id = str(row['entry_id'] or '').casefold()
                    sample = row['sample']
                    if isinstance(sample, memoryview):
                        sample = sample.tobytes()
                    content_size = int(row['content_size'] or 0)
                    if not isinstance(sample, bytes):
                        # No BLOB, so fall back to the physical cache file
                        sample = b''
                        content_size = 0
                        physical = assets_path / entry_id[:2] / entry_id
                        try:
                            if physical.is_file():
                                content_size = physical.stat().st_size
                                with physical.open('rb') as handle:
                                    sample = handle.read(_SAMPLE_BYTES)
                        except OSError:
                            sample = b''
                            content_size = 0
                        if not content_size:
                            continue
                    total += 1
                    total_size += content_size
                    if len(loaded) >= _MAX_ROWS:
                        continue
                    kind, url = _detect_kind(sample)
                    asset_id = _asset_id_from_url(url)
                    cached_meta = self._name_cache.get(asset_id, {})
                    embedded_name = _embedded_name(sample)
                    loaded.append(
                        _AssetItem(
                            entry_id=entry_id,
                            db_size=int(row['db_size'] or 0),
                            content_size=content_size,
                            kind=kind,
                            url=url,
                            asset_id=asset_id,
                            category=int(row['category']) if row['category'] is not None else None,
                            hits=int(row['hits']) if row['hits'] is not None else None,
                            atime=int(row['atime']) if row['atime'] is not None else None,
                            name=str(cached_meta.get('name') or embedded_name or ''),
                            creator_name=str(cached_meta.get('creator_name') or ''),
                            created_at=str(cached_meta.get('created_at') or ''),
                            updated_at=str(cached_meta.get('updated_at') or ''),
                        )
                    )
            self.items = loaded
            self.items_by_id = {item.entry_id: item for item in loaded}
            if total > _MAX_ROWS:
                self._scan_note = f'Loaded {_MAX_ROWS:,} of {total:,} cached assets; older rows may be omitted.'
            else:
                self._scan_note = f'{total:,} cached assets loaded.'
            self.stats_total_label.setText(f'{total:,} assets')
            self.stats_size_label.setText(_format_size(total_size))
            self._page_offset = 0
            self._render_items()
        except (SQLiteBrowserError, sqlite3.Error, OSError) as exc:
            self.items = []
            self.items_by_id = {}
            self._scan_note = f'Could not scan database assets: {exc}'
            self._render_items()

    def _filters_changed(self, *_args: Any) -> None:
        self._page_offset = 0
        self._persist_settings_later()
        self._render_items()

    def _page_size_changed(self, *_args: Any) -> None:
        self._page_offset = 0
        self._persist_settings_later()
        self._render_items()

    def _previous_page(self) -> None:
        self._page_offset = max(0, self._page_offset - self._page_size())
        self._render_items()

    def _next_page(self) -> None:
        self._page_offset += self._page_size()
        self._render_items()

    def _page_size(self) -> int:
        return int(self.page_size.currentData() or 250)

    def _visible_items(self) -> list[_AssetItem]:
        raw_query = self.search_edit.text().strip()
        token_re = re.compile(r'(?P<key>[A-Za-z]+):(?P<value>"[^"]+"|\S+)')
        structured: list[tuple[str, str]] = []
        residue = raw_query
        supported = {'name', 'creator', 'type', 'size', 'dbsize', 'id', 'asset', 'url', 'hash', 'cached_at'}
        for match in token_re.finditer(raw_query):
            key = match.group('key').casefold()
            if key in supported:
                structured.append((key, match.group('value').strip('"')))
                residue = residue.replace(match.group(0), ' ', 1)
        query = ' '.join(residue.split()).casefold()
        result = []
        for item in self.items:
            if (
                self._active_kinds
                and not self._active_kinds.issuperset(_KIND_ORDER)
                and item.kind not in self._active_kinds
            ):
                continue

            fields = {
                'name': _display_name(item),
                'creator': item.creator_name or '',
                'asset': item.asset_id or '',
                'asset_id': item.asset_id or '',
                'type': _KIND_LABELS.get(item.kind, item.kind),
                'hash': item.entry_id,
                'id': item.entry_id,
                'cached_at': _format_atime(item.atime),
                'url': item.url or '',
            }
            matched = True
            for key, expected in structured:
                needle = expected.casefold()
                if key == 'size':
                    matched = _size_query_matches(item.content_size, expected)
                elif key == 'dbsize':
                    matched = _size_query_matches(item.db_size, expected)
                else:
                    field_value = str(fields.get(key, '')).casefold()
                    matched = needle in field_value
                    if key == 'type' and not matched:
                        matched = needle in item.kind.casefold()
                if not matched:
                    break
            if not matched:
                continue

            if query:
                if not self._active_search_fields:
                    continue
                searchable = ' '.join(
                    str(fields[field]) for field in self._active_search_fields if field in fields
                ).casefold()
                # Include local name/hash/type/URL metadata by default when the search field
                # chooser has no usable selection.
                if query not in searchable:
                    continue
            result.append(item)
        return result

    def _render_items(self, *_args: Any) -> None:
        selected_id = self._current_entry_id
        visible = self._visible_items()
        page_size = self._page_size()
        page_count = (len(visible) + page_size - 1) // page_size
        if page_count:
            self._page_offset = min(self._page_offset, (page_count - 1) * page_size)
            current_page = self._page_offset // page_size + 1
        else:
            self._page_offset = 0
            current_page = 0
        page_items = visible[self._page_offset:self._page_offset + page_size]
        self.prev_page_button.setEnabled(self._page_offset > 0)
        self.next_page_button.setEnabled(self._page_offset + len(page_items) < len(visible))
        self.page_label.setText(f'Page {current_page} / {page_count}' if page_count else 'Page 0 / 0')

        self.asset_table.setSortingEnabled(False)
        self.asset_table.setUpdatesEnabled(False)
        self.asset_table.blockSignals(True)
        self.asset_table.clearSelection()
        self.asset_table.setCurrentCell(-1, -1)
        self.asset_table.setRowCount(len(page_items))
        selected_row = -1
        for row_index, item in enumerate(page_items):
            display_name = _display_name(item)
            short_id = item.asset_id or (item.entry_id[:12] + '…')
            values = [
                str(self._page_offset + row_index + 1),
                display_name,
                item.creator_name or '—',
                item.asset_id or '—',
                _KIND_LABELS.get(item.kind, item.kind.upper()),
                _format_size(item.content_size),
                _format_atime(item.atime),
                item.url or '—',
                item.entry_id,
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(
                    f'Name: {display_name}\n'
                    f'Creator: {item.creator_name or "—"}\n'
                    f'Asset ID: {item.asset_id or "not in cache URL"}\n'
                    f'Type: {_KIND_LABELS.get(item.kind, item.kind)}\n'
                    f'Payload size: {_format_size(item.content_size)}\n'
                    f'Database size: {_format_size(item.db_size)}\n'
                    f'Cache entry ID: {item.entry_id}\n'
                    f'URL: {item.url or "not stored in RBXH header"}'
                )
                cell.setData(Qt.ItemDataRole.UserRole, item.entry_id)
                cell.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.asset_table.setItem(row_index, column, cell)
            if item.entry_id == selected_id:
                selected_row = row_index

        self.asset_table.blockSignals(False)
        self.asset_table.setUpdatesEnabled(True)
        self.asset_table.setSortingEnabled(True)
        self.asset_table.resizeRowsToContents()

        if selected_id:
            selected_row = next(
                (
                    row for row in range(self.asset_table.rowCount())
                    if self.asset_table.item(row, 0) is not None
                    and self.asset_table.item(row, 0).data(Qt.ItemDataRole.UserRole) == selected_id
                ),
                -1,
            )
        if selected_row >= 0:
            self.asset_table.selectRow(selected_row)
        else:
            self._current_entry_id = ''
            self.copy_id_button.setEnabled(False)
            self.copy_asset_button.setEnabled(False)
            self.export_button.setEnabled(False)
            self._reset_preview_widgets()
            if self._preview_enabled:
                self.preview_panel.show()
                self.preview_title.setText('Select an asset')
                self.preview_details.setText(
                    'Choose a row to load its preview. Search and type filters continue to work while this panel stays open.'
                    if page_items else 'No matching assets. Change the search text or type filter.'
                )
            else:
                self.preview_panel.hide()
                if not page_items:
                    self.preview_title.setText('No matching assets')
                    self.preview_details.setText('Change the search text or type filter.')

        shown_start = self._page_offset + 1 if page_items else 0
        shown_end = self._page_offset + len(page_items)
        note = (
            f'Showing {shown_start:,}–{shown_end:,} of {len(visible):,} matching assets '
            f'({len(self.items):,} loaded).'
        )
        if self._scan_note:
            note += f'  {self._scan_note}'
        self.status_label.setText(note)
        self._resolve_visible_names(page_items)

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
            self.stop_preview_button.setVisible(False)
            self._reset_preview_widgets()
            return
        was_hidden = self.preview_panel.isHidden()
        self.preview_panel.show()
        if was_hidden:
            QTimer.singleShot(0, lambda: self.splitter.setSizes([650, 300]))

        self._current_entry_id = item.entry_id
        self._current_request_id = item.entry_id
        self._preview_stopped = False
        self.preview_title.show()
        self.preview_details.show()
        self.preview_stack.show()
        self.copy_id_button.show()
        self.copy_asset_button.show()
        self.export_button.show()
        self.stop_preview_button.setVisible(self._preview_enabled)
        self.copy_id_button.setEnabled(True)
        self.copy_asset_button.setEnabled(bool(item.asset_id))
        self.export_button.setEnabled(True)

        name = _display_name(item)
        self.preview_title.setText(name)
        if item.asset_id:
            asset_id_label = item.asset_id
        elif item.kind == 'roblox-settings':
            asset_id_label = 'not applicable (client settings resource)'
        elif item.kind in {'video-metadata', 'video-manifest'}:
            asset_id_label = 'not applicable (video resource)'
        elif item.kind == 'http-response':
            asset_id_label = 'not applicable (cached HTTP response)'
        elif item.kind == 'asset-metadata':
            asset_id_label = 'not included in the type marker'
        else:
            asset_id_label = 'not embedded in cache URL'
        detail = [
            f'Asset ID: {asset_id_label}',
            f'Type: {_KIND_LABELS.get(item.kind, item.kind)}',
            f'Payload: {_format_size(item.content_size)}  ·  DB size: {_format_size(item.db_size)}',
            f'Cache entry: {item.entry_id}',
        ]
        if item.creator_name:
            detail.append(f'Creator: {item.creator_name}')
        if item.url:
            detail.append(f'URL: {item.url}')
        if item.category is not None:
            detail.append(f'Database category: {item.category}')
        if item.hits is not None:
            detail.append(f'Cache hits: {item.hits}')
        detail.append(f'Last accessed: {_format_atime(item.atime)}')
        self.preview_details.setText('\n'.join(detail))
        if not self._preview_enabled:
            self.stop_preview_button.setVisible(False)
            self.preview_panel.hide()
            self._reset_preview_widgets()
            return
        self._load_preview(item)

    def set_preview_enabled(self, enabled: bool) -> None:
        """Enable or hide the asset preview panel and persist the preference."""
        self._preview_enabled = bool(enabled)
        settings = self._settings()
        settings['asset_database_preview_enabled'] = self._preview_enabled
        save = getattr(self.config_manager, 'save', None)
        if callable(save):
            save()

        if not self._preview_enabled:
            self.stop_preview_button.setVisible(False)
            self.preview_panel.hide()
            self._reset_preview_widgets()
            return

        if self._selected_item() is not None:
            self._selection_changed()
        else:
            self.preview_panel.hide()

    def _track_decoder_worker(self, attribute: str, worker: QThread) -> None:
        setattr(self, attribute, worker)
        self._decoder_workers.add(worker)
        worker.finished.connect(
            lambda current=worker, attr=attribute: self._decoder_worker_finished(attr, current)
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _decoder_worker_finished(self, attribute: str, worker: QThread) -> None:
        self._decoder_workers.discard(worker)
        if getattr(self, attribute, None) is worker:
            setattr(self, attribute, None)

    def _stop_loader(self, attribute: str) -> None:
        worker = getattr(self, attribute, None)
        if worker is None:
            return
        try:
            stop = getattr(worker, 'stop', None)
            if callable(stop):
                stop()
            worker.requestInterruption()
        except RuntimeError:
            pass
        setattr(self, attribute, None)

    def _reset_preview_widgets(self) -> None:
        self._current_request_id = ''
        for worker in tuple(self._preview_workers):
            worker.requestInterruption()
        self._stop_loader('_image_loader')
        self._stop_loader('_mesh_loader')

        if self._animation_panel is not None:
            try:
                self._animation_panel.stop()
                self._animation_panel.clear_animation()
                self._animation_panel.hide()
            except Exception:
                pass
        if self._model_panel is not None:
            try:
                self._model_panel.clear()
                self._model_panel.hide()
            except Exception:
                pass
        if self._model_preview_tabs is not None:
            try:
                self._model_preview_tabs.setCurrentIndex(0)
                self._model_preview_tabs.hide()
            except Exception:
                pass
        if self._mesh_panel is not None:
            try:
                self._mesh_panel.clear()
                self._mesh_panel.hide()
            except Exception:
                pass
        if self._audio_player is not None:
            try:
                self._audio_player.stop()
                self._audio_player.deleteLater()
            except Exception:
                pass
            self._audio_player = None
        if self.json_preview is not None:
            try:
                self.json_preview.hide()
            except Exception:
                pass
        for layout in (self.audio_layout, self.font_layout):
            while layout.count():
                child = layout.takeAt(0)
                if child.widget():
                    child.widget().deleteLater()
        self._font_widget = None
        self.image_preview.clear()
        self.text_preview.clear()
        self._current_image = None
        self.preview_stack.setCurrentWidget(self.info_preview)

    def _asset_row_clicked(self, item) -> None:
        """Allow the currently selected entry to reopen after Stop preview."""
        del item
        if not self._preview_stopped or not self._preview_enabled:
            return
        selected = self._selected_item()
        if selected is None:
            return
        self._preview_stopped = False
        self.stop_preview_button.setVisible(True)
        self._load_preview(selected)

    def _stop_preview(self) -> None:
        """Cancel preview work and collapse the entire preview pane."""
        self._preview_stopped = True
        self.stop_preview_button.setVisible(False)
        self._current_request_id = ''
        self._reset_preview_widgets()
        self.preview_panel.hide()

    def _load_preview(self, item: _AssetItem) -> None:
        self.preview_panel.show()
        self.preview_title.show()
        self.preview_details.show()
        self.preview_stack.show()
        self.copy_id_button.show()
        self.copy_asset_button.show()
        self.export_button.show()
        self._reset_preview_widgets()
        request_id = item.entry_id
        self._current_request_id = request_id
        if self.database_path is None:
            return
        self._show_info('Reading cache entry and decoding preview…')
        worker = _AssetPreviewWorker(self.database_path, request_id, self)
        self._preview_workers.add(worker)
        worker.payload_ready.connect(self._on_preview_payload_ready)
        worker.preview_failed.connect(self._on_preview_error)
        worker.finished.connect(
            lambda current=worker: self._preview_workers.discard(current)
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_preview_payload_ready(
        self, request_id: str, payload: bytes, kind: str
    ) -> None:
        if request_id != self._current_request_id:
            return
        item = self.items_by_id.get(request_id)
        if item is None:
            return
        if item.kind in {'compressed', 'other'} and kind != 'other':
            item.kind = kind
            row = self.asset_table.currentRow()
            if row >= 0 and self.asset_table.item(row, 4) is not None:
                self.asset_table.item(row, 4).setText(
                    _KIND_LABELS.get(item.kind, item.kind.upper())
                )
        try:
            if item.kind in _IMAGE_KINDS:
                if item.kind == 'ktx':
                    from ..cache.tools.ktx_to_png.ktx_to_png import inspect_texture_metadata

                    texture = inspect_texture_metadata(payload)
                    if texture:
                        detail_lines = [
                            f'Texture: {texture["container"]} · {texture["width"]}×{texture["height"]}',
                            f'Layers: {texture["layers"]} · Faces: {texture["faces"]} · Mip levels: {texture["mip_levels"]}',
                            f'Format: {texture["format"]}',
                        ]
                        self.preview_details.setText(
                            self.preview_details.text() + '\n' + '\n'.join(detail_lines)
                            + '\nPreview displays the base mip level and first layer/face.'
                        )
                from ..cache.cache_viewer import ImageLoaderThread

                image_loader = ImageLoaderThread(payload)
                image_loader.image_ready.connect(
                    lambda rgba, width, height, rid=request_id: self._on_image_ready(
                        rid, rgba, width, height
                    )
                )
                image_loader.error.connect(
                    lambda error, rid=request_id: self._on_preview_error(rid, error)
                )
                self._track_decoder_worker('_image_loader', image_loader)
                self.info_preview.setText('Loading image preview…')
                self.preview_stack.setCurrentWidget(self.info_preview)
            elif item.kind in {'anim', 'curve-anim', 'rbxm', 'xml'} and (
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
                if self._model_preview_tabs is None:
                    self._model_preview_tabs = QTabWidget()
                    self._model_preview_tabs.addTab(self._model_panel, 'Structure')
                    self.preview_stack.addWidget(self._model_preview_tabs)

                self._model_panel.load_bytes(payload, item.asset_id or item.entry_id[:12])
                self._model_panel.show()
                animation_paths = self._model_panel.animation_paths()
                if animation_paths:
                    shown_paths = animation_paths[:40]
                    suffix = (
                        f'\n… and {len(animation_paths) - len(shown_paths)} more'
                        if len(animation_paths) > len(shown_paths) else ''
                    )
                    self.preview_details.setText(
                        self.preview_details.text()
                        + '\nAnimations: ' + '\n• '.join(shown_paths) + suffix
                    )

                # Keep the structure tree available for animation assets as well as playback.
                if item.kind in {'anim', 'curve-anim'} or animation_paths:
                    from ..cache.animation_viewer import AnimationViewerPanel

                    if self._animation_panel is None:
                        self._animation_panel = AnimationViewerPanel(
                            config_manager=self.config_manager
                        )
                    loaded = self._animation_panel.load_animation(payload)
                    tab_index = self._model_preview_tabs.indexOf(self._animation_panel)
                    if loaded and tab_index < 0:
                        self._model_preview_tabs.addTab(self._animation_panel, 'Playback')
                    if loaded:
                        self._animation_panel.show()
                    elif not loaded and tab_index >= 0:
                        self._model_preview_tabs.removeTab(tab_index)
                    if not loaded:
                        self.preview_details.setText(
                            self.preview_details.text()
                            + '\nPlayback decoder could not load keyframes; the full structure remains available.'
                        )
                elif self._animation_panel is not None:
                    tab_index = self._model_preview_tabs.indexOf(self._animation_panel)
                    if tab_index >= 0:
                        self._model_preview_tabs.removeTab(tab_index)

                if self._model_panel.document and self._model_panel.document.roots:
                    root_name = self._model_panel.document.roots[0].name.strip()
                    generic_names = {'folder', 'model', 'workspace', 'datamodel', 'instance'}
                    if root_name and root_name.casefold() not in generic_names and not item.name:
                        item.name = root_name
                        self.preview_title.setText(root_name)
                        current_row = self.asset_table.currentRow()
                        name_cell = self.asset_table.item(current_row, 1) if current_row >= 0 else None
                        if name_cell is not None:
                            name_cell.setText(root_name)

                self._model_preview_tabs.show()
                self._model_preview_tabs.setCurrentIndex(0)
                self.preview_stack.setCurrentWidget(self._model_preview_tabs)
            elif item.kind == 'mesh':
                from ..cache.cache_viewer import MeshLoaderThread

                mesh_loader = MeshLoaderThread(payload, item.asset_id or item.entry_id[:12])
                mesh_loader.mesh_ready.connect(
                    lambda obj_text, rid=request_id: self._on_mesh_ready(rid, obj_text)
                )
                mesh_loader.error.connect(
                    lambda error, rid=request_id: self._on_preview_error(rid, error)
                )
                self._track_decoder_worker('_mesh_loader', mesh_loader)
                self.info_preview.setText('Converting mesh to a 3D preview…')
                self.preview_stack.setCurrentWidget(self.info_preview)
            elif item.kind in _AUDIO_KINDS:
                self._load_audio_preview(payload, item, request_id)
            elif item.kind == 'font':
                from ..cache.font_viewer import FontViewerWidget

                self._font_widget = FontViewerWidget(payload, self.font_panel)
                self.font_layout.addWidget(self._font_widget)
                self.preview_stack.setCurrentWidget(self.font_panel)
            elif item.kind == 'json' or self._is_json_payload(payload):
                self._load_json_preview(payload)
            elif item.kind in {'xml', 'asset-metadata'}:
                self.text_preview.setPlainText(self._format_text_payload(payload))
                self.preview_stack.setCurrentWidget(self.text_preview)
            elif item.kind == 'csgphs':
                self.text_preview.setPlainText(format_csgphs_preview(payload))
                self.preview_stack.setCurrentWidget(self.text_preview)
            elif item.kind in {'roblox-settings', 'video-metadata', 'video-manifest', 'http-response'}:
                self.text_preview.setPlainText(
                    format_cached_http_response_preview(payload, item.url)
                    if item.kind != 'video-manifest' or not payload.lstrip().startswith(b'#EXTM3U')
                    else payload[:20000].decode('utf-8', errors='replace')
                )
                self.preview_stack.setCurrentWidget(self.text_preview)
            else:
                sample = payload[:4096]
                lines = [
                    f'Unrecognized payload · {len(payload):,} bytes',
                    f'Showing the first {len(sample):,} byte(s) as hexadecimal and ASCII.',
                    '',
                ]
                for offset in range(0, len(sample), 16):
                    block = sample[offset:offset + 16]
                    hex_part = ' '.join(f'{byte:02x}' for byte in block)
                    ascii_part = ''.join(
                        chr(byte) if 32 <= byte < 127 else '.' for byte in block
                    )
                    lines.append(f'{offset:08x}  {hex_part:<47}  |{ascii_part}|')
                if len(payload) > len(sample):
                    lines.append(f'… {len(payload) - len(sample):,} byte(s) omitted')
                self.text_preview.setPlainText('\n'.join(lines))
                self.preview_stack.setCurrentWidget(self.text_preview)
        except Exception as exc:
            self._show_info(f'Preview failed: {type(exc).__name__}: {exc}')


    def _on_image_ready(self, request_id: str, rgba: bytes, width: int, height: int) -> None:
        if request_id != self._current_request_id:
            return
        image = QImage(rgba, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
        if image.isNull():
            self._show_info('The image decoder returned an empty image.')
            return
        self._current_image = image
        self._scale_image()
        self.preview_stack.setCurrentWidget(self.image_preview)

    def _on_mesh_ready(self, request_id: str, obj_text: str) -> None:
        if request_id != self._current_request_id:
            return
        try:
            if self._mesh_panel is None:
                from ..cache.obj_viewer import ObjViewerPanel

                self._mesh_panel = ObjViewerPanel(self, config_manager=self.config_manager)
                self.mesh_layout.addWidget(self._mesh_panel)
            self._mesh_panel.load_obj(obj_text, request_id[:12])
            self.preview_stack.setCurrentWidget(self.mesh_panel)
        except Exception as exc:
            self._show_info(f'Could not create mesh preview: {type(exc).__name__}: {exc}')

    def _on_preview_error(self, request_id: str, error: str) -> None:
        if request_id == self._current_request_id:
            self._show_info(f'Preview could not be loaded: {error}')

    def _load_audio_preview(self, payload: bytes, item: _AssetItem, request_id: str) -> None:
        from ..cache.audio_player import AudioPlayerWidget

        extension = '.ogg' if item.kind == 'ogg' else '.mp3'
        audio_dir = Path(tempfile.gettempdir()) / 'fleasion_local_db_audio'
        audio_dir.mkdir(parents=True, exist_ok=True)
        audio_path = audio_dir / f'{item.entry_id}{extension}'
        audio_path.write_bytes(payload)
        if request_id != self._current_request_id:
            return
        self._audio_player = AudioPlayerWidget(str(audio_path), self, self.config_manager)
        self.audio_layout.addWidget(self._audio_player)
        self.preview_stack.setCurrentWidget(self.audio_panel)

    @staticmethod
    def _is_json_payload(payload: bytes) -> bool:
        data = payload
        if data.startswith(b'\x1f\x8b'):
            try:
                data = gzip.decompress(data)
            except (OSError, EOFError):
                return False
        for encoding in ('utf-8', 'utf-16', 'utf-16-le', 'utf-16-be'):
            try:
                json.loads(data.decode(encoding))
                return True
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        return False

    def _load_json_preview(self, payload: bytes) -> None:
        data = payload
        if data.startswith(b'\x1f\x8b'):
            data = gzip.decompress(data)
        parsed: Any = None
        for encoding in ('utf-8', 'utf-16', 'utf-16-le', 'utf-16-be'):
            try:
                parsed = json.loads(data.decode(encoding))
                break
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        if parsed is None:
            self._show_info('JSON data was detected but could not be parsed.')
            return
        if self.json_preview is None:
            from ..cache.cache_json_viewer import CacheJsonViewer

            self.json_preview = CacheJsonViewer()
            self.preview_stack.addWidget(self.json_preview)
        self.json_preview.load_json(parsed)
        self.preview_stack.setCurrentWidget(self.json_preview)

    @staticmethod
    def _format_text_payload(payload: bytes) -> str:
        text = payload.decode('utf-8', errors='replace')
        try:
            import xml.dom.minidom

            if text.lstrip().startswith('<'):
                pretty = xml.dom.minidom.parseString(text).toprettyxml(indent='  ')
                lines = [line for line in pretty.splitlines() if line.strip()]
                return '\n'.join(lines[:1000])
        except Exception:
            pass
        return text[:20000] if text else 'Empty payload.'

    def _show_info(self, text: str) -> None:
        self.info_preview.setText(text)
        self.preview_stack.setCurrentWidget(self.info_preview)

    def _scale_image(self) -> None:
        if self._current_image is None:
            return
        target = self.image_preview.size()
        if target.width() < 20 or target.height() < 20:
            target = QSize(600, 420)
        self.image_preview.setPixmap(
            QPixmap.fromImage(self._current_image).scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self.preview_stack.currentWidget() is self.image_preview:
            self._scale_image()

    def _clear_preview(self) -> None:
        self._current_image = None
        self.image_preview.clear()
        self.preview_stack.setCurrentWidget(self.info_preview)

    def _copy_selected_id(self) -> None:
        if self._current_entry_id:
            QApplication.clipboard().setText(self._current_entry_id)
            self.status_label.setText(f'Copied cache entry ID {self._current_entry_id}.')

    def _copy_asset_id(self) -> None:
        item = self.items_by_id.get(self._current_entry_id)
        if item is not None and item.asset_id:
            QApplication.clipboard().setText(item.asset_id)
            self.status_label.setText(f'Copied asset ID {item.asset_id}.')

    def _export_payload(self) -> None:
        item = self.items_by_id.get(self._current_entry_id)
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
            'tiff': '.tiff',
            'ico': '.ico',
            'dds': '.dds',
            'ktx': '.ktx',
            'psd': '.psd',
            'font': '.ttf',
            'ogg': '.ogg',
            'mp3': '.mp3',
            'mesh': '.mesh',
            'json': '.json',
        }
        default_name = (item.asset_id or item.entry_id) + suffixes.get(item.kind, '.bin')
        target, _filter = QFileDialog.getSaveFileName(
            self,
            'Export raw payload',
            str(Path.home() / default_name),
            'All files (*.*)',
        )
        if not target:
            return
        try:
            # Stream the original payload to disk without loading the full BLOB into RAM.
            store = RobloxCacheStore(self.database_path.parent)
            store.copy_entry_payload_to_file(item.entry_id, target)
            self.status_label.setText(f'Exported raw payload to {target}')
        except (OSError, SQLiteBrowserError, RobloxCacheError) as exc:
            QMessageBox.warning(self, 'Export failed', str(exc))

    def _resolve_visible_names(self, visible_items: list[_AssetItem]) -> None:
        if self._name_resolution_disabled:
            return
        if len(self._name_resolution_attempted) >= _MAX_NAME_RESOLUTION:
            return
        if self._name_resolver is not None and self._name_resolver.isRunning():
            return

        # Prioritise the displayed page, then gradually resolve other entries too.
        ordered_items = list(visible_items)
        visible_entry_ids = {item.entry_id for item in visible_items}
        ordered_items.extend(item for item in self.items if item.entry_id not in visible_entry_ids)
        ids = []
        for item in ordered_items:
            asset_id = item.asset_id
            if (
                asset_id
                and asset_id not in self._name_cache
                and asset_id not in self._name_resolution_attempted
                and asset_id not in self._name_resolution_inflight
                and asset_id not in ids
            ):
                ids.append(asset_id)
            if len(ids) >= _NAME_RESOLUTION_BATCH:
                break
        if not ids:
            return
        self._name_resolution_inflight.update(ids)
        self._name_resolution_attempted.update(ids)
        resolver = _AssetNameResolver(ids, self)
        self._name_resolver = resolver
        resolver.names_ready.connect(self._on_names_resolved)
        resolver.finished.connect(lambda worker=resolver: self._on_name_resolver_finished(worker))
        resolver.start()

    def _on_names_resolved(self, results: dict, processed_ids: list, failed: bool) -> None:
        for asset_id in processed_ids:
            self._name_resolution_inflight.discard(str(asset_id))
        if failed:
            self._name_resolution_disabled = True
            self._scan_note = (
                'Roblox name lookup is unavailable; showing detected names, asset IDs and cache hashes.'
            )
            self.status_label.setText(self._scan_note)
            return
        if not results:
            return
        for asset_id, metadata in results.items():
            if not isinstance(metadata, dict) or not metadata.get('name'):
                continue
            key = str(asset_id)
            cached = self._name_cache.setdefault(key, {})
            cached.update(
                {
                    'name': str(metadata.get('name') or cached.get('name') or ''),
                    'creator_name': str(
                        metadata.get('creator_name') or cached.get('creator_name') or ''
                    ),
                    'created_at': str(
                        metadata.get('created_at') or cached.get('created_at') or ''
                    ),
                    'updated_at': str(
                        metadata.get('updated_at') or cached.get('updated_at') or ''
                    ),
                }
            )
            for item in self.items:
                if item.asset_id == key:
                    item.name = self._name_cache[key]['name']
                    item.creator_name = self._name_cache[key]['creator_name']
                    item.created_at = self._name_cache[key]['created_at']
                    item.updated_at = self._name_cache[key]['updated_at']
        self._save_settings()
        self._render_items()

    def _on_name_resolver_finished(self, worker: _AssetNameResolver) -> None:
        if self._name_resolver is not worker:
            return
        self._name_resolver = None
        # Resolve the next batch for the same visible page when more IDs remain.
        QTimer.singleShot(100, self._resolve_current_page_names)

    def _resolve_current_page_names(self) -> None:
        if not self.items:
            return
        visible = self._visible_items()
        page_items = visible[self._page_offset:self._page_offset + self._page_size()]
        self._resolve_visible_names(page_items)

    def _shutdown_workers(self) -> None:
        if self._settings_save_timer.isActive():
            self._settings_save_timer.stop()
            self._save_settings()
        if self._name_resolver is not None and self._name_resolver.isRunning():
            self._name_resolver.stop()
            self._name_resolver.wait()

        for worker in tuple(self._preview_workers):
            worker.requestInterruption()
        for worker in tuple(self._preview_workers):
            worker.wait()
        self._preview_workers.clear()

        for worker in tuple(self._decoder_workers):
            try:
                stop = getattr(worker, 'stop', None)
                if callable(stop):
                    stop()
                worker.requestInterruption()
            except RuntimeError:
                pass
        for worker in tuple(self._decoder_workers):
            worker.wait()
        self._decoder_workers.clear()

    def closeEvent(self, event) -> None:
        self._shutdown_workers()
        super().closeEvent(event)
