"""Direct reader and guarded editor for Roblox's local rbx-storage cache."""

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
from typing import Any

from ..utils import CONFIG_DIR
from .sqlite_browser import SQLiteBrowser, SQLiteBrowserError, _is_roblox_process_running

HEX32 = re.compile(r'[0-9a-f]{32}')
ASSET_ID_PATTERN = re.compile(r'[?&]id=(\d+)|assetId[=/](\d+)|/asset/(\d+)', re.I)
RBXH_META_SIZE = 25
ENTRY_HEAD_READ = 8192
MAX_IMPORT_BYTES = 100_000_000
MAX_ENTRY_READ_BYTES = 500_000_000


class RobloxCacheError(Exception):
    """A cache operation failed for a reason suitable for display in the GUI."""


@dataclass(frozen=True)
class RobloxCacheEntry:
    """One physical Roblox cache entry and its corresponding database row."""

    entry_id: str
    file_size: int
    database_size: int | None
    modified_at: float
    kind: str
    url: str
    asset_id: str
    has_rbxh_header: bool
    swap_status: str = ''


def default_roblox_cache_root() -> Path:
    """Return the usual Roblox cache root on Windows, or a reasonable fallback."""
    override = os.environ.get('FLEASION_ROBLOX_CACHE_DIR') or os.environ.get('CACHESWAP_ROBLOX_DIR')
    if override:
        return Path(override).expanduser()
    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        return Path(local_app_data) / 'Roblox'
    sober_cache = (
        Path.home()
        / '.var'
        / 'app'
        / 'org.vinegarhq.Sober'
        / 'data'
        / 'sober'
    )
    if sober_cache.is_dir():
        return sober_cache
    return Path.home() / 'Roblox'


def parse_rbxh_header(data: bytes) -> tuple[str, int]:
    """Return the encoded URL and payload start for a standard RBXH cache entry."""
    if data[:4] != b'RBXH' or len(data) < 12:
        raise RobloxCacheError('This cache entry does not have an RBXH header.')
    url_length = struct.unpack_from('<I', data, 8)[0]
    payload_start = 12 + url_length + RBXH_META_SIZE
    if url_length > 4096 or payload_start > len(data):
        raise RobloxCacheError('The RBXH header has an invalid URL length.')
    return data[12 : 12 + url_length].decode('utf-8', 'replace'), payload_start


def split_rbxh_entry(data: bytes) -> tuple[str, bytes, bool]:
    """Return URL, payload and whether the input contains an RBXH header."""
    try:
        url, payload_start = parse_rbxh_header(data)
        return url, data[payload_start:], True
    except RobloxCacheError:
        return '', data, False


def detect_roblox_payload_kind(payload: bytes) -> str:
    """Detect common Roblox cache payload formats without decoding their contents."""
    if payload.startswith((b'\x00\x01\x00\x00', b'OTTO', b'ttcf', b'wOFF', b'wOF2')):
        return 'font'
    if payload[:8] == b'<roblox!':
        if b'KeyframeSequence' in payload:
            return 'anim'
        if b'CurveAnimation' in payload:
            return 'curve-anim'
        return 'rbxm'
    if payload.startswith(b'\x89PNG'):
        return 'png'
    if payload.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if payload[:12] in (b'\xabKTX 11\xbb\r\n\x1a\n', b'\xabKTX 20\xbb\r\n\x1a\n'):
        return 'ktx'
    if payload.startswith(b'BM'):
        return 'bmp'
    if payload.startswith((b'GIF87a', b'GIF89a')):
        return 'gif'
    if len(payload) >= 12 and payload[:4] == b'RIFF' and payload[8:12] == b'WEBP':
        return 'webp'
    if payload.startswith((b'II*\x00', b'MM\x00*', b'II+\x00', b'MM\x00+')):
        return 'tiff'
    if payload.startswith(b'\x00\x00\x01\x00'):
        return 'ico'
    if payload.startswith(b'DDS '):
        return 'dds'
    if payload.startswith(b'8BPS'):
        return 'psd'
    if payload[:8] == b'version ':
        return 'mesh'
    if payload[:4] == b'OggS':
        return 'ogg'
    if payload[:3] == b'ID3' or payload[:2] == b'\xff\xfb':
        return 'mp3'
    if payload[:6] == b'<roblox':
        return 'xml'
    if payload[:1] in (b'{', b'['):
        return 'json'
    return 'other'


def _asset_id_from_url(url: str) -> str:
    match = ASSET_ID_PATTERN.search(url)
    return next((group for group in match.groups() if group), '') if match else ''


def _rewrite_payload_length_fields(
    template: bytes,
    payload: bytes,
    fields: list[tuple[int, int]],
) -> bytes:
    """Preserve the target's RBXH header and rewrite only proven payload-length fields."""
    _url, payload_start = parse_rbxh_header(template)
    header = bytearray(template[:payload_start])
    meta_start = payload_start - RBXH_META_SIZE
    for offset, width in fields:
        if width not in (4, 8) or offset < 0 or offset + width > RBXH_META_SIZE:
            raise RobloxCacheError('The detected RBXH length-field layout is invalid.')
        if width == 4:
            if len(payload) > 0xFFFFFFFF:
                raise RobloxCacheError('The payload is too large for the detected 32-bit length field.')
            struct.pack_into('<I', header, meta_start + offset, len(payload))
        else:
            struct.pack_into('<Q', header, meta_start + offset, len(payload))
    return bytes(header) + payload


class RobloxCacheStore:
    """Inspect and update local rbx-storage files together with their SQLite metadata."""

    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = Path(root).expanduser().resolve() if root else default_roblox_cache_root().resolve()
        self.database_path = self.root / 'rbx-storage.db'
        self.assets_path = self.root / 'rbx-storage'
        self.state_dir = CONFIG_DIR / 'FleasionNT' / 'LocalCacheSwap'
        self.library_dir = self.state_dir / 'library'
        self.originals_dir = self.state_dir / 'originals'
        self.manifest_path = self.state_dir / 'manifest.json'
        self.history_path = self.state_dir / 'history.json'

    @staticmethod
    def _validate_entry_id(entry_id: str) -> None:
        if not entry_id or HEX32.fullmatch(entry_id) is None:
            raise RobloxCacheError('Cache entry IDs must be 32 lowercase hexadecimal characters.')

    def validate(self) -> None:
        """Validate the expected directory layout and SQLite schema without writing."""
        if not self.assets_path.is_dir():
            raise RobloxCacheError(f'Roblox cache folder not found:\n{self.assets_path}')
        if not self.database_path.is_file():
            raise RobloxCacheError(f'Roblox cache database not found:\n{self.database_path}')
        try:
            browser = SQLiteBrowser(self.database_path)
            tables = {table.name: table for table in browser.tables()}
            table = tables.get('files')
            if table is None or table.kind != 'table':
                raise RobloxCacheError("The database has no standard 'files' table.")
            columns, _ddl, _kind = browser.schema('files')
            column_names = {str(column['name']) for column in columns}
            if not {'id', 'size'}.issubset(column_names):
                raise RobloxCacheError("The 'files' table does not have the expected id and size columns.")
        except SQLiteBrowserError as exc:
            raise RobloxCacheError(str(exc)) from exc

    def _entry_path(self, entry_id: str) -> Path:
        self._validate_entry_id(entry_id)
        return self.assets_path / entry_id[:2] / entry_id

    def _read_database_sizes(self) -> dict[str, int]:
        """Read the known files table in read-only mode, mapping 16-byte IDs to sizes."""
        try:
            browser = SQLiteBrowser(self.database_path)
            with contextlib.closing(browser.connect_readonly()) as connection:
                rows = connection.execute('SELECT id, size FROM files').fetchall()
        except (SQLiteBrowserError, sqlite3.Error) as exc:
            raise RobloxCacheError(f'Could not read the Roblox files table:\n{exc}') from exc
        result: dict[str, int] = {}
        for row in rows:
            ident = row['id']
            size = row['size']
            if isinstance(ident, memoryview):
                ident = ident.tobytes()
            if isinstance(ident, bytes) and len(ident) == 16 and isinstance(size, int):
                result[ident.hex()] = size
        return result

    def _load_json(self, path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text('utf-8'))
        except (OSError, ValueError):
            return default

    def _save_json(self, path: Path, value: Any) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), 'utf-8')
        os.replace(temporary, path)

    def _manifest(self) -> dict[str, dict[str, Any]]:
        result = self._load_json(self.manifest_path, {})
        return result if isinstance(result, dict) else {}

    def history(self) -> list[dict[str, Any]]:
        """Return the newest persisted swap and restore records."""
        records = self._load_json(self.history_path, [])
        return records if isinstance(records, list) else []

    def _log_history(self, action: str, target: str, source: str = '', detail: str = '') -> None:
        records = self.history()
        records.append({
            'time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'action': action,
            'target': target,
            'source': source,
            'detail': detail,
        })
        self._save_json(self.history_path, records[-1000:])

    def swap_status(self, entry_id: str) -> str:
        """Return empty, swapped or reverted based on the manifest and current file size."""
        record = self._manifest().get(entry_id)
        if not record:
            return ''
        try:
            current_size = self._entry_path(entry_id).stat().st_size
        except OSError:
            return 'reverted'
        return 'swapped' if current_size == record.get('swapped_size') else 'reverted'

    def scan(self, *, limit: int = 100_000) -> list[RobloxCacheEntry]:
        """Scan physical cache files and join them with rows from the files table."""
        self.validate()
        sizes = self._read_database_sizes()
        manifest = self._manifest()
        results: list[RobloxCacheEntry] = []
        for directory, dirnames, filenames in os.walk(self.assets_path, followlinks=False):
            dirnames.sort()
            for filename in filenames:
                if HEX32.fullmatch(filename) is None:
                    continue
                path = Path(directory) / filename
                try:
                    stat_result = path.stat()
                    with path.open('rb') as handle:
                        head = handle.read(ENTRY_HEAD_READ)
                    url, payload, has_header = split_rbxh_entry(head)
                    kind = detect_roblox_payload_kind(payload)
                except OSError:
                    continue
                record = manifest.get(filename, {})
                swapped_size = record.get('swapped_size')
                status = ''
                if record:
                    status = 'swapped' if stat_result.st_size == swapped_size else 'reverted'
                results.append(RobloxCacheEntry(
                    entry_id=filename,
                    file_size=stat_result.st_size,
                    database_size=sizes.get(filename),
                    modified_at=stat_result.st_mtime,
                    kind=kind,
                    url=url,
                    asset_id=_asset_id_from_url(url),
                    has_rbxh_header=has_header,
                    swap_status=status,
                ))
                if len(results) >= limit:
                    results.sort(key=lambda item: (-item.modified_at, item.entry_id))
                    return results
        results.sort(key=lambda item: (-item.modified_at, item.entry_id))
        return results

    def database_size(self, entry_id: str) -> int | None:
        self._validate_entry_id(entry_id)
        return self._read_database_sizes().get(entry_id)

    def _require_client_closed(self) -> None:
        if _is_roblox_process_running():
            raise RobloxCacheError(
                'Roblox Player, Studio or Sober appears to be running. Close it completely before changing the cache.'
            )

    def _backup_database(self) -> Path:
        try:
            return SQLiteBrowser(self.database_path).backup()
        except SQLiteBrowserError as exc:
            raise RobloxCacheError(f'Could not back up the Roblox cache database:\n{exc}') from exc

    def _update_database_size(
        self,
        entry_id: str,
        new_size: int,
        *,
        expected_old_size: int | None = None,
    ) -> None:
        """Update exactly one files.size value and reject a stale row snapshot."""
        self._validate_entry_id(entry_id)
        try:
            with contextlib.closing(sqlite3.connect(self.database_path, timeout=5)) as connection:
                connection.execute('PRAGMA busy_timeout = 5000')
                connection.execute('BEGIN IMMEDIATE')
                row = connection.execute(
                    'SELECT size FROM files WHERE id = ?',
                    (bytes.fromhex(entry_id),),
                ).fetchone()
                if row is None:
                    raise RobloxCacheError(f'Entry {entry_id} has no row in the files table.')
                if expected_old_size is not None and row[0] != expected_old_size:
                    raise RobloxCacheError(
                        f'Database size for {entry_id} changed from {expected_old_size} to {row[0]}; refresh and retry.'
                    )
                cursor = connection.execute(
                    'UPDATE files SET size = ? WHERE id = ?',
                    (new_size, bytes.fromhex(entry_id)),
                )
                if cursor.rowcount != 1:
                    raise RobloxCacheError(
                        f'Expected to update one size row for {entry_id}, updated {cursor.rowcount}.'
                    )
                connection.commit()
        except RobloxCacheError:
            raise
        except sqlite3.Error as exc:
            raise RobloxCacheError(f'Could not update files.size for {entry_id}:\n{exc}') from exc

    def _atomic_replace(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.fleasion-tmp')
        try:
            with temporary.open('wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise

    def _backup_original(self, entry_id: str, data: bytes) -> Path:
        self.originals_dir.mkdir(parents=True, exist_ok=True)
        destination = self.originals_dir / entry_id
        temporary = destination.with_suffix('.tmp')
        temporary.write_bytes(data)
        os.replace(temporary, destination)
        return destination

    def _source_bytes(self, source_id: str) -> bytes:
        self._validate_entry_id(source_id)
        for path in (self._entry_path(source_id), self.library_dir / source_id):
            if path.is_file():
                size = path.stat().st_size
                if size > MAX_ENTRY_READ_BYTES:
                    raise RobloxCacheError(f'Source entry exceeds the {MAX_ENTRY_READ_BYTES:,}-byte safety limit.')
                return path.read_bytes()
        raise RobloxCacheError('Source entry is not in the cache or the Fleasion local library.')

    def swap(self, target_id: str, source_id: str, *, origin: str = '') -> str:
        """Swap a whole source entry into a target slot, with DB backup and rollback."""
        self._require_client_closed()
        self.validate()
        self._validate_entry_id(target_id)
        self._validate_entry_id(source_id)
        if target_id == source_id:
            raise RobloxCacheError('Target and source are the same cache entry.')

        target_path = self._entry_path(target_id)
        if not target_path.is_file():
            raise RobloxCacheError('Target entry file is not present in the local cache.')
        current_db_size = self.database_size(target_id)
        if current_db_size is None:
            raise RobloxCacheError('Target has no corresponding row in rbx-storage.db.')
        new_data = self._source_bytes(source_id)
        if target_path.stat().st_size > MAX_ENTRY_READ_BYTES:
            raise RobloxCacheError('Target entry exceeds the configured safety limit.')
        old_data = target_path.read_bytes()

        status = self.swap_status(target_id)
        if self.swap_status(source_id) == 'swapped':
            raise RobloxCacheError('Source is currently swapped itself. Restore its original entry first.')

        backup_path = self._backup_database()
        old_manifest = self._manifest()
        if status != 'swapped':
            self._backup_original(target_id, old_data)

        if source_id != target_id and self._entry_path(source_id).is_file():
            self.library_dir.mkdir(parents=True, exist_ok=True)
            library_copy = self.library_dir / source_id
            if not library_copy.exists():
                library_copy.write_bytes(new_data)

        replaced_file = False
        try:
            self._atomic_replace(target_path, new_data)
            replaced_file = True
            self._update_database_size(target_id, len(new_data), expected_old_size=current_db_size)

            manifest = self._manifest()
            manifest[target_id] = {
                'source': source_id,
                'swapped_size': len(new_data),
                'original_size': len(old_data),
                'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                'origin': origin or f'entry {source_id[:8]}',
                'database_backup': str(backup_path),
            }
            self._save_json(self.manifest_path, manifest)
            self._log_history('swap', target_id, source_id, origin or f'entry {source_id[:8]}')
        except Exception as exc:
            if replaced_file:
                with contextlib.suppress(OSError):
                    self._atomic_replace(target_path, old_data)
            with contextlib.suppress(Exception):
                self._update_database_size(target_id, current_db_size)
            with contextlib.suppress(Exception):
                self._save_json(self.manifest_path, old_manifest)
            if isinstance(exc, RobloxCacheError):
                raise
            raise RobloxCacheError(
                f'The swap failed and rollback was attempted. Database backup: {backup_path}\n{exc}'
            ) from exc

        return (
            f'Swapped {target_id[:8]}… ← {source_id[:8]}… ({len(new_data):,} bytes). '
            'Restart Roblox to test it.'
        )

    def restore(self, target_id: str) -> str:
        """Restore a target entry and keep its SQLite size row consistent."""
        self._require_client_closed()
        self.validate()
        self._validate_entry_id(target_id)
        manifest = self._manifest()
        record = manifest.get(target_id)
        if not record:
            raise RobloxCacheError('That entry has no active replacement to restore.')

        target_path = self._entry_path(target_id)
        if not target_path.is_file():
            raise RobloxCacheError('The cache target file no longer exists.')
        current_data = target_path.read_bytes()
        current_db_size = self.database_size(target_id)
        if current_db_size is None:
            raise RobloxCacheError('The target has no corresponding row in rbx-storage.db.')
        backup_path = self._backup_database()

        if self.swap_status(target_id) == 'swapped':
            original_path = self.originals_dir / target_id
            if not original_path.is_file():
                raise RobloxCacheError(
                    f'The original cache file is missing. Database backup: {backup_path}'
                )
            original_data = original_path.read_bytes()
            self._atomic_replace(target_path, original_data)
            try:
                self._update_database_size(
                    target_id, len(original_data), expected_old_size=current_db_size
                )
            except Exception:
                with contextlib.suppress(OSError):
                    self._atomic_replace(target_path, current_data)
                raise
            message = f'Restored {target_id[:8]}… to its saved original ({len(original_data):,} bytes).'
        else:
            # Roblox or another process rewrote this slot. Keep that file and synchronize the DB size.
            try:
                self._update_database_size(
                    target_id, len(current_data), expected_old_size=current_db_size
                )
            except Exception:
                raise
            message = f'{target_id[:8]}… was rewritten externally; kept the current file and synchronized its DB size.'

        manifest.pop(target_id, None)
        self._save_json(self.manifest_path, manifest)
        with contextlib.suppress(OSError):
            (self.originals_dir / target_id).unlink()
        self._log_history('restore', target_id, str(record.get('source', '')), str(backup_path))
        return message

    def restore_all(self) -> list[str]:
        """Restore every active replacement, stopping if the client is running."""
        messages: list[str] = []
        for target_id in list(self._manifest()):
            try:
                messages.append(self.restore(target_id))
            except RobloxCacheError as exc:
                messages.append(f'{target_id[:8]}…: {exc}')
                if _is_roblox_process_running():
                    break
        return messages or ['There are no active replacements to restore.']

    def detect_payload_length_fields(self, *, sample_limit: int = 400) -> dict[str, Any] | None:
        """Infer length field positions only when at least 90% of real samples agree."""
        votes: dict[tuple[int, int], int] = {}
        total = 0
        if not self.assets_path.is_dir():
            return None
        for directory, dirnames, filenames in os.walk(self.assets_path, followlinks=False):
            dirnames.sort()
            for filename in filenames:
                if HEX32.fullmatch(filename) is None:
                    continue
                path = Path(directory) / filename
                try:
                    if path.stat().st_size > 1_000_000:
                        continue
                    data = path.read_bytes()
                    _url, payload_start = parse_rbxh_header(data)
                except (OSError, RobloxCacheError):
                    continue
                payload_length = len(data) - payload_start
                if payload_length < 256:
                    continue
                metadata_start = payload_start - RBXH_META_SIZE
                total += 1
                for width, fmt in ((4, '<I'), (8, '<Q')):
                    for offset in range(RBXH_META_SIZE - width + 1):
                        if struct.unpack_from(fmt, data, metadata_start + offset)[0] == payload_length:
                            votes[(offset, width)] = votes.get((offset, width), 0) + 1
                if total >= sample_limit:
                    break
            if total >= sample_limit:
                break
        if total < 3:
            return None
        chosen: dict[int, tuple[int, int]] = {}
        for (offset, width), hits in votes.items():
            if hits / total >= 0.9 and (offset not in chosen or width < chosen[offset][0]):
                chosen[offset] = (width, hits)
        if not chosen:
            return None
        return {
            'fields': sorted((offset, width) for offset, (width, _hits) in chosen.items()),
            'hits': {offset: hits for offset, (_width, hits) in chosen.items()},
            'total': total,
        }

    def import_file(
        self,
        target_id: str,
        file_path: str | os.PathLike[str],
        *,
        origin: str = '',
    ) -> str:
        """Wrap an external payload in the target's proven RBXH header, then swap it in."""
        self._require_client_closed()
        self.validate()
        self._validate_entry_id(target_id)
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise RobloxCacheError(f'Import file not found:\n{path}')
        if path.stat().st_size <= 0:
            raise RobloxCacheError('The selected import file is empty.')
        if path.stat().st_size > MAX_IMPORT_BYTES:
            raise RobloxCacheError(f'Import file exceeds the {MAX_IMPORT_BYTES:,}-byte limit.')
        payload = path.read_bytes()
        if payload.startswith(b'RBXH'):
            raise RobloxCacheError(
                'The selected file appears to be a complete RBXH cache entry, not a raw asset payload. '
                'Choose an asset file such as RBXM, PNG, mesh or audio, or select the source entry in Cache Slots.'
            )
        target_path = self._entry_path(target_id)
        if not target_path.is_file():
            raise RobloxCacheError('The target entry file is not in the local cache.')
        fields = self.detect_payload_length_fields()
        if not fields:
            raise RobloxCacheError(
                'Could not verify the RBXH payload-length fields from real cache entries; import was refused.'
            )
        if self.database_size(target_id) is None:
            raise RobloxCacheError('The target has no corresponding row in rbx-storage.db.')

        manifest_record = self._manifest().get(target_id, {})
        saved_original = self.originals_dir / target_id
        template = saved_original.read_bytes() if manifest_record and saved_original.is_file() else target_path.read_bytes()
        wrapped_entry = _rewrite_payload_length_fields(template, payload, fields['fields'])
        source_id = hashlib.md5(wrapped_entry).hexdigest()

        self.library_dir.mkdir(parents=True, exist_ok=True)
        library_path = self.library_dir / source_id
        if not library_path.exists():
            library_path.write_bytes(wrapped_entry)
        return self.swap(
            target_id,
            source_id,
            origin=origin or f'file {path.name}',
        )

    def export_entry(self, entry_id: str, destination: str | os.PathLike[str]) -> Path:
        """Export raw cache bytes and a small header/DB diagnostic report."""
        self._validate_entry_id(entry_id)
        physical = self._entry_path(entry_id)
        source = physical if physical.is_file() else self.library_dir / entry_id
        if not source.is_file():
            raise RobloxCacheError('Cache entry file not found.')
        data = source.read_bytes()
        output_dir = Path(destination).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f'{entry_id}.bin'
        output_path.write_bytes(data)
        url, payload, has_header = split_rbxh_entry(data)
        try:
            db_size = self.database_size(entry_id)
        except RobloxCacheError:
            db_size = None
        report = [
            f'Entry ID: {entry_id}',
            f'File size: {len(data)}',
            f'Database size: {db_size}',
            f'RBXH header: {"yes" if has_header else "NO"}',
            f'URL: {url}',
            f'Detected kind: {detect_roblox_payload_kind(payload)}',
            f'First 96 bytes: {data[:96].hex(" ")}',
            f'Modified: {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(source.stat().st_mtime))}',
        ]
        (output_dir / f'{entry_id}.txt').write_text('\n'.join(report) + '\n', 'utf-8')
        return output_path

    def probe_header(self, *, samples: int = 6) -> str:
        """Return a report of inferred RBXH length fields and sample entry headers."""
        fields = self.detect_payload_length_fields()
        lines = [f'Fleasion local cache header probe {time.strftime("%Y-%m-%d %H:%M:%S")}',
                 f'Cache root: {self.root}']
        if fields:
            lines.append(
                f'Payload-length fields: {fields["fields"]} '
                f'(hits {fields["hits"]} / {fields["total"]} sampled entries)'
            )
        else:
            lines.append('Payload-length fields: NOT FOUND; external-file import is disabled until verified.')
        lines.append('')
        lines.append('Sample entries (metadata is the 25-byte block before the payload):')
        shown = 0
        try:
            entries = self.scan()
        except RobloxCacheError as exc:
            return '\n'.join(lines + [str(exc)])
        for entry in entries:
            if shown >= samples:
                break
            if entry.file_size > 1_000_000 or not entry.has_rbxh_header:
                continue
            try:
                data = self._entry_path(entry.entry_id).read_bytes()
                _url, payload_start = parse_rbxh_header(data)
            except (OSError, RobloxCacheError):
                continue
            lines.append(
                f'  {entry.entry_id} kind={entry.kind} file={len(data)} '
                f'payload={len(data) - payload_start} '
                f'metadata={data[payload_start - RBXH_META_SIZE:payload_start].hex()} '
                f'url={entry.url[:120]}'
            )
            shown += 1
        return '\n'.join(lines)
