"""Asset lookup dialog for enriching one selected Roblox cache slot."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QThread, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..database.roblox_cache_store import RobloxCacheStore
from ..utils.asset_lookup import (
    AssetDownloadResult,
    AssetLookupError,
    download_asset,
    download_cached_asset,
    inspect_cache_entry,
    lookup_asset_metadata,
    normalize_asset_id,
)


class _AssetTaskWorker(QThread):
    completed = pyqtSignal(str, object, str)

    def __init__(self, task_name: str, task: Callable[[], object], parent: QWidget | None = None):
        super().__init__(parent)
        self.task_name = task_name
        self.task = task

    def run(self) -> None:
        try:
            self.completed.emit(self.task_name, self.task(), '')
        except AssetLookupError as exc:
            self.completed.emit(self.task_name, None, str(exc))
        except Exception as exc:
            self.completed.emit(self.task_name, None, f'{type(exc).__name__}: {exc}')


class AssetLookupDialog(QDialog):
    """Inspect a selected cache slot and optionally look up or download by numeric ID."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        entry_id: str,
        store: RobloxCacheStore,
        initial_asset_id: str = '',
        initial_metadata: dict[str, str] | None = None,
        on_metadata: Callable[[str, dict[str, str]], None] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f'Cache Asset Lookup · {entry_id[:12]}')
        self.resize(760, 600)
        self._entry_id = entry_id
        self._store = store
        self._on_metadata = on_metadata
        self._asset_id = initial_asset_id.strip()
        self._metadata = {
            str(key): str(value)
            for key, value in (initial_metadata or {}).items()
            if value is not None
        }
        self._source_url = self._metadata.get('source_url', '')
        self._worker: _AssetTaskWorker | None = None
        self._build_ui()
        self._render_metadata()
        self._start_inspection()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        intro = QLabel(
            'Inspect the selected cache payload and its original Roblox CDN URL. '
            'A numeric Asset ID is optional; the CDN may expose metadata even when the ID is absent.'
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.cache_label = QLabel(f'Cache entry: {self._entry_id}')
        self.cache_label.setTextInteractionFlags(
            self.cache_label.textInteractionFlags()
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        root.addWidget(self.cache_label)

        self.metadata_view = QPlainTextEdit()
        self.metadata_view.setReadOnly(True)
        self.metadata_view.setPlaceholderText('Inspecting this cache entry…')
        root.addWidget(self.metadata_view, 1)

        root.addWidget(QLabel('Optional: look up another numeric Roblox Asset ID'))
        input_row = QHBoxLayout()
        self.asset_id_edit = QLineEdit(self._asset_id)
        self.asset_id_edit.setPlaceholderText('Numeric Asset ID')
        self.asset_id_edit.textChanged.connect(self._update_id_download_button)
        self.asset_id_edit.returnPressed.connect(self._start_id_lookup)
        input_row.addWidget(self.asset_id_edit, 1)
        self.lookup_button = QPushButton('Look up ID')
        self.lookup_button.clicked.connect(self._start_id_lookup)
        input_row.addWidget(self.lookup_button)
        root.addLayout(input_row)

        self.status_label = QLabel('Inspecting selected cache slot…')
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.open_browser_button = QPushButton('Open Roblox page')
        self.open_browser_button.setEnabled(False)
        self.open_browser_button.clicked.connect(self._open_browser_page)
        buttons.addWidget(self.open_browser_button)
        self.download_cached_button = QPushButton('Download cached file…')
        self.download_cached_button.setEnabled(bool(self._source_url))
        self.download_cached_button.clicked.connect(self._start_cached_download)
        buttons.addWidget(self.download_cached_button)
        self.download_id_button = QPushButton('Download by ID…')
        self.download_id_button.setEnabled(bool(self._asset_id))
        self.download_id_button.clicked.connect(self._start_id_download)
        buttons.addWidget(self.download_id_button)
        buttons.addStretch(1)
        close_button = QPushButton('Close')
        close_button.clicked.connect(self.close)
        buttons.addWidget(close_button)
        root.addLayout(buttons)

    def _set_busy(self, busy: bool) -> None:
        self.lookup_button.setEnabled(not busy)
        self.download_cached_button.setEnabled(bool(self._source_url) and not busy)
        self._update_id_download_button(busy=busy)
        self.open_browser_button.setEnabled(bool(self._metadata.get('asset_url')) and not busy)

    def _update_id_download_button(self, *_args, busy: bool | None = None) -> None:
        worker_busy = (
            self._worker is not None and self._worker.isRunning()
            if busy is None else busy
        )
        has_asset_id = bool(
            self._asset_id or self.asset_id_edit.text().strip().isdigit()
        )
        self.download_id_button.setEnabled(has_asset_id and not worker_busy)

    def _start_task(self, name: str, task: Callable[[], object], message: str) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self.status_label.setText(message)
        self._set_busy(True)
        worker = _AssetTaskWorker(name, task, self)
        self._worker = worker
        worker.completed.connect(self._on_task_completed)
        worker.finished.connect(lambda current=worker: self._worker_finished(current))
        worker.start()

    def _start_inspection(self) -> None:
        self._start_task(
            'inspect',
            lambda: inspect_cache_entry(self._entry_id, self._store),
            'Reading local payload and probing Roblox CDN headers…',
        )

    def _start_id_lookup(self) -> None:
        try:
            asset_id = normalize_asset_id(self.asset_id_edit.text())
        except AssetLookupError as exc:
            self.status_label.setText(str(exc))
            return
        self._asset_id = asset_id
        self._start_task(
            'lookup',
            lambda: lookup_asset_metadata(asset_id),
            f'Looking up Roblox Asset ID {asset_id}…',
        )

    def _on_task_completed(self, task_name: str, result: object, error: str) -> None:
        if error:
            self.status_label.setText(error)
            self._set_busy(False)
            return

        if task_name in {'inspect', 'lookup'}:
            if not isinstance(result, dict):
                self.status_label.setText('Metadata lookup returned an invalid result.')
                return
            metadata = {str(key): str(value) for key, value in result.items() if value is not None}
            if task_name == 'inspect':
                self._source_url = metadata.get('source_url', self._source_url)
                self.status_label.setText(
                    metadata.get('cdn_probe_note')
                    or metadata.get('metadata_lookup_note')
                    or 'Cache entry inspection completed.'
                )
            else:
                self.status_label.setText('Roblox metadata retrieved.')
            self._asset_id = metadata.get('asset_id', self._asset_id)
            if self._asset_id:
                self.asset_id_edit.setText(self._asset_id)
            self._metadata.update(metadata)
            if self._on_metadata is not None:
                self._on_metadata(self._entry_id, dict(self._metadata))
        elif task_name in {'download_cached', 'download_id'}:
            if not isinstance(result, AssetDownloadResult):
                self.status_label.setText('Download finished without a valid result.')
                return
            self.status_label.setText(
                f'Saved {result.path.name} · {result.size:,} bytes · SHA-256 {result.sha256}'
            )
            QMessageBox.information(
                self,
                'Asset downloaded',
                f'Saved to:\n{result.path}\n\n'
                f'Size: {result.size:,} bytes\nSHA-256: {result.sha256}',
            )
        self._render_metadata()

    def _render_metadata(self) -> None:
        labels = {
            'cache_entry_id': 'Cache entry ID',
            'cache_payload_size': 'Cache payload size',
            'asset_id': 'Asset ID',
            'name': 'Name',
            'description': 'Description',
            'creator_name': 'Creator',
            'creator_type': 'Creator type',
            'creator_id': 'Creator ID',
            'asset_type_label': 'Roblox asset type',
            'asset_type_id': 'Asset type ID',
            'asset_type': 'Asset type',
            'asset_version': 'Asset version',
            'created_at': 'Created',
            'updated_at': 'Updated',
            'price': 'Price',
            'price_status': 'Price status',
            'favorite_count': 'Favorites',
            'cdn_hash': 'CDN content hash',
            'cdn_host': 'CDN host',
            'cdn_url_expires_at': 'Signed URL expiry',
            'cdn_probe_status': 'CDN probe',
            'cdn_probe_note': 'CDN note',
            'cdn_content_type': 'CDN content type',
            'cdn_content_length': 'CDN content length',
            'uncompressed_size': 'Uncompressed size',
            'cdn_last_modified': 'CDN last modified',
            'metadata_lookup_note': 'Asset ID lookup note',
            'source': 'Metadata source',
            'source_url': 'Original cache URL',
            'asset_url': 'Roblox page',
        }
        lines = [
            f'{label}: {self._metadata[key]}'
            for key, label in labels.items()
            if self._metadata.get(key)
        ]
        self.metadata_view.setPlainText('\n'.join(lines) or 'No metadata has been resolved yet.')

    def _start_cached_download(self) -> None:
        if not self._source_url:
            self.status_label.setText('This cache entry has no stored CDN URL.')
            return
        target, _filter = QFileDialog.getSaveFileName(
            self,
            'Save cached Roblox asset',
            str(Path.home() / f'cache_{self._entry_id[:12]}.bin'),
            'All files (*.*)',
        )
        if not target:
            return
        url = self._source_url
        self._start_task(
            'download_cached',
            lambda: download_cached_asset(url, Path(target)),
            'Downloading cached asset in the background…',
        )

    def _start_id_download(self) -> None:
        try:
            asset_id = normalize_asset_id(self.asset_id_edit.text() or self._asset_id)
        except AssetLookupError as exc:
            self.status_label.setText(str(exc))
            return
        target, _filter = QFileDialog.getSaveFileName(
            self,
            'Save Roblox asset by ID',
            str(Path.home() / f'asset_{asset_id}.bin'),
            'All files (*.*)',
        )
        if not target:
            return
        self._asset_id = asset_id
        self._start_task(
            'download_id',
            lambda: download_asset(asset_id, Path(target)),
            f'Downloading Asset ID {asset_id} using normal Roblox Asset Delivery…',
        )

    def _open_browser_page(self) -> None:
        url = self._metadata.get('asset_url')
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _worker_finished(self, worker: _AssetTaskWorker) -> None:
        if self._worker is worker:
            self._worker = None
            self._set_busy(False)
        worker.deleteLater()

    def closeEvent(self, event) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.requestInterruption()
            self._worker.wait()
        super().closeEvent(event)
