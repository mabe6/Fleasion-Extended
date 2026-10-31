"""Offscreen tests for the Local DB cache-swap workflow."""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QItemSelectionModel, Qt
from PyQt6.QtWidgets import QApplication, QAbstractItemView

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
            "Scraper DB",
        ]
        assert widget.pages.currentIndex() == 0
        assert not widget.swaps_page.isHidden()
        assert widget.swaps_table.rowCount() == 1
        assert widget.swaps_table.item(0, 0).text() == "Active"
        assert widget.swaps_table.item(0, 1).text() == "Replace idle animation"
        assert widget.swap_count_label.text() == "1 swaps"
        assert widget.pages.tabText(1) == 'Scraper DB'

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



def test_scraper_db_sort_cycles_all_columns_and_restores_source_order(monkeypatch, tmp_path):
    app = _qapp()

    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            self.root = tmp_path

        def _manifest(self):
            return {}

        def swap_status(self, _entry_id):
            return ''

    monkeypatch.setattr(cache_tab_module, "RobloxCacheStore", FakeStore)
    widget = cache_tab_module.RobloxCacheTab(
        config_manager=SimpleNamespace(settings={}),
        view_mode='slots',
    )
    preview_calls = []
    widget._load_slot_preview = lambda entry: preview_calls.append(entry.entry_id)

    def make_entry(entry_id, *, name, kind, size, db_size, modified, asset_id, status, storage, url):
        return RobloxCacheEntry(
            entry_id=entry_id * 32,
            file_size=size,
            database_size=db_size,
            modified_at=modified,
            kind=kind,
            url=url,
            asset_id=asset_id,
            has_rbxh_header=False,
            swap_status=status,
            storage_mode=storage,
            name=name,
        )

    source_entries = [
        make_entry(
            'c', name='asset10', kind='mesh', size=20, db_size=100,
            modified=20, asset_id='30', status='', storage='database', url='https://x/c',
        ),
        make_entry(
            'a', name='asset2', kind='png', size=10, db_size=200,
            modified=10, asset_id='10', status='swapped', storage='file', url='https://x/a',
        ),
        make_entry(
            'b', name='asset1', kind='rbxm', size=20, db_size=150,
            modified=20, asset_id='20', status='changed', storage='database', url='https://x/b',
        ),
        make_entry(
            'd', name='', kind='other', size=None, db_size=None,
            modified=None, asset_id='', status='', storage='', url='',
        ),
    ]

    def visible_ids():
        return [widget.table.item(row, 0).text() for row in range(widget.table.rowCount())]

    try:
        widget.entries = list(source_entries)
        widget.entries_by_id = {entry.entry_id: entry for entry in source_entries}
        widget._render_entries()
        assert visible_ids() == ['c' * 32, 'a' * 32, 'b' * 32, 'd' * 32]
        assert widget.table.item(3, 5).text() == '—'

        widget.table.selectRow(1)
        assert widget._selected_entry_id() == 'a' * 32
        assert preview_calls == ['a' * 32]

        header = widget.table.horizontalHeader()
        # Size: descending → ascending → original source order.
        widget._on_table_header_clicked(3)
        assert visible_ids() == ['c' * 32, 'b' * 32, 'a' * 32, 'd' * 32]
        assert header.isSortIndicatorShown()
        assert header.sortIndicatorSection() == 3
        assert header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
        assert widget._selected_entry_id() == 'a' * 32
        assert preview_calls == ['a' * 32]

        widget._on_table_header_clicked(3)
        assert visible_ids() == ['a' * 32, 'c' * 32, 'b' * 32, 'd' * 32]
        assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder

        widget._on_table_header_clicked(3)
        assert visible_ids() == ['c' * 32, 'a' * 32, 'b' * 32, 'd' * 32]
        assert not header.isSortIndicatorShown()

        # Date uses raw timestamps and keeps a missing timestamp at the end.
        widget._on_table_header_clicked(5)
        assert visible_ids() == ['c' * 32, 'b' * 32, 'a' * 32, 'd' * 32]
        widget._on_table_header_clicked(5)
        assert visible_ids() == ['a' * 32, 'c' * 32, 'b' * 32, 'd' * 32]
        widget._on_table_header_clicked(5)
        assert visible_ids() == ['c' * 32, 'a' * 32, 'b' * 32, 'd' * 32]

        # Name uses natural, case-insensitive ordering (asset2 before asset10).
        widget._on_table_header_clicked(9)
        assert visible_ids() == ['c' * 32, 'a' * 32, 'b' * 32, 'd' * 32]
        widget._on_table_header_clicked(9)
        assert visible_ids() == ['b' * 32, 'a' * 32, 'c' * 32, 'd' * 32]
        widget._on_table_header_clicked(9)
        assert visible_ids() == ['c' * 32, 'a' * 32, 'b' * 32, 'd' * 32]

        # Every data column has a complete three-state cycle; switching columns
        # always starts again in descending order.
        for column in range(widget.table.columnCount()):
            widget._on_table_header_clicked(column)
            assert widget._sort_column == column
            assert widget._sort_direction == 'descending'
            assert header.isSortIndicatorShown()
            widget._on_table_header_clicked(column)
            assert widget._sort_direction == 'ascending'
            widget._on_table_header_clicked(column)
            assert widget._sort_column is None
            assert widget._sort_direction == 'none'
            assert not header.isSortIndicatorShown()
    finally:
        widget.close()
        app.processEvents()




def test_scraper_db_page_navigation_does_not_snap_back_to_selected_row(
    monkeypatch, tmp_path
):
    app = _qapp()

    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            self.root = tmp_path

        def _manifest(self):
            return {}

        def swap_status(self, _entry_id):
            return ''

    monkeypatch.setattr(cache_tab_module, "RobloxCacheStore", FakeStore)
    widget = cache_tab_module.RobloxCacheTab(
        config_manager=SimpleNamespace(settings={}),
        view_mode='slots',
    )
    widget._load_slot_preview = lambda _entry: None
    try:
        entries = [
            RobloxCacheEntry(
                entry_id=f'{index:032x}',
                file_size=1,
                database_size=1,
                modified_at=float(index),
                kind='rbxm',
                url=f'https://assets.invalid/{index}',
                asset_id=str(index + 1),
                has_rbxh_header=False,
                swap_status='',
                storage_mode='database',
            )
            for index in range(600)
        ]
        widget.entries = entries
        widget.entries_by_id = {entry.entry_id: entry for entry in entries}
        widget._render_entries()
        assert widget.table.rowCount() == 250

        widget.table.selectRow(0)
        widget.entries_next_button.click()
        app.processEvents()

        assert widget._entries_page_offset == 250
        assert widget.table.rowCount() == 250
        assert widget.table.item(0, 0).text() == entries[250].entry_id
        assert widget._selected_entry_id() == ''

        widget.entries_prev_button.click()
        app.processEvents()
        assert widget._entries_page_offset == 0
        assert widget.table.item(0, 0).text() == entries[0].entry_id
    finally:
        widget.close()
        app.processEvents()




def test_large_rbxm_preview_populates_tree_branches_lazily():
    from fleasion.cache.rbxm_preview import (
        PreviewDocument,
        PreviewInstance,
        RbxmPreviewWidget,
    )

    app = _qapp()
    root = PreviewInstance("Folder", "RBX0")
    children = [
        PreviewInstance("Part", f"RBX{i}")
        for i in range(1600)
    ]
    children[0].children = [
        PreviewInstance("Attachment", "RBX-CHILD-1"),
        PreviewInstance("Attachment", "RBX-CHILD-2"),
    ]
    root.children = children
    instances = {"RBX0": root}
    instances.update({child.referent: child for child in children})
    instances.update({child.referent: child for child in children[0].children})
    document = PreviewDocument(
        roots=[root],
        instances=instances,
    )
    widget = RbxmPreviewWidget()
    try:
        widget.load_document(document, asset_label="large-fixture", dirty=False)
        assert widget._lazy_tree_enabled
        assert widget.tree.topLevelItemCount() == 1

        root_item = widget.tree.topLevelItem(0)
        assert root_item.childCount() == 1
        root_item.setExpanded(True)
        app.processEvents()

        assert root_item.childCount() == 1600
        first_child = root_item.child(0)
        assert first_child.text(0) == "Part"
        assert first_child.childCount() == 1
        first_child.setExpanded(True)
        app.processEvents()
        assert first_child.childCount() == 2
    finally:
        widget.close()
        app.processEvents()



def test_scraper_db_kind_filter_uses_grouped_multiselect_category_popup():
    from fleasion.cache.asset_type_filter import CategoryFilterPopup

    app = _qapp()
    labels = dict(cache_tab_module._KIND_FILTER_OPTIONS)
    assigned_kinds = [
        kind
        for kinds in cache_tab_module._KIND_FILTER_CATEGORIES.values()
        for kind in kinds
    ]
    assert len(assigned_kinds) == len(set(assigned_kinds))
    assert set(assigned_kinds) == set(labels)

    popup = CategoryFilterPopup(
        active_filters={'ktx'},
        categories=cache_tab_module._KIND_FILTER_CATEGORIES,
        type_labels=labels,
        category_labels=cache_tab_module._KIND_FILTER_CATEGORY_LABELS,
    )
    emitted = []
    popup.filters_changed.connect(lambda kinds: emitted.append(set(kinds)))
    popup.show()
    app.processEvents()

    try:
        grid = popup.container.layout().itemAt(0).layout()
        assert grid.columnCount() == 4
        assert grid.rowCount() == 2
        assert len(popup.category_checkboxes) == 5
        assert len(popup.checkboxes) == len(labels)
        assert popup.checkboxes['ktx'].isChecked()
        assert (
            popup.category_checkboxes['images_textures'].checkState()
            == Qt.CheckState.PartiallyChecked
        )

        # Individual type toggles stay in the same popup and preserve selections.
        popup.checkboxes['png'].click()
        assert popup.isVisible()
        assert popup.active_filters == {'ktx', 'png'}
        assert popup.category_checkboxes['images_textures'].checkState() == Qt.CheckState.PartiallyChecked

        popup.checkboxes['ogg'].click()
        assert popup.isVisible()
        assert popup.active_filters == {'ktx', 'png', 'ogg'}
        assert popup.category_checkboxes['audio_video'].checkState() == Qt.CheckState.PartiallyChecked

        # Clicking a partially selected category selects every kind in that group.
        popup.category_checkboxes['images_textures'].click()
        assert popup.isVisible()
        assert set(cache_tab_module._KIND_FILTER_CATEGORIES['images_textures']) <= popup.active_filters
        assert (
            popup.category_checkboxes['images_textures'].checkState()
            == Qt.CheckState.Checked
        )

        # Clicking a fully selected category clears that group only.
        popup.category_checkboxes['images_textures'].click()
        assert popup.isVisible()
        assert not (
            set(cache_tab_module._KIND_FILTER_CATEGORIES['images_textures'])
            & popup.active_filters
        )
        assert 'ogg' in popup.active_filters

        popup._clear_all()
        assert popup.isVisible()
        assert popup.active_filters == set()
        assert emitted[-1] == set()
    finally:
        popup.close()
        app.processEvents()



def test_scraper_db_extended_selection_and_copy_actions(monkeypatch, tmp_path):
    app = _qapp()

    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            self.root = tmp_path

        def _manifest(self):
            return {}

        def swap_status(self, _entry_id):
            return ''

    monkeypatch.setattr(cache_tab_module, "RobloxCacheStore", FakeStore)
    widget = cache_tab_module.RobloxCacheTab(
        config_manager=SimpleNamespace(settings={}),
        view_mode='slots',
    )
    widget.resize(1200, 700)
    widget.show()
    app.processEvents()

    entries = [
        RobloxCacheEntry(
            entry_id='a' * 32, file_size=100, database_size=110, modified_at=1,
            kind='mesh', url='https://assets.invalid/a', asset_id='100',
            has_rbxh_header=False, swap_status='', storage_mode='database', name='First Mesh',
        ),
        RobloxCacheEntry(
            entry_id='b' * 32, file_size=200, database_size=210, modified_at=2,
            kind='png', url='https://assets.invalid/b', asset_id='200',
            has_rbxh_header=False, swap_status='', storage_mode='file', name='Second Image',
        ),
        RobloxCacheEntry(
            entry_id='c' * 32, file_size=300, database_size=310, modified_at=3,
            kind='rbxm', url='https://assets.invalid/c', asset_id='300',
            has_rbxh_header=False, swap_status='', storage_mode='database', name='Third Model',
        ),
    ]
    try:
        assert (
            widget.table.selectionMode()
            == QAbstractItemView.SelectionMode.ExtendedSelection
        )
        assert (
            widget.table.selectionBehavior()
            == QAbstractItemView.SelectionBehavior.SelectRows
        )

        widget.entries = list(entries)
        widget.entries_by_id = {entry.entry_id: entry for entry in entries}
        widget._render_entries()

        selection = widget.table.selectionModel()
        widget.table.selectRow(0)
        selection.select(
            widget.table.model().index(1, 0),
            QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        assert widget._selected_table_rows() == [0, 1]

        widget._copy_selected_table_column(1)
        assert QApplication.clipboard().text() == '100\n200'

        widget._copy_selected_table_rows()
        copied_rows = QApplication.clipboard().text().splitlines()
        assert len(copied_rows) == 2
        # Name is visually moved next to Entry ID in this table.
        assert copied_rows[0].split('\t')[:3] == ['a' * 32, 'First Mesh', '100']
        assert copied_rows[1].split('\t')[:3] == ['b' * 32, 'Second Image', '200']

        # Sorting must preserve all selected entry IDs, not only the current row.
        widget._on_table_header_clicked(3)
        retained_ids = {
            widget.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            for row in widget._selected_table_rows()
        }
        assert retained_ids == {'a' * 32, 'b' * 32}
        assert widget._selected_entry_id() in {'a' * 32, 'b' * 32}
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
    widget.resize(1200, 700)
    widget.show()
    app.processEvents()
    try:
        assert widget.layout().indexOf(widget.entries_page) >= 0
        assert not widget.entries_page.isHidden()
        assert widget.layout().indexOf(widget.pages) == -1
        assert widget.preview_panel is not None
        assert widget.preview_panel.isHidden()
        assert widget.entries_splitter.sizes()[1] == 0
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
        app.processEvents()
        assert not widget.preview_panel.isHidden()
        assert widget.entries_splitter.sizes()[1] > 0
        assert preview_calls[-1] == 'a' * 32

        widget.set_preview_enabled(False)
        app.processEvents()
        assert widget.preview_panel.isHidden()
        assert widget.entries_splitter.sizes()[1] == 0
        assert widget.stop_preview_button.isHidden()
        widget.set_preview_enabled(True)
        app.processEvents()
        assert not widget.preview_panel.isHidden()
        assert widget.entries_splitter.sizes()[1] > 0
        assert not widget.stop_preview_button.isHidden()
        assert preview_calls[-1] == 'a' * 32

        widget._stop_preview()
        app.processEvents()
        assert widget.stop_preview_button.isHidden()
        assert widget.preview_panel.isHidden()
        assert widget.entries_splitter.sizes()[1] == 0
        row_item = widget.table.item(0, 0)
        widget._cache_entry_clicked(row_item)
        assert not widget.stop_preview_button.isHidden()
        assert not widget.preview_panel.isHidden()
        app.processEvents()
        assert widget.entries_splitter.sizes()[1] > 0
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


def test_cache_asset_type_filter_popup_uses_grouped_multiselect_categories():
    app = _qapp()
    from fleasion.cache.asset_type_filter import CategoryFilterPopup

    options = dict(cache_tab_module._KIND_FILTER_OPTIONS)
    popup = CategoryFilterPopup(
        parent=None,
        active_filters=set(options),
        categories={
            name: list(kinds)
            for name, kinds in cache_tab_module._KIND_FILTER_CATEGORIES.items()
        },
        type_labels=options,
        category_labels=cache_tab_module._KIND_FILTER_CATEGORY_LABELS,
    )
    try:
        assert popup.scroll_area.height() > 0
        assert len(popup.category_checkboxes) == 5
        assert set(popup.checkboxes) == set(options)
        assert popup.checkboxes['csgphs'].isChecked()
        assert popup.checkboxes['png'].isChecked()

        # Individual asset kinds can be toggled independently.
        popup.checkboxes['csgphs'].click()
        assert 'csgphs' not in popup.active_filters
        assert 'png' in popup.active_filters

        # Category checkboxes select or clear all kinds in that group.
        popup.category_checkboxes['models_3d'].click()
        assert set(cache_tab_module._KIND_FILTER_CATEGORIES['models_3d']).issubset(
            popup.active_filters
        )

        popup._clear_all()
        assert popup.active_filters == set()
        popup.category_checkboxes['images_textures'].click()
        assert set(cache_tab_module._KIND_FILTER_CATEGORIES['images_textures']).issubset(
            popup.active_filters
        )
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
