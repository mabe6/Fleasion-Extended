"""Offline tests for the external file/URL source library."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

TEMP_HOME = Path(tempfile.mkdtemp(prefix="cacheswap-external-test-"))
os.environ["CACHESWAP_HOME"] = str(TEMP_HOME)

from external_sources import (  # noqa: E402
    ExternalSourceError, SourceLibrary, normalize_source_url,
)


def test_asset_id_normalization():
    expected = "https://assetdelivery.roblox.com/v1/asset/?id=73885081417917"
    assert normalize_source_url("73885081417917") == expected
    assert normalize_source_url("rbxassetid://73885081417917") == expected
    assert normalize_source_url("https://www.roblox.com/library/73885081417917/Example") == expected
    assert normalize_source_url("https://create.roblox.com/store/asset/73885081417917/Example") == expected


def test_direct_https_url_kept():
    url = "https://cdn.example.test/assets/wave.rbxm?version=2"
    assert normalize_source_url(url) == url


def test_reject_unsafe_or_non_direct_urls():
    for value in (
        "http://example.com/file.rbxm",
        "file:///C:/temp/a.rbxm",
        "https://user:password@example.com/file.rbxm",
        "https://localhost/file.rbxm",
        "https://127.0.0.1/file.rbxm",
        "https://www.roblox.com/games/123/example",
    ):
        try:
            normalize_source_url(value)
        except ExternalSourceError:
            pass
        else:
            raise AssertionError(f"Expected this URL/input to be rejected: {value}")


def test_file_library_persistence_and_remove():
    root = Path(tempfile.mkdtemp())
    source = root / "example.rbxm"
    data = b"<roblox!\x89\xff\r\n\x1a\n" + bytes(range(64))
    source.write_bytes(data)
    library = SourceLibrary()
    item = library.add_file(source)
    assert item.path.read_bytes() == data
    assert item.kind == "rbxm"
    assert item.size == len(data)
    assert library.get(item.id) == item
    reloaded = SourceLibrary()
    assert reloaded.get(item.id) is not None
    assert reloaded.get(item.id).path.read_bytes() == data
    assert reloaded.remove(item.id)
    assert reloaded.get(item.id) is None
    assert not item.path.exists()


def test_identical_file_deduplicates():
    root = Path(tempfile.mkdtemp())
    a = root / "a.rbxm"
    b = root / "b.rbxm"
    data = b"<roblox!\x89\xff\r\n\x1a\n" + b"same payload"
    a.write_bytes(data)
    b.write_bytes(data)
    library = SourceLibrary()
    ia = library.add_file(a)
    ib = library.add_file(b)
    assert ia.id == ib.id
    assert len(library.list()) == 1
    assert library.get(ia.id).name == "b.rbxm"


def run():
    tests = [test_asset_id_normalization, test_direct_https_url_kept,
             test_reject_unsafe_or_non_direct_urls, test_file_library_persistence_and_remove,
             test_identical_file_deduplicates]
    for test in tests:
        test()
        print("PASS", test.__name__)
    print(f"All {len(tests)} external-source tests passed.")


if __name__ == "__main__":
    run()
