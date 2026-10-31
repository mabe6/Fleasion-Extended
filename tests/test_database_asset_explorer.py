"""Tests for the local database asset explorer."""

from __future__ import annotations

import os
import struct
import subprocess
import sys
from types import SimpleNamespace

from fleasion.gui.database_asset_explorer import (
    _asset_id_from_url,
    _detect_kind,
    _embedded_name,
    _kind_group,
)


def test_asset_kind_recognizes_readable_animation_and_image_formats() -> None:
    kind, url = _detect_kind(
        b'<roblox version="4"><Item class="KeyframeSequence"></Item></roblox>'
    )
    assert kind == 'anim'
    assert url == ''

    kind, url = _detect_kind(b'\x89PNG\r\n\x1a\n' + b'payload')
    assert kind == 'png'
    assert url == ''


def test_detect_kind_defers_truncated_gzip_prefix_until_preview() -> None:
    import gzip
    import hashlib

    from fleasion.database.roblox_cache_store import (
        decompress_roblox_payload,
        detect_roblox_payload_kind,
    )

    payload = b'version 1.00\n' + b''.join(
        hashlib.sha256(str(index).encode()).digest() for index in range(3000)
    )
    compressed = gzip.compress(payload)
    assert len(compressed) > 8192

    # Database refresh classifies by the compression signature only.
    kind, url = _detect_kind(compressed[:8192])
    assert kind == 'compressed'
    assert url == ''

    # The actual format is identified only once the selected payload is decoded.
    assert detect_roblox_payload_kind(decompress_roblox_payload(compressed)) == 'mesh'


def test_embedded_roblox_xml_name_is_readable() -> None:
    payload = (
        b'<roblox version="4"><Item class="KeyframeSequence"><Properties>'
        b'<string name="Name">wave</string></Properties></Item></roblox>'
    )
    assert _embedded_name(payload) == 'wave'


def test_ktx2_bc7_uses_extended_decoder(monkeypatch) -> None:
    import sys

    from fleasion.cache.tools.ktx_to_png.ktx_to_png import KTX2_MAGIC, convert

    # A valid 4x4, single-level KTX2 BC7 payload (Vulkan format 145).
    header = (
        KTX2_MAGIC
        + struct.pack('<9I', 145, 1, 4, 4, 0, 0, 1, 1, 0)
        + bytes(32)
    )
    level_index = struct.pack('<QQQ', 104, 16, 16)
    ktx = header + level_index + bytes(16)
    fake_decoder = SimpleNamespace(
        decode_bc7=lambda _payload, width, height: bytes((0, 0, 255, 255)) * width * height
    )
    monkeypatch.setitem(sys.modules, 'texture2ddecoder', fake_decoder)

    png = convert(ktx)
    assert png is not None
    assert png.startswith(b'\x89PNG\r\n\x1a\n')


def test_asset_url_extraction_and_type_groups() -> None:
    assert _asset_id_from_url('https://www.roblox.com/asset/?id=123456') == '123456'
    assert _asset_id_from_url('https://example.invalid/assets/789') == '789'
    assert _kind_group('anim') == 'Animations'
    assert _kind_group('curve-anim') == 'Animations'
    assert _kind_group('png') == 'Images'
    assert _kind_group('other') == 'Other'


def test_asset_explorer_scans_filters_and_previews_database_rows_offscreen() -> None:
    code = r"""
import sqlite3
import tempfile
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from fleasion.gui.database_asset_explorer import DatabaseAssetExplorer

app = QApplication([])
with tempfile.TemporaryDirectory() as folder:
    path = Path(folder) / 'rbx-storage.db'
    payload = b'\x89PNG\r\n\x1a\n' + bytes(64)
    with sqlite3.connect(path) as connection:
        connection.execute(
            'CREATE TABLE files (id BLOB PRIMARY KEY, content BLOB, size INTEGER NOT NULL, category INTEGER, hits INTEGER, atime INTEGER)'
        )
        connection.execute(
            'INSERT INTO files (id, content, size, category, hits, atime) VALUES (?, ?, ?, ?, ?, ?)',
            (bytes.fromhex('01' * 16), payload, len(payload), 1, 3, 100),
        )
        connection.execute(
            'INSERT INTO files (id, content, size, category, hits, atime) VALUES (?, ?, ?, ?, ?, ?)',
            (bytes.fromhex('02' * 16), b'unknown-payload', 15, 2, 1, 50),
        )

    widget = DatabaseAssetExplorer()
    assert widget.set_database(path) is True
    assert len(widget.items) == 2
    assert widget.items_by_id['01' * 16].kind == 'png'
    widget._active_kinds = {'png'}
    widget._render_items()
    assert widget.asset_table.rowCount() == 1
    assert widget.asset_table.item(0, 4).text() == 'PNG image'
    assert widget.asset_table.horizontalHeaderItem(1).text() == 'Name / hash'
    assert not widget.preview_panel.isHidden()
    widget.search_edit.setText('01010101')
    assert widget.asset_table.rowCount() == 1
    widget.search_edit.setText('type:png size:>32b')
    assert widget.asset_table.rowCount() == 1
    widget.search_edit.setText('no-such-asset')
    assert widget.asset_table.rowCount() == 0
    widget.close()
"""
    env = os.environ.copy()
    env['QT_QPA_PLATFORM'] = 'offscreen'
    subprocess.run([sys.executable, '-c', code], check=True, env=env)


def test_asset_explorer_reads_file_backed_entries_offscreen() -> None:
    code = r"""
import sqlite3
import struct
import tempfile
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from fleasion.gui.database_asset_explorer import DatabaseAssetExplorer

app = QApplication([])
with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    path = root / 'rbx-storage.db'
    url = b'https://assets.example/asset/?id=120350549417076'
    payload = b'<roblox version="4"><Item class="KeyframeSequence"></Item></roblox>'
    entry = b'RBXH' + bytes(4) + struct.pack('<I', len(url)) + url + bytes(25) + payload
    entry_id = 'ab' + '12' * 15
    physical = root / 'rbx-storage' / entry_id[:2] / entry_id
    physical.parent.mkdir(parents=True)
    physical.write_bytes(entry)
    with sqlite3.connect(path) as connection:
        connection.execute(
            'CREATE TABLE files (id BLOB PRIMARY KEY, content BLOB, size INTEGER NOT NULL, atime INTEGER)'
        )
        connection.execute(
            'INSERT INTO files (id, content, size, atime) VALUES (?, NULL, ?, 1)',
            (bytes.fromhex(entry_id), len(entry)),
        )
        # A metadata row with neither a BLOB nor a file must be skipped
        connection.execute(
            'INSERT INTO files (id, content, size, atime) VALUES (?, NULL, 10, 2)',
            (bytes.fromhex('cd' * 16),),
        )

    widget = DatabaseAssetExplorer()
    assert widget.set_database(path) is True
    assert list(widget.items_by_id) == [entry_id]
    item = widget.items_by_id[entry_id]
    assert item.kind == 'anim'
    assert item.asset_id == '120350549417076'
    assert item.content_size == len(entry)
    assert widget._read_entry_bytes(entry_id) == entry
    widget.close()
"""
    env = os.environ.copy()
    env['QT_QPA_PLATFORM'] = 'offscreen'
    subprocess.run([sys.executable, '-c', code], check=True, env=env)


def test_detect_kind_recognizes_ktx1_and_ktx2_texture_headers() -> None:
    ktx1 = b'\xabKTX 11\xbb\r\n\x1a\n' + bytes(64)
    ktx2 = b'\xabKTX 20\xbb\r\n\x1a\n' + bytes(64)
    assert _detect_kind(ktx1)[0] == 'ktx'
    assert _detect_kind(ktx2)[0] == 'ktx'


def test_asset_preview_preference_can_be_disabled_and_restored() -> None:
    code = r"""
from types import SimpleNamespace
from PyQt6.QtWidgets import QApplication
from fleasion.gui.database_asset_explorer import DatabaseAssetExplorer

app = QApplication([])
settings = {'asset_database_preview_enabled': True}
save_calls = []
config = SimpleNamespace(settings=settings, save=lambda: save_calls.append(True))
widget = DatabaseAssetExplorer(config_manager=config)
try:
    widget.set_preview_enabled(False)
    assert widget._preview_enabled is False
    assert settings['asset_database_preview_enabled'] is False
    widget.set_preview_enabled(True)
    assert widget._preview_enabled is True
    assert settings['asset_database_preview_enabled'] is True
    assert len(save_calls) == 2
finally:
    widget.close()
    app.processEvents()
"""
    env = os.environ.copy()
    env['QT_QPA_PLATFORM'] = 'offscreen'
    subprocess.run([sys.executable, '-c', code], check=True, env=env)



def test_detect_kind_recognises_roblox_settings_and_video_resources() -> None:
    from fleasion.database.roblox_cache_store import detect_roblox_payload_kind

    settings_url = (
        'https://clientsettingscdn.roblox.com/v2/settings-compressed/'
        'application/PCDesktopClient/abcdef.dcz'
    )
    hls_url = 'https://sc5.rbxcdn.com/c/hash/hash/main.m3u8?token=abc'
    assert detect_roblox_payload_kind(
        b'content-encoding: dcz\r\n', url=settings_url
    ) == 'roblox-settings'
    assert detect_roblox_payload_kind(
        b'#EXTM3U\n#EXTINF:4,\nsegment.ts', url=hls_url
    ) == 'video-manifest'
    assert detect_roblox_payload_kind(
        b'roblox-assettypeid: 86\r\n', url=hls_url
    ) == 'video-metadata'
    assert _kind_group('video-manifest') == 'Video'
    assert _kind_group('roblox-settings') == 'Settings'


def test_ktx_texture_metadata_exposes_layers_faces_and_mip_levels() -> None:
    from fleasion.cache.tools.ktx_to_png.ktx_to_png import (
        KTX1_MAGIC,
        KTX2_MAGIC,
        inspect_texture_metadata,
    )

    ktx2_header = struct.pack(
        '<9I', 37, 1, 128, 64, 0, 4, 6, 5, 0
    ) + bytes(32)
    metadata2 = inspect_texture_metadata(KTX2_MAGIC + ktx2_header)
    assert metadata2 is not None
    assert metadata2['container'] == 'KTX2'
    assert metadata2['width'] == 128
    assert metadata2['height'] == 64
    assert metadata2['layers'] == 4
    assert metadata2['faces'] == 6
    assert metadata2['mip_levels'] == 5

    ktx1_header = struct.pack(
        '<13I',
        0x04030201, 0, 1, 0, 0x83F1, 0x1908,
        256, 128, 0, 3, 6, 8, 0,
    )
    metadata1 = inspect_texture_metadata(KTX1_MAGIC + ktx1_header)
    assert metadata1 is not None
    assert metadata1['container'] == 'KTX1'
    assert metadata1['layers'] == 3
    assert metadata1['faces'] == 6
    assert metadata1['mip_levels'] == 8


def test_rbxm_preview_lists_animation_names_and_folders() -> None:
    code = r"""
from PyQt6.QtWidgets import QApplication
from fleasion.cache.rbxm_preview import RbxmPreviewWidget

app = QApplication([])
xml = b'''<roblox version="4">
  <Item class="Folder" referent="RBX0">
    <Properties><string name="Name">Animations</string></Properties>
    <Item class="KeyframeSequence" referent="RBX1">
      <Properties><string name="Name">Wave</string></Properties>
    </Item>
    <Item class="Folder" referent="RBX2">
      <Properties><string name="Name">Movement</string></Properties>
      <Item class="KeyframeSequence" referent="RBX3">
        <Properties><string name="Name">Run</string></Properties>
      </Item>
    </Item>
  </Item>
</roblox>'''
widget = RbxmPreviewWidget()
widget.load_bytes(xml, 'test')
assert widget.animation_names() == ['Animations', 'Wave', 'Movement', 'Run']
assert widget.animation_paths() == ['Animations / Wave', 'Animations / Movement / Run']
widget.close()
app.processEvents()
"""
    env = os.environ.copy()
    env['QT_QPA_PLATFORM'] = 'offscreen'
    subprocess.run([sys.executable, '-c', code], check=True, env=env)


def test_asset_database_mesh_preview_creates_obj_viewer(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace
    from PyQt6.QtWidgets import QApplication, QWidget

    from fleasion.gui import database_asset_explorer as explorer_module

    app = QApplication.instance() or QApplication([])

    class FakeObjViewerPanel(QWidget):
        def __init__(self, parent=None, config_manager=None):
            super().__init__(parent)
            self.obj_text = ''

        def load_obj(self, obj_text, label):
            self.obj_text = obj_text
            self.label = label

    monkeypatch.setitem(
        sys.modules,
        'fleasion.cache.obj_viewer',
        SimpleNamespace(ObjViewerPanel=FakeObjViewerPanel),
    )
    widget = explorer_module.DatabaseAssetExplorer()
    try:
        request_id = 'ab' * 16
        widget._current_request_id = request_id
        widget._on_mesh_ready(request_id, 'v 0 0 0\nf 1 1 1')
        assert isinstance(widget._mesh_panel, FakeObjViewerPanel)
        assert widget._mesh_panel.obj_text.startswith('v 0 0 0')
        assert widget.preview_stack.currentWidget() is widget.mesh_panel
    finally:
        widget.close()
        app.processEvents()


def test_rbxm_preview_selects_first_instance_with_real_properties() -> None:
    code = r"""
from PyQt6.QtWidgets import QApplication
from fleasion.cache.rbxm_preview import RbxmPreviewWidget

app = QApplication([])
xml = b'''<roblox version="4">
  <Item class="Folder" referent="RBX0">
    <Item class="KeyframeSequence" referent="RBX1">
      <Properties><string name="Name">Mood</string></Properties>
    </Item>
  </Item>
</roblox>'''
widget = RbxmPreviewWidget()
widget.load_bytes(xml, 'nested-animation')
selected = widget._item_to_instance[id(widget.tree.currentItem())]
assert selected.class_name == 'KeyframeSequence'
assert widget.properties_table.rowCount() >= 3
widget.close()
app.processEvents()
"""
    env = os.environ.copy()
    env['QT_QPA_PLATFORM'] = 'offscreen'
    subprocess.run([sys.executable, '-c', code], check=True, env=env)


def test_asset_database_stop_preview_hides_full_preview_pane() -> None:
    code = r"""
from types import SimpleNamespace
from PyQt6.QtWidgets import QApplication
from fleasion.gui.database_asset_explorer import DatabaseAssetExplorer

app = QApplication([])
widget = DatabaseAssetExplorer()
assert not widget.preview_panel.isHidden()
widget._stop_preview()
assert widget.preview_panel.isHidden()
item = SimpleNamespace(entry_id='a' * 32)
widget._load_preview(item)
assert not widget.preview_panel.isHidden()
widget.close()
app.processEvents()
"""
    env = os.environ.copy()
    env['QT_QPA_PLATFORM'] = 'offscreen'
    subprocess.run([sys.executable, '-c', code], check=True, env=env)


def test_asset_type_filter_popup_is_searchable_and_scrollable() -> None:
    code = r"""
from PyQt6.QtWidgets import QApplication
from fleasion.gui.database_asset_explorer import _CheckListPopup

app = QApplication([])
options = [(f'type-{index}', f'Type {index}') for index in range(35)]
options.extend([('png', 'PNG image'), ('anim', 'Animation')])
popup = _CheckListPopup(options, {key for key, _label in options})
assert popup.scroll_area.height() == 250
assert not popup.all_button.isHidden()
assert not popup.clear_button.isHidden()
popup.filter_edit.setText('png')
assert not popup.checkboxes['png'].isHidden()
assert popup.checkboxes['anim'].isHidden()
popup._clear()
assert not popup.active
popup._select_all()
assert popup.active == {key for key, _label in options}
popup.close()
app.processEvents()
"""
    env = os.environ.copy()
    env['QT_QPA_PLATFORM'] = 'offscreen'
    subprocess.run([sys.executable, '-c', code], check=True, env=env)
