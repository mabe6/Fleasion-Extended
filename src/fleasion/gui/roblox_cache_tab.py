"""GUI tab for reading and modifying local Roblox cache entries without the proxy."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..database.roblox_cache_store import (
    RobloxCacheEntry,
    RobloxCacheError,
    RobloxCacheStore,
    default_roblox_cache_root,
    detect_roblox_payload_kind,
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

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.store = RobloxCacheStore()
        self.entries: list[RobloxCacheEntry] = []
        self.entries_by_id: dict[str, RobloxCacheEntry] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel('Roblox cache root:'))
        self.root_edit = QLineEdit(str(default_roblox_cache_root()))
        self.root_edit.setPlaceholderText('Folder containing rbx-storage.db and rbx-storage/')
        path_row.addWidget(self.root_edit, 1)
        browse_button = QPushButton('Browse…')
        browse_button.clicked.connect(self._browse_root)
        path_row.addWidget(browse_button)
        refresh_button = QPushButton('Scan cache')
        refresh_button.clicked.connect(self.refresh)
        path_row.addWidget(refresh_button)
        root.addLayout(path_row)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel('Filter:'))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('Entry ID, asset ID, type or URL')
        self.search_edit.textChanged.connect(self._render_entries)
        search_row.addWidget(self.search_edit, 1)
        root.addLayout(search_row)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            ['Entry ID', 'Asset ID', 'Kind', 'File/content size', 'DB size', 'Modified', 'Swap status', 'Storage', 'URL']
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemDoubleClicked.connect(lambda _item: self._set_selected_as_target())
        root.addWidget(self.table, 1)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel('Target slot:'))
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText('32-character cache entry ID to replace')
        target_row.addWidget(self.target_edit, 1)
        set_target = QPushButton('Selected → target')
        set_target.clicked.connect(self._set_selected_as_target)
        target_row.addWidget(set_target)
        root.addLayout(target_row)

        source_row = QHBoxLayout()
        source_row.addWidget(QLabel('Source entry:'))
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText('32-character source entry ID')
        source_row.addWidget(self.source_edit, 1)
        set_source = QPushButton('Selected → source')
        set_source.clicked.connect(self._set_selected_as_source)
        source_row.addWidget(set_source)
        root.addLayout(source_row)

        actions = QHBoxLayout()
        self.swap_button = QPushButton('Swap source → target')
        self.swap_button.clicked.connect(self._swap_selected_entries)
        actions.addWidget(self.swap_button)
        import_button = QPushButton('Import file → target…')
        import_button.clicked.connect(self._import_file)
        actions.addWidget(import_button)
        restore_button = QPushButton('Restore target')
        restore_button.clicked.connect(self._restore_target)
        actions.addWidget(restore_button)
        restore_all_button = QPushButton('Restore ALL')
        restore_all_button.clicked.connect(self._restore_all)
        actions.addWidget(restore_all_button)
        export_button = QPushButton('Export entry…')
        export_button.clicked.connect(self._export_selected)
        actions.addWidget(export_button)
        probe_button = QPushButton('Probe header')
        probe_button.clicked.connect(self._probe_header)
        actions.addWidget(probe_button)
        root.addLayout(actions)

        self.status_label = QLabel(
            'Local mode: no proxy traffic is required. Scan the cache folder to begin.'
        )
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText('Operation details and header-probe output appear here.')
        self.output.setMaximumBlockCount(500)
        self.output.setMaximumHeight(145)
        root.addWidget(self.output)

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
            self.status_label.setText(str(exc))
            return
        self._render_entries()
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

    def _confirm_type_mismatch(self, target_id: str, source_kind: str, source_label: str) -> bool:
        target = self.entries_by_id.get(target_id)
        if target is None or not source_kind or source_kind == target.kind:
            return True
        if {target.kind, source_kind}.issubset({'anim', 'curve-anim'}):
            detail = (
                f'Target is {target.kind}; source is {source_kind}. Both are animation formats, '
                'but compatibility has not been verified.'
            )
        else:
            detail = f'Target looks like {target.kind}; source looks like {source_kind}.'
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
            message = self.store.swap(target_id, source_id, origin='Fleasion Local Cache tab')
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
            with path.open('rb') as handle:
                sample = handle.read(8192)
            source_kind = detect_roblox_payload_kind(sample)
            if not self._confirm_type_mismatch(target_id, source_kind, path.name):
                return
            answer = QMessageBox.warning(
                self,
                'Confirm file import',
                f'Use {path.name} as the payload for cache slot {target_id}?\n\n'
                'Fleasion will retain the target URL/header, update only the payload-length field(s) '
                'that can be verified from existing cache samples, back up the database, save the '
                'original file, and update the target size row. If header layout cannot be proved, '
                'the import will be refused.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            message = self.store.import_file(target_id, path, origin=f'file {path.name}')
            self.output.appendPlainText(message)
            self.status_label.setText(message)
            self.refresh()
        except (RobloxCacheError, OSError, sqlite3.Error) as exc:
            self._show_error('File import failed', str(exc))

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
