"""GUI tab for reading and modifying local Roblox cache entries without the proxy."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
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
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..database.animation_payload import inspect_animation_payload
from ..database.roblox_cache_store import (
    RobloxCacheEntry,
    RobloxCacheError,
    RobloxCacheStore,
    default_roblox_cache_root,
    detect_roblox_payload_kind,
    split_rbxh_entry,
)
from ..utils import log_buffer


def _format_size(value: int | None) -> str:
    if value is None:
        return '—'
    size = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if size < 1024 or unit == 'GiB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return str(value)


class RobloxCacheTab(QWidget):
    """Browse local rbx-storage entries, replace a whole slot and restore originals."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        config_manager=None,
        additional_tab: QWidget | None = None,
    ):
        super().__init__(parent)
        self.config_manager = config_manager
        self.store = RobloxCacheStore()
        self.entries: list[RobloxCacheEntry] = []
        self.entries_by_id: dict[str, RobloxCacheEntry] = {}
        settings = getattr(config_manager, 'settings', {})
        self._probe_header_enabled = bool(
            settings.get('show_cache_probe_header', False)
            if isinstance(settings, dict) else False
        )
        self._build_ui()
        if additional_tab is not None:
            self.pages.addTab(additional_tab, 'Asset Database')
        self._render_active_swaps()

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
        refresh_button = QPushButton('Scan Cache')
        refresh_button.clicked.connect(self.refresh)
        path_row.addWidget(refresh_button)
        entries_layout.addLayout(path_row)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel('Filter:'))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('Cache ID, asset ID, type or URL')
        self.search_edit.textChanged.connect(self._render_entries)
        search_row.addWidget(self.search_edit, 1)
        entries_layout.addLayout(search_row)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            ['Entry ID', 'Asset ID', 'Kind', 'File/content size', 'DB size', 'Modified', 'Swap status', 'Storage', 'URL']
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemDoubleClicked.connect(self._set_selected_as_target)
        entries_layout.addWidget(self.table, 1)

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

    def _visible_entries(self) -> list[RobloxCacheEntry]:
        query = self.search_edit.text().strip().casefold()
        if not query:
            return self.entries
        return [
            entry for entry in self.entries
            if query in entry.entry_id.casefold()
            or query in entry.asset_id.casefold()
            or query in entry.kind.casefold()
            or query in entry.url.casefold()
            or query in entry.swap_status.casefold()
        ]

    def _render_entries(self, *_args) -> None:
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(0)
        for entry in self._visible_entries():
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

    def refresh(self) -> None:
        root = self.root_edit.text().strip().strip('"')
        try:
            self.store = RobloxCacheStore(root)
            self.entries = self.store.scan()
            self.entries_by_id = {entry.entry_id: entry for entry in self.entries}
        except (RobloxCacheError, OSError) as exc:
            self.entries = []
            self.entries_by_id = {}
            self._render_entries()
            self._render_active_swaps()
            self.status_label.setText(str(exc))
            return
        self._render_entries()
        self._render_active_swaps()
        active_swaps = sum(bool(entry.swap_status) for entry in self.entries)
        self.status_label.setText(
            f'Scanned {len(self.entries):,} cache entry/entries. '
            f'{active_swaps} slot(s) have swap state. '
            'Writes are blocked while Roblox Player, Studio or Sober is running.'
        )
        log_buffer.log('LocalCache', f'Scanned {len(self.entries)} entries at {self.store.root}')

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
                'but KeyframeSequence and CurveAnimation are not converted by this importer.'
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
            target_kind = detect_roblox_payload_kind(target_payload)

            allow_type_mismatch = source_kind != target_kind and source_kind != 'other' and target_kind != 'other'
            if not self._confirm_type_mismatch(
                target_id,
                source_kind,
                path.name,
                target_kind=target_kind,
            ):
                return

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
            kind = detect_roblox_payload_kind(payload)
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
