"""CacheSwap - swap Roblox cache entries (PySide6 GUI). Logic lives in core.py."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal, QUrl
from PySide6.QtWidgets import QDialog, QDialogButtonBox
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

import core
import external_sources
from preview_ui import PreviewPane
from fleasion_port.gui.theme import ThemeManager
from fleasion_port.gui.file_drop import FileDropLineEdit

KINDS = ["All", "anim", "curve-anim", "mesh", "png", "jpg", "bmp", "gif", "webp", "tiff", "ico", "dds", "psd", "ktx", "font", "rbxm", "xml", "json", "ogg", "mp3",
         "other", "?"]


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n / 1:.1f} {unit}"
        n /= 1024
    return str(n)


class NumItem(QTableWidgetItem):
    """Table item that sorts by a number but shows text."""

    def __init__(self, text: str, value: float):
        super().__init__(text)
        self._v = value

    def __lt__(self, other):
        return self._v < getattr(other, "_v", 0)


class DropTable(QTableWidget):
    """Table that accepts a file dropped onto a row (replaces that entry with the file)."""
    file_dropped = Signal(int, str)

    def __init__(self, *a):
        super().__init__(*a)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, e):
        urls = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        row = self.rowAt(int(e.position().y()))
        if urls and row >= 0:
            e.acceptProposedAction()
            self.file_dropped.emit(row, urls[0])


class ReplaceDialog(QDialog):
    """Pick what to replace an entry with: another cache entry or a file on disk."""

    def __init__(self, parent, store: core.Store, entries, target: str, kind: str):
        super().__init__(parent)
        self.setWindowTitle("Replace with…")
        self.resize(560, 460)
        self.store = store
        self.chosen_entry: str | None = None
        self.chosen_file: str | None = None
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"Replace {store.name_of(target) or target[:12] + '…'} ({kind}) with:"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter entries…")
        self.same = QCheckBox(f"Only '{kind}' entries")
        self.same.setChecked(True)
        row = QHBoxLayout()
        row.addWidget(self.search, 1)
        row.addWidget(self.same)
        lay.addLayout(row)
        self.lst = QListWidget()
        lay.addWidget(self.lst, 1)
        self._all = [e for e in entries if e.id != target]
        self._kind = kind
        self.search.textChanged.connect(self._fill)
        self.same.toggled.connect(self._fill)
        self.lst.itemDoubleClicked.connect(lambda _i: self._accept_entry())
        bb = QDialogButtonBox()
        b_entry = bb.addButton("Use selected entry", QDialogButtonBox.AcceptRole)
        b_file = bb.addButton("Choose a file…", QDialogButtonBox.ActionRole)
        bb.addButton(QDialogButtonBox.Cancel)
        b_entry.clicked.connect(self._accept_entry)
        b_file.clicked.connect(self._choose_file)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._fill()

    def _fill(self, *_):
        t = self.search.text().strip().lower()
        self.lst.clear()
        for e in sorted(self._all, key=lambda e: -e.mtime):
            if self.same.isChecked() and e.kind != self._kind:
                continue
            name = self.store.name_of(e.id)
            text = f"{name + '  ' if name else ''}[{e.id[:8]}…]  {e.kind}  {fmt_size(e.size)}  {e.asset_id}"
            if t and t not in (text + e.id + e.url).lower():
                continue
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, e.id)
            self.lst.addItem(it)
            if self.lst.count() >= 500:
                break

    def _accept_entry(self):
        it = self.lst.currentItem()
        if it:
            self.chosen_entry = it.data(Qt.UserRole)
            self.accept()

    def _choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a file", "", "All files (*)")
        if path:
            self.chosen_file = path
            self.accept()


class ScanWorker(QThread):
    done = Signal(list)

    def __init__(self, store: core.Store):
        super().__init__()
        self.store = store

    def run(self):
        self.done.emit(self.store.scan())


class ExternalDownloadWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, library: external_sources.SourceLibrary, url_or_id: str):
        super().__init__()
        self.library = library
        self.url_or_id = url_or_id

    def run(self):
        try:
            item = self.library.add_url(self.url_or_id)
        except Exception as err:
            self.failed.emit(str(err))
        else:
            self.completed.emit(item)


def make_table(headers: list[str], drop: bool = False) -> QTableWidget:
    t = DropTable(0, len(headers)) if drop else QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setStretchLastSection(True)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    t.setContextMenuPolicy(Qt.CustomContextMenu)
    return t


class MainWindow(QMainWindow):
    # cache table columns
    C_NAME, C_KIND, C_ASSET, C_SIZE, C_MOD, C_STATUS, C_ID = range(7)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("CacheSwap v1.7")
        self.resize(1250, 800)
        self.store = core.Store()
        self.source_library = external_sources.SourceLibrary()
        self.external_worker: ExternalDownloadWorker | None = None
        self.entries: list[core.Entry] = []
        self.target: str | None = None
        self.source: str | None = None
        self.learn: core.LearnSession | None = None
        self.kind_filter = "All"
        self.worker: ScanWorker | None = None

        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        self._build_view_menu()
        lay.addLayout(self._top_bar())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._cache_tab(), "Cache")
        self.tabs.addTab(self._external_sources_tab(), "External assets")
        self.tabs.addTab(self._replacements_tab(), "Replacements")
        self.tabs.addTab(self._learn_tab(), "Learn")
        self.tabs.addTab(self._presets_tab(), "Presets && profiles")

        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumBlockCount(500)
        self.preview = PreviewPane(lambda: self.store)
        self.tabs.currentChanged.connect(self._tab_changed)
        top = QSplitter(Qt.Horizontal)
        top.addWidget(self.tabs)
        top.addWidget(self.preview)
        top.setSizes([760, 380])
        self.cache.itemSelectionChanged.connect(lambda: self.preview_selected(self.cache, self.C_ID))
        self.learn_table.itemSelectionChanged.connect(lambda: self.preview_selected(self.learn_table, 6))
        split = QSplitter(Qt.Vertical)
        split.addWidget(top)
        split.addWidget(self.logbox)
        split.setSizes([560, 120])
        lay.addWidget(split, 1)
        lay.addLayout(self._action_bar())
        about = QLabel("Local to your PC only. Roblox may refresh entries at any time, and cache edits are "
                       "not officially supported by Roblox. Use at your own risk.")
        about.setStyleSheet("color: gray;")
        about.setWordWrap(True)
        lay.addWidget(about)

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._learn_poll)
        self.rb_timer = QTimer(self)
        self.rb_timer.timeout.connect(self.update_state)
        self.rb_timer.start(3000)

        self.update_state()
        self.refresh()

    def _build_view_menu(self):
        view_menu = self.menuBar().addMenu("View")
        theme_menu = view_menu.addMenu("Theme")
        group = QActionGroup(self)
        group.setExclusive(True)
        self._theme_actions = {}
        for theme in ("System", "Dark", "Light"):
            action = QAction(theme, self, checkable=True)
            action.setChecked(theme == "System")
            action.triggered.connect(lambda checked=False, value=theme: self._apply_theme(value))
            group.addAction(action)
            theme_menu.addAction(action)
            self._theme_actions[theme] = action
        ThemeManager.apply_theme("System")

    def _apply_theme(self, theme: str):
        ThemeManager.apply_theme(theme)
        for name, action in self._theme_actions.items():
            action.setChecked(name == theme)
        self.log(f"Theme changed to {theme}.")

    def closeEvent(self, event):
        self.rb_timer.stop()
        self.poll_timer.stop()
        self.preview.clear()
        if self.worker and self.worker.isRunning():
            self.worker.wait(5000)
        if self.external_worker and self.external_worker.isRunning():
            self.external_worker.wait(5000)
        super().closeEvent(event)

    # ------------------------------------------------------------------ layout
    def _top_bar(self):
        h = QHBoxLayout()
        self.path_label = QLabel()
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        b = QPushButton("Change folder…")
        b.clicked.connect(self.change_folder)
        self.rb_label = QLabel()
        h.addWidget(QLabel("Roblox folder:"))
        h.addWidget(self.path_label, 1)
        h.addWidget(b)
        h.addWidget(self.rb_label)
        return h

    def _action_bar(self):
        h = QHBoxLayout()
        self.target_label = QLabel("Target: (none)")
        self.source_label = QLabel("Source: (none)")
        self.swap_btn = QPushButton("Swap  (source → target)")
        self.swap_btn.clicked.connect(self.do_swap)
        self.restore_btn = QPushButton("Restore target")
        self.restore_btn.clicked.connect(self.do_restore)
        self.restore_all_btn = QPushButton("Restore ALL")
        self.restore_all_btn.clicked.connect(self.do_restore_all)
        for w in (self.target_label, self.source_label):
            w.setMinimumWidth(260)
            h.addWidget(w)
        h.addStretch(1)
        for w in (self.swap_btn, self.restore_btn, self.restore_all_btn):
            h.addWidget(w)
        return h

    def _cache_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        f = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by name, asset ID, entry hash or URL…")
        self.search.textChanged.connect(self.fill_cache)
        self.only_swapped = QCheckBox("Only swapped")
        self.only_swapped.toggled.connect(self.fill_cache)
        rb = QPushButton("Refresh")
        rb.clicked.connect(self.refresh)
        self.count_label = QLabel()
        for x in (self.search, self.only_swapped, rb, self.count_label):
            f.addWidget(x, 1 if x is self.search else 0)
        lay.addLayout(f)
        self.cache = make_table(["Name", "Kind", "Asset ID", "Size", "Modified", "Status", "Entry ID"], drop=True)
        self.cache.file_dropped.connect(self.on_file_dropped)
        self.cache.setSortingEnabled(True)
        self.cache.customContextMenuRequested.connect(lambda pos: self.context_menu(self.cache, self.C_ID, pos))
        self.sidebar = QListWidget()
        self.sidebar.setMaximumWidth(150)
        self.sidebar.currentItemChanged.connect(self._sidebar_changed)
        body = QHBoxLayout()
        body.addWidget(self.sidebar)
        body.addWidget(self.cache, 1)
        lay.addLayout(body, 1)
        hint = QLabel("Tip: right-click an entry for Replace with…, or drop a file onto a row.")
        hint.setStyleSheet("color: gray;")
        lay.addWidget(hint)
        row = QHBoxLayout()
        for text, fn in (("Set as target", lambda: self.pick(self.cache, self.C_ID, "target")),
                         ("Replace with…", lambda: self.replace_with(self.selected_id(self.cache, self.C_ID))),
                         ("Export entry…", lambda: self.export_entry(self.selected_id(self.cache, self.C_ID))),
                         ("Set as source", lambda: self.pick(self.cache, self.C_ID, "source")),
                         ("Name…", lambda: self.rename(self.cache, self.C_ID)),
                         ("Import file → target…", self.import_file_dialog),
                         ("Probe header", self.probe_header)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        return w

    def _external_sources_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        info = QLabel(
            "Import local files or download public assets by Roblox asset ID / direct HTTPS URL. "
            "Sources are saved in CacheSwap's own library, can be previewed here, and can be applied "
            "to the cache target selected on the Cache tab. Downloads do not use browser cookies or logins."
        )
        info.setWordWrap(True)
        lay.addWidget(info)
        self.external_table = make_table(["Name", "Kind", "Size", "Origin", "Added"])
        self.external_table.setSortingEnabled(False)
        self.external_table.itemSelectionChanged.connect(self.preview_external_selected)
        self.external_table.setColumnWidth(0, 180)
        self.external_table.setColumnWidth(1, 90)
        self.external_table.setColumnWidth(2, 90)
        self.external_table.setColumnWidth(4, 140)
        lay.addWidget(self.external_table, 1)
        self.external_target_label = QLabel("Target: (none) — select a cache entry and click Set as target.")
        self.external_target_label.setStyleSheet("color: gray;")
        lay.addWidget(self.external_target_label)
        quick = QHBoxLayout()
        self.external_quick_input = FileDropLineEdit()
        self.external_quick_input.setPlaceholderText("Drop a file here, or enter a Roblox asset ID / HTTPS URL…")
        self.external_quick_input.fileDropped.connect(self._external_file_dropped)
        self.external_quick_add_btn = QPushButton("Add entered source")
        self.external_quick_add_btn.clicked.connect(self.external_add_input)
        quick.addWidget(self.external_quick_input, 1)
        quick.addWidget(self.external_quick_add_btn)
        lay.addLayout(quick)
        row = QHBoxLayout()
        self.external_add_file_btn = QPushButton("Add local file(s)…")
        self.external_add_file_btn.clicked.connect(self.external_add_files)
        self.external_add_url_btn = QPushButton("Download URL / asset ID…")
        self.external_add_url_btn.clicked.connect(self.external_add_url)
        b_apply = QPushButton("Import selected → target…")
        b_apply.clicked.connect(self.external_import_to_target)
        b_remove = QPushButton("Remove from library")
        b_remove.clicked.connect(self.external_remove_selected)
        b_folder = QPushButton("Open library folder")
        b_folder.clicked.connect(self.external_open_folder)
        for b in (self.external_add_file_btn, self.external_add_url_btn, b_apply, b_remove, b_folder):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        if self.source_library.load_error:
            QTimer.singleShot(0, lambda: self.log("WARNING: " + self.source_library.load_error))
        self.fill_external_sources()
        return w

    def fill_external_sources(self, select_id: str | None = None):
        if not hasattr(self, "external_table"):
            return
        rows = self.source_library.list()
        table = self.external_table
        table.setRowCount(len(rows))
        selected_row = -1
        for r, source in enumerate(rows):
            vals = [source.name, source.kind, fmt_size(source.size), source.origin, source.added]
            for c, value in enumerate(vals):
                item = QTableWidgetItem(value)
                item.setToolTip(str(source.path) if c == 0 else source.origin)
                if c == 0:
                    item.setData(Qt.UserRole, source.id)
                table.setItem(r, c, item)
            if source.id == select_id:
                selected_row = r
        if selected_row >= 0:
            table.selectRow(selected_row)
        elif rows and table.currentRow() < 0:
            table.selectRow(0)
        if hasattr(self, "external_target_label"):
            self.external_target_label.setText(
                "Target: " + self.label_for(self.target) +
                " — import writes this source into that cache slot; it is not a live URL redirect/proxy."
            )

    def selected_external_source(self) -> external_sources.ExternalSource | None:
        row = self.external_table.currentRow() if hasattr(self, "external_table") else -1
        item = self.external_table.item(row, 0) if row >= 0 else None
        return self.source_library.get(item.data(Qt.UserRole)) if item else None

    def preview_external_selected(self):
        # A pre-existing source can be selected while the tabs are being built,
        # before PreviewPane is created in MainWindow.__init__.
        if not hasattr(self, "preview"):
            return
        source = self.selected_external_source()
        if not source:
            return
        self.preview.show_file(source.path, source.name, source.origin)

    def external_add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add external asset files", "", "All files (*)")
        if not paths:
            return
        last = None
        for path in paths:
            try:
                last = self.source_library.add_file(path)
                self.log(f"Added external source: {last.name} ({last.kind}, {fmt_size(last.size)}).")
            except (external_sources.ExternalSourceError, OSError) as err:
                self.warn(f"Could not import {path}: {err}")
        self.fill_external_sources(last.id if last else None)
        if last:
            self.preview.show_file(last.path, last.name, last.origin)

    def _external_file_dropped(self, path: str):
        """Import a file dropped into the Fleasion-style quick source field."""
        if not Path(path).is_file():
            return
        try:
            source = self.source_library.add_file(path)
        except (external_sources.ExternalSourceError, OSError) as err:
            self.warn(f"Could not import {path}: {err}")
            return
        self.fill_external_sources(source.id)
        self.preview.show_file(source.path, source.name, source.origin)
        self.log(f"Added dropped file: {source.name} ({source.kind}, {fmt_size(source.size)}).")

    def external_add_input(self):
        raw = self.external_quick_input.text().strip()
        if not raw:
            return
        path = Path(raw.strip('"'))
        if path.is_file():
            self._external_file_dropped(str(path))
            self.external_quick_input.clear()
            return
        self._start_external_download(raw)

    def external_add_url(self):
        raw, ok = QInputDialog.getText(
            self, "Download external asset", "Roblox asset ID (e.g. 73885081417917) or direct HTTPS URL:"
        )
        if ok and raw.strip():
            self._start_external_download(raw.strip())

    def _start_external_download(self, raw: str):
        if self.external_worker and self.external_worker.isRunning():
            self.warn("A download is already running. Let it finish first.")
            return
        try:
            normalized = external_sources.normalize_source_url(raw)
        except external_sources.ExternalSourceError as err:
            self.warn(str(err))
            return
        self.external_add_url_btn.setEnabled(False)
        if hasattr(self, "external_quick_add_btn"):
            self.external_quick_add_btn.setEnabled(False)
        self.log(f"Downloading external source: {normalized}")
        self.external_worker = ExternalDownloadWorker(self.source_library, raw)
        self.external_worker.completed.connect(self.external_download_ready)
        self.external_worker.failed.connect(self.external_download_failed)
        self.external_worker.start()

    def external_download_ready(self, source):
        self.external_add_url_btn.setEnabled(True)
        if hasattr(self, "external_quick_add_btn"):
            self.external_quick_add_btn.setEnabled(True)
        if hasattr(self, "external_quick_input"):
            self.external_quick_input.clear()
        self.log(f"Downloaded external source: {source.name} ({source.kind}, {fmt_size(source.size)}).")
        self.fill_external_sources(source.id)
        self.preview.show_file(source.path, source.name, source.origin)

    def external_download_failed(self, message: str):
        self.external_add_url_btn.setEnabled(True)
        if hasattr(self, "external_quick_add_btn"):
            self.external_quick_add_btn.setEnabled(True)
        self.warn("External download failed: " + message)

    def external_import_to_target(self):
        source = self.selected_external_source()
        if not source:
            self.warn("Select an external source first.")
            return
        if not self.target:
            self.warn("Pick a cache target first on the Cache tab (e.g. select Wave and click Set as target).")
            return
        try:
            warn = self.store.import_warning(self.target, source.path)
        except (OSError, core.CacheError) as err:
            self.warn(str(err))
            return
        msg = (f"Replace\n  {self.label_for(self.target)}\nwith external source\n  {source.name}\n\n"
               "CacheSwap will keep the target's existing header, update the payload length, back up the database, "
               "and save the original so you can restore it. Roblox must be fully closed.\n\n"
               "This is a local cache import, not a live external URL redirect.")
        if warn:
            msg += "\n\nWARNING: " + warn
        if QMessageBox.question(self, "Confirm external import", msg) != QMessageBox.Yes:
            return
        origin = ("external URL " if source.origin.startswith("https://") else "external file ") + source.origin
        self._run(lambda: self.store.import_file(
            self.target, source.path, name="External: " + source.name, origin=origin
        ))

    def external_remove_selected(self):
        source = self.selected_external_source()
        if not source:
            self.warn("Select an external source first.")
            return
        if QMessageBox.question(
            self, "Remove external source", f"Remove {source.name} from the external source library?\n\n"
            "This does not revert any cache replacement already made from it."
        ) != QMessageBox.Yes:
            return
        self.source_library.remove(source.id)
        self.preview.clear()
        self.fill_external_sources()
        self.log(f"Removed external source {source.name} from the library.")

    def external_open_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.source_library.root_dir())))

    def _learn_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        info = QLabel(
            "Find which cache entry an in-game asset uses:\n"
            "1. Click Start Learn (you can do this before launching Roblox).\n"
            "2. Launch Roblox, join an experience, press 'I joined', then play the emote/animation once "
            "and press 'I played it'.\n"
            "3. Close Roblox, click Stop && show results. Entries marked NEW after your marks are the candidates;\n"
            "   the Asset ID column usually tells you which one it is.")
        info.setWordWrap(True)
        lay.addWidget(info)
        row = QHBoxLayout()
        self.l_start = QPushButton("Start Learn")
        self.l_joined = QPushButton("I joined")
        self.l_played = QPushButton("I played it")
        self.l_stop = QPushButton("Stop && show results")
        self.l_start.clicked.connect(self.learn_start)
        self.l_joined.clicked.connect(lambda: self.learn_mark("joined"))
        self.l_played.clicked.connect(lambda: self.learn_mark("played it"))
        self.l_stop.clicked.connect(self.learn_stop)
        for b in (self.l_start, self.l_joined, self.l_played, self.l_stop):
            row.addWidget(b)
        row.addStretch(1)
        self.l_status = QLabel("Idle.")
        row.addWidget(self.l_status)
        lay.addLayout(row)
        self.learn_table = make_table(["Time", "Phase", "Kind", "Change", "Asset ID", "Name", "Entry ID"])
        self.learn_table.customContextMenuRequested.connect(lambda pos: self.context_menu(self.learn_table, 6, pos))
        lay.addWidget(self.learn_table)
        row2 = QHBoxLayout()
        for text, fn in (("Use as source", lambda: self.pick(self.learn_table, 6, "source")),
                         ("Use as target", lambda: self.pick(self.learn_table, 6, "target")),
                         ("Name…", lambda: self.rename(self.learn_table, 6))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row2.addWidget(b)
        row2.addStretch(1)
        lay.addLayout(row2)
        self._learn_buttons(False)
        return w

    def _presets_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("A preset remembers a target and a source. Apply it in one click "
                             "(Roblox must be closed)."))
        self.preset_list = QListWidget()
        lay.addWidget(self.preset_list, 1)
        row = QHBoxLayout()
        for text, fn in (("Save current target/source as preset…", self.preset_save),
                         ("Apply", self.preset_apply),
                         ("Apply all", self.preset_apply_all),
                         ("Delete", self.preset_delete)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(QLabel("Profiles remember ALL active replacements as a set. Export one to share it "
                             "(the file contains the replacement files)."))
        self.profile_list = QListWidget()
        lay.addWidget(self.profile_list, 1)
        prow = QHBoxLayout()
        for text, fn in (("Save active replacements as profile…", self.profile_save),
                         ("Apply profile", self.profile_apply),
                         ("Export…", self.profile_export),
                         ("Import…", self.profile_import),
                         ("Delete", self.profile_delete)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            prow.addWidget(b)
        prow.addStretch(1)
        lay.addLayout(prow)
        self.fill_presets()
        return w

    # ----------------------------------------------------------------- helpers
    def log(self, msg: str):
        self.logbox.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def warn(self, msg: str):
        self.log("ERROR: " + msg.replace("\n", " "))
        QMessageBox.warning(self, "CacheSwap", msg)

    def label_for(self, ident: str | None) -> str:
        if not ident:
            return "(none)"
        name = self.store.name_of(ident)
        return f"{name} [{ident[:8]}…]" if name else f"{ident[:12]}…"

    def update_state(self):
        self.path_label.setText(str(self.store.root))
        running = core.roblox_running()
        self.rb_label.setText("Roblox: RUNNING - close it before swapping" if running else "Roblox: closed")
        self.rb_label.setStyleSheet("color: #c0392b;" if running else "color: #27ae60;")
        self.target_label.setText("Target: " + self.label_for(self.target))
        self.source_label.setText("Source: " + self.label_for(self.source))
        self.swap_btn.setEnabled(bool(self.target and self.source) and not running)
        self.restore_btn.setEnabled(bool(self.target) and not running)
        self.restore_all_btn.setEnabled(not running)
        if hasattr(self, "external_target_label"):
            self.external_target_label.setText(
                "Target: " + self.label_for(self.target) +
                " — external sources are imported into this local cache slot, not redirected live."
            )

    def selected_id(self, table: QTableWidget, col: int) -> str | None:
        r = table.currentRow()
        it = table.item(r, col) if r >= 0 else None
        return it.text() if it else None

    def preview_selected(self, table, col):
        self.preview.show_entry(self.selected_id(table, col))

    def _tab_changed(self, index: int):
        # The external source table may already have a selected row from startup,
        # so selecting the tab should also refresh its preview.
        if index == 1:
            self.preview_external_selected()

    def pick(self, table, col, which):
        ident = self.selected_id(table, col)
        if not ident:
            return
        if which == "target":
            self.target = ident
        else:
            self.source = ident
        self.log(f"{which} = {self.label_for(ident)}")
        self.update_state()
        self.fill_cache()

    def rename(self, table, col):
        ident = self.selected_id(table, col)
        if not ident:
            return
        text, ok = QInputDialog.getText(self, "Name entry", "Name (empty clears it):",
                                        text=self.store.name_of(ident))
        if ok:
            core.Store.set_name(ident, text)
            self.fill_cache()
            self.fill_presets()
            self.update_state()

    def context_menu(self, table, col, pos):
        ident = self.selected_id(table, col)
        if not ident:
            return
        m = QMenu(self)
        items = [("Replace with…", lambda: self.replace_with(ident)),
                 ("Set as target", lambda: self.pick(table, col, "target")),
                 ("Set as source", lambda: self.pick(table, col, "source")),
                 ("Name…", lambda: self.rename(table, col)),
                 ("Export entry…", lambda: self.export_entry(ident)),
                 ("Copy entry ID", lambda: QGuiApplication.clipboard().setText(ident))]
        if ident in self.store.manifest():
            items.insert(1, ("Revert this replacement", lambda: self.revert(ident)))
        for text, fn in items:
            a = QAction(text, m)
            a.triggered.connect(fn)
            m.addAction(a)
        m.exec(table.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------- folder/scan
    def change_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Select the Roblox folder (contains rbx-storage.db)",
                                             str(self.store.root))
        if d:
            self.store = core.Store(d)
            self.target = self.source = None
            self.update_state()
            self.fill_replacements()
            self.refresh()

    def refresh(self):
        try:
            self.store.validate()
        except core.CacheError as e:
            self.entries = []
            self.fill_cache()
            self.log("ERROR: " + str(e).replace("\n", " "))
            return
        if self.worker and self.worker.isRunning():
            return
        self.log("Scanning cache…")
        self.worker = ScanWorker(self.store)
        self.worker.done.connect(self._scan_done)
        self.worker.start()

    def _scan_done(self, entries):
        self.entries = entries
        self.log(f"Scan finished: {len(entries)} entries.")
        self.fill_sidebar()
        self.fill_cache()
        self.fill_replacements()

    def fill_cache(self, *_):
        text = self.search.text().strip().lower()
        kind = self.kind_filter
        only = self.only_swapped.isChecked()
        manifest = self.store.manifest()
        rows = []
        for e in self.entries:
            if kind != "All" and e.kind != kind:
                continue
            name = self.store.name_of(e.id)
            if text and text not in (name + " " + e.id + " " + e.url + " " + e.asset_id).lower():
                continue
            if only and e.id not in manifest:
                continue
            rows.append((e, name))
        t = self.cache
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for r, (e, name) in enumerate(rows):
            status = []
            if not e.header:
                status.append("NO-RBXH")
            if e.id == self.target:
                status.append("TARGET")
            if e.id == self.source:
                status.append("SOURCE")
            s = self.store.swap_status(e.id) if e.id in manifest else ""
            if s:
                status.append(s.upper())
            vals = [name, e.kind, e.asset_id, None, None, " ".join(status), e.id]
            for c, v in enumerate(vals):
                if c == self.C_SIZE:
                    item = NumItem(fmt_size(e.size), e.size)
                elif c == self.C_MOD:
                    item = NumItem(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.mtime)), e.mtime)
                else:
                    item = QTableWidgetItem(v)
                t.setItem(r, c, item)
        t.setSortingEnabled(True)
        t.sortByColumn(self.C_MOD, Qt.DescendingOrder)
        self.count_label.setText(f"{len(rows)} / {len(self.entries)}")

    # ---------------------------------------------------------------- sidebar
    def fill_sidebar(self):
        counts: dict = {}
        for e in self.entries:
            counts[e.kind] = counts.get(e.kind, 0) + 1
        order = [k for k in KINDS[1:] if k in counts] + sorted(k for k in counts if k not in KINDS)
        self.sidebar.blockSignals(True)
        self.sidebar.clear()
        for k in ["All"] + order:
            n = len(self.entries) if k == "All" else counts[k]
            it = QListWidgetItem(f"{k} ({n})")
            it.setData(Qt.UserRole, k)
            self.sidebar.addItem(it)
            if k == self.kind_filter:
                self.sidebar.setCurrentItem(it)
        if self.sidebar.currentItem() is None and self.sidebar.count():
            self.kind_filter = "All"
            self.sidebar.setCurrentRow(0)
        self.sidebar.blockSignals(False)

    def _sidebar_changed(self, cur, _prev):
        if cur:
            self.kind_filter = cur.data(Qt.UserRole)
            self.fill_cache()

    # ------------------------------------------------------- replace / export
    def entry_kind(self, ident: str) -> str:
        e = next((x for x in self.entries if x.id == ident), None)
        return e.kind if e else "?"

    def replace_with(self, ident: str | None):
        if not ident:
            return
        dlg = ReplaceDialog(self, self.store, self.entries, ident, self.entry_kind(ident))
        if dlg.exec() != QDialog.Accepted:
            return
        if dlg.chosen_file:
            self.import_file_to(ident, dlg.chosen_file)
        elif dlg.chosen_entry:
            self.target, self.source = ident, dlg.chosen_entry
            self.update_state()
            self.do_swap()

    def on_file_dropped(self, row: int, path: str):
        it = self.cache.item(row, self.C_ID)
        if it:
            self.import_file_to(it.text(), path)

    def export_entry(self, ident: str | None):
        if not ident:
            return
        d = QFileDialog.getExistingDirectory(self, "Export entry to folder", str(core.app_dir()))
        if not d:
            return
        try:
            out = self.store.export_entry(ident, d)
        except (core.CacheError, OSError) as e:
            self.warn(str(e))
            return
        self.log(f"Exported {ident[:8]}… to {out} (plus a .txt report next to it).")

    def revert(self, ident: str):
        self._run(lambda: self.store.restore(ident))

    # ---------------------------------------------------------- replacements
    def _replacements_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Every replacement CacheSwap has made and not yet reverted."))
        self.repl_table = make_table(["Target", "Replaced with", "Origin", "Since", "Status", "Target ID"])
        self.repl_table.itemSelectionChanged.connect(lambda: self.preview_selected(self.repl_table, 5))
        lay.addWidget(self.repl_table, 3)
        row = QHBoxLayout()
        b1 = QPushButton("Revert selected")
        b1.clicked.connect(lambda: self.selected_id(self.repl_table, 5) and self.revert(self.selected_id(self.repl_table, 5)))
        b2 = QPushButton("Revert ALL")
        b2.clicked.connect(self.do_restore_all)
        row.addWidget(b1)
        row.addWidget(b2)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(QLabel("History (kept between sessions):"))
        self.hist_table = make_table(["Time", "Action", "Target", "Source", "Detail"])
        lay.addWidget(self.hist_table, 2)
        return w

    def fill_replacements(self):
        m = self.store.manifest()
        t = self.repl_table
        t.setRowCount(len(m))
        for r, (target, info) in enumerate(sorted(m.items(), key=lambda kv: kv[1].get("time", ""), reverse=True)):
            st = self.store.swap_status(target)
            status = {"swapped": "ACTIVE", "reverted": "REVERTED by Roblox"}.get(st, st)
            vals = [self.label_for(target), self.label_for(info.get("source")), info.get("origin", ""),
                    info.get("time", ""), status, target]
            for c, v in enumerate(vals):
                t.setItem(r, c, QTableWidgetItem(v))
        h = list(reversed(self.store.history()))[:300]
        ht = self.hist_table
        ht.setRowCount(len(h))
        for r, x in enumerate(h):
            vals = [x["time"], x["action"], self.label_for(x["target"]),
                    self.label_for(x["source"]) if x.get("source") else "", x.get("detail", "")]
            for c, v in enumerate(vals):
                ht.setItem(r, c, QTableWidgetItem(v))
        self.tabs.setTabText(2, f"Replacements ({len(m)})" if m else "Replacements")

    # -------------------------------------------------------------- operations
    def do_swap(self):
        if not (self.target and self.source):
            return
        try:
            warn = self.store.kind_warning(self.target, self.source)
        except Exception as e:  # never let a preview check block the user
            warn = ""
            self.log(f"kind check skipped: {e}")
        msg = (f"Replace\n  {self.label_for(self.target)}\nwith\n  {self.label_for(self.source)}\n\n"
               "The database is backed up first and the original is kept so you can restore it.")
        if warn:
            msg += "\n\nWARNING: " + warn
        if QMessageBox.question(self, "Confirm swap", msg) != QMessageBox.Yes:
            return
        self._run(lambda: self.store.swap(self.target, self.source))

    def do_restore(self):
        if self.target:
            self._run(lambda: self.store.restore(self.target))

    def do_restore_all(self):
        if QMessageBox.question(self, "Restore ALL", "Restore every swapped entry to its original?") != QMessageBox.Yes:
            return
        try:
            for m in self.store.restore_all():
                self.log(m)
        except Exception as e:
            self.warn(str(e))
        self.fill_replacements()
        self.refresh()

    def _run(self, fn):
        try:
            self.log(fn())
        except core.CacheError as e:
            self.warn(str(e))
        except Exception as e:
            self.warn(f"Unexpected error: {e}")
        self.fill_replacements()
        self.refresh()

    def import_file_dialog(self):
        if not self.target:
            self.warn("Pick a target first: the entry the file should replace (for example Wave).")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose a file (animation .rbxm, image, mesh, sound)", "",
                                              "All files (*)")
        if path:
            self.import_file_to(self.target, path)

    def import_file_to(self, target: str, path: str):
        try:
            warn = self.store.import_warning(target, path)
        except (OSError, core.CacheError) as e:
            self.warn(str(e))
            return
        msg = (f"Replace\n  {self.label_for(target)}\nwith the contents of\n  {path}\n\n"
               "The header is copied from the target and its length field is updated. The database is "
               "backed up first and the original is kept so you can restore it.\n\n"
               "Experimental: Roblox rejected payload-only edits in an earlier test.")
        if warn:
            msg += "\n\nNOTE: " + warn
        if QMessageBox.question(self, "Confirm import", msg) != QMessageBox.Yes:
            return
        self._run(lambda: self.store.import_file(target, path))

    def probe_header(self):
        try:
            self.store.validate()
            text = self.store.probe_header()
        except (core.CacheError, OSError) as e:
            self.warn(str(e))
            return
        out = core.app_dir() / "header_probe.txt"
        out.write_text(text, "utf-8")
        QGuiApplication.clipboard().setText(text)
        self.log(text)
        self.log(f"Probe saved to {out} and copied to the clipboard.")

    # ------------------------------------------------------------------- learn
    def _learn_buttons(self, active: bool):
        self.l_start.setEnabled(not active)
        for b in (self.l_joined, self.l_played, self.l_stop):
            b.setEnabled(active)

    def learn_start(self):
        try:
            self.store.validate()
        except core.CacheError as e:
            self.warn(str(e))
            return
        self.learn = core.LearnSession(self.store)
        self.learn_table.setRowCount(0)
        self.poll_timer.start(500)
        self._learn_buttons(True)
        self.l_status.setText("Watching the cache… launch Roblox now.")
        self.log("Learn started.")

    def _learn_poll(self):
        if self.learn:
            self.learn.poll()
            self.l_status.setText(f"Watching… {len(self.learn.first)} entries changed so far.")

    def learn_mark(self, label: str):
        if self.learn:
            self.learn.mark(label)
            self.log(f"Learn mark: {label}")

    def learn_stop(self):
        if not self.learn:
            return
        self.poll_timer.stop()
        rows = self.learn.results()
        self.learn = None
        self._learn_buttons(False)
        self.l_status.setText(f"Done: {len(rows)} entries changed.")
        t = self.learn_table
        t.setRowCount(len(rows))
        for r, row in enumerate(rows):
            e = row["entry"]
            vals = [f"T+{row['t']:.1f}s", row["phase"], e.kind, "NEW" if row["new"] else "rewritten",
                    e.asset_id, self.store.name_of(e.id), e.id]
            for c, v in enumerate(vals):
                t.setItem(r, c, QTableWidgetItem(v))
        self.log(f"Learn finished: {len(rows)} changed entries.")
        self.refresh()

    # ----------------------------------------------------------------- presets
    def fill_profiles(self):
        self.profile_list.clear()
        for n in core.profile_names():
            self.profile_list.addItem(n)

    def _sel_profile(self):
        it = self.profile_list.currentItem()
        return it.text() if it else None

    def profile_save(self):
        name, ok = QInputDialog.getText(self, "Save profile", "Profile name:")
        if ok and name.strip():
            try:
                core.save_profile(name.strip(), self.store)
                self.log(f"Profile '{name.strip()}' saved.")
            except core.CacheError as e:
                self.warn(str(e))
            self.fill_profiles()

    def profile_apply(self):
        n = self._sel_profile()
        if n:
            try:
                for m in core.apply_profile(n, self.store):
                    self.log(m)
            except core.CacheError as e:
                self.warn(str(e))
            self.fill_replacements()
            self.refresh()

    def profile_export(self):
        n = self._sel_profile()
        if not n:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export profile", f"{n}.cswap", "CacheSwap profile (*.cswap)")
        if path:
            try:
                self.log(f"Exported {core.export_profile(n, path)} replacement(s) to {path}")
            except (core.CacheError, OSError) as e:
                self.warn(str(e))

    def profile_import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import profile", "", "CacheSwap profile (*.cswap);;All files (*)")
        if path:
            try:
                self.log(f"Imported profile '{core.import_profile(path)}'.")
            except (core.CacheError, OSError) as e:
                self.warn(str(e))
            self.fill_profiles()

    def profile_delete(self):
        n = self._sel_profile()
        if n:
            core.delete_profile(n)
            self.fill_profiles()

    def fill_presets(self):
        self.fill_profiles()
        self.preset_list.clear()
        for p in self.store.presets():
            self.preset_list.addItem(f"{p['name']}:  {self.label_for(p['source'])}  →  {self.label_for(p['target'])}")
            self.preset_list.item(self.preset_list.count() - 1).setData(Qt.UserRole, p["name"])

    def _selected_preset(self):
        it = self.preset_list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def preset_save(self):
        if not (self.target and self.source):
            self.warn("Pick a target and a source first (Cache or Learn tab).")
            return
        name, ok = QInputDialog.getText(self, "Save preset", "Preset name:")
        if ok and name.strip():
            core.Store.save_preset(name.strip(), self.target, self.source)
            self.fill_presets()
            self.log(f"Preset '{name.strip()}' saved.")

    def preset_apply(self):
        name = self._selected_preset()
        if name:
            self._run(lambda: self.store.apply_preset(name))

    def preset_apply_all(self):
        for p in self.store.presets():
            try:
                self.log(self.store.apply_preset(p["name"]))
            except core.CacheError as e:
                self.warn(f"{p['name']}: {e}")
                break
        self.refresh()

    def preset_delete(self):
        name = self._selected_preset()
        if name:
            core.Store.delete_preset(name)
            self.fill_presets()


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
