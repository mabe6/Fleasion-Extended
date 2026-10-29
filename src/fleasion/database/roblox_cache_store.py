"""Direct reader and guarded editor for Roblox's local rbx-storage cache."""

from __future__ import annotations

import contextlib
import hashlib
import html
import json
import os
import re
import sqlite3
import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..utils import CONFIG_DIR
from .animation_convert import AnimationConversionError, convert_animation_for_slot
from .animation_payload import inspect_animation_payload
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
    storage_mode: str = 'file'
    name: str = ''


def _embedded_name(payload: bytes) -> str:
    """Read a textual Roblox Name property when it is present in the payload prefix."""
    if not payload:
        return ''
    data = payload
    if data.startswith(b'\x1f\x8b'):
        try:
            data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data)
        except zlib.error:
            pass
    text = data.decode('utf-8', errors='ignore')
    match = re.search(
        r'<(?:string|Content)\s+name=["\']Name["\']\s*>(.*?)</(?:string|Content)>',
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return ''
    value = html.unescape(match.group(1)).strip()
    return value[:160] if value else ''


def default_roblox_cache_root() -> Path:
    """Return the usual Roblox cache root on Windows, or a reasonable fallback."""
    override = os.environ.get('FLEASION_ROBLOX_CACHE_DIR') or os.environ.get('CACHESWAP_ROBLOX_DIR')
    if override:
        return Path(override).expanduser()
    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        return Path(local_app_data) / 'Roblox'
    sober_data = (
        Path.home()
        / '.var'
        / 'app'
        / 'org.vinegarhq.Sober'
        / 'data'
        / 'sober'
    )
    # Sober commonly keeps its Roblox database under appData while assets use sibling folders.
    sober_appdata = sober_data / 'appData'
    # Check the database itself first. An empty appData directory must not hide a DB
    # that lives one directory above it.
    if (sober_appdata / 'rbx-storage.db').is_file():
        return sober_appdata
    if (sober_data / 'rbx-storage.db').is_file():
        return sober_data
    if sober_appdata.is_dir():
        return sober_appdata
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
    # Some Roblox cache payloads add a short wrapper/prefix before the KTX header.
    # Detect only near the start so arbitrary binary data cannot be misclassified.
    ktx1 = b'\xabKTX 11\xbb\r\n\x1a\n'
    ktx2 = b'\xabKTX 20\xbb\r\n\x1a\n'
    if payload[:12] in (ktx1, ktx2) or any(
        payload.find(magic, 1, 64) >= 0 for magic in (ktx1, ktx2)
    ):
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
    xml_start = payload.lstrip(b'\xef\xbb\xbf \t\r\n')
    if xml_start.startswith(b'<roblox') or (
        xml_start.startswith(b'<?xml') and b'<roblox' in xml_start[:1024]
    ):
        if b'CurveAnimation' in payload:
            return 'curve-anim'
        if b'KeyframeSequence' in payload:
            return 'anim'
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
        """Validate the cache database and accept either BLOB-backed or file-backed entries."""
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
            if 'content' not in column_names and not self.assets_path.is_dir():
                raise RobloxCacheError(
                    "This database has no files.content BLOB column and the external rbx-storage folder is missing."
                )
        except SQLiteBrowserError as exc:
            raise RobloxCacheError(str(exc)) from exc

    def _decode_database_row(self, row: sqlite3.Row) -> tuple[str, dict[str, Any]] | None:
        ident = row['id']
        if isinstance(ident, memoryview):
            ident = ident.tobytes()
        if not isinstance(ident, bytes) or len(ident) != 16:
            return None
        content = row['content'] if 'content' in row.keys() else None
        if isinstance(content, memoryview):
            content = content.tobytes()
        return ident.hex(), {
            'size': row['size'],
            'content': content,
            'has_content_column': 'content' in row.keys(),
            'atime': row['atime'] if 'atime' in row.keys() else 0,
        }

    def _read_database_record(self, entry_id: str) -> dict[str, Any] | None:
        """Read one row. SQLite's own connection includes committed WAL frames when present."""
        self._validate_entry_id(entry_id)
        try:
            browser = SQLiteBrowser(self.database_path)
            columns, _ddl, _kind = browser.schema('files')
            names = {str(column['name']) for column in columns}
            select = 'id, size'
            if 'content' in names:
                select += ', content'
            if 'atime' in names:
                select += ', atime'
            with contextlib.closing(browser.connect_readonly()) as connection:
                row = connection.execute(
                    f'SELECT {select} FROM files WHERE id = ?',
                    (bytes.fromhex(entry_id),),
                ).fetchone()
            if row is None:
                return None
            decoded = self._decode_database_row(row)
            return decoded[1] if decoded else None
        except (SQLiteBrowserError, sqlite3.Error) as exc:
            raise RobloxCacheError(f'Could not read database entry {entry_id}:\\n{exc}') from exc

    def _iter_database_records(self):
        """Yield one cache row at a time, avoiding loading a large BLOB database into RAM."""
        browser = SQLiteBrowser(self.database_path)
        try:
            columns, _ddl, _kind = browser.schema('files')
            names = {str(column['name']) for column in columns}
            select = 'id, size'
            if 'content' in names:
                select += ', content'
            if 'atime' in names:
                select += ', atime'
            with contextlib.closing(browser.connect_readonly()) as connection:
                cursor = connection.execute(f'SELECT {select} FROM files')
                for row in cursor:
                    decoded = self._decode_database_row(row)
                    if decoded is not None:
                        yield decoded
        except (SQLiteBrowserError, sqlite3.Error) as exc:
            raise RobloxCacheError(f'Could not scan the Roblox files table:\\n{exc}') from exc

    def _get_entry_data(
        self, entry_id: str, record: dict[str, Any] | None = None
    ) -> tuple[bytes, str]:
        """Return bytes and storage mode, preferring a non-NULL files.content BLOB."""
        self._validate_entry_id(entry_id)
        if record is None:
            record = self._read_database_record(entry_id)
        if record is not None and isinstance(record.get('content'), bytes):
            data = record['content']
            if len(data) > MAX_ENTRY_READ_BYTES:
                raise RobloxCacheError(f'Entry exceeds the {MAX_ENTRY_READ_BYTES:,}-byte safety limit.')
            return data, 'database'
        physical = self._entry_path(entry_id)
        for path, mode in ((physical, 'file'), (self.library_dir / entry_id, 'library')):
            if path.is_file():
                if path.stat().st_size > MAX_ENTRY_READ_BYTES:
                    raise RobloxCacheError(f'Entry exceeds the {MAX_ENTRY_READ_BYTES:,}-byte safety limit.')
                return path.read_bytes(), mode
        raise RobloxCacheError(f'Entry {entry_id} has no content BLOB or physical cache file.')

    def _entry_path(self, entry_id: str) -> Path:
        self._validate_entry_id(entry_id)
        return self.assets_path / entry_id[:2] / entry_id

    def _read_database_sizes(self) -> dict[str, int]:
        """Read only ID/size metadata, not content BLOBs."""
        result: dict[str, int] = {}
        try:
            browser = SQLiteBrowser(self.database_path)
            with contextlib.closing(browser.connect_readonly()) as connection:
                for row in connection.execute('SELECT id, size FROM files'):
                    ident = row['id']
                    if isinstance(ident, memoryview):
                        ident = ident.tobytes()
                    if isinstance(ident, bytes) and len(ident) == 16 and isinstance(row['size'], int):
                        result[ident.hex()] = row['size']
        except (SQLiteBrowserError, sqlite3.Error) as exc:
            raise RobloxCacheError(f'Could not read the Roblox files table:\\n{exc}') from exc
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
        """Return empty, swapped or reverted using a content hash where available."""
        record = self._manifest().get(entry_id)
        if not record:
            return ''
        try:
            data, _mode = self._get_entry_data(entry_id)
        except RobloxCacheError:
            return 'reverted'
        expected_hash = record.get('swapped_sha256')
        if expected_hash:
            return 'swapped' if hashlib.sha256(data).hexdigest() == expected_hash else 'reverted'
        return 'swapped' if len(data) == record.get('swapped_size') else 'reverted'

    def scan(self, *, limit: int = 100_000) -> list[RobloxCacheEntry]:
        """Scan database BLOBs first, then include legacy file-backed cache entries."""
        self.validate()
        manifest = self._manifest()
        results: list[RobloxCacheEntry] = []
        seen: set[str] = set()

        def add_entry(entry_id: str, data: bytes, storage_mode: str,
                      database_size: int | None, modified_at: float) -> None:
            if entry_id in seen:
                return
            seen.add(entry_id)
            url, payload, has_header = split_rbxh_entry(data[:ENTRY_HEAD_READ])
            swap_record = manifest.get(entry_id, {})
            status = ''
            if swap_record:
                expected_hash = swap_record.get('swapped_sha256')
                if expected_hash:
                    status = 'swapped' if hashlib.sha256(data).hexdigest() == expected_hash else 'reverted'
                else:
                    status = 'swapped' if len(data) == swap_record.get('swapped_size') else 'reverted'
            results.append(RobloxCacheEntry(
                entry_id=entry_id,
                file_size=len(data),
                database_size=database_size,
                modified_at=modified_at,
                kind=detect_roblox_payload_kind(payload),
                url=url,
                asset_id=_asset_id_from_url(url),
                has_rbxh_header=has_header,
                swap_status=status,
                storage_mode=storage_mode,
                name=_embedded_name(payload),
            ))

        for entry_id, record in self._iter_database_records():
            try:
                data, storage_mode = self._get_entry_data(entry_id, record)
            except RobloxCacheError:
                # Rows with NULL content and no physical entry are metadata-only.
                continue
            path = self._entry_path(entry_id)
            try:
                modified_at = path.stat().st_mtime if path.is_file() else 0.0
            except OSError:
                modified_at = 0.0
            if modified_at <= 0:
                atime = record.get('atime') or 0
                if isinstance(atime, (int, float)) and atime > 0:
                    modified_at = float(atime / 1000 if atime > 10_000_000_000 else atime)
            add_entry(entry_id, data, storage_mode, record.get('size'), modified_at)
            if len(results) >= limit:
                break

        # Some older cache versions store payloads as separate files and leave content NULL.
        # Also retain orphan files so the UI can diagnose entries missing DB metadata.
        if len(results) < limit and self.assets_path.is_dir():
            sizes = self._read_database_sizes()
            for directory, dirnames, filenames in os.walk(self.assets_path, followlinks=False):
                dirnames.sort()
                for filename in filenames:
                    if HEX32.fullmatch(filename) is None or filename in seen:
                        continue
                    path = Path(directory) / filename
                    try:
                        stat_result = path.stat()
                        if stat_result.st_size > MAX_ENTRY_READ_BYTES:
                            continue
                        data = path.read_bytes()
                    except OSError:
                        continue
                    add_entry(filename, data, 'file', sizes.get(filename), stat_result.st_mtime)
                    if len(results) >= limit:
                        break
                if len(results) >= limit:
                    break

        results.sort(key=lambda item: (-item.modified_at, item.entry_id))
        return results

    def database_size(self, entry_id: str) -> int | None:
        record = self._read_database_record(entry_id)
        return int(record['size']) if record and isinstance(record.get('size'), int) else None

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
        """Update exactly one files.size cell for legacy file-backed entries."""
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

    def _update_database_blob(
        self,
        entry_id: str,
        new_data: bytes,
        *,
        expected_old_size: int,
        expected_old_data: bytes,
    ) -> None:
        """Atomically update content and size in the same files row."""
        self._validate_entry_id(entry_id)
        try:
            with contextlib.closing(sqlite3.connect(self.database_path, timeout=5)) as connection:
                connection.execute('PRAGMA busy_timeout = 5000')
                connection.execute('BEGIN IMMEDIATE')
                row = connection.execute(
                    'SELECT size, content FROM files WHERE id = ?',
                    (bytes.fromhex(entry_id),),
                ).fetchone()
                if row is None:
                    raise RobloxCacheError(f'Entry {entry_id} has no row in the files table.')
                old_size, old_data = row
                if old_size != expected_old_size:
                    raise RobloxCacheError(
                        f'Database size for {entry_id} changed; refresh and retry before writing.'
                    )
                if old_data is None or bytes(old_data) != expected_old_data:
                    raise RobloxCacheError(
                        f'Database content for {entry_id} changed; refresh and retry before writing.'
                    )
                cursor = connection.execute(
                    'UPDATE files SET content = ?, size = ? WHERE id = ?',
                    (sqlite3.Binary(new_data), len(new_data), bytes.fromhex(entry_id)),
                )
                if cursor.rowcount != 1:
                    raise RobloxCacheError(
                        f'Expected to update one content row for {entry_id}, updated {cursor.rowcount}.'
                    )
                connection.commit()
        except RobloxCacheError:
            raise
        except sqlite3.Error as exc:
            raise RobloxCacheError(f'Could not update files.content for {entry_id}:\n{exc}') from exc

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
        data, _mode = self._get_entry_data(source_id)
        return data

    def swap(self, target_id: str, source_id: str, *, origin: str = '') -> str:
        """Replace one cache entry in SQLite content BLOBs or legacy files with rollback."""
        self._require_client_closed()
        self.validate()
        self._validate_entry_id(target_id)
        self._validate_entry_id(source_id)
        if target_id == source_id:
            raise RobloxCacheError('Target and source are the same cache entry.')

        target_record = self._read_database_record(target_id)
        target_data, target_mode = self._get_entry_data(target_id, target_record)
        current_db_size = target_record.get('size') if target_record else None
        if not isinstance(current_db_size, int):
            raise RobloxCacheError('Target has no corresponding row in rbx-storage.db.')
        if len(target_data) > MAX_ENTRY_READ_BYTES:
            raise RobloxCacheError('Target entry exceeds the configured safety limit.')
        if target_mode == 'database' and current_db_size != len(target_data):
            raise RobloxCacheError(
                f'The target content BLOB has {len(target_data)} bytes but files.size says {current_db_size}. '
                'Refresh and inspect the database before replacing it.'
            )

        new_data = self._source_bytes(source_id)
        if len(new_data) > MAX_ENTRY_READ_BYTES:
            raise RobloxCacheError('Source entry exceeds the configured safety limit.')
        status = self.swap_status(target_id)
        if self.swap_status(source_id) == 'swapped':
            raise RobloxCacheError('Source is currently swapped itself. Restore its original entry first.')

        backup_path = self._backup_database()
        old_manifest = self._manifest()
        if status != 'swapped':
            self._backup_original(target_id, target_data)

        if target_mode == 'file' and self._entry_path(source_id).is_file():
            self.library_dir.mkdir(parents=True, exist_ok=True)
            library_copy = self.library_dir / source_id
            if not library_copy.exists():
                library_copy.write_bytes(new_data)

        replaced_file = False
        db_write_attempted = False
        try:
            if target_mode == 'database':
                db_write_attempted = True
                self._update_database_blob(
                    target_id,
                    new_data,
                    expected_old_size=current_db_size,
                    expected_old_data=target_data,
                )
            else:
                self._atomic_replace(self._entry_path(target_id), new_data)
                replaced_file = True
                self._update_database_size(target_id, len(new_data), expected_old_size=current_db_size)

            manifest = self._manifest()
            manifest[target_id] = {
                'source': source_id,
                'swapped_size': len(new_data),
                'swapped_sha256': hashlib.sha256(new_data).hexdigest(),
                'original_size': len(target_data),
                'storage_mode': target_mode,
                'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                'origin': origin or f'entry {source_id[:8]}',
                'database_backup': str(backup_path),
            }
            self._save_json(self.manifest_path, manifest)
            self._log_history('swap', target_id, source_id, origin or f'entry {source_id[:8]}')
        except Exception as exc:
            if replaced_file:
                with contextlib.suppress(OSError):
                    self._atomic_replace(self._entry_path(target_id), target_data)
            if db_write_attempted:
                with contextlib.suppress(Exception):
                    self._update_database_blob(
                        target_id,
                        target_data,
                        expected_old_size=len(new_data),
                        expected_old_data=new_data,
                    )
            else:
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
            f'Swapped {target_id[:8]}… ← {source_id[:8]}… ({len(new_data):,} bytes, {target_mode}). '
            'Restart Roblox to test it.'
        )

    def restore(self, target_id: str) -> str:
        """Restore an entry backup and atomically synchronize BLOB content and size where applicable."""
        self._require_client_closed()
        self.validate()
        self._validate_entry_id(target_id)
        manifest = self._manifest()
        record = manifest.get(target_id)
        if not record:
            raise RobloxCacheError('That entry has no active replacement to restore.')

        target_record = self._read_database_record(target_id)
        current_data, current_mode = self._get_entry_data(target_id, target_record)
        current_db_size = target_record.get('size') if target_record else None
        if not isinstance(current_db_size, int):
            raise RobloxCacheError('The target has no corresponding row in rbx-storage.db.')
        backup_path = self._backup_database()
        original_path = self.originals_dir / target_id

        if self.swap_status(target_id) == 'swapped':
            if not original_path.is_file():
                raise RobloxCacheError(
                    f'The original cache entry is missing. Database backup: {backup_path}'
                )
            original_data = original_path.read_bytes()
            if record.get('storage_mode') == 'database':
                self._update_database_blob(
                    target_id,
                    original_data,
                    expected_old_size=current_db_size,
                    expected_old_data=current_data,
                )
            else:
                self._atomic_replace(self._entry_path(target_id), original_data)
                try:
                    self._update_database_size(
                        target_id, len(original_data), expected_old_size=current_db_size
                    )
                except Exception:
                    with contextlib.suppress(OSError):
                        self._atomic_replace(self._entry_path(target_id), current_data)
                    raise
            message = f'Restored {target_id[:8]}… to its saved original ({len(original_data):,} bytes).'
        else:
            if record.get('storage_mode') == 'database':
                self._update_database_blob(
                    target_id,
                    current_data,
                    expected_old_size=current_db_size,
                    expected_old_data=current_data,
                )
            elif current_mode == 'file':
                self._update_database_size(
                    target_id, len(current_data), expected_old_size=current_db_size
                )
            message = (
                f'{target_id[:8]}… changed outside Fleasion; kept the current content '
                'and synchronized its size metadata.'
            )

        manifest.pop(target_id, None)
        self._save_json(self.manifest_path, manifest)
        with contextlib.suppress(OSError):
            original_path.unlink()
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
        """Infer length field positions from database BLOBs and legacy physical cache files."""
        votes: dict[tuple[int, int], int] = {}
        total = 0
        seen: set[str] = set()

        def inspect(data: bytes) -> None:
            nonlocal total
            if total >= sample_limit or len(data) > 1_000_000:
                return
            try:
                _url, payload_start = parse_rbxh_header(data)
            except RobloxCacheError:
                return
            payload_length = len(data) - payload_start
            if payload_length < 256:
                return
            metadata_start = payload_start - RBXH_META_SIZE
            total += 1
            for width, fmt in ((4, '<I'), (8, '<Q')):
                for offset in range(RBXH_META_SIZE - width + 1):
                    if struct.unpack_from(fmt, data, metadata_start + offset)[0] == payload_length:
                        votes[(offset, width)] = votes.get((offset, width), 0) + 1

        for entry_id, record in self._iter_database_records():
            content = record.get('content')
            if isinstance(content, bytes):
                inspect(content)
                seen.add(entry_id)
            else:
                path = self._entry_path(entry_id)
                if path.is_file():
                    try:
                        inspect(path.read_bytes())
                        seen.add(entry_id)
                    except OSError:
                        pass
            if total >= sample_limit:
                break

        if total < sample_limit and self.assets_path.is_dir():
            for directory, dirnames, filenames in os.walk(self.assets_path, followlinks=False):
                dirnames.sort()
                for filename in filenames:
                    if filename in seen or HEX32.fullmatch(filename) is None:
                        continue
                    path = Path(directory) / filename
                    try:
                        inspect(path.read_bytes())
                    except OSError:
                        continue
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
        allow_suspicious_animation: bool = True,
        allow_type_mismatch: bool = False,
    ) -> str:
        """Import a raw asset with preflight, verified header metadata, and read-back checks.

        Animation preflight findings are reported as warnings and the import still
        proceeds; pass ``allow_suspicious_animation=False`` to refuse them instead.
        Detected source/target kind mismatches are refused unless the caller passes
        ``allow_type_mismatch=True`` after the user has confirmed.
        """
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
                'Choose a raw asset such as RBXM/RBXMX, PNG, mesh or audio, or select the source entry in Cache Slots.'
            )

        source_kind = detect_roblox_payload_kind(payload)
        animation_report = (
            inspect_animation_payload(payload)
            if source_kind in {'anim', 'curve-anim'}
            else None
        )
        if (
            animation_report is not None
            and animation_report.requires_override
            and not allow_suspicious_animation
        ):
            raise RobloxCacheError(
                'Animation preflight found suspicious data. No cache bytes were changed. '
                'Review the report, repair/re-export the asset, or explicitly allow the import after review.\n\n'
                + animation_report.format()
            )

        target_data, _target_mode = self._get_entry_data(target_id)
        manifest_record = self._manifest().get(target_id, {})
        saved_original = self.originals_dir / target_id
        template = (
            saved_original.read_bytes()
            if manifest_record and saved_original.is_file()
            else target_data
        )
        _target_url, target_payload, _has_target_header = split_rbxh_entry(template)
        target_kind = detect_roblox_payload_kind(target_payload)
        conversion_notes: tuple[str, ...] = ()
        conversion_warning = ''
        written_animation_report = animation_report

        # Convert animation format and rig before deciding whether a type mismatch remains.
        if source_kind in {'anim', 'curve-anim'} and target_kind in {'anim', 'curve-anim'}:
            try:
                conversion = convert_animation_for_slot(payload, target_payload)
            except AnimationConversionError as exc:
                # Keep the original payload available for the explicitly confirmed fallback path.
                conversion_warning = str(exc)
            else:
                payload = conversion.payload
                conversion_notes = conversion.notes
                if conversion.changed:
                    source_kind = detect_roblox_payload_kind(payload)
                    written_animation_report = inspect_animation_payload(payload) or animation_report
                    if (
                        written_animation_report is not None
                        and written_animation_report.requires_override
                        and not allow_suspicious_animation
                    ):
                        raise RobloxCacheError(
                            'Animation conversion produced payload with suspicious data. '
                            'No cache bytes were changed. Review the converted animation or explicitly '
                            'allow the import after review.\n\n'
                            + written_animation_report.format()
                        )

        if (
            source_kind != target_kind
            and source_kind != 'other'
            and target_kind != 'other'
            and not allow_type_mismatch
        ):
            mismatch_message = (
                f'Import type mismatch: target cache slot contains {target_kind}, '
                f'but the selected file contains {source_kind}. No cache bytes were changed. '
                'Review the mismatch and explicitly allow the import only if you intend to test '
                'the original payload unchanged.'
            )
            if conversion_warning:
                mismatch_message += f'\n\nAnimation conversion warning: {conversion_warning}'
            raise RobloxCacheError(mismatch_message)

        fields = self.detect_payload_length_fields()
        if not fields:
            raise RobloxCacheError(
                'Could not verify the RBXH payload-length fields from real cache entries; import was refused.'
            )
        if self.database_size(target_id) is None:
            raise RobloxCacheError('The target has no corresponding row in rbx-storage.db.')

        wrapped_entry = _rewrite_payload_length_fields(template, payload, fields['fields'])
        expected_url, _wrapped_payload, has_wrapped_header = split_rbxh_entry(wrapped_entry)
        if not has_wrapped_header or _wrapped_payload != payload:
            raise RobloxCacheError(
                'The generated cache entry did not preserve the payload exactly; import was refused.'
            )
        source_id = hashlib.md5(wrapped_entry).hexdigest()

        self.library_dir.mkdir(parents=True, exist_ok=True)
        library_path = self.library_dir / source_id
        if not library_path.exists():
            library_path.write_bytes(wrapped_entry)
        swap_message = self.swap(
            target_id,
            source_id,
            origin=origin or f'file {path.name}',
        )

        # Verify what was really written, rather than treating a completed file write
        # as proof that Roblox can parse or will continue using the asset.
        try:
            stored_data, storage_mode = self._get_entry_data(target_id)
            stored_url, stored_payload, has_header = split_rbxh_entry(stored_data)
            db_size = self.database_size(target_id)
            if not has_header or stored_payload != payload:
                raise RobloxCacheError(
                    'Read-back verification found that the stored payload differs from the selected file.'
                )
            if db_size != len(stored_data):
                raise RobloxCacheError(
                    f'Read-back verification found files.size={db_size}, but the entry has {len(stored_data)} bytes.'
                )
            if stored_url != expected_url:
                raise RobloxCacheError(
                    f'Read-back verification found a changed cache URL: expected {expected_url!r}, got {stored_url!r}.'
                )
        except Exception as exc:
            restore_message = ''
            try:
                restore_message = self.restore(target_id)
            except Exception as restore_exc:
                restore_message = f' Automatic restore also failed: {restore_exc}'
            raise RobloxCacheError(
                'The import write failed its read-back check; the original was restored when possible. '
                f'{exc}{restore_message}'
            ) from exc

        verification = (
            f'Read-back verification: OK; payload {len(stored_payload):,} bytes, '
            f'entry {len(stored_data):,} bytes, files.size matches, storage={storage_mode}, '
            f'URL={stored_url}.'
        )
        if written_animation_report is not None:
            verification += (
                f' Animation preflight (written payload): {written_animation_report.root_class}, '
                f'{written_animation_report.curve_count if written_animation_report.root_class == "CurveAnimation" else written_animation_report.keyframe_count} '
                f'curves/keyframes, estimated duration={written_animation_report.duration_seconds!s} s.'
            )
        if written_animation_report is not None and written_animation_report.findings:
            verification += (
                ' Preflight warnings (imported anyway): '
                + ' '.join(written_animation_report.findings)
            )
        if written_animation_report is not None and written_animation_report.advisories:
            verification += ' Advisory notes: ' + ' '.join(written_animation_report.advisories)
        if animation_report is not None and animation_report is not written_animation_report:
            if animation_report.findings:
                verification += ' Source preflight warnings: ' + ' '.join(animation_report.findings)
            if animation_report.advisories:
                verification += ' Source advisory notes: ' + ' '.join(animation_report.advisories)
        if conversion_notes:
            verification += ' Animation conversion: ' + ' '.join(conversion_notes)
        if conversion_warning:
            verification += (
                ' Animation conversion warning (original payload imported unchanged): '
                + conversion_warning
            )
        verification += (
            ' This verifies the local write only, not runtime playback. After testing, '
            'scan the cache again: “Changed since swap” means the cache entry no longer matches '
            'the imported bytes.'
        )
        return f'{swap_message}\n{verification}'

    def export_entry(self, entry_id: str, destination: str | os.PathLike[str]) -> Path:
        """Export raw cache bytes (from content BLOB or a physical file) and a diagnostic report."""
        self._validate_entry_id(entry_id)
        data, storage_mode = self._get_entry_data(entry_id)
        output_dir = Path(destination).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f'{entry_id}.bin'
        output_path.write_bytes(data)
        url, payload, has_header = split_rbxh_entry(data)
        try:
            db_size = self.database_size(entry_id)
        except RobloxCacheError:
            db_size = None
        physical = self._entry_path(entry_id)
        if physical.is_file():
            modified_text = time.strftime(
                '%Y-%m-%d %H:%M:%S', time.localtime(physical.stat().st_mtime)
            )
        else:
            modified_text = 'stored in SQLite BLOB'
        report = [
            f'Entry ID: {entry_id}',
            f'Storage mode: {storage_mode}',
            f'File/content size: {len(data)}',
            f'Database size: {db_size}',
            f'RBXH header: {"yes" if has_header else "NO"}',
            f'URL: {url}',
            f'Detected kind: {detect_roblox_payload_kind(payload)}',
            f'First 96 bytes: {data[:96].hex(" ")}',
            f'Modified: {modified_text}',
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
                data, _mode = self._get_entry_data(entry.entry_id)
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
