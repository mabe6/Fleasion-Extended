"""Dialog for looking up and downloading Roblox assets by numeric ID."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QThread, QUrl, pyqtSignal
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

from ..utils.asset_lookup import (
    AssetDownloadResult,
    AssetLookupError,
    download_asset,
    lookup_asset_metadata,
    normalize_asset_id,
)


class _LookupWorker(QThread):
    completed = pyqtSignal(dict, str)

    def __init__(self, asset_id: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.asset_id = asset_id

    def run(self) -> None:
        try:
            self.completed.emit(lookup_asset_metadata(self.asset_id), '')
        except AssetLookupError as exc:
            self.completed.emit({}, str(exc))
        except Exception as exc:
            self.completed.emit({}, f'{type(exc).__name__}: {exc}')


class _DownloadWorker(QThread):
    completed = pyqtSignal(object, str)

    def __init__(
        self,
        asset_id: str,
        destination: Path,
        api_key: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.asset_id = asset_id
        self.destination = destination
        self.api_key = api_key

    def run(self) -> None:
        try:
            self.completed.emit(
                download_asset(self.asset_id, self.destination, api_key=self.api_key),
                '',
            )
        except AssetLookupError as exc:
            self.completed.emit(None, str(exc))
        except Exception as exc:
            self.completed.emit(None, f'{type(exc).__name__}: {exc}')


class AssetLookupDialog(QDialog):
    """Look up public metadata and optionally download an asset to a chosen path."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        on_metadata: Callable[[str, dict[str, str]], None] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle('Roblox Asset Lookup')
        self.resize(720, 560)
        self._on_metadata = on_metadata
        self._asset_id = ''
        self._metadata: dict[str, str] = {}
        self._worker: QThread | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.addWidget(QLabel('Enter a numeric Roblox Asset ID to retrieve available metadata.'))

        input_row = QHBoxLayout()
        input_row.addWidget(QLabel('Asset ID'))
        self.asset_id_edit = QLineEdit()
        self.asset_id_edit.setPlaceholderText('e.g. 123456789')
        self.asset_id_edit.returnPressed.connect(self._start_lookup)
        input_row.addWidget(self.asset_id_edit, 1)
        self.lookup_button = QPushButton('Look up')
        self.lookup_button.clicked.connect(self._start_lookup)
        input_row.addWidget(self.lookup_button)
        root.addLayout(input_row)

        self.metadata_view = QPlainTextEdit()
        self.metadata_view.setReadOnly(True)
        self.metadata_view.setPlaceholderText('Asset metadata will appear here.')
        root.addWidget(self.metadata_view, 1)

        root.addWidget(QLabel('Open Cloud API key (optional; only used for this download):'))
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText('Leave blank to use normal Asset Delivery')
        root.addWidget(self.api_key_edit)

        note = QLabel(
            'Private or restricted assets may require an Open Cloud API key that has '
            'Asset Delivery read permission. The key is not saved by Fleasion.'
        )
        note.setWordWrap(True)
        root.addWidget(note)

        self.status_label = QLabel('Ready')
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.open_browser_button = QPushButton('Open Roblox page')
        self.open_browser_button.setEnabled(False)
        self.open_browser_button.clicked.connect(self._open_browser_page)
        buttons.addWidget(self.open_browser_button)
        self.download_button = QPushButton('Download original…')
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self._start_download)
        buttons.addWidget(self.download_button)
        buttons.addStretch()
        close_button = QPushButton('Close')
        close_button.clicked.connect(self.close)
        buttons.addWidget(close_button)
        root.addLayout(buttons)

    def _set_busy(self, busy: bool) -> None:
        self.lookup_button.setEnabled(not busy)
        self.download_button.setEnabled(bool(self._metadata) and not busy)

    def _start_lookup(self) -> None:
        try:
            asset_id = normalize_asset_id(self.asset_id_edit.text())
        except AssetLookupError as exc:
            QMessageBox.warning(self, 'Invalid Asset ID', str(exc))
            return
        if self._worker is not None and self._worker.isRunning():
            return
        self._asset_id = asset_id
        self._metadata = {}
        self.metadata_view.clear()
        self.download_button.setEnabled(False)
        self.open_browser_button.setEnabled(False)
        self.status_label.setText(f'Looking up asset {asset_id}…')
        self._set_busy(True)
        worker = _LookupWorker(asset_id, self)
        self._worker = worker
        worker.completed.connect(self._on_lookup_completed)
        worker.finished.connect(lambda current=worker: self._worker_finished(current))
        worker.start()

    def _on_lookup_completed(self, metadata: dict, error: str) -> None:
        if error:
            self.status_label.setText(error)
            return
        self._metadata = {str(key): str(value) for key, value in metadata.items()}
        labels = (
            ('Asset ID', 'asset_id'),
            ('Name', 'name'),
            ('Creator', 'creator_name'),
            ('Creator type', 'creator_type'),
            ('Creator ID', 'creator_id'),
            ('Asset type', 'asset_type'),
            ('Created', 'created_at'),
            ('Updated', 'updated_at'),
            ('Price', 'price'),
            ('Price status', 'price_status'),
            ('Favorites', 'favorite_count'),
            ('Description', 'description'),
            ('Metadata source', 'source'),
            ('Roblox page', 'asset_url'),
        )
        lines = [
            f'{label}: {self._metadata.get(key) or "Not available"}'
            for label, key in labels
        ]
        self.metadata_view.setPlainText('\n'.join(lines))
        self.status_label.setText('Metadata retrieved. The original file can be downloaded separately.')
        self.download_button.setEnabled(True)
        self.open_browser_button.setEnabled(bool(self._metadata.get('asset_url')))
        if self._on_metadata is not None:
            self._on_metadata(self._asset_id, self._metadata)

    def _start_download(self) -> None:
        if not self._asset_id or (self._worker is not None and self._worker.isRunning()):
            return
        target, _selected_filter = QFileDialog.getSaveFileName(
            self,
            'Save original Roblox asset',
            str(Path.home() / f'asset_{self._asset_id}.bin'),
            'All files (*.*)',
        )
        if not target:
            return
        self.status_label.setText('Downloading original asset in the background…')
        self._set_busy(True)
        worker = _DownloadWorker(
            self._asset_id,
            Path(target),
            self.api_key_edit.text().strip(),
            self,
        )
        self._worker = worker
        worker.completed.connect(self._on_download_completed)
        worker.finished.connect(lambda current=worker: self._worker_finished(current))
        worker.start()

    def _on_download_completed(self, result: object, error: str) -> None:
        if error:
            self.status_label.setText(error)
            return
        if not isinstance(result, AssetDownloadResult):
            self.status_label.setText('Download finished without a valid result.')
            return
        self.status_label.setText(
            f'Saved {result.path.name} · {result.size:,} bytes · SHA-256 {result.sha256}'
        )
        QMessageBox.information(
            self,
            'Asset downloaded',
            f'Original asset saved to:\n{result.path}\n\n'
            f'Size: {result.size:,} bytes\nSHA-256: {result.sha256}',
        )

    def _open_browser_page(self) -> None:
        url = self._metadata.get('asset_url')
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _worker_finished(self, worker: QThread) -> None:
        if self._worker is worker:
            self._worker = None
            self._set_busy(False)
        worker.deleteLater()

    def closeEvent(self, event) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait()
        super().closeEvent(event)
