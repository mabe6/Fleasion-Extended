from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from fleasion.database.hash_replacer_store import HashReplacerError, HashReplacerStore
from fleasion.database.legacy_replacer_conversion import convert_legacy_replacer_profile
from fleasion.database.roblox_cache_store import RobloxCacheEntry


class FakeCacheStore:
    def __init__(self, entries):
        self.entries = list(entries)
        self.scans = 0

    def scan(self):
        self.scans += 1
        return list(self.entries)


def _entry(
    cache_hash: str,
    asset_id: str = "",
    kind: str = "mesh",
    url: str = "https://assets.invalid/content",
):

    return RobloxCacheEntry(
        entry_id=cache_hash,
        file_size=100,
        database_size=100,
        modified_at=1.0,
        kind=kind,
        url=url,
        asset_id=asset_id,
        has_rbxh_header=True,
        storage_mode="database",
    )


def test_legacy_profile_conversion_maps_ids_to_hashes_and_converts_obj(
    tmp_path: Path, monkeypatch,
):
    obj = tmp_path / "replacement.obj"
    obj.write_text(
        "o triangle\n"
        "v 0 0 0\n"
        "v 1 0 0\n"
        "v 0 1 0\n"
        "f 1 2 3\n",
        encoding="utf-8",
    )
    from fleasion.cache.tools.solidmodel_converter import obj_to_mesh

    monkeypatch.setattr(obj_to_mesh, "CONVERTED_MESHES_DIR", tmp_path / "ConvertedMeshes")
    original_config = tmp_path / "fallen.json"
    original_bytes = json.dumps(
        {
            "replacement_rules": [
                {
                    "name": "wall",
                    "replace_ids": [12345, "12346"],
                    "mode": "local",
                    "enabled": True,
                    "local_path": str(obj),
                }
            ]
        },
        indent=2,
    ).encode("utf-8")
    original_config.write_bytes(original_bytes)

    cache = FakeCacheStore([
        _entry("a" * 32, "12345"),
        _entry("b" * 32, "12345"),
        _entry("c" * 32, "12346"),
    ])
    assets = HashReplacerStore(tmp_path / "HashReplacer")
    source = json.loads(original_config.read_text(encoding="utf-8"))

    report = convert_legacy_replacer_profile(
        "fallen",
        source["replacement_rules"],
        cache_store=cache,
        asset_store=assets,
    )

    profile = assets.load_config(report["profile_name"])
    assert report["rules_created"] == 3
    assert report["asset_ids_matched"] == 2
    assert {rule["target_hash"] for rule in profile["rules"]} == {
        "a" * 32,
        "b" * 32,
        "c" * 32,
    }
    source_hashes = {rule["source_hash"] for rule in profile["rules"]}
    assert len(source_hashes) == 1
    converted_bytes = assets.asset_path(next(iter(source_hashes))).read_bytes()
    assert converted_bytes.startswith(b"version 2.00\n")
    assert original_config.read_bytes() == original_bytes
    assert obj.read_text(encoding="utf-8").startswith("o triangle")


def test_legacy_conversion_uses_metadata_ids_when_url_has_no_asset_id(tmp_path: Path):
    target = "d" * 32
    cache = FakeCacheStore([_entry(target, "", "png")])
    source = tmp_path / "replacement.png"
    source.write_bytes(b"PNG replacement payload")
    store = HashReplacerStore(tmp_path / "HashReplacer")

    report = convert_legacy_replacer_profile(
        "images",
        [{
            "name": "image",
            "replace_ids": [777],
            "mode": "local",
            "local_path": str(source),
        }],
        cache_store=cache,
        asset_store=store,
        cached_metadata={target: {"asset_id": "777"}},
    )

    rule = store.load_config(report["profile_name"])["rules"][0]
    assert rule["target_hash"] == target
    assert report["asset_ids_matched"] == 1



def test_legacy_conversion_resolves_asset_id_via_cdn_hash(tmp_path: Path):
    cdn_hash = "a1" * 16
    target_hash = "b2" * 16
    url = f"https://fts.rbxcdn.com/sc3/{cdn_hash}?encoding=zstd&version=1"
    cache = FakeCacheStore([_entry(target_hash, "", "png", url=url)])
    source = tmp_path / "replacement.png"
    source.write_bytes(b"replacement image")
    assets = HashReplacerStore(tmp_path / "HashReplacer")
    resolved = []

    def resolver(asset_id: str) -> str:
        resolved.append(asset_id)
        return cdn_hash

    report = convert_legacy_replacer_profile(
        "images",
        [{
            "name": "image",
            "replace_ids": [777],
            "mode": "local",
            "local_path": str(source),
        }],
        cache_store=cache,
        asset_store=assets,
        cdn_hash_resolver=resolver,
    )

    rules = assets.load_config(report["profile_name"])["rules"]
    assert resolved == ["777"]
    assert rules[0]["target_hash"] == target_hash
    assert report["asset_ids_matched"] == 1
    assert report["asset_ids_resolved_by_cdn"] == 1



def test_legacy_conversion_reports_missing_ids_without_writing_partial_profile(tmp_path: Path):
    store = HashReplacerStore(tmp_path / "HashReplacer")
    source = tmp_path / "replacement.png"
    source.write_bytes(b"image")
    with pytest.raises(HashReplacerError, match="No rules could be converted"):
        convert_legacy_replacer_profile(
            "missing",
            [{
                "name": "not cached",
                "replace_ids": [99999],
                "mode": "local",
                "local_path": str(source),
            }],
            cache_store=FakeCacheStore([]),
            asset_store=store,
        )
    assert "missing" not in store.list_configs()


def test_legacy_conversion_skips_proxy_only_modes_and_preserves_existing_hash_profiles(
    tmp_path: Path,
):
    store = HashReplacerStore(tmp_path / "HashReplacer")
    source = tmp_path / "replacement.png"
    source.write_bytes(b"image")
    store.save_config("Default", {"rules": []})

    with pytest.raises(HashReplacerError):
        convert_legacy_replacer_profile(
            "unsupported",
            [{
                "name": "cdn",
                "replace_ids": [12],
                "mode": "cdn",
                "cdn_url": "https://example.invalid",
            }],
            cache_store=FakeCacheStore([_entry("e" * 32, "12", "png")]),
            asset_store=store,
        )
    assert store.load_config("Default")["rules"] == []


def test_obj_conversion_deduplicates_the_same_local_source_across_rules(
    tmp_path: Path, monkeypatch,
):
    from fleasion.cache.tools.solidmodel_converter import obj_to_mesh

    monkeypatch.setattr(obj_to_mesh, "CONVERTED_MESHES_DIR", tmp_path / "ConvertedMeshes")
    obj = tmp_path / "shared.obj"
    obj.write_text(
        "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
        encoding="utf-8",
    )
    cache = FakeCacheStore([
        _entry("1" * 32, "100"),
        _entry("2" * 32, "200"),
    ])
    store = HashReplacerStore(tmp_path / "HashReplacer")
    report = convert_legacy_replacer_profile(
        "duplicate",
        [
            {"name": "a", "replace_ids": [100], "mode": "local", "local_path": str(obj)},
            {"name": "b", "replace_ids": [200], "mode": "local", "local_path": str(obj)},
        ],
        cache_store=cache,
        asset_store=store,
    )
    rules = store.load_config(report["profile_name"])["rules"]
    assert len(rules) == 2
    assert len({rule["source_hash"] for rule in rules}) == 1
    assert len(store.list_assets()) == 1
