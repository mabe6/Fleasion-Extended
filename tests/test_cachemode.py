"""Tests for the cache-editing replacement mode, using a synthetic Roblox cache."""

from __future__ import annotations

import sqlite3
import struct
from typing import TYPE_CHECKING

import pytest

from fleasion.cachemode import CacheModeError, CacheReplacer, RobloxCacheStore

if TYPE_CHECKING:
    from pathlib import Path

LEN_OFFSET = 13


def make_entry(
    root: Path, con: sqlite3.Connection, ident: str, asset_id: int, payload: bytes
) -> bytes:
    url = f'https://assetdelivery.roblox.com/v1/asset/?id={asset_id}'.encode()
    meta = bytearray(25)
    struct.pack_into('<I', meta, LEN_OFFSET, len(payload))
    data = b'RBXH\x01\x00\x00\x00' + struct.pack('<I', len(url)) + url + bytes(meta) + payload
    path = root / 'rbx-storage' / ident[:2] / ident
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    con.execute(
        'INSERT OR REPLACE INTO files VALUES (?,?,?,?)', (bytes.fromhex(ident), len(data), 1, 0)
    )
    con.commit()
    return data


A, B, C, D = ('aa' * 16, 'bb' * 16, 'cc' * 16, 'dd' * 16)
PNG = b'\x89PNG\r\n\x1a\n' + b'\x01' * 400


@pytest.fixture
def env(tmp_path: Path):
    root = tmp_path / 'Roblox'
    (root / 'rbx-storage').mkdir(parents=True)
    con = sqlite3.connect(root / 'rbx-storage.db')
    con.execute(
        'CREATE TABLE files (id BLOB PRIMARY KEY, size INTEGER, hits INTEGER, atime INTEGER)'
    )
    orig = {
        A: make_entry(root, con, A, 111, PNG),
        B: make_entry(root, con, B, 222, PNG.replace(b'\x01', b'\x02')),
        C: make_entry(root, con, C, 333, PNG.replace(b'\x01', b'\x03')),
        D: make_entry(root, con, D, 444, PNG.replace(b'\x01', b'\x04')),
    }
    running = {'v': False}
    store = RobloxCacheStore(root, tmp_path / 'state', roblox_running=lambda: running['v'])
    yield store, con, orig, running
    con.close()


def test_whole_entry_replace_and_restore(env):
    store, con, orig, _ = env
    rep = CacheReplacer(store)
    plan = rep.plan({111: 222}, set(), {}, {})
    assert plan[0].entries[0].ident == A
    report = rep.apply(plan)
    assert report.applied and not report.errors
    assert store.path(A).read_bytes() == orig[B]
    assert store.db_size(A) == len(orig[B])
    assert store.swap_status(A) == 'swapped'
    # Applying again is a no-op
    assert rep.apply(rep.plan({111: 222}, set(), {}, {})).skipped
    assert rep.restore_all()
    assert store.path(A).read_bytes() == orig[A] and store.db_size(A) == len(orig[A])
    assert not store.manifest()
    assert [h['action'] for h in store.history()] == ['replace', 'restore']


def test_local_file_builds_entry_with_fixed_length(env, tmp_path: Path):
    store, _con, _orig, _ = env
    f = tmp_path / 'new.png'
    new = PNG.replace(b'\x01', b'\x09') + b'tail'
    f.write_bytes(new)
    rep = CacheReplacer(store)
    report = rep.apply(rep.plan({}, set(), {}, {333: str(f)}))
    assert report.applied, report
    data = store.path(C).read_bytes()
    url, payload, has_header = (
        lambda d: __import__('fleasion.cachemode.store', fromlist=['x']).split_entry(d)
    )(data)
    assert has_header and payload == new and '333' in url  # URL kept, payload swapped
    assert struct.unpack_from('<I', data, len(data) - len(payload) - 25 + LEN_OFFSET)[0] == len(new)


def test_download_used_when_not_cached(env):
    store, *_ = env
    rep = CacheReplacer(store, fetch_asset=lambda i: PNG + b'dl' if i == 999 else None)
    report = rep.apply(rep.plan({444: 999}, set(), {}, {}))
    assert report.applied, report
    assert store.path(D).read_bytes().endswith(b'dl')


def test_skips_and_errors(env):
    store, *_ = env
    rep = CacheReplacer(store)
    plan = rep.plan({999: 222, 'TexturePack:1': 5}, {111}, {}, {})
    report = rep.apply(plan)
    assert not report.applied and len(report.skipped) == 3 and not report.errors
    report = rep.apply(rep.plan({111: 888}, set(), {}, {}))  # 888 not cached, no downloader
    assert report.errors


def test_refuses_while_roblox_runs(env):
    store, _con, orig, running = env
    running['v'] = True
    report = CacheReplacer(store).apply(CacheReplacer(store).plan({111: 222}, set(), {}, {}))
    assert report.errors and 'running' in report.errors[0]
    assert store.path(A).read_bytes() == orig[A]
    with pytest.raises(CacheModeError):
        store.restore_all() or store.restore(A)


def test_reverted_by_roblox_is_detected(env):
    store, _con, orig, _ = env
    rep = CacheReplacer(store)
    rep.apply(rep.plan({111: 222}, set(), {}, {}))
    store.path(A).write_bytes(orig[A])  # Roblox rewrote the file
    assert store.swap_status(A) == 'reverted'
    assert 'already rewritten' in store.restore(A)
