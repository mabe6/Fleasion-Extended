"""Offscreen tests for the Local DB cache-swap workflow."""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from fleasion.gui import roblox_cache_tab as cache_tab_module
from fleasion.database.roblox_cache_store import RobloxCacheEntry


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


def test_cache_slots_view_has_preview_and_type_status_filters(monkeypatch, tmp_path):
    app = _qapp()

    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            self.root = tmp_path

        def _manifest(self):
            return {}

        def swap_status(self, _entry_id):
            return ''

    monkeypatch.setattr(cache_tab_module, 'RobloxCacheStore', FakeStore)
    widget = cache_tab_module.RobloxCacheTab(
        config_manager=SimpleNamespace(settings={}),
        view_mode='slots',
    )
    try:
        assert widget.layout().indexOf(widget.entries_page) >= 0
        assert widget.layout().indexOf(widget.pages) == -1
        assert widget.preview_panel is not None
        assert widget.kind_filter_combo.findData('ktx') >= 0
        assert widget.slot_status_filter.findData('swapped') >= 0

        widget.entries = [
            RobloxCacheEntry(
                entry_id='a' * 32, file_size=1024, database_size=1024, modified_at=1,
                kind='ktx', url='https://assets.invalid/?id=123', asset_id='123',
                has_rbxh_header=False, swap_status='swapped', storage_mode='database', name='Classic Sword',
            ),
            RobloxCacheEntry(
                entry_id='b' * 32, file_size=128, database_size=128, modified_at=2,
                kind='mesh', url='https://assets.invalid/?id=456', asset_id='456',
                has_rbxh_header=False, swap_status='', storage_mode='file',
            ),
        ]
        widget.entries_by_id = {entry.entry_id: entry for entry in widget.entries}
        widget._render_entries()
        assert widget.table.rowCount() == 2

        widget.kind_filter_combo.setCurrentIndex(widget.kind_filter_combo.findData('ktx'))
        assert widget.table.rowCount() == 1
        assert widget.table.item(0, 2).text() == 'ktx'

        widget.kind_filter_combo.setCurrentIndex(widget.kind_filter_combo.findData('all'))
        widget.slot_status_filter.setCurrentIndex(
            widget.slot_status_filter.findData('swapped')
        )
        assert widget.table.rowCount() == 1
        assert widget.table.item(0, 0).text() == 'a' * 32

        widget.search_edit.setText('type:ktx size:>500b')
        assert widget.table.rowCount() == 1
        assert widget.table.item(0, 0).text() == 'a' * 32

        widget.search_edit.setText('name:"Classic Sword"')
        assert widget.table.rowCount() == 1
        assert widget.table.item(0, 0).text() == 'a' * 32
    finally:
        widget.close()
        app.processEvents()
