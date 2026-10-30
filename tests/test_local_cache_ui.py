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
            "Asset Explorer",
        ]
        assert widget.pages.currentIndex() == 0
        assert not widget.swaps_page.isHidden()
        assert widget.swaps_table.rowCount() == 1
        assert widget.swaps_table.item(0, 0).text() == "Active"
        assert widget.swaps_table.item(0, 1).text() == "Replace idle animation"
        assert widget.swap_count_label.text() == "1 swaps"
        assert widget.pages.tabText(1) == 'Asset Explorer'

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
        assert widget.kind_filter_button.text() == 'All types'
        available_kinds = {kind for kind, _label in cache_tab_module._KIND_FILTER_OPTIONS}
        for exact_kind in (
            'png', 'jpg', 'bmp', 'gif', 'webp', 'tiff', 'ico', 'dds', 'ktx',
            'psd', 'rbxm', 'xml', 'curve-anim', 'ogg', 'mp3', 'csgphs',
            'roblox-settings', 'video-metadata', 'video-manifest', 'http-response',
            'asset-metadata', 'other',
        ):
            assert exact_kind in available_kinds
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

        widget.set_preview_enabled(False)
        assert widget.preview_panel.isHidden()
        assert widget.stop_preview_button.isHidden()
        widget.set_preview_enabled(True)
        assert not widget.preview_panel.isHidden()
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

        widget._on_kind_filters_changed({'ktx'})
        assert widget.kind_filter_button.text() == 'KTX texture'
        assert widget.table.rowCount() == 1
        assert widget.table.item(0, 2).text() == 'ktx'

        widget._on_kind_filters_changed(set())
        assert widget.kind_filter_button.text() == 'All types'
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


def test_cache_asset_type_filter_popup_is_searchable_multi_select_and_scrollable():
    app = _qapp()
    options = list(cache_tab_module._KIND_FILTER_OPTIONS)
    popup = cache_tab_module._KindFilterPopup(options, {kind for kind, _label in options})
    try:
        assert popup.scroll_area.height() == 265
        assert not popup.all_button.isHidden()
        assert not popup.clear_button.isHidden()
        popup.filter_edit.setText('collision')
        assert not popup.checkboxes['csgphs'].isHidden()
        assert popup.checkboxes['png'].isHidden()
        popup._clear()
        assert popup.active == set()
        popup._select_all()
        assert popup.active == {kind for kind, _label in options}
    finally:
        popup.close()
        app.processEvents()


def test_asset_explorer_metadata_update_keeps_selected_cache_entry(monkeypatch, tmp_path):
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
        widget.entries = [
            RobloxCacheEntry(
                entry_id='a' * 32, file_size=80, database_size=80, modified_at=1,
                kind='rbxm', url='', asset_id='', has_rbxh_header=False,
                swap_status='', storage_mode='database',
            ),
            RobloxCacheEntry(
                entry_id='b' * 32, file_size=90, database_size=90, modified_at=2,
                kind='png', url='', asset_id='', has_rbxh_header=False,
                swap_status='', storage_mode='database',
            ),
        ]
        widget.entries_by_id = {entry.entry_id: entry for entry in widget.entries}
        widget._render_entries()
        widget._load_slot_preview = lambda _entry: None
        widget.table.selectRow(1)
        selected_id = widget._selected_entry_id()
        assert selected_id == 'b' * 32

        widget._save_cache_metadata('b' * 32, {'name': 'Texture Atlas'})
        assert widget._selected_entry_id() == selected_id
        assert widget.table.item(widget.table.currentRow(), 9).text() == 'Texture Atlas'

        widget._render_entries()
        assert widget._selected_entry_id() == selected_id
    finally:
        widget.close()
        app.processEvents()


def test_asset_explorer_rbxm_preview_lists_full_nested_animation_paths(monkeypatch, tmp_path):
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
        entry = RobloxCacheEntry(
            entry_id='c' * 32, file_size=256, database_size=256, modified_at=1,
            kind='rbxm', url='', asset_id='', has_rbxh_header=False,
            swap_status='', storage_mode='database',
        )
        widget.entries = [entry]
        widget.entries_by_id = {entry.entry_id: entry}
        widget._render_entries()
        widget._load_slot_preview = lambda _entry: None
        widget.table.selectRow(0)
        widget._current_preview_request_id = entry.entry_id

        xml = b'''<roblox version="4">
          <Item class="Folder" referent="RBX0">
            <Properties><string name="Name">r15artstintent</string></Properties>
            <Item class="Folder" referent="RBX1">
              <Properties><string name="Name">mood</string></Properties>
              <Item class="KeyframeSequence" referent="RBX2">
                <Properties><string name="Name">Idle</string></Properties>
              </Item>
            </Item>
          </Item>
        </roblox>'''
        widget._on_slot_payload_ready(entry.entry_id, xml, 'rbxm')

        assert widget._selected_entry_id() == entry.entry_id
        assert widget.preview_stack.currentWidget() is widget._model_panel
        assert widget.preview_title.text() == 'r15artstintent'
        assert 'r15artstintent / mood / Idle' in widget.preview_details.text()
    finally:
        widget.close()
        app.processEvents()
