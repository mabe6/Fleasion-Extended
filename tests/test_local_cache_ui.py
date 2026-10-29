"""Offscreen tests for the Local DB cache-swap workflow."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QWidget

from fleasion.gui import roblox_cache_tab as cache_tab_module


_APP = None


def _qapp():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


class _FakeStore:
    """In-memory stand-in for RobloxCacheStore with one saved swap."""

    def __init__(self, *_args, target_id="a" * 32, source_id="b" * 32, **_kwargs):
        self.root = Path(".")
        self.database_path = Path("rbx-storage.db")
        self.target_id = target_id
        self.source_id = source_id
        self.applied = True
        self.calls: list[tuple[str, str]] = []

    def validate(self):
        return None

    def saved_swaps(self):
        return [{"target": self.target_id, "name": "Replace idle animation", "source": self.source_id}]

    def _manifest(self):
        if not self.applied:
            return {}
        return {self.target_id: {"source": self.source_id, "time": "2026-10-26 12:00:00"}}

    def swap_status(self, entry_id):
        return "swapped" if self.applied and entry_id == self.target_id else ""

    def swap(self, target_id, source_id, *, origin=""):
        self.calls.append(("swap", target_id))
        self.applied = True
        return "swapped"

    def restore(self, target_id):
        self.calls.append(("restore", target_id))
        self.applied = False
        return "restored"


def test_local_db_swaps_page_is_first_and_toggles_swaps(monkeypatch):
    app = _qapp()
    monkeypatch.setattr(cache_tab_module, "RobloxCacheStore", _FakeStore)
    editor = QWidget()
    editor.database_opened = SimpleNamespace(connect=lambda _slot: None)
    editor.asset_explorer = SimpleNamespace(entry_picked=SimpleNamespace(connect=lambda _slot: None))
    widget = cache_tab_module.RobloxCacheTab(
        config_manager=SimpleNamespace(settings={}), additional_tab=editor
    )
    try:
        assert [widget.pages.tabText(i) for i in range(widget.pages.count())] == [
            "Swaps",
            "Load DB",
            "Cache Slots",
        ]
        assert widget.pages.widget(1) is editor
        assert widget.pages.currentIndex() == 0

        tree = widget.swaps_tree
        assert tree.topLevelItemCount() == 1
        item = tree.topLevelItem(0)
        assert item.text(0) == "On"
        assert item.checkState(0) == Qt.CheckState.Checked
        assert item.text(1) == "Replace idle animation"
        assert widget.swap_count_label.text() == "1 swaps"

        # Unticking restores the original but keeps the swap in the list
        widget._set_swap_enabled("a" * 32, "b" * 32, "Replace idle animation", False)
        assert widget.store.calls == [("restore", "a" * 32)]
        item = tree.topLevelItem(0)
        assert item.text(0) == "Off"
        assert item.checkState(0) == Qt.CheckState.Unchecked

        widget._set_swap_enabled("a" * 32, "b" * 32, "Replace idle animation", True)
        assert widget.store.calls[-1] == ("swap", "a" * 32)
        assert tree.topLevelItem(0).text(0) == "On"

        widget.swap_filter_edit.setText("no matching swap")
        assert tree.topLevelItemCount() == 0
        assert widget.swap_count_label.text() == "0 / 1 swaps"
        widget.swap_filter_edit.clear()
        assert tree.topLevelItemCount() == 1

        tree.setCurrentItem(tree.topLevelItem(0))
        widget._load_selected_swap()
        assert widget.target_edit.text() == "a" * 32
        assert widget.source_edit.text() == "b" * 32
        assert widget.swap_name_edit.text() == "Replace idle animation"

        widget._entry_picked("source", "c" * 32)
        assert widget.source_edit.text() == "c" * 32

        assert widget.probe_button.isHidden()
        widget.set_probe_header_enabled(True)
        assert not widget.probe_button.isHidden()
        widget.set_probe_header_enabled(False)
        assert widget.probe_button.isHidden()
    finally:
        widget.close()
        app.processEvents()
