"""GUI for local Roblox cache-hash replacement profiles."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..database.hash_replacer_store import HashReplacerError, HashReplacerStore
from ..database.roblox_cache_store import RobloxCacheError, RobloxCacheStore
from ..utils import open_folder


def _format_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


class HashReplacerTab(QWidget):
    """Map target cache hashes to files imported into the separate hash library."""

    def __init__(
        self,
        parent=None,
        *,
        asset_store: HashReplacerStore | None = None,
        cache_store: RobloxCacheStore | None = None,
    ):
        super().__init__(parent)
        self.asset_store = asset_store or HashReplacerStore()
        self.cache_store = cache_store or RobloxCacheStore()
        self._build_ui()
        self.refresh_profiles()
        self.refresh_assets()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        description = QLabel(
            "Local cache mode: map a 32-character Roblox cache hash to an imported asset. "
            "It works without the HTTP proxy and keeps the legacy Asset-ID profiles separate."
        )
        description.setWordWrap(True)
        root.addWidget(description)

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Hash profile:"))
        self.profile_combo = QComboBox()
        self.profile_combo.currentTextChanged.connect(self.refresh_rules)
        profile_row.addWidget(self.profile_combo, 1)

        self.new_profile_button = QPushButton("New")
        self.new_profile_button.clicked.connect(self._create_profile)
        profile_row.addWidget(self.new_profile_button)
        self.delete_profile_button = QPushButton("Delete")
        self.delete_profile_button.clicked.connect(self._delete_profile)
        profile_row.addWidget(self.delete_profile_button)
        self.open_configs_button = QPushButton("Open hash configs")
        self.open_configs_button.clicked.connect(lambda: open_folder(self.asset_store.configs_dir))
        profile_row.addWidget(self.open_configs_button)
        root.addLayout(profile_row)

        rule_row = QHBoxLayout()
        rule_row.addWidget(QLabel("Target cache hash:"))
        self.target_hash_edit = QLineEdit()
        self.target_hash_edit.setPlaceholderText("32 hexadecimal characters from Scraper DB")
        rule_row.addWidget(self.target_hash_edit, 2)

        rule_row.addWidget(QLabel("Replacement asset:"))
        self.asset_combo = QComboBox()
        self.asset_combo.setMinimumWidth(220)
        rule_row.addWidget(self.asset_combo, 3)

        self.add_rule_button = QPushButton("Add / update rule")
        self.add_rule_button.clicked.connect(self._add_rule)
        rule_row.addWidget(self.add_rule_button)
        root.addLayout(rule_row)

        self.rules_table = QTableWidget(0, 4)
        self.rules_table.setHorizontalHeaderLabels(
            ["Target cache hash", "Imported file", "Type", "Size"]
        )
        self.rules_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.rules_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.rules_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.rules_table.setAlternatingRowColors(True)
        self.rules_table.verticalHeader().hide()
        self.rules_table.horizontalHeader().setStretchLastSection(True)
        self.rules_table.itemSelectionChanged.connect(self._rule_selected)
        root.addWidget(self.rules_table, 1)

        actions = QHBoxLayout()
        self.refresh_assets_button = QPushButton("Refresh imported files")
        self.refresh_assets_button.clicked.connect(self.refresh_assets)
        actions.addWidget(self.refresh_assets_button)
        self.open_assets_button = QPushButton("Open asset folder")
        self.open_assets_button.clicked.connect(lambda: open_folder(self.asset_store.assets_dir))
        actions.addWidget(self.open_assets_button)
        actions.addStretch(1)
        self.remove_rule_button = QPushButton("Remove rule")
        self.remove_rule_button.clicked.connect(self._remove_rule)
        actions.addWidget(self.remove_rule_button)
        self.restore_button = QPushButton("Restore original cache entry")
        self.restore_button.clicked.connect(self._restore_selected)
        actions.addWidget(self.restore_button)
        self.apply_button = QPushButton("Apply selected")
        self.apply_button.clicked.connect(self._apply_selected)
        actions.addWidget(self.apply_button)
        root.addLayout(actions)

        self.status_label = QLabel(
            "Import files or folders in Miscellaneous. Adding a rule only saves the mapping; "
            "the Roblox cache changes only when you click Apply."
        )
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

    def refresh_profiles(self, selected: str | None = None) -> None:
        current = selected or self.profile_combo.currentText() or "Default"
        try:
            names = self.asset_store.list_configs()
        except HashReplacerError as exc:
            self.status_label.setText(str(exc))
            names = ["Default"]
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(names)
        index = self.profile_combo.findText(current)
        self.profile_combo.setCurrentIndex(index if index >= 0 else 0)
        self.profile_combo.blockSignals(False)
        self.refresh_rules()

    def refresh_assets(self) -> None:
        current = self.asset_combo.currentData()
        self.asset_combo.clear()
        try:
            assets = self.asset_store.list_assets()
        except HashReplacerError as exc:
            self.status_label.setText(str(exc))
            assets = []
        for asset in assets:
            digest = str(asset["sha256"])
            name = str(asset.get("name") or asset.get("file") or digest[:12])
            kind = str(asset.get("kind") or "unknown")
            size = int(asset.get("size") or 0)
            self.asset_combo.addItem(
                f"{name}  ·  {kind}  ·  {_format_size(size)}",
                digest,
            )
            self.asset_combo.setItemData(
                self.asset_combo.count() - 1,
                f"{digest}\n{asset.get('path', '')}",
                Qt.ItemDataRole.ToolTipRole,
            )
        index = self.asset_combo.findData(current)
        if index >= 0:
            self.asset_combo.setCurrentIndex(index)
        if not assets:
            self.status_label.setText(
                "No imported assets yet. Use Miscellaneous → Hash Replacer Assets to import files or a folder."
            )

    def refresh_rules(self, *_args) -> None:
        name = self.profile_combo.currentText()
        self.rules_table.setRowCount(0)
        if not name:
            return
        try:
            config = self.asset_store.load_config(name)
            records = {asset["sha256"]: asset for asset in self.asset_store.list_assets()}
        except HashReplacerError as exc:
            self.status_label.setText(str(exc))
            return
        for rule in config["rules"]:
            asset = records.get(rule["source_hash"], {})
            row = self.rules_table.rowCount()
            self.rules_table.insertRow(row)
            values = [
                rule["target_hash"],
                str(asset.get("name") or rule["source_hash"][:16]),
                str(asset.get("kind") or "missing file"),
                _format_size(int(asset.get("size") or 0)),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(
                    rule["source_hash"] if column == 1 else value
                )
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, rule["target_hash"])
                    item.setData(Qt.ItemDataRole.UserRole + 1, rule["source_hash"])
                self.rules_table.setItem(row, column, item)
        self.rules_table.resizeColumnsToContents()
        self.rules_table.setColumnWidth(0, 250)
        self.rules_table.setColumnWidth(1, 240)
        self.remove_rule_button.setEnabled(self.rules_table.rowCount() > 0)
        self.apply_button.setEnabled(self.rules_table.rowCount() > 0)
        self.restore_button.setEnabled(self.rules_table.rowCount() > 0)

    def _create_profile(self) -> None:
        name, accepted = QInputDialog.getText(self, "New hash profile", "Profile name:")
        if not accepted:
            return
        try:
            if not self.asset_store.create_config(name):
                QMessageBox.warning(self, "Hash profiles", "A profile with that name already exists.")
                return
        except HashReplacerError as exc:
            QMessageBox.warning(self, "Hash profiles", str(exc))
            return
        self.refresh_profiles(name.strip())

    def _delete_profile(self) -> None:
        name = self.profile_combo.currentText()
        if not name or name == "Default":
            QMessageBox.information(self, "Hash profiles", "The Default profile cannot be deleted.")
            return
        answer = QMessageBox.question(
            self,
            "Delete hash profile",
            f"Delete profile '{name}'? Imported asset files will be kept.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.asset_store.delete_config(name)
        except (OSError, HashReplacerError) as exc:
            QMessageBox.warning(self, "Hash profiles", str(exc))
            return
        self.refresh_profiles("Default")

    def _add_rule(self) -> None:
        name = self.profile_combo.currentText()
        try:
            target_hash = self.asset_store.validate_cache_hash(self.target_hash_edit.text())
            source_hash = str(self.asset_combo.currentData() or "")
            if not source_hash:
                raise HashReplacerError("Import at least one replacement file in Miscellaneous first.")
            self.asset_store.add_rule(name, target_hash, source_hash)
        except HashReplacerError as exc:
            QMessageBox.warning(self, "Hash replacement rule", str(exc))
            return
        self.refresh_rules()
        self.status_label.setText(f"Saved rule: {target_hash} → {source_hash[:16]}…")

    def _selected_rule(self) -> tuple[str, str] | None:
        row = self.rules_table.currentRow()
        if row < 0:
            return None
        item = self.rules_table.item(row, 0)
        if item is None:
            return None
        return (
            str(item.data(Qt.ItemDataRole.UserRole) or item.text()),
            str(item.data(Qt.ItemDataRole.UserRole + 1) or ""),
        )

    def _rule_selected(self) -> None:
        selected = self._selected_rule()
        if selected is not None:
            self.target_hash_edit.setText(selected[0])

    def _remove_rule(self) -> None:
        selected = self._selected_rule()
        if selected is None:
            return
        try:
            self.asset_store.remove_rule(self.profile_combo.currentText(), selected[0])
        except HashReplacerError as exc:
            QMessageBox.warning(self, "Hash replacement rule", str(exc))
            return
        self.refresh_rules()
        self.status_label.setText(f"Removed rule for cache hash {selected[0]}.")

    def _apply_selected(self) -> None:
        selected = self._selected_rule()
        if selected is None:
            QMessageBox.information(self, "Hash Replacer", "Select a rule first.")
            return
        target_hash, source_hash = selected
        try:
            source_path = self.asset_store.asset_path(source_hash)
            message = self.cache_store.import_file(
                target_hash,
                source_path,
                origin=f"Hash Replacer / {self.profile_combo.currentText()}",
            )
        except (HashReplacerError, RobloxCacheError, OSError) as exc:
            QMessageBox.warning(self, "Hash replacement failed", str(exc))
            return
        self.status_label.setText(message)
        QMessageBox.information(self, "Hash replacement applied", message)

    def _restore_selected(self) -> None:
        selected = self._selected_rule()
        if selected is None:
            QMessageBox.information(self, "Hash Replacer", "Select a rule first.")
            return
        try:
            message = self.cache_store.restore(selected[0])
        except (RobloxCacheError, OSError) as exc:
            QMessageBox.warning(self, "Restore failed", str(exc))
            return
        self.status_label.setText(message)
        QMessageBox.information(self, "Cache entry restored", message)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh_profiles()
        self.refresh_assets()
