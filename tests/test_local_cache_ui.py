"""Offscreen tests for the Local DB cache-swap workflow."""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from fleasion.gui import roblox_cache_tab as cache_tab_module


_APP = None


def _qapp():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def test_local_db_opens_on_active_swaps_with_pasteable_ids_and_optional_probe(
    monkeypatch, tmp_path
):
    app = _qapp()
    target_id = "a" * 32
    source_id = "b" * 32

    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            self.root = tmp_path

        def _manifest(self):
            return {
                target_id: {
                    "source": source_id,
                    "origin": "Replace idle animation",
                    "time": "2026-10-26 12:00:00",
                }
            }

        def swap_status(self, entry_id):
            return "swapped" if entry_id == target_id else ""

    monkeypatch.setattr(cache_tab_module, "RobloxCacheStore", FakeStore)
    config = SimpleNamespace(settings={})
    widget = cache_tab_module.RobloxCacheTab(config_manager=config)
    try:
        assert [widget.pages.tabText(i) for i in range(widget.pages.count())] == [
            "Active Swaps",
            "Cache Slots",
        ]
        assert widget.pages.currentIndex() == 0
        assert widget.swaps_table.rowCount() == 1
        assert widget.swaps_table.item(0, 0).text() == "Active"
        assert widget.swaps_table.item(0, 1).text() == "Replace idle animation"
        assert widget.swap_count_label.text() == "1 swaps"

        widget.swap_filter_edit.setText("no matching swap")
        assert widget.swaps_table.rowCount() == 0
        assert widget.swap_count_label.text() == "0 / 1 swaps"
        widget.swap_filter_edit.clear()
        assert widget.swaps_table.rowCount() == 1
        widget.swap_status_filter.setCurrentIndex(2)
        assert widget.swaps_table.rowCount() == 0
        widget.swap_status_filter.setCurrentIndex(1)
        assert widget.swaps_table.rowCount() == 1

        assert widget.probe_button.isHidden()
        assert widget.output.isHidden()

        widget.swaps_table.setCurrentCell(0, 1)
        widget._load_selected_swap()
        assert widget.target_edit.text() == target_id
        assert widget.source_edit.text() == source_id
        assert widget.swap_name_edit.text() == "Replace idle animation"

        widget.set_probe_header_enabled(True)
        assert not widget.probe_button.isHidden()
        assert not widget.output.isHidden()
        widget.set_probe_header_enabled(False)
        assert widget.probe_button.isHidden()
        assert widget.output.isHidden()
    finally:
        widget.close()
        app.processEvents()
