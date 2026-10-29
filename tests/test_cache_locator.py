"""Tests for finding rbx-storage.db from a user-picked folder (Linux layouts included)."""

import sqlite3
from pathlib import Path

import pytest

from fleasion.database import cache_locator
from fleasion.database import roblox_cache_store as store_module
from fleasion.database import sqlite_browser as sqlite_browser_module
from fleasion.database.cache_locator import resolve_cache_location
from fleasion.database.roblox_cache_store import RobloxCacheError, RobloxCacheStore


def _make_db(path: Path, entry_id: str = 'ab' * 16) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE files (id BLOB PRIMARY KEY, size INTEGER NOT NULL)')
        connection.execute('INSERT INTO files (id, size) VALUES (?, 3)', (bytes.fromhex(entry_id),))


def test_accepts_database_file_folder_assets_folder_and_parent(tmp_path):
    db = tmp_path / 'Roblox' / 'rbx-storage.db'
    _make_db(db)
    (tmp_path / 'Roblox' / 'rbx-storage').mkdir()
    for picked in (db, db.parent, db.parent / 'rbx-storage', tmp_path):
        location = resolve_cache_location(picked)
        assert location.database_path == db.resolve()
        assert location.assets_path == (db.parent / 'rbx-storage').resolve()


def test_finds_database_inside_hidden_wine_prefix_and_ignores_name_case(tmp_path):
    db = tmp_path / '.wine' / 'drive_c' / 'users' / 'me' / 'AppData' / 'Local' / 'Roblox' / 'RBX-Storage.DB'
    _make_db(db)
    assert resolve_cache_location(tmp_path / '.wine').database_path == db.resolve()


def test_sober_layout_uses_cache_dir_for_external_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    base = tmp_path / '.var' / 'app' / 'org.vinegarhq.Sober'
    db = base / 'data' / 'sober' / 'appData' / 'rbx-storage.db'
    _make_db(db)
    assets = base / 'cache' / 'sober' / 'rbx-storage'
    assets.mkdir(parents=True)
    location = resolve_cache_location(base / 'data' / 'sober')
    assert location.database_path == db.resolve()
    assert location.assets_path == assets.resolve()


def test_missing_database_error_lists_what_was_searched(tmp_path):
    (tmp_path / 'empty').mkdir()
    with pytest.raises(FileNotFoundError, match='rbx-storage.db'):
        resolve_cache_location(tmp_path / 'empty')


def test_search_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_locator, 'MAX_SEARCH_DIRS', 3)
    deep = tmp_path / 'a' / 'b' / 'c' / 'd'
    _make_db(deep / 'rbx-storage.db')
    with pytest.raises(FileNotFoundError):
        resolve_cache_location(tmp_path)


def test_store_scans_when_user_picks_parent_of_sober_data(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    config_dir = tmp_path / 'cfg'
    monkeypatch.setattr(store_module, 'CONFIG_DIR', config_dir)
    monkeypatch.setattr(sqlite_browser_module, 'CONFIG_DIR', config_dir)
    base = tmp_path / '.var' / 'app' / 'org.vinegarhq.Sober'
    entry_id = 'cd' * 16
    _make_db(base / 'data' / 'sober' / 'appData' / 'rbx-storage.db', entry_id)
    (base / 'cache' / 'sober' / 'rbx-storage' / entry_id[:2]).mkdir(parents=True)
    (base / 'cache' / 'sober' / 'rbx-storage' / entry_id[:2] / entry_id).write_bytes(b'abc')

    store = RobloxCacheStore(base)
    store.validate()
    assert [entry.entry_id for entry in store.scan()] == [entry_id]


def test_store_error_explains_where_it_looked(tmp_path):
    store = RobloxCacheStore(tmp_path)
    with pytest.raises(RobloxCacheError, match='searched'):
        store.validate()


def test_saved_swap_list_survives_restore_and_can_be_forgotten(tmp_path, monkeypatch):
    config_dir = tmp_path / 'cfg'
    monkeypatch.setattr(store_module, 'CONFIG_DIR', config_dir)
    store = RobloxCacheStore(tmp_path)
    store.remember_swap('a' * 32, 'b' * 32, 'Idle')
    assert store.saved_swaps() == [{'target': 'a' * 32, 'name': 'Idle', 'source': 'b' * 32}]
    store.forget_swap('a' * 32)
    assert store.saved_swaps() == []
