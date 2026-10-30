"""Integration tests for file-backed Roblox cache replacements."""

import sqlite3
import struct
from pathlib import Path

import pytest

from fleasion.database.roblox_cache_store import (
    RobloxCacheError,
    RobloxCacheStore,
    decompress_roblox_payload,
    decompress_roblox_payload_file,
    detect_roblox_payload_kind,
    split_rbxh_entry,
)
from fleasion.database import roblox_cache_store as store_module
from fleasion.database import sqlite_browser as sqlite_browser_module


def _make_entry(url: str, payload: bytes) -> bytes:
    encoded_url = url.encode("utf-8")
    header = bytearray(b"RBXH" + b"\x00" * 4 + struct.pack("<I", len(encoded_url)))
    header.extend(encoded_url)
    header.extend(b"\x00" * 25)
    struct.pack_into("<I", header, len(header) - 25, len(payload))
    return bytes(header) + payload


def _make_store(tmp_path: Path, monkeypatch, entries: dict[str, bytes]) -> RobloxCacheStore:
    root = tmp_path / "Roblox"
    entry_dir = root / "rbx-storage"
    entry_dir.mkdir(parents=True)
    db_path = root / "rbx-storage.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE files (id BLOB PRIMARY KEY, size INTEGER NOT NULL)")
        for entry_id, data in entries.items():
            path = entry_dir / entry_id[:2] / entry_id
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            connection.execute(
                "INSERT INTO files (id, size) VALUES (?, ?)",
                (bytes.fromhex(entry_id), len(data)),
            )

    config_dir = tmp_path / "app-config"
    monkeypatch.setattr(store_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(sqlite_browser_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(store_module, "_is_roblox_process_running", lambda: False)
    return RobloxCacheStore(root)


def _make_blob_store(tmp_path: Path, monkeypatch, entries: dict[str, bytes]) -> RobloxCacheStore:
    root = tmp_path / "RobloxBlobCache"
    root.mkdir(parents=True)
    db_path = root / "rbx-storage.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE files (id BLOB PRIMARY KEY NOT NULL, content BLOB, "
            "size INTEGER DEFAULT 0 NOT NULL, hits INTEGER DEFAULT 0 NOT NULL, "
            "atime INTEGER DEFAULT 0 NOT NULL, category INTEGER DEFAULT 0 NOT NULL, "
            "score INTEGER DEFAULT 0 NOT NULL, ttl INTEGER DEFAULT 0 NOT NULL)"
        )
        for entry_id, data in entries.items():
            connection.execute(
                "INSERT INTO files (id, content, size, atime) VALUES (?, ?, ?, ?)",
                (bytes.fromhex(entry_id), data, len(data), 1_792_965_842_000),
            )

    config_dir = tmp_path / "blob-app-config"
    monkeypatch.setattr(store_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(sqlite_browser_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(store_module, "_is_roblox_process_running", lambda: False)
    # Intentionally do not create an rbx-storage/ directory: these entries live in SQLite BLOBs.
    return RobloxCacheStore(root)


def test_blob_cache_scans_database_content_without_external_folder(tmp_path, monkeypatch):
    target_id = "a" * 32
    source_id = "b" * 32
    entries = {
        target_id: _make_entry(
            "https://assetdelivery.roblox.com/v1/asset?id=12345",
            b"target-payload-" + b"T" * 340,
        ),
        source_id: _make_entry(
            "https://assetdelivery.roblox.com/v1/asset?id=98765",
            b"source-payload-" + b"S" * 410,
        ),
    }
    store = _make_blob_store(tmp_path, monkeypatch, entries)

    found = {item.entry_id: item for item in store.scan()}

    assert set(found) == {target_id, source_id}
    assert found[target_id].storage_mode == "database"
    assert found[target_id].has_rbxh_header is True
    assert found[target_id].asset_id == "12345"
    assert found[target_id].file_size == found[target_id].database_size == len(entries[target_id])
    assert found[source_id].storage_mode == "database"


def test_blob_cache_swap_and_restore_update_content_and_size_atomically(tmp_path, monkeypatch):
    target_id = "a" * 32
    source_id = "b" * 32
    target_data = _make_entry("https://example.test/?id=111", b"T" * 350)
    source_data = _make_entry("https://example.test/?id=222", b"S" * 420)
    store = _make_blob_store(tmp_path, monkeypatch, {
        target_id: target_data,
        source_id: source_data,
    })

    message = store.swap(target_id, source_id, origin="blob fixture")

    assert "database" in message
    assert store._get_entry_data(target_id)[0] == source_data
    assert store.database_size(target_id) == len(source_data)
    assert (store.originals_dir / target_id).read_bytes() == target_data
    assert store.swap_status(target_id) == "swapped"
    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT content, size FROM files WHERE id = ?", (bytes.fromhex(target_id),)
        ).fetchone()
        assert row == (source_data, len(source_data))

    message = store.restore(target_id)

    assert "Restored" in message
    assert store._get_entry_data(target_id)[0] == target_data
    assert store.database_size(target_id) == len(target_data)
    assert store.swap_status(target_id) == ""
    assert target_id not in store._manifest()


def test_blob_cache_import_wraps_payload_and_updates_db_content(tmp_path, monkeypatch):
    target_id = "a" * 32
    entries = {
        target_id: _make_entry("https://example.test/?id=111", b"T" * 350),
        "b" * 32: _make_entry("https://example.test/?id=222", b"B" * 360),
        "c" * 32: _make_entry("https://example.test/?id=333", b"C" * 370),
    }
    store = _make_blob_store(tmp_path, monkeypatch, entries)
    payload_path = tmp_path / "custom-animation.rbxm"
    payload = b"<roblox!" + b"custom CurveAnimation payload" * 12
    payload_path.write_bytes(payload)

    message = store.import_file(
        target_id, payload_path, origin="blob import fixture", allow_suspicious_animation=True
    )
    wrapped = store._get_entry_data(target_id)[0]
    url, recovered_payload, has_header = split_rbxh_entry(wrapped)

    assert "database" in message
    assert "Read-back verification: OK" in message
    assert has_header is True
    assert url == "https://example.test/?id=111"
    assert recovered_payload == payload
    assert store.database_size(target_id) == len(wrapped)
    assert store.swap_status(target_id) == "swapped"


def test_asset_type_metadata_payload_is_not_classified_as_mesh() -> None:
    assert store_module.detect_roblox_payload_kind(b'roblox-assettypeid: 1\r\n') == 'asset-metadata'


def test_scan_joins_physical_entries_with_sqlite_metadata(tmp_path, monkeypatch):
    target_id = "a" * 32
    source_id = "b" * 32
    entries = {
        target_id: _make_entry(
            "https://assetdelivery.roblox.com/v1/asset?id=12345",
            b"\x89PNG" + b"T" * 320,
        ),
        source_id: _make_entry(
            "https://assetdelivery.roblox.com/v1/asset?id=98765",
            b"\x00\x01\x00\x00" + b"F" * 320,
        ),
    }
    store = _make_store(tmp_path, monkeypatch, entries)

    found = {item.entry_id: item for item in store.scan()}

    assert found[target_id].has_rbxh_header is True
    assert found[target_id].kind == "png"
    assert found[target_id].asset_id == "12345"
    assert found[target_id].database_size == len(entries[target_id])
    assert found[source_id].kind == "font"
    assert found[source_id].database_size == len(entries[source_id])


def test_swap_and_restore_keep_entry_file_and_sql_size_in_sync(tmp_path, monkeypatch):
    target_id = "a" * 32
    source_id = "b" * 32
    original_target = _make_entry("https://example.test/?id=111", b"T" * 350)
    source = _make_entry("https://example.test/?id=222", b"S" * 420)
    store = _make_store(tmp_path, monkeypatch, {
        target_id: original_target,
        source_id: source,
    })

    result = store.swap(target_id, source_id, origin="test fixture")

    assert "Swapped" in result
    assert store._entry_path(target_id).read_bytes() == source
    assert store.database_size(target_id) == len(source)
    assert (store.originals_dir / target_id).read_bytes() == original_target
    assert store.swap_status(target_id) == "swapped"
    assert list((tmp_path / "app-config" / "FleasionNT" / "DatabaseBackups").glob("*.db"))

    restored = store.restore(target_id)

    assert "Restored" in restored
    assert store._entry_path(target_id).read_bytes() == original_target
    assert store.database_size(target_id) == len(original_target)
    assert store.swap_status(target_id) == ""
    assert target_id not in store._manifest()
    assert store.history()[-1]["action"] == "restore"


def test_external_file_import_requires_verified_header_layout_and_syncs_size(tmp_path, monkeypatch):
    target_id = "a" * 32
    entries = {
        target_id: _make_entry("https://example.test/?id=111", b"T" * 350),
        "b" * 32: _make_entry("https://example.test/?id=222", b"B" * 360),
        "c" * 32: _make_entry("https://example.test/?id=333", b"C" * 370),
    }
    store = _make_store(tmp_path, monkeypatch, entries)
    import_file = tmp_path / "custom-animation.rbxm"
    payload = b"<roblox!" + b"custom CurveAnimation payload" * 12
    import_file.write_bytes(payload)

    result = store.import_file(
        target_id, import_file, origin="test animation", allow_suspicious_animation=True
    )
    current = store._entry_path(target_id).read_bytes()
    url, current_payload, has_header = split_rbxh_entry(current)

    assert "Swapped" in result
    assert has_header is True
    assert url == "https://example.test/?id=111"
    assert current_payload == payload
    assert store.database_size(target_id) == len(current)
    assert store.swap_status(target_id) == "swapped"


def test_import_refuses_to_guess_header_layout_from_too_few_entries(tmp_path, monkeypatch):
    target_id = "a" * 32
    store = _make_store(tmp_path, monkeypatch, {
        target_id: _make_entry("https://example.test/?id=111", b"T" * 350),
        "b" * 32: _make_entry("https://example.test/?id=222", b"B" * 360),
    })
    import_file = tmp_path / "custom.rbxm"
    import_file.write_bytes(b"<roblox!" + b"X" * 400)

    with pytest.raises(RobloxCacheError, match="Could not verify the RBXH payload-length fields"):
        store.import_file(target_id, import_file, allow_suspicious_animation=True)

    assert store._entry_path(target_id).read_bytes().endswith(b"T" * 350)


def test_import_refuses_unparseable_animation_without_explicit_override(tmp_path, monkeypatch):
    target_id = "a" * 32
    target_original = _make_entry("https://example.test/?id=111", b"T" * 350)
    store = _make_store(tmp_path, monkeypatch, {
        target_id: target_original,
        "b" * 32: _make_entry("https://example.test/?id=222", b"B" * 360),
        "c" * 32: _make_entry("https://example.test/?id=333", b"C" * 370),
    })
    import_file = tmp_path / "malformed-animation.rbxm"
    import_file.write_bytes(b"<roblox!" + b"CurveAnimation" + b"truncated")

    # Strict mode is still available to callers that ask for it
    with pytest.raises(RobloxCacheError, match="Animation preflight found suspicious data"):
        store.import_file(target_id, import_file, allow_suspicious_animation=False)

    assert store._entry_path(target_id).read_bytes() == target_original
    assert target_id not in store._manifest()


def test_import_defaults_to_warning_and_still_writes_flagged_animation(tmp_path, monkeypatch):
    target_id = "a" * 32
    target_original = _make_entry("https://example.test/?id=111", b"T" * 350)
    store = _make_store(tmp_path, monkeypatch, {
        target_id: target_original,
        "b" * 32: _make_entry("https://example.test/?id=222", b"B" * 360),
        "c" * 32: _make_entry("https://example.test/?id=333", b"C" * 370),
    })
    import_file = tmp_path / "malformed-animation.rbxm"
    payload = b"<roblox!" + b"CurveAnimation" + b"truncated"
    import_file.write_bytes(payload)

    message = store.import_file(target_id, import_file)

    assert "Preflight warnings (imported anyway)" in message
    assert target_id in store._manifest()
    assert store._entry_path(target_id).read_bytes().endswith(payload)


def test_import_refuses_curve_animation_into_legacy_sequence_without_override(
    tmp_path, monkeypatch
):
    target_id = "a" * 32
    target_original = _make_entry(
        "https://example.test/?id=111",
        b"<roblox!" + b"KeyframeSequence" + b"T" * 350,
    )
    store = _make_store(tmp_path, monkeypatch, {
        target_id: target_original,
        "b" * 32: _make_entry("https://example.test/?id=222", b"B" * 360),
        "c" * 32: _make_entry("https://example.test/?id=333", b"C" * 370),
    })
    import_file = tmp_path / "curve-animation.rbxm"
    import_file.write_bytes(b"<roblox!" + b"CurveAnimation" + b"not-a-complete-file")

    with pytest.raises(RobloxCacheError, match="Import type mismatch"):
        store.import_file(
            target_id,
            import_file,
            allow_suspicious_animation=True,
        )

    assert store._entry_path(target_id).read_bytes() == target_original
    assert target_id not in store._manifest()


def test_swap_is_blocked_while_roblox_process_is_detected(tmp_path, monkeypatch):
    target_id = "a" * 32
    source_id = "b" * 32
    store = _make_store(tmp_path, monkeypatch, {
        target_id: _make_entry("https://example.test/?id=111", b"T" * 350),
        source_id: _make_entry("https://example.test/?id=222", b"S" * 360),
    })
    monkeypatch.setattr(store_module, "_is_roblox_process_running", lambda: True)

    with pytest.raises(RobloxCacheError, match="Close it completely"):
        store.swap(target_id, source_id)

    assert store.database_size(target_id) == len(store._entry_path(target_id).read_bytes())
    assert not (tmp_path / "app-config" / "FleasionNT" / "DatabaseBackups").exists()


def test_animation_import_writes_converted_payload_and_reports_conversion(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    target_id = 'a' * 32
    target_payload = (
        b'<roblox version="4"><Item class="KeyframeSequence"><Properties><string name="Name">'
        + b'T' * 300
        + b'</string></Properties></Item></roblox>'
    )
    converted_payload = (
        b'<roblox version="4"><Item class="KeyframeSequence">'
        b'<string name="Converted">true</string></Item></roblox>'
    )
    entries = {
        target_id: _make_entry('https://example.test/?id=111', target_payload),
        'b' * 32: _make_entry('https://example.test/?id=222', b'B' * 350),
        'c' * 32: _make_entry('https://example.test/?id=333', b'C' * 360),
    }
    store = _make_store(tmp_path, monkeypatch, entries)
    import_path = tmp_path / 'curve-animation.rbxmx'
    import_path.write_bytes(b'<roblox version="4"><Item class="CurveAnimation"/></roblox>')

    monkeypatch.setattr(
        store_module,
        'convert_animation_for_slot',
        lambda _payload, _target: SimpleNamespace(
            payload=converted_payload,
            notes=('Converted CurveAnimation to KeyframeSequence to match the cache slot.',),
            changed=True,
        ),
    )

    message = store.import_file(target_id, import_path, allow_type_mismatch=True)

    _url, stored_payload, has_header = split_rbxh_entry(store._entry_path(target_id).read_bytes())
    assert has_header is True
    assert stored_payload == converted_payload
    assert 'Animation conversion: Converted CurveAnimation to KeyframeSequence' in message
    assert 'Read-back verification: OK' in message


def test_animation_import_falls_back_to_original_when_conversion_fails(
    tmp_path, monkeypatch
):
    from fleasion.database.animation_convert import AnimationConversionError

    target_id = 'a' * 32
    target_payload = (
        b'<roblox version="4"><Item class="KeyframeSequence"><Properties><string name="Name">'
        + b'T' * 300
        + b'</string></Properties></Item></roblox>'
    )
    original = _make_entry('https://example.test/?id=111', target_payload)
    source_payload = b'<roblox version="4"><Item class="CurveAnimation"/></roblox>'
    entries = {
        target_id: original,
        'b' * 32: _make_entry('https://example.test/?id=222', b'B' * 350),
        'c' * 32: _make_entry('https://example.test/?id=333', b'C' * 360),
    }
    store = _make_store(tmp_path, monkeypatch, entries)
    import_path = tmp_path / 'curve-animation.rbxmx'
    import_path.write_bytes(source_payload)

    def fail_conversion(_payload: bytes, _target_payload: bytes):
        raise AnimationConversionError('test conversion failure')

    monkeypatch.setattr(store_module, 'convert_animation_for_slot', fail_conversion)

    message = store.import_file(target_id, import_path, allow_type_mismatch=True)

    _url, stored_payload, has_header = split_rbxh_entry(store._entry_path(target_id).read_bytes())
    assert has_header is True
    assert stored_payload == source_payload
    assert 'Animation conversion warning (original payload imported unchanged)' in message
    assert 'test conversion failure' in message


def test_payload_kind_detects_ktx_with_a_short_prefix() -> None:
    ktx1 = b'\xabKTX 11\xbb\r\n\x1a\n' + bytes(64)
    ktx2 = b'\xabKTX 20\xbb\r\n\x1a\n' + bytes(64)

    assert detect_roblox_payload_kind(ktx1) == 'ktx'
    assert detect_roblox_payload_kind(b'RBX-prefix:' + ktx1) == 'ktx'
    assert detect_roblox_payload_kind(b'header:' + ktx2) == 'ktx'
    assert detect_roblox_payload_kind(b'ordinary binary data') == 'other'


def test_scan_detects_zstd_encoded_fts_payload_without_changing_cache_bytes(tmp_path, monkeypatch):
    import zstandard

    target_id = "c" * 32
    inner_payload = b"\x89PNG\r\n\x1a\n" + b"fixture-data-" * 24
    compressed_payload = zstandard.ZstdCompressor().compress(inner_payload)
    url = (
        "https://fts.rbxcdn.com/v2/sc1/50f96a5fdc9efbfb12c8eaf2f1de071c"
        "?encoding=zstd&version=1"
    )
    entry_data = _make_entry(url, compressed_payload)
    store = _make_store(tmp_path, monkeypatch, {target_id: entry_data})

    def fail_if_scan_decompresses(*_args, **_kwargs):
        raise AssertionError("cache scanning must not decompress payloads")

    monkeypatch.setattr(
        store_module, "decompress_roblox_payload_prefix", fail_if_scan_decompresses
    )
    found = {item.entry_id: item for item in store.scan()}[target_id]

    assert found.kind == "compressed"
    assert found.asset_id == ""
    assert decompress_roblox_payload(compressed_payload) == inner_payload
    assert store._get_entry_data(target_id)[0] == entry_data


def test_zstd_preview_decompression_has_no_default_output_limit():
    import zstandard

    expected_size = 51 * 1024 * 1024
    payload = zstandard.ZstdCompressor().compress(b"A" * expected_size)

    assert len(decompress_roblox_payload(payload)) == expected_size
    with pytest.raises(RobloxCacheError, match="64-byte limit"):
        decompress_roblox_payload(payload, max_output_bytes=64)


def test_copy_entry_payload_to_file_streams_database_blob_and_strips_rbxh_header(
    tmp_path, monkeypatch
):
    target_id = "d" * 32
    payload = b"large-cache-payload-" * 5000
    entry_data = _make_entry("https://example.test/?id=12345", payload)
    store = _make_blob_store(tmp_path, monkeypatch, {target_id: entry_data})
    destination = tmp_path / "streamed-payload.bin"

    url, written = store.copy_entry_payload_to_file(
        target_id, destination, chunk_size=257
    )

    assert url == "https://example.test/?id=12345"
    assert written == len(payload)
    assert destination.read_bytes() == payload


def test_copy_entry_payload_to_file_streams_legacy_file_and_strips_header(
    tmp_path, monkeypatch
):
    target_id = "e" * 32
    payload = b"legacy-cache-payload-" * 3000
    entry_data = _make_entry("https://example.test/?id=54321", payload)
    store = _make_store(tmp_path, monkeypatch, {target_id: entry_data})
    destination = tmp_path / "streamed-legacy-payload.bin"

    url, written = store.copy_entry_payload_to_file(
        target_id, destination, chunk_size=193
    )

    assert url == "https://example.test/?id=54321"
    assert written == len(payload)
    assert destination.read_bytes() == payload


def test_streaming_file_decompression_handles_zstd_payload_over_50_mib(tmp_path):
    import zstandard

    expected = b"Fleasion streaming preview " * (2 * 1024 * 1024)
    source = tmp_path / "compressed.zst"
    destination = tmp_path / "decoded.bin"
    source.write_bytes(zstandard.ZstdCompressor().compress(expected))

    decoded_size = decompress_roblox_payload_file(
        source, destination, chunk_size=4096
    )

    assert decoded_size == len(expected)
    assert destination.stat().st_size == len(expected)
    assert destination.read_bytes() == expected



def test_dcz_cache_resource_and_embedded_http_headers_are_recognised() -> None:
    url = (
        'https://clientsettingscdn.roblox.com/v2/settings-compressed/'
        'application/PCDesktopClient/abcdef0123456789.dcz'
    )
    payload = (
        b'access-control-allow-methods: GET,OPTIONS\r\n'
        b'access-control-allow-origin: *\r\n'
        b'access-control-expose-headers: Rbx-Cdn-Provider,Akamai-Request-BC\r\n'
        b'cache-control: public, max-age=30\r\n'
        b'content-encoding: dcz\r\n'
        b'content-type: application/octet-stream\r\n'
        b'x-rbx-zstd-dict-sha: ' + b'a' * 64 + b'\r\n'
        b'\r\n' + bytes(range(32))
    )
    assert detect_roblox_payload_kind(payload, url=url) == 'roblox-settings'

    parsed = store_module.parse_cached_http_response(payload)
    assert parsed is not None
    headers, body = parsed
    assert headers['content-encoding'] == 'dcz'
    assert headers['x-rbx-zstd-dict-sha'] == 'a' * 64
    assert body == bytes(range(32))

    preview = store_module.format_cached_http_response_preview(payload, url)
    assert 'Content encoding: dcz' in preview
    assert 'Zstandard dictionary SHA-256: ' + 'a' * 64 in preview


def test_dcz_without_embedded_headers_is_described_without_hex_dump() -> None:
    url = 'https://clientsettingscdn.roblox.com/v2/settings-compressed/application/abc.dcz'
    preview = store_module.format_cached_http_response_preview(
        b'\x28\xb5\x2f\xfd' + bytes(24), url
    )
    assert 'Roblox client settings resource (DCZ)' in preview
    assert 'without the matching dictionary' in preview


def test_hls_playlist_and_video_metadata_are_not_asset_ids() -> None:
    playlist_url = 'https://sc5.rbxcdn.com/c/hash/hash/main.m3u8?__token__=signed'
    playlist = b'#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:4.0,\nsegment0.ts\n'
    assert detect_roblox_payload_kind(playlist, url=playlist_url) == 'video-manifest'
    video_marker = b'roblox-assettypeid: 86\r\n'
    assert detect_roblox_payload_kind(video_marker, url=playlist_url) == 'video-metadata'


def test_wrapped_zstd_prefix_is_decompressed_and_detected_as_csgphs(tmp_path):
    import zstandard

    csg_header = (
        b'CSGPHS'
        + struct.pack('<I', 8)
        + b'\\x00\\x00'
        + struct.pack(
            '<9I6f',
            1, 10, 5, 10, 5, 0, 0, 0, 120,
            -1.0, -2.0, -3.0, 1.0, 2.0, 3.0,
        )
    )
    csg_payload = csg_header + bytes(120)
    compressed = zstandard.ZstdCompressor().compress(csg_payload)
    wrapped = b'\\x7b\\x29\\xbe\\x18\\x00\\x00\\xb4\\x18\\x00\\x00' + compressed

    assert store_module.is_roblox_compressed_payload(wrapped)
    assert decompress_roblox_payload(wrapped) == csg_payload
    assert detect_roblox_payload_kind(wrapped) == 'csgphs'
    preview = store_module.format_csgphs_preview(csg_payload)
    assert 'CSGPHS version 8' in preview
    assert 'Convex hulls: 1' in preview
    assert 'Vertices: 10' in preview

    source = tmp_path / 'wrapped.zst'
    destination = tmp_path / 'decoded.bin'
    source.write_bytes(wrapped)
    decompress_roblox_payload_file(source, destination)
    assert destination.read_bytes() == csg_payload


def test_roblox_asset_type_header_exposes_image_body_for_preview():
    image = b'\\x89PNG\\r\\n\\x1a\\n' + bytes(64)
    response = (
        b'roblox-assettypeid: 1\\r\\n'
        b'content-type: image/png\\r\\n'
        b'\\r\\n'
        + image
    )

    assert detect_roblox_payload_kind(response) == 'png'
    extracted = store_module.extract_cached_http_response_asset_payload(response)
    assert extracted is not None
    body, kind, headers = extracted
    assert body == image
    assert kind == 'png'
    assert headers['roblox-assettypeid'] == '1'
    preview = store_module.format_cached_http_response_preview(response)
    assert 'Roblox asset type: Image (1)' in preview


def test_roblox_asset_type_header_does_not_require_two_known_headers():
    response = b'roblox-assettypeid: 78\\r\\n\\r\\n'
    parsed = store_module.parse_cached_http_response(response)
    assert parsed is not None
    assert parsed[0]['roblox-assettypeid'] == '78'
    assert detect_roblox_payload_kind(response) == 'asset-metadata'
