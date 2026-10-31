"""Tests for isolated cache-hash replacement profiles and asset imports."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fleasion.database.hash_replacer_store import HashReplacerError, HashReplacerStore


def test_hash_profiles_are_separate_and_map_cache_hash_to_asset_hash(tmp_path: Path):
    store = HashReplacerStore(tmp_path / "HashReplacer")
    source = tmp_path / "body.rbxm"
    payload = b"<roblox version=\"4\">model-content</roblox>"
    source.write_bytes(payload)

    imported = store.import_paths([source])
    source_hash = hashlib.sha256(payload).hexdigest()
    assert imported[0]["sha256"] == source_hash
    assert store.asset_path(source_hash).read_bytes() == payload

    assert store.create_config("Test Profile") is True
    store.add_rule("Test Profile", "a" * 32, source_hash)
    config = store.load_config("Test Profile")
    assert config["rules"] == [{
        "target_hash": "a" * 32,
        "source_hash": source_hash,
        "enabled": True,
    }]
    assert "Test Profile" in store.list_configs()
    assert store.remove_rule("Test Profile", "a" * 32) is True
    assert store.load_config("Test Profile")["rules"] == []


def test_folder_import_is_recursive_and_deduplicates_identical_content(tmp_path: Path):
    root = tmp_path / "source"
    (root / "nested").mkdir(parents=True)
    first = root / "first.png"
    second = root / "nested" / "copy.png"
    first.write_bytes(b"same image payload")
    second.write_bytes(b"same image payload")

    store = HashReplacerStore(tmp_path / "HashReplacer")
    imported = store.import_paths([root])

    assert len(imported) == 2
    assert imported[0]["sha256"] == imported[1]["sha256"]
    assert len(store.list_assets()) == 1
    assert len(store.list_assets()[0]["source_names"]) == 2


def test_hash_store_rejects_invalid_cache_and_source_hashes(tmp_path: Path):
    store = HashReplacerStore(tmp_path / "HashReplacer")
    payload = tmp_path / "asset.bin"
    payload.write_bytes(b"payload")
    imported = store.import_paths([payload])
    source_hash = imported[0]["sha256"]

    with pytest.raises(HashReplacerError, match="32 hexadecimal"):
        store.add_rule("Default", "not-a-cache-hash", source_hash)
    with pytest.raises(HashReplacerError, match="64 hexadecimal"):
        store.add_rule("Default", "b" * 32, "not-a-sha256")


def test_import_asset_id_uses_asset_delivery_and_stores_returned_file(tmp_path, monkeypatch):
    payload = b"asset fetched from Roblox Asset Delivery"
    seen = {}

    def fake_cookie():
        return "cookie-token"

    def fake_download(asset_id, destination, *, session=None):
        seen["asset_id"] = asset_id
        seen["cookie"] = session.cookies.get(".ROBLOSECURITY", domain=".roblox.com")
        Path(destination).write_bytes(payload)

    monkeypatch.setattr("fleasion.utils.roblox_auth.get_roblosecurity", fake_cookie)
    monkeypatch.setattr("fleasion.utils.asset_lookup.download_asset", fake_download)

    store = HashReplacerStore(tmp_path / "HashReplacer")
    imported = store.import_asset_id("1234567")

    assert seen == {"asset_id": "1234567", "cookie": "cookie-token"}
    assert store.asset_path(imported["sha256"]).read_bytes() == payload
    assert imported["asset_id"] == "1234567"


def test_import_asset_id_rejects_non_numeric_source(tmp_path: Path):
    store = HashReplacerStore(tmp_path / "HashReplacer")
    with pytest.raises(HashReplacerError, match="numeric Roblox Asset ID"):
        store.import_asset_id("https://www.roblox.com/library/123")



def test_import_obj_converts_to_roblox_mesh_and_keeps_source_name(tmp_path: Path, monkeypatch):
    source = tmp_path / "fallen_wall.obj"
    source.write_text(
        "o triangle\n"
        "v 0 0 0\n"
        "v 1 0 0\n"
        "v 0 1 0\n"
        "f 1 2 3\n",
        encoding="utf-8",
    )
    from fleasion.cache.tools.solidmodel_converter import obj_to_mesh

    monkeypatch.setattr(obj_to_mesh, "CONVERTED_MESHES_DIR", tmp_path / "ConvertedMeshes")
    store = HashReplacerStore(tmp_path / "HashReplacer")

    imported = store.import_paths([source])[0]
    stored = store.asset_path(imported["sha256"])

    assert imported["kind"] == "mesh"
    assert imported["name"] == "fallen_wall.obj"
    assert imported["converted_from"] == "obj"
    assert stored.suffix == ".mesh"
    assert stored.read_bytes().startswith(b"version 2.00\n")
    assert source.read_text(encoding="utf-8").startswith("o triangle")
