"""Low level access to Roblox's ``rbx-storage`` cache folder and ``rbx-storage.db``.

Safety rules:

* Nothing is written while Roblox is running (the caller checks and passes ``roblox_running``).
* The database is only ever touched with ``UPDATE files SET size = ?`` for one row.
* The database is backed up before each change and the original entry file is kept until restored.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..utils import log_buffer

if TYPE_CHECKING:
    from collections.abc import Callable

HEX32 = re.compile(r'[0-9a-f]{32}')
META = 25  # Bytes between the URL and the payload in the RBXH header
HEAD_READ = 262144
_ASSET_RE = re.compile(r'[?&]id=(\d+)|assetId[=/](\d+)|/asset/(\d+)', re.IGNORECASE)
_LOG_TAG = 'CacheMode'


class CacheModeError(Exception):
    """A problem the user can act on; the message is shown in the UI."""


def asset_id_of(url: str) -> int | None:
    """Return the numeric asset ID in a cache entry URL, if any."""
    match = _ASSET_RE.search(url)
    if not match:
        return None
    value = next((g for g in match.groups() if g), '')
    return int(value) if value else None


def kind_of(payload: bytes) -> str:
    """Classify a payload by its magic bytes."""
    if payload[:8] == b'<roblox!':
        if b'KeyframeSequence' in payload:
            return 'anim'
        if b'CurveAnimation' in payload:
            return 'curve-anim'
        return 'rbxm'
    if payload[:4] == b'\x89PNG':
        return 'png'
    if payload[:2] == b'\xff\xd8':
        return 'jpg'
    if payload[:5] == b'\xabKTX ':
        return 'ktx'
    if payload[:8] == b'version ':
        return 'mesh'
    if payload[:4] == b'OggS':
        return 'ogg'
    if payload[:3] == b'ID3' or payload[:2] == b'\xff\xfb':
        return 'mp3'
    if payload[:1] in (b'{', b'['):
        return 'json'
    return 'other'


def split_entry(data: bytes) -> tuple[str, bytes, bool]:
    """Return ``(url, payload, has_header)``; files without an RBXH header are raw payloads."""
    if data[:4] == b'RBXH' and len(data) >= 12:
        url_len = struct.unpack_from('<I', data, 8)[0]
        start = 12 + url_len + META
        if url_len <= 4096 and start <= len(data):
            return data[12 : 12 + url_len].decode('utf-8', 'replace'), data[start:], True
    return '', data, False


@dataclass(frozen=True)
class CacheEntry:
    """One file in the Roblox cache."""

    ident: str
    size: int
    mtime: float
    kind: str
    url: str
    asset_id: int | None
    has_header: bool


class RobloxCacheStore:
    """The Roblox cache plus CacheMode's own state (manifest, originals, backups)."""

    def __init__(
        self,
        roblox_dir: Path,
        state_dir: Path,
        *,
        roblox_running: Callable[[], bool] | None = None,
    ) -> None:
        self.root = Path(roblox_dir)
        self.db = self.root / 'rbx-storage.db'
        self.dir = self.root / 'rbx-storage'
        self.state = Path(state_dir)
        self._running = roblox_running or (lambda: False)
        self._memo: dict[tuple[str, int, int], CacheEntry] = {}

    # State files
    def _load(self, name: str, default: Any) -> Any:
        try:
            return json.loads((self.state / name).read_text('utf-8'))
        except OSError, ValueError:
            return default

    def _save(self, name: str, obj: Any) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        path = self.state / name
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(obj, indent=2), 'utf-8')
        os.replace(tmp, path)

    def manifest(self) -> dict[str, dict[str, Any]]:
        """Active replacements keyed by target entry ID."""
        return self._load('manifest.json', {})

    def history(self) -> list[dict[str, str]]:
        return self._load('history.json', [])

    def _log_history(self, action: str, target: str, detail: str) -> None:
        hist = self.history()
        hist.append(
            {
                'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                'action': action,
                'target': target,
                'detail': detail,
            }
        )
        self._save('history.json', hist[-1000:])

    # Validation
    def validate(self) -> None:
        if not self.dir.is_dir():
            raise CacheModeError(f'Cache folder not found: {self.dir}')
        if not self.db.is_file():
            raise CacheModeError(f'Database not found: {self.db}')

    def _require_closed(self) -> None:
        if self._running():
            raise CacheModeError('Roblox is running. Close it completely and try again.')

    def path(self, ident: str) -> Path:
        return self.dir / ident[:2] / ident

    @staticmethod
    def _check_id(ident: str) -> None:
        if not HEX32.fullmatch(ident):
            raise CacheModeError('Entry IDs are 32 lowercase hex characters.')

    # Database (only the ``size`` cell)
    def db_size(self, ident: str) -> int | None:
        with contextlib.closing(sqlite3.connect(self.db, timeout=5)) as con:
            row = con.execute(
                'SELECT size FROM files WHERE id = ?', (bytes.fromhex(ident),)
            ).fetchone()
        return int(row[0]) if row else None

    def _set_db_size(self, ident: str, size: int) -> None:
        with contextlib.closing(sqlite3.connect(self.db, timeout=5)) as con:
            with con:
                cur = con.execute(
                    'UPDATE files SET size = ? WHERE id = ?', (size, bytes.fromhex(ident))
                )
            if cur.rowcount != 1:
                raise CacheModeError(f'Entry {ident} has no row in the database.')

    def backup_db(self) -> Path:
        dest = self.state / 'backups' / time.strftime('%Y%m%d-%H%M%S')
        dest.mkdir(parents=True, exist_ok=True)
        with (
            contextlib.closing(sqlite3.connect(self.db, timeout=5)) as src,
            contextlib.closing(sqlite3.connect(dest / self.db.name)) as dst,
        ):
            src.backup(dst)
        old = sorted(p for p in (self.state / 'backups').iterdir() if p.is_dir())
        for p in old[:-15]:
            shutil.rmtree(p, ignore_errors=True)
        return dest

    # Scanning
    def _describe(self, path: Path, ident: str, st: os.stat_result) -> CacheEntry:
        try:
            with path.open('rb') as f:
                head = f.read(HEAD_READ)
        except OSError:
            return CacheEntry(ident, st.st_size, st.st_mtime, '?', '', None, True)
        url, payload, has_header = split_entry(head)
        return CacheEntry(
            ident, st.st_size, st.st_mtime, kind_of(payload), url, asset_id_of(url), has_header
        )

    def scan(self) -> list[CacheEntry]:
        out: list[CacheEntry] = []
        for dirpath, _dirs, files in os.walk(self.dir):
            for name in files:
                if not HEX32.fullmatch(name):
                    continue
                path = Path(dirpath) / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                key = (name, st.st_size, st.st_mtime_ns)
                entry = self._memo.get(key)
                if entry is None:
                    entry = self._memo[key] = self._describe(path, name, st)
                out.append(entry)
        return out

    def find_by_asset_id(self, asset_id: int) -> list[CacheEntry]:
        return [e for e in self.scan() if e.asset_id == asset_id]

    def swap_status(self, ident: str) -> str:
        """``''`` untouched, ``'swapped'`` still ours, ``'reverted'`` Roblox rewrote it."""
        info = self.manifest().get(ident)
        if not info:
            return ''
        path = self.path(ident)
        try:
            if path.stat().st_size != info['size']:
                return 'reverted'
            sha256 = info.get('sha256')
            if sha256 and hashlib.sha256(path.read_bytes()).hexdigest() != sha256:
                return 'reverted'  # Same size but different content
        except OSError:
            return 'reverted'
        return 'swapped'

    # Header layout (where the payload length is stored in the 25 meta bytes)
    def detect_length_fields(self, limit: int = 400) -> list[tuple[int, int]] | None:
        """Find the offset/width of the payload-length field using real entries, or ``None``."""
        votes: dict[tuple[int, int], int] = {}
        total = 0
        for dirpath, _dirs, files in os.walk(self.dir):
            for name in files:
                if total >= limit:
                    break
                if not HEX32.fullmatch(name):
                    continue
                try:
                    path = Path(dirpath) / name
                    if path.stat().st_size > 1_000_000:
                        continue
                    data = path.read_bytes()
                except OSError:
                    continue
                _url, payload, has_header = split_entry(data)
                if not has_header or len(payload) < 256:
                    continue
                meta = data[len(data) - len(payload) - META : len(data) - len(payload)]
                total += 1
                for width, fmt in ((4, '<I'), (8, '<Q')):
                    for off in range(META - width + 1):
                        if struct.unpack_from(fmt, meta, off)[0] == len(payload):
                            votes[(off, width)] = votes.get((off, width), 0) + 1
            if total >= limit:
                break
        if total < 3:
            return None
        best: dict[int, int] = {}
        for (off, width), hits in sorted(votes.items(), key=lambda kv: kv[0][1]):
            if hits / total >= 0.9 and off not in best:
                best[off] = width
        return sorted(best.items()) or None

    @staticmethod
    def build_entry(template: bytes, payload: bytes, fields: list[tuple[int, int]]) -> bytes:
        """New entry = the template's RBXH header (length fields rewritten) + ``payload``."""
        _url, tpayload, has_header = split_entry(template)
        if not has_header:
            raise CacheModeError('Target has no RBXH header to copy.')
        start = len(template) - len(tpayload)
        header = bytearray(template[:start])
        for off, width in fields:
            struct.pack_into('<I' if width == 4 else '<Q', header, start - META + off, len(payload))
        return bytes(header) + payload

    # Operations
    def write_entry(self, target: str, new: bytes, origin: str) -> None:
        """Replace the whole entry file of ``target`` with ``new`` and fix the database size."""
        self._require_closed()
        self.validate()
        self._check_id(target)
        tp = self.path(target)
        if not tp.is_file():
            raise CacheModeError(f'Entry {target[:8]}… is not in the cache.')
        if self.db_size(target) is None:
            raise CacheModeError(f'Entry {target[:8]}… has no row in the database.')
        self.backup_db()
        orig_dir = self.state / 'originals'
        orig_dir.mkdir(parents=True, exist_ok=True)
        if self.swap_status(target) != 'swapped':  # Current file is a genuine Roblox copy
            shutil.copy2(tp, orig_dir / target)
        tmp = tp.with_name(tp.name + '.tmp')
        tmp.write_bytes(new)
        os.replace(tmp, tp)
        self._set_db_size(target, len(new))
        manifest = self.manifest()
        manifest[target] = {
            'size': len(new),
            'time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'origin': origin,
            'sha256': hashlib.sha256(new).hexdigest(),
        }
        self._save('manifest.json', manifest)
        self._log_history('replace', target, origin)
        log_buffer.log(_LOG_TAG, f'Replaced {target[:8]}… ({len(new)} bytes) from {origin}')

    def restore(self, target: str) -> str:
        self._require_closed()
        self.validate()
        self._check_id(target)
        manifest = self.manifest()
        if target not in manifest:
            raise CacheModeError('That entry has no replacement to restore.')
        status = self.swap_status(target)
        tp = self.path(target)
        orig = self.state / 'originals' / target
        if status == 'swapped':
            if not orig.is_file():
                raise CacheModeError('The saved original is missing.')
            data = orig.read_bytes()
            tmp = tp.with_name(tp.name + '.tmp')
            tmp.write_bytes(data)
            os.replace(tmp, tp)
            self._set_db_size(target, len(data))
            msg = f'Restored {target[:8]}… to the original.'
        else:
            with contextlib.suppress(OSError, CacheModeError):
                self._set_db_size(target, tp.stat().st_size)
            msg = f'{target[:8]}… was already rewritten by Roblox; nothing to restore.'
        self._log_history('restore', target, status)
        manifest.pop(target)
        self._save('manifest.json', manifest)
        with contextlib.suppress(OSError):
            orig.unlink()
        return msg

    def restore_all(self) -> list[str]:
        out: list[str] = []
        for target in list(self.manifest()):
            try:
                out.append(self.restore(target))
            except CacheModeError as exc:
                out.append(f'{target[:8]}…: {exc}')
                if 'running' in str(exc):
                    break
        return out
