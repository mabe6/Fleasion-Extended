"""GUI tab for reading and modifying local Roblox cache entries without the proxy."""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QComboBox,
    QGroupBox,
    QHeaderView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..database.animation_payload import inspect_animation_payload
from ..database.animation_convert import AnimationConversionError, convert_animation_for_slot
from ..database.roblox_cache_store import (
    RobloxCacheEntry,
    RobloxCacheError,
    RobloxCacheStore,
    default_roblox_cache_root,
    detect_roblox_cache_payload_kind,
    detect_roblox_payload_kind,
    decompress_roblox_payload_file,
    split_rbxh_entry,
)
from ..utils import log_buffer


_SIZE_QUERY_RE = re.compile(
    r'^(<=|>=|<|>|=)?\s*(\d+(?:\.\d+)?)\s*(b|kb|kib|mb|mib|gb|gib)?$',
    re.I,
)
_SIZE_QUERY_MULTIPLIERS = {
    'b': 1, 'kb': 1000, 'kib': 1024, 'mb': 1000**2, 'mib': 1024**2,
    'gb': 1000**3, 'gib': 1024**3, '': 1,
}
_IMAGE_KINDS = {'png', 'jpg', 'jpeg', 'bmp', 'gif', 'webp', 'tiff', 'ico', 'dds', 'ktx', 'psd'}


def _matches_size_query(value: int | None, expression: str) -> bool:
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


def _format_size(value: int | None) -> str:
    if value is None:
        return '—'
    size = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if size < 1024 or unit == 'GiB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return str(value)



class _CacheScanWorker(QThread):
    """Scan local cache metadata away from the GUI thread."""

    completed = pyqtSignal(object, object, str)

    def __init__(self, root: str, parent=None):
        super().__init__(parent)
        self.root = root

    def run(self) -> None:
        try:
            store = RobloxCacheStore(self.root)
            entries = store.scan()
        except Exception as exc:
            self.completed.emit(None, [], f'{type(exc).__name__}: {exc}')
            return
        self.completed.emit(store, entries, '')


class _CacheEntryPreviewWorker(QThread):
    """Read and decode one cache entry after the user selects it."""

    payload_ready = pyqtSignal(str, bytes, str)
    preview_failed = pyqtSignal(str, str)

    def __init__(self, entry_id: str, store: RobloxCacheStore, parent=None):
        super().__init__(parent)
        self.entry_id = entry_id
        self.store = store

    def run(self) -> None:
        try:
            if self.isInterruptionRequested():
                return
            with tempfile.TemporaryDirectory(prefix='fleasion-cache-preview-') as folder:
                raw_path = Path(folder) / 'payload.raw'
                decoded_path = Path(folder) / 'payload.decoded'
                self.store.copy_entry_payload_to_file(self.entry_id, raw_path)
                if self.isInterruptionRequested():
                    return
                decompress_roblox_payload_file(raw_path, decoded_path)
                if self.isInterruptionRequested():
                    return
                with decoded_path.open('rb') as handle:
                    prefix = handle.read(8192)
                kind = detect_roblox_payload_kind(prefix)
                payload = decoded_path.read_bytes()
                if kind == 'mesh':
                    from ..cache.mesh_processing import is_mesh_data

                    if not is_mesh_data(payload):
                        kind = 'other'
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


class RobloxCacheTab(QWidget):
    """Browse local rbx-storage entries, replace a whole slot and restore originals."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        config_manager=None,
        additional_tab: QWidget | None = None,
        view_mode: str = 'all',
    ):
        super().__init__(parent)
        if view_mode not in {'all', 'swaps', 'slots'}:
            raise ValueError(f'Unsupported cache tab view mode: {view_mode}')
        if additional_tab is not None and view_mode != 'all':
            raise ValueError('additional_tab is only supported when view_mode="all".')
        self.config_manager = config_manager
        self._view_mode = view_mode
        self.store = RobloxCacheStore()
        self.entries: list[RobloxCacheEntry] = []
        self.entries_by_id: dict[str, RobloxCacheEntry] = {}
        self._entries_page_offset = 0
        self._image_loader = None
        self._mesh_loader = None
        self._current_preview_image: QImage | None = None
        self._current_preview_request_id = ''
        self._preview_workers: set[_CacheEntryPreviewWorker] = set()
        self._scan_worker: _CacheScanWorker | None = None
        self._scan_pending = False
        settings = getattr(config_manager, 'settings', {})
        cached_metadata = (
            settings.get('local_db_cache_metadata', {})
            if isinstance(settings, dict) else {}
        )
        self._cache_metadata = dict(cached_metadata) if isinstance(cached_metadata, dict) else {}
        self._name_resolver = None
        self._name_resolution_attempted: set[str] = set()
        self._name_resolution_disabled = False
        self._mesh_panel = None
        self._model_panel = None
        self._animation_panel = None
        settings = getattr(config_manager, 'settings', {})
        self._probe_header_enabled = bool(
            settings.get('show_cache_probe_header', False)
            if isinstance(settings, dict) else False
        )
        self._build_ui()
        if additional_tab is not None:
            self.pages.addTab(additional_tab, 'Asset Database')
        self._render_active_swaps()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown_workers)
        if view_mode == 'slots' and callable(getattr(self.store, 'scan', None)):
            QTimer.singleShot(0, self.refresh)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(5)

        self.pages = QTabWidget()
        root.addWidget(self.pages, 1)

        # First page follows Fleasion's original profile editor: active swaps above,
        # editable IDs below. Users can paste cache-entry IDs directly.
        self.swaps_page = QWidget()
        swaps_layout = QVBoxLayout(self.swaps_page)
        swaps_layout.setContentsMargins(6, 6, 6, 6)
        swaps_layout.setSpacing(6)

        filters_group = QGroupBox('Filters')
        filters_layout = QHBoxLayout(filters_group)
        filters_layout.setContentsMargins(8, 5, 8, 5)
        filters_layout.addWidget(QLabel('Search:'))
        self.swap_filter_edit = QLineEdit()
        self.swap_filter_edit.setPlaceholderText(
            'Swap name, target cache ID, replacement ID or date'
        )
        self.swap_filter_edit.textChanged.connect(self._render_active_swaps)
        filters_layout.addWidget(self.swap_filter_edit, 1)
        filters_layout.addWidget(QLabel('Status:'))
        self.swap_status_filter = QComboBox()
        self.swap_status_filter.addItem('All swaps', 'all')
        self.swap_status_filter.addItem('Active only', 'active')
        self.swap_status_filter.addItem('Changed since swap', 'changed')
        self.swap_status_filter.currentIndexChanged.connect(self._render_active_swaps)
        filters_layout.addWidget(self.swap_status_filter)
        self.swap_count_label = QLabel('0 swaps')
        filters_layout.addWidget(self.swap_count_label)
        swaps_layout.addWidget(filters_group)

        active_group = QWidget()
        active_layout = QVBoxLayout(active_group)
        active_layout.setContentsMargins(0, 0, 0, 0)
        active_layout.setSpacing(3)
        active_layout.addWidget(QLabel('Active Swaps'))

        self.swaps_table = QTableWidget(0, 5)
        self.swaps_table.setHorizontalHeaderLabels(
            ['Status', 'Swap Name', 'Target Cache ID', 'Replacement Cache ID', 'Applied']
        )
        self.swaps_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.swaps_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.swaps_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.swaps_table.setAlternatingRowColors(True)
        self.swaps_table.setSortingEnabled(False)
        self.swaps_table.verticalHeader().hide()
        self.swaps_table.horizontalHeader().setStretchLastSection(False)
        self.swaps_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.swaps_table.setColumnWidth(0, 125)
        self.swaps_table.setColumnWidth(2, 250)
        self.swaps_table.setColumnWidth(3, 250)
        self.swaps_table.setColumnWidth(4, 145)
        self.swaps_table.itemDoubleClicked.connect(self._load_selected_swap)
        active_layout.addWidget(self.swaps_table, 1)
        self.swap_empty_label = QLabel(
            'No saved swaps yet. Use Cache Slots to browse every local entry, then choose a target and replacement ID.'
        )
        self.swap_empty_label.setWordWrap(True)
        self.swap_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.swap_empty_label.setStyleSheet('color: palette(placeholder-text); padding: 12px;')
        active_layout.addWidget(self.swap_empty_label)
        swaps_layout.addWidget(active_group, 1)

        edit_group = QWidget()
        edit_layout = QVBoxLayout(edit_group)
        edit_layout.setContentsMargins(0, 0, 0, 0)
        edit_layout.setSpacing(4)
        edit_layout.addWidget(QLabel('Add / Edit Swap'))

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel('Swap name:'))
        self.swap_name_edit = QLineEdit()
        self.swap_name_edit.setPlaceholderText('Optional label, e.g. Replace idle animation')
        name_row.addWidget(self.swap_name_edit, 1)
        edit_layout.addLayout(name_row)

        ids_row = QHBoxLayout()
        ids_row.addWidget(QLabel('Target cache ID:'))
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText('Paste the 32-character cache ID to replace')
        self.target_edit.setClearButtonEnabled(True)
        ids_row.addWidget(self.target_edit, 1)
        ids_row.addWidget(QLabel('Replace with:'))
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText('Paste the 32-character replacement cache ID')
        self.source_edit.setClearButtonEnabled(True)
        ids_row.addWidget(self.source_edit, 1)
        edit_layout.addLayout(ids_row)

        actions = QHBoxLayout()
        self.swap_button = QPushButton('Apply / Update Swap')
        self.swap_button.clicked.connect(self._swap_selected_entries)
        actions.addWidget(self.swap_button)
        load_button = QPushButton('Load Selected')
        load_button.clicked.connect(self._load_selected_swap)
        actions.addWidget(load_button)
        restore_button = QPushButton('Restore Selected')
        restore_button.clicked.connect(self._restore_selected_swap)
        actions.addWidget(restore_button)
        restore_all_button = QPushButton('Restore All')
        restore_all_button.clicked.connect(self._restore_all)
        actions.addWidget(restore_all_button)
        clear_button = QPushButton('Clear Fields')
        clear_button.clicked.connect(self._clear_swap_fields)
        actions.addWidget(clear_button)
        verify_button = QPushButton('Verify Target')
        verify_button.clicked.connect(self._verify_selected_target)
        actions.addWidget(verify_button)
        edit_layout.addLayout(actions)
        swaps_layout.addWidget(edit_group)

        self.status_label = QLabel(
            'Local cache mode. Close Roblox Player / Studio before applying or restoring a swap.'
        )
        self.status_label.setWordWrap(True)
        swaps_layout.addWidget(self.status_label)

        self.pages.addTab(self.swaps_page, 'Active Swaps')

        # Cache inventory remains available in a separate compact page. Selecting a
        # row can fill either of the ID fields above.
        self.entries_page = QWidget()
        entries_layout = QVBoxLayout(self.entries_page)
        entries_layout.setContentsMargins(6, 6, 6, 6)
        entries_layout.setSpacing(5)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel('Roblox cache root:'))
        self.root_edit = QLineEdit(str(default_roblox_cache_root()))
        self.root_edit.setPlaceholderText('Folder containing rbx-storage.db and rbx-storage/')
        path_row.addWidget(self.root_edit, 1)
        browse_button = QPushButton('Browse…')
        browse_button.clicked.connect(self._browse_root)
        path_row.addWidget(browse_button)
        self.scan_button = QPushButton('Scan Cache')
        self.scan_button.clicked.connect(self.refresh)
        path_row.addWidget(self.scan_button)
        entries_layout.addLayout(path_row)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel('Search:'))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('Name, asset ID, URL, or filters: type:ktx size:>1mb status:swapped')
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._entry_filters_changed)
        search_row.addWidget(self.search_edit, 2)

        self.kind_filter_combo = QComboBox()
        for label, value in (
            ('All types', 'all'),
            ('Images / textures', 'images'),
            ('KTX textures', 'ktx'),
            ('Meshes', 'mesh'),
            ('Roblox models', 'models'),
            ('Animations', 'animations'),
            ('Audio', 'audio'),
            ('Fonts', 'font'),
            ('JSON', 'json'),
            ('Other', 'other'),
        ):
            self.kind_filter_combo.addItem(label, value)
        self.kind_filter_combo.currentIndexChanged.connect(self._entry_filters_changed)
        search_row.addWidget(self.kind_filter_combo)

        self.slot_status_filter = QComboBox()
        for label, value in (
            ('All slots', 'all'),
            ('Unmodified', 'unmodified'),
            ('Active swaps', 'swapped'),
            ('Changed since swap', 'changed'),
        ):
            self.slot_status_filter.addItem(label, value)
        self.slot_status_filter.currentIndexChanged.connect(self._entry_filters_changed)
        search_row.addWidget(self.slot_status_filter)

        self.entries_count_label = QLabel('0 entries')
        search_row.addWidget(self.entries_count_label)
        entries_layout.addLayout(search_row)

        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(
            ['Entry ID', 'Asset ID', 'Kind', 'File/content size', 'DB size', 'Modified', 'Swap status', 'Storage', 'URL', 'Name']
        )
        self.table.horizontalHeader().moveSection(9, 1)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemDoubleClicked.connect(self._set_selected_as_target)
        self.table.itemSelectionChanged.connect(self._selection_changed)

        self.entries_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.entries_splitter.setChildrenCollapsible(False)
        table_host = QWidget()
        table_host_layout = QVBoxLayout(table_host)
        table_host_layout.setContentsMargins(0, 0, 0, 0)
        table_host_layout.addWidget(self.table, 1)
        self.entries_splitter.addWidget(table_host)

        self.preview_panel = QWidget()
        preview_layout = QVBoxLayout(self.preview_panel)
        preview_layout.setContentsMargins(8, 4, 4, 4)
        preview_layout.setSpacing(5)

        self.preview_title = QLabel('Cache Slot Preview')
        self.preview_title.setStyleSheet('font-weight: 600;')
        preview_layout.addWidget(self.preview_title)

        self.preview_details = QLabel('Select a cache slot to preview its contents.')
        self.preview_details.setWordWrap(True)
        self.preview_details.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        preview_layout.addWidget(self.preview_details)

        self.preview_stack = QStackedWidget()
        self.preview_info = QLabel(
            'Select a cache slot to preview an image, KTX texture, mesh, animation or model. '
            'Large payloads are skipped for safety.'
        )
        self.preview_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_info.setWordWrap(True)
        self.preview_stack.addWidget(self.preview_info)

        self.preview_image = QLabel()
        self.preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_image.setMinimumSize(220, 200)
        self.preview_stack.addWidget(self.preview_image)

        self.preview_text = QPlainTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.preview_stack.addWidget(self.preview_text)
        preview_layout.addWidget(self.preview_stack, 1)

        self.preview_panel.setMinimumWidth(250)
        self.entries_splitter.addWidget(self.preview_panel)
        self.entries_splitter.setSizes([720, 320])
        entries_layout.addWidget(self.entries_splitter, 1)

        paging_row = QHBoxLayout()
        paging_row.addWidget(QLabel('Rows per page:'))
        self.entries_page_size_combo = QComboBox()
        for page_size in (100, 250, 500, 1000, 2500):
            self.entries_page_size_combo.addItem(str(page_size), page_size)
        self.entries_page_size_combo.setCurrentIndex(self.entries_page_size_combo.findData(250))
        self.entries_page_size_combo.currentIndexChanged.connect(self._entry_page_size_changed)
        paging_row.addWidget(self.entries_page_size_combo)
        self.entries_prev_button = QPushButton('← Previous')
        self.entries_prev_button.clicked.connect(self._previous_entries_page)
        paging_row.addWidget(self.entries_prev_button)
        self.entries_page_label = QLabel('Page 0 / 0')
        paging_row.addWidget(self.entries_page_label)
        self.entries_next_button = QPushButton('Next →')
        self.entries_next_button.clicked.connect(self._next_entries_page)
        paging_row.addWidget(self.entries_next_button)
        paging_row.addStretch(1)
        entries_layout.addLayout(paging_row)

        inventory_actions = QHBoxLayout()
        set_target = QPushButton('Selected → target ID')
        set_target.clicked.connect(self._set_selected_as_target)
        inventory_actions.addWidget(set_target)
        set_source = QPushButton('Selected → replacement ID')
        set_source.clicked.connect(self._set_selected_as_source)
        inventory_actions.addWidget(set_source)
        import_button = QPushButton('Import File → Target…')
        import_button.clicked.connect(self._import_file)
        inventory_actions.addWidget(import_button)
        export_button = QPushButton('Export Entry…')
        export_button.clicked.connect(self._export_selected)
        inventory_actions.addWidget(export_button)
        self.asset_lookup_button = QPushButton('Asset Lookup…')
        self.asset_lookup_button.setEnabled(False)
        self.asset_lookup_button.clicked.connect(self._open_asset_lookup)
        inventory_actions.addWidget(self.asset_lookup_button)
        self.probe_button = QPushButton('Probe Header')
        self.probe_button.clicked.connect(self._probe_header)
        inventory_actions.addWidget(self.probe_button)
        inventory_actions.addStretch(1)
        entries_layout.addLayout(inventory_actions)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText('Operation details and optional header-probe output.')
        self.output.setMaximumBlockCount(500)
        self.output.setMaximumHeight(100)
        entries_layout.addWidget(self.output)
        self.set_probe_header_enabled(self._probe_header_enabled)

        self.pages.addTab(self.entries_page, 'Cache Slots')

        # In the dashboard these pages are promoted to sibling top-level tabs.
        # Keep the combined inner tab widget as the default for existing callers/tests.
        if self._view_mode != 'all':
            selected_page = self.swaps_page if self._view_mode == 'swaps' else self.entries_page
            selected_index = self.pages.indexOf(selected_page)
            if selected_index >= 0:
                self.pages.removeTab(selected_index)
            root.removeWidget(self.pages)
            self.pages.hide()
            selected_page.setParent(self)
            root.insertWidget(0, selected_page, 1)
            # QWidget.setParent() hides a widget. The promoted dashboard page must
            # explicitly be shown, otherwise only the detached status label is visible.
            selected_page.setVisible(True)
            if self._view_mode == 'slots':
                # Share the scan/operation status row with the standalone Cache Slots view.
                self.status_label.setParent(self)
                root.addWidget(self.status_label)

    def set_probe_header_enabled(self, enabled: bool) -> None:
        """Show or hide the optional header-probe controls to save vertical space."""
        self._probe_header_enabled = bool(enabled)
        if hasattr(self, 'probe_button'):
            self.probe_button.setVisible(self._probe_header_enabled)
        if hasattr(self, 'output'):
            self.output.setVisible(self._probe_header_enabled)

    def _clear_swap_fields(self) -> None:
        self.swap_name_edit.clear()
        self.target_edit.clear()
        self.source_edit.clear()

    def _load_selected_swap(self, *_args) -> None:
        row = self.swaps_table.currentRow()
        if row < 0:
            return
        target_item = self.swaps_table.item(row, 2)
        source_item = self.swaps_table.item(row, 3)
        name_item = self.swaps_table.item(row, 1)
        if target_item is not None:
            self.target_edit.setText(str(target_item.data(Qt.ItemDataRole.UserRole) or target_item.text()))
        if source_item is not None:
            self.source_edit.setText(str(source_item.data(Qt.ItemDataRole.UserRole) or source_item.text()))
        if name_item is not None:
            self.swap_name_edit.setText(name_item.text())

    def _restore_selected_swap(self) -> None:
        if self.swaps_table.currentRow() < 0:
            self._show_error('Select a swap', 'Select an active swap row first.')
            return
        self._load_selected_swap()
        self._restore_target()

    def _browse_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, 'Choose Roblox cache root', self.root_edit.text()
        )
        if folder:
            self.root_edit.setText(folder)
            self.refresh()

    def _selected_entry_id(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ''
        item = self.table.item(row, 0)
        return str(item.data(Qt.ItemDataRole.UserRole) or '') if item else ''

    def _set_selected_as_target(self) -> None:
        entry_id = self._selected_entry_id()
        if entry_id:
            self.target_edit.setText(entry_id)

    def _set_selected_as_source(self) -> None:
        entry_id = self._selected_entry_id()
        if entry_id:
            self.source_edit.setText(entry_id)

    def _entry_name(self, entry: RobloxCacheEntry) -> str:
        if entry.name:
            return entry.name
        settings = getattr(self.config_manager, 'settings', {})
        cached_names = settings.get('local_db_asset_names', {}) if isinstance(settings, dict) else {}
        if isinstance(cached_names, dict):
            cached = cached_names.get(entry.asset_id, {})
            if isinstance(cached, dict) and cached.get('name'):
                return str(cached['name'])
        return ''

    def _visible_entries(self) -> list[RobloxCacheEntry]:
        raw_query = self.search_edit.text().strip()
        query = raw_query.casefold()
        structured: list[tuple[str, str]] = []
        token_re = re.compile(r'(?P<key>[A-Za-z]+):(?P<value>"[^"]+"|\S+)')
        supported_keys = {'name', 'type', 'size', 'dbsize', 'id', 'asset', 'url', 'status', 'storage'}
        for match in token_re.finditer(raw_query):
            key = match.group('key').casefold()
            if key not in supported_keys:
                continue
            value = match.group('value').strip('"')
            structured.append((key, value))
            query = query.replace(match.group(0).casefold(), ' ', 1)
        query = ' '.join(query.split())

        kind_filter = self.kind_filter_combo.currentData()
        status_filter = self.slot_status_filter.currentData()
        image_kinds = _IMAGE_KINDS
        model_kinds = {'rbxm', 'xml'}
        animation_kinds = {'anim', 'curve-anim'}
        audio_kinds = {'ogg', 'mp3'}

        visible: list[RobloxCacheEntry] = []
        for entry in self.entries:
            entry_name = self._entry_name(entry)
            if query and not any(
                query in str(value).casefold()
                for value in (
                    entry_name,
                    entry.entry_id,
                    entry.asset_id,
                    entry.kind,
                    entry.url,
                    entry.swap_status or 'unmodified',
                    entry.storage_mode,
                    _format_size(entry.file_size),
                    _format_size(entry.database_size),
                )
            ):
                continue

            matches = True
            for key, expected in structured:
                value = expected.casefold()
                if key == 'name':
                    matches = value in entry_name.casefold()
                elif key == 'type':
                    aliases = {
                        'images': image_kinds,
                        'image': image_kinds,
                        'textures': image_kinds,
                        'models': model_kinds,
                        'animations': animation_kinds,
                        'audio': audio_kinds,
                    }
                    matches = entry.kind in aliases.get(value, {value}) or value in entry.kind.casefold()
                elif key == 'size':
                    matches = _matches_size_query(entry.file_size, expected)
                elif key == 'dbsize':
                    matches = _matches_size_query(entry.database_size, expected)
                elif key == 'id':
                    matches = value in entry.entry_id.casefold()
                elif key == 'asset':
                    matches = value in entry.asset_id.casefold()
                elif key == 'url':
                    matches = value in entry.url.casefold()
                elif key == 'status':
                    matches = value in (entry.swap_status or 'unmodified').casefold()
                elif key == 'storage':
                    matches = value in entry.storage_mode.casefold()
                if not matches:
                    break
            if not matches:
                continue

            if kind_filter == 'images' and entry.kind not in image_kinds:
                continue
            if kind_filter == 'ktx' and entry.kind != 'ktx':
                continue
            if kind_filter == 'mesh' and entry.kind != 'mesh':
                continue
            if kind_filter == 'models' and entry.kind not in model_kinds:
                continue
            if kind_filter == 'animations' and entry.kind not in animation_kinds:
                continue
            if kind_filter == 'audio' and entry.kind not in audio_kinds:
                continue
            if kind_filter == 'font' and entry.kind != 'font':
                continue
            if kind_filter == 'json' and entry.kind != 'json':
                continue
            if kind_filter == 'other' and entry.kind in (
                image_kinds | model_kinds | animation_kinds | audio_kinds | {'font', 'json', 'mesh'}
            ):
                continue

            if status_filter == 'unmodified' and entry.swap_status:
                continue
            if status_filter == 'swapped' and entry.swap_status != 'swapped':
                continue
            if status_filter == 'changed' and entry.swap_status != 'reverted':
                continue
            visible.append(entry)
        return visible

    def _entry_filters_changed(self, *_args) -> None:
        self._entries_page_offset = 0
        self._render_entries()

    def _entry_page_size_changed(self, *_args) -> None:
        self._entries_page_offset = 0
        self._render_entries()

    def _entry_page_size(self) -> int:
        return int(self.entries_page_size_combo.currentData() or 250)

    def _previous_entries_page(self) -> None:
        self._entries_page_offset = max(0, self._entries_page_offset - self._entry_page_size())
        self._render_entries()

    def _next_entries_page(self) -> None:
        self._entries_page_offset += self._entry_page_size()
        self._render_entries()

    def _render_entries(self, *_args) -> None:
        visible_entries = self._visible_entries()
        page_size = self._entry_page_size()
        page_count = (len(visible_entries) + page_size - 1) // page_size
        if page_count:
            self._entries_page_offset = min(self._entries_page_offset, (page_count - 1) * page_size)
            current_page = self._entries_page_offset // page_size + 1
        else:
            self._entries_page_offset = 0
            current_page = 0
        page_entries = visible_entries[self._entries_page_offset:self._entries_page_offset + page_size]
        self.entries_prev_button.setEnabled(self._entries_page_offset > 0)
        self.entries_next_button.setEnabled(self._entries_page_offset + len(page_entries) < len(visible_entries))
        self.entries_page_label.setText(f'Page {current_page} / {page_count}' if page_count else 'Page 0 / 0')

        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(0)
        for entry in page_entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                entry.entry_id,
                entry.asset_id or '—',
                entry.kind,
                _format_size(entry.file_size),
                _format_size(entry.database_size),
                time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(entry.modified_at)),
                entry.swap_status or '—',
                entry.storage_mode,
                entry.url,
                self._entry_name(entry),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry.entry_id)
                self.table.setItem(row, column, item)
        self.table.setUpdatesEnabled(True)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(0, 270)
        self.table.setColumnWidth(8, 400)
        self.table.setColumnWidth(9, 220)
        self.entries_count_label.setText(
            f'{len(visible_entries):,} / {len(self.entries):,} entries'
            if len(visible_entries) != len(self.entries) else f'{len(self.entries):,} entries'
        )

    def _selection_changed(self) -> None:
        entry_id = self._selected_entry_id()
        if not entry_id:
            self.asset_lookup_button.setEnabled(False)
            self._reset_slot_preview()
            return
        entry = self.entries_by_id.get(entry_id)
        if entry is None:
            self.asset_lookup_button.setEnabled(False)
            self._reset_slot_preview()
            return
        self.asset_lookup_button.setEnabled(True)
        display_name = self._entry_name(entry)
        cached = self._cache_metadata.get(entry_id, {})
        self.preview_title.setText(display_name or f'{entry.kind.upper()} · {entry.entry_id[:12]}')
        details = [
            f'Asset ID: {entry.asset_id or cached.get("asset_id") or "not found"}',
            f'Type: {entry.kind}',
            f'Name: {display_name or cached.get("name") or "not stored in local metadata"}',
            f'File/content size: {_format_size(entry.file_size)}',
            f'Database size: {_format_size(entry.database_size)}',
            f'Swap status: {entry.swap_status or "unmodified"}',
            f'Storage: {entry.storage_mode}',
        ]
        if cached.get('asset_type_label'):
            details.append(f'Roblox asset type: {cached["asset_type_label"]}')
        if cached.get('asset_version'):
            details.append(f'Asset version: {cached["asset_version"]}')
        if cached.get('cdn_probe_status'):
            details.append(f'CDN metadata: {cached["cdn_probe_status"]}')
        if cached.get('cdn_probe_note'):
            details.append(str(cached['cdn_probe_note']))
        if entry.url:
            details.append(f'URL: {entry.url}')
        self.preview_details.setText('\n'.join(details))
        self._load_slot_preview(entry)

    def _stop_slot_worker(self, attribute: str) -> None:
        worker = getattr(self, attribute, None)
        if worker is None:
            return
        try:
            stop = getattr(worker, 'stop', None)
            if callable(stop):
                stop()
            worker.quit()
            worker.wait()
            worker.deleteLater()
        except RuntimeError:
            pass
        setattr(self, attribute, None)

    def _reset_slot_preview(self) -> None:
        self._current_preview_request_id = ''
        self._stop_slot_worker('_image_loader')
        self._stop_slot_worker('_mesh_loader')
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
        if self._mesh_panel is not None:
            try:
                self._mesh_panel.hide()
            except Exception:
                pass
        self.preview_image.clear()
        self.preview_text.clear()
        self._current_preview_image = None
        self.preview_stack.setCurrentWidget(self.preview_info)

    def _load_slot_preview(self, entry: RobloxCacheEntry) -> None:
        self._reset_slot_preview()
        self._current_preview_request_id = entry.entry_id
        self.preview_info.setText('Reading cache entry and decoding preview…')
        self.preview_stack.setCurrentWidget(self.preview_info)
        worker = _CacheEntryPreviewWorker(entry.entry_id, self.store, self)
        self._preview_workers.add(worker)
        worker.payload_ready.connect(self._on_slot_payload_ready)
        worker.preview_failed.connect(self._on_slot_preview_error)
        worker.finished.connect(
            lambda current=worker: self._preview_workers.discard(current)
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_slot_payload_ready(self, request_id: str, payload: bytes, kind: str) -> None:
        if request_id != self._current_preview_request_id:
            return
        entry = self.entries_by_id.get(request_id)
        if entry is None:
            return
        if entry.kind == 'compressed':
            entry.kind = kind
            self.preview_title.setText(f'{kind.upper()} · {entry.entry_id[:12]}')
            self.preview_details.setText(
                self.preview_details.text().replace('Type: compressed', f'Type: {kind}')
            )
        try:
            if kind in _IMAGE_KINDS:
                from ..cache.cache_viewer import ImageLoaderThread

                self.preview_info.setText('Decoding image / KTX texture…')
                self._image_loader = ImageLoaderThread(payload)
                self._image_loader.image_ready.connect(
                    lambda rgba, width, height, request_id=entry.entry_id:
                    self._on_slot_image_ready(request_id, rgba, width, height)
                )
                self._image_loader.error.connect(
                    lambda error, request_id=entry.entry_id:
                    self._on_slot_preview_error(request_id, error)
                )
                self.preview_stack.setCurrentWidget(self.preview_info)
                self._image_loader.start()
            elif kind == 'mesh':
                from ..cache.cache_viewer import MeshLoaderThread

                self.preview_info.setText('Preparing 3D mesh preview…')
                self._mesh_loader = MeshLoaderThread(
                    payload,
                    entry.asset_id or entry.entry_id[:12],
                )
                self._mesh_loader.mesh_ready.connect(
                    lambda obj_text, request_id=entry.entry_id:
                    self._on_slot_mesh_ready(request_id, obj_text)
                )
                self._mesh_loader.error.connect(
                    lambda error, request_id=entry.entry_id:
                    self._on_slot_preview_error(request_id, error)
                )
                self.preview_stack.setCurrentWidget(self.preview_info)
                self._mesh_loader.start()
            elif kind == 'asset-metadata':
                self.preview_text.setPlainText(payload.decode('ascii', errors='replace').strip())
                self.preview_stack.setCurrentWidget(self.preview_text)
            elif kind in {'anim', 'curve-anim'}:
                from ..cache.animation_viewer import AnimationViewerPanel

                if self._animation_panel is None:
                    self._animation_panel = AnimationViewerPanel(config_manager=self.config_manager)
                    self.preview_stack.addWidget(self._animation_panel)
                if self._animation_panel.load_animation(payload):
                    self.preview_stack.setCurrentWidget(self._animation_panel)
                else:
                    self.preview_info.setText(
                        'Animation detected, but no playable keyframes could be loaded.'
                    )
                    self.preview_stack.setCurrentWidget(self.preview_info)
            elif kind in {'rbxm', 'xml'} and (
                payload.startswith(b'<roblox!') or payload.lstrip().startswith((b'<roblox', b'<?xml'))
            ):
                from ..cache.rbxm_preview import RbxmPreviewWidget

                if self._model_panel is None:
                    self._model_panel = RbxmPreviewWidget()
                    self._model_panel.tree.setEditTriggers(
                        QAbstractItemView.EditTrigger.NoEditTriggers
                    )
                    self._model_panel.properties_table.setEditTriggers(
                        QAbstractItemView.EditTrigger.NoEditTriggers
                    )
                    self._model_panel.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
                    self._model_panel.properties_table.setContextMenuPolicy(
                        Qt.ContextMenuPolicy.NoContextMenu
                    )
                    self._model_panel.add_child_btn.setEnabled(False)
                    self._model_panel.add_property_btn.setEnabled(False)
                    self.preview_stack.addWidget(self._model_panel)
                self._model_panel.load_bytes(payload, entry.asset_id or entry.entry_id[:12])
                self.preview_stack.setCurrentWidget(self._model_panel)
            elif kind == 'json' or payload.lstrip().startswith((b'{', b'[')):
                try:
                    parsed = json.loads(payload.decode('utf-8-sig'))
                    text = json.dumps(parsed, ensure_ascii=False, indent=2)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    text = payload[:50_000].decode('utf-8', errors='replace')
                self.preview_text.setPlainText(text[:200_000])
                self.preview_stack.setCurrentWidget(self.preview_text)
            elif kind == 'xml':
                self.preview_text.setPlainText(payload[:200_000].decode('utf-8', errors='replace'))
                self.preview_stack.setCurrentWidget(self.preview_text)
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
                self.preview_text.setPlainText('\n'.join(lines))
                self.preview_stack.setCurrentWidget(self.preview_text)
        except Exception as exc:
            self.preview_info.setText(f'Preview failed: {type(exc).__name__}: {exc}')
            self.preview_stack.setCurrentWidget(self.preview_info)


    def _on_slot_image_ready(
        self, request_id: str, rgba: bytes, width: int, height: int
    ) -> None:
        if request_id != self._current_preview_request_id:
            return
        image = QImage(rgba, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
        if image.isNull():
            self._on_slot_preview_error(request_id, 'Image decoder returned an empty image.')
            return
        self._current_preview_image = image
        self._scale_slot_preview()
        self.preview_stack.setCurrentWidget(self.preview_image)

    def _on_slot_mesh_ready(self, request_id: str, obj_text: str) -> None:
        if request_id != self._current_preview_request_id:
            return
        try:
            if self._mesh_panel is None:
                from ..cache.obj_viewer import ObjViewerPanel

                self._mesh_panel = ObjViewerPanel(self.preview_panel, config_manager=self.config_manager)
                self.preview_stack.addWidget(self._mesh_panel)
            self._mesh_panel.load_obj(obj_text, request_id[:12])
            self.preview_stack.setCurrentWidget(self._mesh_panel)
        except Exception as exc:
            self._on_slot_preview_error(request_id, str(exc))

    def _on_slot_preview_error(self, request_id: str, error: str) -> None:
        if request_id != self._current_preview_request_id:
            return
        self.preview_info.setText(f'Preview could not be loaded: {error}')
        self.preview_stack.setCurrentWidget(self.preview_info)

    def _scale_slot_preview(self) -> None:
        if self._current_preview_image is None:
            return
        target = self.preview_image.size()
        if target.width() < 1 or target.height() < 1:
            return
        self.preview_image.setPixmap(
            QPixmap.fromImage(self._current_preview_image).scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.preview_stack.currentWidget() is self.preview_image:
            self._scale_slot_preview()

    def _render_active_swaps(self, *_args) -> None:
        """Render saved swap state in the same list-first style as Fleasion profiles."""
        try:
            manifest = self.store._manifest()
        except (OSError, ValueError):
            manifest = {}
        records = sorted(
            manifest.items(),
            key=lambda pair: str(pair[1].get('time') or ''),
            reverse=True,
        )
        query = self.swap_filter_edit.text().strip().casefold()
        status_filter = self.swap_status_filter.currentData()
        visible_records = []
        for target_id, record in records:
            status = self.store.swap_status(target_id)
            status_label = 'Active' if status == 'swapped' else 'Changed since swap'
            haystack = ' '.join((
                str(record.get('origin') or 'Local cache swap'),
                target_id,
                str(record.get('source') or ''),
                str(record.get('time') or ''),
                status_label,
            )).casefold()
            if query and query not in haystack:
                continue
            if status_filter == 'active' and status != 'swapped':
                continue
            if status_filter == 'changed' and status == 'swapped':
                continue
            visible_records.append((target_id, record, status, status_label))

        self.swap_count_label.setText(
            f'{len(visible_records):,} / {len(records):,} swaps'
            if query or status_filter != 'all' else f'{len(records):,} swaps'
        )
        self.swaps_table.setUpdatesEnabled(False)
        self.swaps_table.setRowCount(0)
        for target_id, record, status, status_label in visible_records:
            row = self.swaps_table.rowCount()
            self.swaps_table.insertRow(row)
            values = [
                status_label,
                str(record.get('origin') or 'Local cache swap'),
                target_id,
                str(record.get('source') or ''),
                str(record.get('time') or '—'),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if column == 2:
                    item.setData(Qt.ItemDataRole.UserRole, target_id)
                elif column == 3:
                    item.setData(Qt.ItemDataRole.UserRole, str(record.get('source') or ''))
                self.swaps_table.setItem(row, column, item)
        self.swaps_table.setUpdatesEnabled(True)
        self.swaps_table.resizeRowsToContents()
        self.swap_empty_label.setVisible(not visible_records)
        if not visible_records:
            if records:
                self.swap_empty_label.setText('No swaps match the selected filters. Clear Search or choose All swaps.')
            else:
                self.swap_empty_label.setText(
                    'No saved swaps yet. Use Cache Slots to browse every local entry, then choose a target and replacement ID.'
                )

    def refresh(self) -> None:
        """Start a metadata-only cache scan without freezing the desktop UI."""
        if self._scan_worker is not None and self._scan_worker.isRunning():
            self._scan_pending = True
            return

        root = self.root_edit.text().strip().strip('"')
        self.scan_button.setEnabled(False)
        self.scan_button.setText('Scanning…')
        self.status_label.setText('Scanning cache metadata…')
        worker = _CacheScanWorker(root, self)
        self._scan_worker = worker
        worker.completed.connect(self._on_scan_completed)
        worker.finished.connect(
            lambda current=worker: self._on_scan_worker_finished(current)
        )
        worker.start()

    def _on_scan_completed(self, store: object, entries: object, error: str) -> None:
        if error or not isinstance(store, RobloxCacheStore) or not isinstance(entries, list):
            self.entries = []
            self.entries_by_id = {}
            self._render_entries()
            self._render_active_swaps()
            self.status_label.setText(error or 'Cache scan returned an invalid result.')
            return

        self.store = store
        self.entries = entries
        self.entries_by_id = {entry.entry_id: entry for entry in entries}
        self._render_entries()
        self._render_active_swaps()
        active_swaps = sum(bool(entry.swap_status) for entry in self.entries)
        self.status_label.setText(
            f'Scanned {len(self.entries):,} cache entry/entries. '
            f'{active_swaps} slot(s) have swap state. '
            'Asset metadata is fetched only when you open Asset Lookup.'
        )
        log_buffer.log('LocalCache', f'Scanned {len(self.entries)} entries at {self.store.root}')

    def _on_scan_worker_finished(self, worker: _CacheScanWorker) -> None:
        if self._scan_worker is worker:
            self._scan_worker = None
            self.scan_button.setEnabled(True)
            self.scan_button.setText('Scan Cache')
        worker.deleteLater()
        if self._scan_pending:
            self._scan_pending = False
            QTimer.singleShot(0, self.refresh)

    def _open_asset_lookup(self) -> None:
        entry_id = self._selected_entry_id()
        if not entry_id or entry_id not in self.entries_by_id:
            return
        from .asset_lookup_dialog import AssetLookupDialog

        entry = self.entries_by_id[entry_id]
        cached = self._cache_metadata.get(entry_id, {})
        dialog = AssetLookupDialog(
            self,
            entry_id=entry_id,
            store=self.store,
            initial_asset_id=entry.asset_id or str(cached.get('asset_id') or ''),
            initial_metadata=cached,
            on_metadata=self._save_cache_metadata,
        )
        dialog.exec()

    def _save_cache_metadata(self, entry_id: str, metadata: dict[str, str]) -> None:
        settings = getattr(self.config_manager, 'settings', None)
        if not isinstance(settings, dict):
            return
        cached = self._cache_metadata.setdefault(entry_id, {})
        cached.update({str(key): str(value) for key, value in metadata.items() if value is not None})
        settings['local_db_cache_metadata'] = dict(list(self._cache_metadata.items())[-20000:])

        asset_id = str(metadata.get('asset_id') or '').strip()
        if asset_id.isdigit():
            names = settings.get('local_db_asset_names', {})
            if not isinstance(names, dict):
                names = {}
            asset_names = names.setdefault(asset_id, {})
            if isinstance(asset_names, dict):
                for key in (
                    'name', 'creator_name', 'created_at', 'updated_at', 'description',
                    'creator_type', 'creator_id', 'asset_type', 'price', 'price_status',
                    'favorite_count', 'source',
                ):
                    value = metadata.get(key)
                    if value:
                        asset_names[key] = str(value)
                settings['local_db_asset_names'] = names

        save = getattr(self.config_manager, 'save', None)
        if callable(save):
            try:
                save()
            except Exception as exc:
                log_buffer.log('LocalCache', f'Could not persist cache asset metadata: {exc}')

        entry = self.entries_by_id.get(entry_id)
        if entry is not None:
            details = [
                f'Asset ID: {metadata.get("asset_id") or entry.asset_id or "not found"}',
                f'Type: {entry.kind}',
                f'Name: {metadata.get("name") or self._entry_name(entry) or "not stored in local metadata"}',
                f'File/content size: {_format_size(entry.file_size)}',
                f'Database size: {_format_size(entry.database_size)}',
                f'Swap status: {entry.swap_status or "unmodified"}',
                f'Storage: {entry.storage_mode}',
            ]
            if metadata.get('asset_type_label'):
                details.append(f'Roblox asset type: {metadata["asset_type_label"]}')
            if metadata.get('asset_version'):
                details.append(f'Asset version: {metadata["asset_version"]}')
            if metadata.get('cdn_probe_status'):
                details.append(f'CDN metadata: {metadata["cdn_probe_status"]}')
            if metadata.get('cdn_probe_note'):
                details.append(str(metadata['cdn_probe_note']))
            if entry.url:
                details.append(f'URL: {entry.url}')
            self.preview_details.setText('\n'.join(details))
        self._render_entries()

    def _resolve_cache_names(self) -> None:
        """Resolve cached asset names in the background, sharing the Asset Database cache."""
        if self._view_mode != 'slots' or self._name_resolution_disabled:
            return
        settings = getattr(self.config_manager, 'settings', None)
        if not isinstance(settings, dict):
            return
        if self._name_resolver is not None and self._name_resolver.isRunning():
            return
        name_cache = settings.get('local_db_asset_names', {})
        if not isinstance(name_cache, dict):
            name_cache = {}
        asset_ids = []
        for entry in self.entries:
            asset_id = str(entry.asset_id or '').strip()
            if (
                asset_id.isdigit()
                and asset_id not in name_cache
                and asset_id not in self._name_resolution_attempted
                and asset_id not in asset_ids
            ):
                asset_ids.append(asset_id)
            if len(asset_ids) >= 50:
                break
        if not asset_ids:
            return

        self._name_resolution_attempted.update(asset_ids)
        try:
            from .database_asset_explorer import _AssetNameResolver

            worker = _AssetNameResolver(asset_ids, self)
            self._name_resolver = worker
            worker.names_ready.connect(self._on_cache_names_resolved)
            worker.finished.connect(
                lambda current=worker: self._on_cache_name_resolver_finished(current)
            )
            worker.start()
        except Exception as exc:
            self._name_resolution_disabled = True
            log_buffer.log('LocalCache', f'Asset name lookup unavailable: {exc}')

    def _on_cache_names_resolved(
        self, results: dict, processed_ids: list, failed: bool
    ) -> None:
        for asset_id in processed_ids:
            self._name_resolution_attempted.add(str(asset_id))
        if failed:
            self._name_resolution_disabled = True
            self.status_label.setText(
                'Roblox name lookup is unavailable; showing local names, asset IDs and cache hashes.'
            )
            return
        if not results:
            return

        settings = getattr(self.config_manager, 'settings', None)
        if not isinstance(settings, dict):
            return
        name_cache = settings.get('local_db_asset_names', {})
        if not isinstance(name_cache, dict):
            name_cache = {}
        for asset_id, metadata in results.items():
            if not isinstance(metadata, dict) or not metadata.get('name'):
                continue
            key = str(asset_id)
            name_cache[key] = {
                'name': str(metadata.get('name') or ''),
                'creator_name': str(metadata.get('creator_name') or ''),
                'created_at': str(metadata.get('created_at') or ''),
                'updated_at': str(metadata.get('updated_at') or ''),
            }
            for entry in self.entries:
                if str(entry.asset_id or '') == key:
                    entry.name = name_cache[key]['name']
        settings['local_db_asset_names'] = name_cache
        save = getattr(self.config_manager, 'save', None)
        if callable(save):
            try:
                save()
            except Exception as exc:
                log_buffer.log('LocalCache', f'Could not persist resolved asset names: {exc}')
        self._render_entries()

    def _on_cache_name_resolver_finished(self, worker) -> None:
        if self._name_resolver is not worker:
            return
        self._name_resolver = None
        if not self._name_resolution_disabled:
            QTimer.singleShot(100, self._resolve_cache_names)

    def _shutdown_workers(self) -> None:
        scan_worker = self._scan_worker
        if scan_worker is not None and scan_worker.isRunning():
            scan_worker.requestInterruption()
            scan_worker.wait()
        self._scan_worker = None
        self._stop_name_resolver()
        workers = tuple(self._preview_workers)
        for worker in workers:
            worker.requestInterruption()
        for worker in workers:
            worker.wait()
        self._preview_workers.clear()

    def _stop_name_resolver(self) -> None:
        worker = self._name_resolver
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.stop()
                worker.wait(3000)
        except RuntimeError:
            pass
        self._name_resolver = None

    def _selected_pair(self) -> tuple[str, str]:
        target = self.target_edit.text().strip().lower()
        source = self.source_edit.text().strip().lower()
        if not target:
            target = self._selected_entry_id()
            self.target_edit.setText(target)
        if len(target) != 32:
            raise RobloxCacheError('Select a target row or enter a 32-character target entry ID.')
        return target, source

    def _confirm_type_mismatch(
        self,
        target_id: str,
        source_kind: str,
        source_label: str,
        *,
        target_kind: str | None = None,
    ) -> bool:
        target = self.entries_by_id.get(target_id)
        target_kind = target_kind or (target.kind if target is not None else '')
        if not target_kind or not source_kind or source_kind == target_kind:
            return True
        if {target_kind, source_kind}.issubset({'anim', 'curve-anim'}):
            detail = (
                f'Target is {target_kind}; source is {source_kind}. Both are animation formats, '
                'the importer will attempt to convert the source to the target slot’s animation format and rig. '
                'If conversion fails, continuing will import the original payload unchanged and report a warning.'
            )
        else:
            detail = f'Target looks like {target_kind}; source looks like {source_kind}.'
        result = QMessageBox.warning(
            self,
            'Asset type mismatch',
            f'{detail}\n\nContinue replacing {target_id} with {source_label}?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return result == QMessageBox.StandardButton.Yes

    def _swap_selected_entries(self) -> None:
        try:
            target_id, source_id = self._selected_pair()
            if not self.entries_by_id:
                self.refresh()
            if len(source_id) != 32:
                raise RobloxCacheError('Enter a 32-character source entry ID.')
            target = self.entries_by_id.get(target_id)
            source = self.entries_by_id.get(source_id)
            if source is None:
                raise RobloxCacheError(
                    'Source entry was not found in the current scan. For imported files, use Import file → target.'
                )
            if target and not self._confirm_type_mismatch(target_id, source.kind, source_id):
                return
            answer = QMessageBox.warning(
                self,
                'Confirm whole-entry swap',
                f'Replace cache entry {target_id} with the complete file for {source_id}?\n\n'
                'Fleasion will back up rbx-storage.db, save the original target file, replace the '
                'target file, and update exactly its files.size row. Close Roblox first.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            swap_name = self.swap_name_edit.text().strip() or 'Local cache swap'
            message = self.store.swap(target_id, source_id, origin=swap_name)
            self.output.appendPlainText(message)
            self.status_label.setText(message)
            self.refresh()
        except (RobloxCacheError, OSError, sqlite3.Error) as exc:
            self._show_error('Cache swap failed', str(exc))

    def _import_file(self) -> None:
        try:
            target_id, _source_id = self._selected_pair()
        except RobloxCacheError as exc:
            self._show_error('Select a target', str(exc))
            return
        file_name, _filter = QFileDialog.getOpenFileName(
            self,
            'Import payload into selected Roblox cache slot',
            '',
            'Asset files (*.rbxm *.rbxmx *.mesh *.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff *.dds *.ktx *.ktx2 *.ogg *.mp3 *.wav *.ttf *.otf *.bin);;All files (*.*)',
        )
        if not file_name:
            return
        path = Path(file_name)
        try:
            if not path.is_file():
                raise RobloxCacheError(f'Import file not found:\n{path}')
            if path.stat().st_size <= 0:
                raise RobloxCacheError('The selected import file is empty.')
            if path.stat().st_size > 100_000_000:
                raise RobloxCacheError('The selected file exceeds the 100 MB import limit.')
            payload = path.read_bytes()
            source_kind = detect_roblox_payload_kind(payload)

            # Inspect the actual slot data, not just the currently displayed table.
            # If this target is already swapped, classify its saved original.
            target_data, _target_mode = self.store._get_entry_data(target_id)
            manifest_record = self.store._manifest().get(target_id, {})
            original_path = self.store.originals_dir / target_id
            target_template = (
                original_path.read_bytes()
                if manifest_record and original_path.is_file()
                else target_data
            )
            _target_url, target_payload, target_has_header = split_rbxh_entry(target_template)
            if not target_has_header:
                raise RobloxCacheError(
                    'The selected target is not a recognized RBXH cache entry. '
                    'No cache bytes were changed.'
                )
            target_kind = detect_roblox_cache_payload_kind(target_payload)

            allow_type_mismatch = source_kind != target_kind and source_kind != 'other' and target_kind != 'other'
            if not self._confirm_type_mismatch(
                target_id,
                source_kind,
                path.name,
                target_kind=target_kind,
            ):
                return

            conversion_preview_text = ''
            if source_kind in {'anim', 'curve-anim'} and target_kind in {'anim', 'curve-anim'}:
                try:
                    conversion_preview = convert_animation_for_slot(payload, target_payload)
                except AnimationConversionError as exc:
                    conversion_preview_text = (
                        'Automatic animation conversion failed. Continuing will import the original '
                        'payload unchanged and report the failure.\n'
                        f'Conversion warning: {exc}'
                    )
                else:
                    if conversion_preview.changed:
                        note_text = '\n'.join(f'• {note}' for note in conversion_preview.notes)
                        if not note_text:
                            note_text = 'Converted the animation payload to match the target slot.'
                        conversion_preview_text = 'Animation conversion preview:\n' + note_text
                    else:
                        conversion_preview_text = (
                            'Animation conversion preview: no type or rig conversion is needed.'
                        )

            animation_report = (
                inspect_animation_payload(payload)
                if source_kind in {'anim', 'curve-anim'}
                else None
            )
            # Preflight findings are shown as a warning in the confirmation below, never as a block
            allow_suspicious_animation = True

            report_text = (
                animation_report.format()
                if animation_report is not None
                else f'Detected format: {source_kind}'
            )
            answer = QMessageBox.warning(
                self,
                'Confirm file import',
                f'Use {path.name} as the payload for cache slot {target_id}?\n\n'
                f'{report_text}\n\n'
                + (f'{conversion_preview_text}\n\n' if conversion_preview_text else '')
                + (
                    'Warnings above do not stop the import: the file will be written as it is, '
                    'and a damaged animation may play wrongly or not at all.\n\n'
                    if animation_report is not None and animation_report.requires_override
                    else ''
                )
                + 'Fleasion will back up the database, preserve the target URL/header, update only '
                'the verified payload-length field, write the entry and read it back to verify '
                'the stored bytes and files.size row. This cannot guarantee Roblox will play the asset.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            message = self.store.import_file(
                target_id,
                path,
                origin=f'file {path.name}',
                allow_suspicious_animation=allow_suspicious_animation,
                allow_type_mismatch=allow_type_mismatch,
            )
            self.refresh()
            self.output.appendPlainText(message)
            self.status_label.setText(message.splitlines()[0])
            QMessageBox.information(self, 'Import write verified', message)
        except (RobloxCacheError, OSError, sqlite3.Error) as exc:
            self._show_error('File import failed', str(exc))

    def _verify_selected_target(self) -> None:
        """Read the selected target back and report whether the intended swap is still present."""
        row = self.swaps_table.currentRow()
        target_id = ''
        if row >= 0:
            item = self.swaps_table.item(row, 2)
            if item is not None:
                target_id = str(item.data(Qt.ItemDataRole.UserRole) or item.text()).strip().lower()
        if not target_id:
            target_id = self.target_edit.text().strip().lower()
        if len(target_id) != 32:
            self._show_error(
                'Select a target',
                'Select an Active Swaps row or paste a 32-character target cache ID first.',
            )
            return
        try:
            record = self.store._manifest().get(target_id, {})
            data, storage_mode = self.store._get_entry_data(target_id)
            url, payload, has_header = split_rbxh_entry(data)
            db_size = self.store.database_size(target_id)
            status = self.store.swap_status(target_id)
            kind = detect_roblox_cache_payload_kind(payload)
            lines = [
                f'Target cache ID: {target_id}',
                f'Swap state: {status or "no saved swap"}',
                f'Storage: {storage_mode}',
                f'Payload type: {kind}',
                f'RBXH header: {"valid" if has_header else "not found"}',
                f'Header URL: {url or "(empty)"}',
                f'Entry bytes: {len(data):,}',
                f'files.size: {db_size}',
                f'Saved source: {record.get("source") or "(none)"}',
                f'Applied at: {record.get("time") or "(unknown)"}',
            ]
            animation_report = (
                inspect_animation_payload(payload)
                if kind in {'anim', 'curve-anim'}
                else None
            )
            if animation_report is not None:
                lines.extend(['', animation_report.format()])
            if status != 'swapped':
                lines.extend([
                    '',
                    'The entry no longer matches the replacement bytes saved by Fleasion. '
                    'Roblox may have refreshed/replaced it, or another tool changed it.',
                ])
            if db_size != len(data):
                lines.extend([
                    '',
                    'WARNING: files.size does not match the current entry size.',
                ])
            details = '\n'.join(lines)
            if status == 'swapped' and has_header and db_size == len(data):
                QMessageBox.information(self, 'Target verification', details)
                self.status_label.setText(f'Verified {target_id[:8]}…: replacement bytes still present.')
            else:
                QMessageBox.warning(self, 'Target verification', details)
                self.status_label.setText(f'Verification warning for {target_id[:8]}…: inspect the report.')
        except (RobloxCacheError, OSError, sqlite3.Error) as exc:
            self._show_error('Target verification failed', str(exc))

    def _restore_target(self) -> None:
        try:
            target_id, _source = self._selected_pair()
            answer = QMessageBox.question(
                self,
                'Restore cache slot',
                f'Restore the saved original for {target_id}?\n\nA database backup will be created first.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            message = self.store.restore(target_id)
            self.output.appendPlainText(message)
            self.status_label.setText(message)
            self.refresh()
        except (RobloxCacheError, OSError, sqlite3.Error) as exc:
            self._show_error('Restore failed', str(exc))

    def _restore_all(self) -> None:
        answer = QMessageBox.warning(
            self,
            'Restore all cache slots',
            'Restore every slot with an active replacement? This writes the local database and entry files. '
            'Roblox must be completely closed.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        messages = self.store.restore_all()
        self.output.appendPlainText('\n'.join(messages))
        self.status_label.setText(messages[-1] if messages else 'Nothing to restore.')
        self.refresh()

    def _export_selected(self) -> None:
        entry_id = self._selected_entry_id()
        if not entry_id:
            entry_id = self.target_edit.text().strip().lower()
        if len(entry_id) != 32:
            self._show_error('Select an entry', 'Select a table row or enter an entry ID to export.')
            return
        folder = QFileDialog.getExistingDirectory(self, 'Export entry and report', str(Path.home()))
        if not folder:
            return
        try:
            path = self.store.export_entry(entry_id, folder)
            self.output.appendPlainText(f'Exported {path}')
            self.status_label.setText(f'Entry exported to {path}')
        except (RobloxCacheError, OSError, sqlite3.Error) as exc:
            self._show_error('Export failed', str(exc))

    def _probe_header(self) -> None:
        try:
            self.store = RobloxCacheStore(self.root_edit.text().strip().strip('"'))
            report = self.store.probe_header()
        except (RobloxCacheError, OSError) as exc:
            self._show_error('Header probe failed', str(exc))
            return
        self.output.setPlainText(report)
        self.status_label.setText('Header probe completed. Payload-length fields are used only if supported by enough real samples.')

    def _show_error(self, title: str, detail: str) -> None:
        self.status_label.setText(detail)
        self.output.appendPlainText(f'{title}: {detail}')
        QMessageBox.warning(self, title, detail)
