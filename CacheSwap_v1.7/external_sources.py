"""External asset source library for CacheSwap.

Supports importing local files and downloading assets from HTTPS URLs or Roblox
asset IDs. Downloads are size-limited, do not use Roblox/browser login cookies,
and are stored in CacheSwap's own external_sources folder.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from email.message import Message
from pathlib import Path

import core

MAX_SOURCE_BYTES = 100_000_000
DOWNLOAD_TIMEOUT_SECONDS = 25
USER_AGENT = "CacheSwap/1.6 (external asset importer)"
_ASSET_ID = re.compile(r"^(?:rbxassetid://)?(\d+)$", re.IGNORECASE)


class ExternalSourceError(ValueError):
    """An external source could not be imported safely or read correctly."""


@dataclass(frozen=True)
class ExternalSource:
    id: str
    name: str
    stored_name: str
    origin: str
    added: str
    size: int
    kind: str

    @property
    def path(self) -> Path:
        return SourceLibrary.root_dir() / self.stored_name


def _is_safe_https_url(url: str) -> urllib.parse.SplitResult:
    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = parts.port  # force validation of malformed ports
    except ValueError as exc:
        raise ExternalSourceError(f"Invalid URL: {exc}") from exc
    if parts.scheme.lower() != "https":
        raise ExternalSourceError("Only HTTPS URLs are supported. Use a direct HTTPS download URL.")
    if not parts.hostname:
        raise ExternalSourceError("The URL has no hostname.")
    if parts.username is not None or parts.password is not None:
        raise ExternalSourceError("URLs containing usernames or passwords are not accepted.")
    host = parts.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ExternalSourceError("Local-network URLs are not accepted; use a public HTTPS address.")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr and (not addr.is_global):
        raise ExternalSourceError("Private or non-public IP addresses are not accepted as download sources.")
    if port not in (None, 443):
        # Public HTTPS servers can use alternate ports, but this keeps URLs predictable.
        raise ExternalSourceError("Only the standard HTTPS port (443) is supported.")
    return parts


def normalize_source_url(text: str) -> str:
    """Normalize a public asset ID/Roblox asset page/direct HTTPS URL to a download URL."""
    value = (text or "").strip()
    if not value:
        raise ExternalSourceError("Enter a Roblox asset ID or a direct HTTPS URL.")
    m = _ASSET_ID.fullmatch(value)
    if m:
        return f"https://assetdelivery.roblox.com/v1/asset/?id={m.group(1)}"
    if value.lower().startswith("rbxassetid://"):
        m = re.fullmatch(r"rbxassetid://(\d+)", value, re.IGNORECASE)
        if not m:
            raise ExternalSourceError("Invalid rbxassetid URL.")
        return f"https://assetdelivery.roblox.com/v1/asset/?id={m.group(1)}"
    if not re.match(r"^https://", value, re.IGNORECASE):
        raise ExternalSourceError("Enter an asset ID or an HTTPS URL starting with https://.")
    parts = _is_safe_https_url(value)
    host = (parts.hostname or "").lower().rstrip(".")
    is_roblox_host = host == "roblox.com" or host.endswith(".roblox.com")
    if is_roblox_host and host != "assetdelivery.roblox.com":
        q = urllib.parse.parse_qs(parts.query)
        candidate = (q.get("id") or [None])[0]
        if not candidate:
            match = re.search(r"/(?:library|catalog|store/asset|asset)/(\d+)(?:/|$)", parts.path, re.IGNORECASE)
            candidate = match.group(1) if match else None
        if candidate and candidate.isdigit():
            return f"https://assetdelivery.roblox.com/v1/asset/?id={candidate}"
        raise ExternalSourceError(
            "That looks like a Roblox webpage, not a direct asset URL. Paste the numeric asset ID instead."
        )
    return urllib.parse.urlunsplit(("https", parts.netloc, parts.path, parts.query, ""))


class _HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _is_safe_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_filename(name: str) -> str:
    name = Path(str(name or "asset.bin")).name
    name = re.sub(r"[^A-Za-z0-9._ ()-]+", "_", name).strip(" .")
    if not name or name in (".", ".."):
        name = "asset.bin"
    if len(name) > 140:
        stem, suffix = Path(name).stem, Path(name).suffix[:16]
        name = stem[:120] + suffix
    return name


def _guess_extension(data: bytes, url: str, content_type: str = "") -> str:
    if data.startswith(b"<roblox!\x89\xff\r\n\x1a\n"):
        return ".rbxm"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"OggS"):
        return ".ogg"
    if data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return ".mp3"
    if data.startswith(b"\xabKTX 11\xbb\r\n\x1a\n") or data.startswith(b"\xabKTX 20\xbb\r\n\x1a\n"):
        return ".ktx"
    try:
        from urllib.parse import urlsplit
        path_ext = Path(urlsplit(url).path).suffix.lower()
    except Exception:
        path_ext = ""
    if path_ext and re.fullmatch(r"\.[a-z0-9]{1,10}", path_ext):
        return path_ext
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    guessed = mimetypes.guess_extension(mime) if mime else None
    return guessed if guessed and re.fullmatch(r"\.[a-z0-9]{1,10}", guessed) else ".bin"


def _filename_from_response(url: str, headers, data: bytes) -> str:
    disposition = headers.get("Content-Disposition", "") if headers else ""
    msg = Message()
    if disposition:
        msg["content-disposition"] = disposition
        name = msg.get_filename()
        if name:
            return _safe_filename(name)
    url_name = Path(urllib.parse.urlsplit(url).path).name
    if url_name and "." in url_name and len(url_name) <= 140:
        return _safe_filename(url_name)
    ext = _guess_extension(data, url, headers.get("Content-Type", "") if headers else "")
    return "roblox_asset" + ext


def download_source(url_or_asset_id: str) -> tuple[bytes, str, str]:
    """Download an external asset with a bounded streaming read; returns data, name, canonical URL."""
    url = normalize_source_url(url_or_asset_id)
    _is_safe_https_url(url)
    opener = urllib.request.build_opener(_HTTPSOnlyRedirectHandler())
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with opener.open(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            final_url = response.geturl()
            _is_safe_https_url(final_url)
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > MAX_SOURCE_BYTES:
                        raise ExternalSourceError(f"Download exceeds {MAX_SOURCE_BYTES // 1_000_000} MB limit.")
                except ValueError:
                    pass
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_SOURCE_BYTES:
                    raise ExternalSourceError(f"Download exceeds {MAX_SOURCE_BYTES // 1_000_000} MB limit.")
                chunks.append(chunk)
            data = b"".join(chunks)
            content_type = response.headers.get("Content-Type", "")
            if not data:
                raise ExternalSourceError("The server returned an empty file.")
            if "text/html" in content_type.lower() and data.lstrip()[:64].lower().startswith((b"<!doctype html", b"<html")):
                raise ExternalSourceError("The URL returned a web page, not an asset file. Use a direct download URL or asset ID.")
            name = _filename_from_response(final_url, response.headers, data)
            return data, name, final_url
    except ExternalSourceError:
        raise
    except urllib.error.HTTPError as exc:
        raise ExternalSourceError(f"HTTP error {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ExternalSourceError(f"Could not download the source: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ExternalSourceError("The download timed out. Try again or use a smaller/direct file URL.") from exc
    except OSError as exc:
        raise ExternalSourceError(f"Download failed: {exc}") from exc


class SourceLibrary:
    """Persistent library of local/external assets used for preview and import."""

    @staticmethod
    def root_dir() -> Path:
        return core.app_dir() / "external_sources"

    @property
    def metadata_path(self) -> Path:
        return self.root_dir() / "sources.json"

    def __init__(self) -> None:
        self.root_dir().mkdir(parents=True, exist_ok=True)
        self._items: dict[str, ExternalSource] = {}
        self.load_error = ""
        self._load()

    def _load(self) -> None:
        self._items.clear()
        try:
            data = json.loads(self.metadata_path.read_text("utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            # Keep the original file for recovery rather than failing the whole application.
            self.load_error = f"Could not read external source index: {self.metadata_path} ({exc})"
            return
        for row in data if isinstance(data, list) else []:
            try:
                item = ExternalSource(
                    id=str(row["id"]), name=_safe_filename(row["name"]),
                    stored_name=_safe_filename(row["stored_name"]), origin=str(row["origin"]),
                    added=str(row["added"]), size=int(row["size"]), kind=str(row.get("kind", "other")),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if re.fullmatch(r"[0-9a-f]{64}", item.id) and item.path.is_file():
                self._items[item.id] = item

    def _save(self) -> None:
        self.root_dir().mkdir(parents=True, exist_ok=True)
        tmp = self.metadata_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps([asdict(x) for x in self.list()], indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(self.metadata_path)

    def list(self) -> list[ExternalSource]:
        return sorted(self._items.values(), key=lambda x: x.added, reverse=True)

    def get(self, ident: str) -> ExternalSource | None:
        return self._items.get(ident)

    def add_file(self, path: str | Path) -> ExternalSource:
        src = Path(path).expanduser().resolve()
        if not src.is_file():
            raise ExternalSourceError("Choose an existing file.")
        if src.stat().st_size == 0:
            raise ExternalSourceError("The selected file is empty.")
        if src.stat().st_size > MAX_SOURCE_BYTES:
            raise ExternalSourceError(f"Files larger than {MAX_SOURCE_BYTES // 1_000_000} MB are not supported.")
        try:
            data = src.read_bytes()
        except OSError as exc:
            raise ExternalSourceError(f"Could not read the file: {exc}") from exc
        return self.add_bytes(data, src.name, f"Local file: {src}")

    def add_url(self, url_or_asset_id: str) -> ExternalSource:
        data, name, final_url = download_source(url_or_asset_id)
        return self.add_bytes(data, name, final_url)

    def add_bytes(self, data: bytes, name: str, origin: str) -> ExternalSource:
        if not data:
            raise ExternalSourceError("The source is empty.")
        if len(data) > MAX_SOURCE_BYTES:
            raise ExternalSourceError(f"Files larger than {MAX_SOURCE_BYTES // 1_000_000} MB are not supported.")
        digest = hashlib.sha256(data).hexdigest()
        safe_name = _safe_filename(name)
        ext = Path(safe_name).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", ext or ""):
            ext = _guess_extension(data, origin)
            safe_name = Path(safe_name).stem + ext
        previous = self._items.get(digest)
        # If the same bytes were previously imported under another extension, retain the
        # existing storage filename instead of creating an orphan duplicate on disk.
        stored_name = previous.stored_name if previous else digest + ext
        dest = self.root_dir() / stored_name
        if not dest.exists():
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(dest)
        try:
            kind = core.kind_of(data)
        except Exception:
            kind = "other"
        if previous is None:
            item = ExternalSource(
                id=digest, name=safe_name, stored_name=stored_name,
                origin=str(origin)[:2048], added=time.strftime("%Y-%m-%d %H:%M:%S"),
                size=len(data), kind=kind,
            )
        else:
            # Deduplicate identical bytes but retain a useful local name/origin for the visible row.
            item = ExternalSource(digest, safe_name, stored_name, str(origin)[:2048], previous.added, len(data), kind)
        self._items[digest] = item
        self._save()
        return item

    def remove(self, ident: str) -> bool:
        item = self._items.pop(ident, None)
        if item is None:
            return False
        self._save()
        if not any(other.stored_name == item.stored_name for other in self._items.values()):
            try:
                item.path.unlink(missing_ok=True)
            except OSError:
                pass
        return True
