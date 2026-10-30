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
        assert not widget.swaps_page.isHidden()
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
        assert not widget.entries_page.isHidden()
        assert widget.layout().indexOf(widget.pages) == -1
        assert widget.preview_panel is not None
        assert widget.asset_lookup_button.text() == 'Asset Lookup…'
        assert not widget.asset_lookup_button.isEnabled()
        assert widget.stop_preview_button.isHidden()
        assert widget.kind_filter_combo.findData('ktx') >= 0
        assert widget.kind_filter_combo.findData('csgphs') >= 0
        for exact_kind in (
            'png', 'jpg', 'bmp', 'gif', 'webp', 'tiff', 'ico', 'dds',
            'psd', 'rbxm', 'xml', 'curve-anim', 'ogg', 'mp3',
            'roblox-settings', 'video-metadata', 'video-manifest', 'http-response',
        ):
            assert widget.kind_filter_combo.findData(exact_kind) >= 0
        assert widget.kind_filter_combo.findData('asset-metadata') >= 0
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
        preview_calls = []
        widget._load_slot_preview = lambda entry: preview_calls.append(entry.entry_id)
        widget._render_entries()
        assert widget.table.rowCount() == 2
        widget.table.selectRow(0)
        assert not widget.stop_preview_button.isHidden()
        assert preview_calls[-1] == 'a' * 32
        widget._stop_preview()
        assert widget.stop_preview_button.isHidden()
        assert widget.preview_panel.isHidden()
        row_item = widget.table.item(0, 0)
        widget._cache_entry_clicked(row_item)
        assert not widget.stop_preview_button.isHidden()
        assert not widget.preview_panel.isHidden()
        assert preview_calls[-1] == 'a' * 32

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


def test_cache_slots_plays_ogg_payload_instead_of_hex_dump(monkeypatch, tmp_path):
    import sys
    from PyQt6.QtWidgets import QWidget

    app = _qapp()

    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            self.root = tmp_path

        def _manifest(self):
            return {}

        def swap_status(self, _entry_id):
            return ''

    class FakeAudioPlayer(QWidget):
        def __init__(self, path, parent=None, config_manager=None):
            super().__init__(parent)
            self.path = path
            self.stopped = False

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(cache_tab_module, 'RobloxCacheStore', FakeStore)
    monkeypatch.setitem(
        sys.modules,
        'fleasion.cache.audio_player',
        SimpleNamespace(AudioPlayerWidget=FakeAudioPlayer),
    )
    widget = cache_tab_module.RobloxCacheTab(
        config_manager=SimpleNamespace(settings={}),
        view_mode='slots',
    )
    try:
        entry = RobloxCacheEntry(
            entry_id='c' * 32,
            asset_id='',
            file_size=128,
            database_size=128,
            modified_at=1,
            kind='other',
            url='https://cdn.example.invalid/audio',
            has_rbxh_header=False,
            swap_status='',
            storage_mode='file',
        )
        widget.entries = [entry]
        widget.entries_by_id = {entry.entry_id: entry}
        widget._current_preview_request_id = entry.entry_id
        widget._on_slot_payload_ready(entry.entry_id, b'OggS' + bytes(64), 'ogg')

        assert isinstance(widget._audio_player, FakeAudioPlayer)
        assert widget.preview_stack.currentWidget() is widget._audio_panel
        assert widget._audio_preview_path.is_file()
        widget._stop_preview()
        assert widget._audio_player is None
        assert not widget._audio_preview_path
    finally:
        widget.close()
        app.processEvents()
