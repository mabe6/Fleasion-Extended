"""Roblox asset metadata lookup for local cache entries and numeric asset IDs."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from .roblox_auth import get_roblosecurity


_ASSET_ID_RE = re.compile(
    r'(?:[?&](?:id|assetId)=|/assetId/|/library/|/assets/)([1-9][0-9]{0,19})',
    re.IGNORECASE,
)
_CDN_HASH_RE = re.compile(r'/(?:v\d+/)?sc\d/([0-9a-f]{32})(?:[/?]|$)', re.IGNORECASE)
_CACHE_HEADER_RE = re.compile(
    rb'(?im)^\s*(?:roblox-)?(assetid|assetversionnumber|assettypeid|asset-recordable|assetname|creatorid)\s*:\s*([^\r\n]+)'
)
_HTTP_METADATA_HEADER_RE = re.compile(
    rb'(?im)^\s*([a-z][a-z0-9-]{0,79})\s*:\s*([^\r\n]{1,512})\s*$'
)
_ASSET_TYPE_NAMES = {
    1: 'Image',
    2: 'TShirt',
    3: 'Audio',
    4: 'Mesh',
    5: 'Lua',
    8: 'Hat',
    9: 'Place',
    10: 'Model',
    11: 'Shirt',
    12: 'Pants',
    13: 'Decal',
    17: 'Head',
    18: 'Face',
    19: 'Gear',
    21: 'Badge',
    24: 'Animation',
    32: 'Package',
    34: 'GamePass',
    38: 'Plugin',
    40: 'MeshPart',
    41: 'HairAccessory',
    42: 'FaceAccessory',
    43: 'NeckAccessory',
    44: 'ShoulderAccessory',
    45: 'FrontAccessory',
    46: 'BackAccessory',
    47: 'WaistAccessory',
    48: 'ClimbAnimation',
    49: 'DeathAnimation',
    50: 'FallAnimation',
    51: 'IdleAnimation',
    52: 'JumpAnimation',
    53: 'RunAnimation',
    54: 'SwimAnimation',
    55: 'WalkAnimation',
    56: 'PoseAnimation',
    61: 'EmoteAnimation',
    62: 'Video',
    73: 'FontFamily',
    79: 'DynamicHead',
}
_USER_AGENT = 'Fleasion/2.x (Roblox cache asset lookup)'
_DEVELOP_ASSETS_URL = 'https://develop.roblox.com/v1/assets'
_CATALOG_DETAILS_URL = 'https://catalog.roblox.com/v1/catalog/items/details'
_ASSET_DELIVERY_URL = 'https://assetdelivery.roblox.com/v2/assetId/{asset_id}'


class AssetLookupError(RuntimeError):
    """A readable error returned by the Roblox asset lookup helpers."""


@dataclass(frozen=True, slots=True)
class AssetDownloadResult:
    """Details about a completed streamed asset download."""

    path: Path
    size: int
    sha256: str
    content_type: str


def normalize_asset_id(value: str) -> str:
    """Validate and normalize a numeric Roblox asset identifier."""
    asset_id = str(value).strip()
    if not re.fullmatch(r'[1-9][0-9]{0,19}', asset_id):
        raise AssetLookupError('Enter a numeric Roblox Asset ID (digits only).')
    return asset_id


def _new_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            'User-Agent': _USER_AGENT,
            'Accept': 'application/json',
            'Referer': 'https://www.roblox.com/',
        }
    )
    try:
        cookie = get_roblosecurity()
    except Exception:
        cookie = None
    if cookie:
        session.cookies.set('.ROBLOSECURITY', cookie, domain='.roblox.com')
    return session


def _find_asset_record(body: Any, asset_id: str) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        return None
    records = body.get('data')
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        return None
    for record in records:
        if isinstance(record, dict) and str(record.get('id') or '') == asset_id:
            return record
    return None


def _metadata_text(value: Any) -> str:
    return '' if value is None else str(value).strip()


def _metadata_from_develop(body: Any, asset_id: str) -> dict[str, str]:
    record = _find_asset_record(body, asset_id)
    if record is None:
        return {}
    creator = record.get('creator')
    creator = creator if isinstance(creator, dict) else {}
    return {
        'name': _metadata_text(record.get('name')),
        'description': _metadata_text(record.get('description')),
        'creator_name': _metadata_text(
            creator.get('name') or creator.get('displayName') or record.get('creatorName')
        ),
        'creator_type': _metadata_text(creator.get('type') or record.get('creatorType')),
        'creator_id': _metadata_text(creator.get('id') or record.get('creatorTargetId')),
        'created_at': _metadata_text(record.get('created')),
        'updated_at': _metadata_text(record.get('updated')),
        'asset_type': _metadata_text(record.get('type') or record.get('assetType')),
    }


def _metadata_from_catalog(body: Any, asset_id: str) -> dict[str, str]:
    record = _find_asset_record(body, asset_id)
    if record is None:
        return {}
    return {
        'name': _metadata_text(record.get('name')),
        'description': _metadata_text(record.get('description')),
        'creator_name': _metadata_text(record.get('creatorName')),
        'creator_type': _metadata_text(record.get('creatorType')),
        'creator_id': _metadata_text(record.get('creatorTargetId')),
        'asset_type': _metadata_text(record.get('assetType')),
        'price': _metadata_text(record.get('price')),
        'price_status': _metadata_text(record.get('priceStatus')),
        'favorite_count': _metadata_text(record.get('favoriteCount')),
    }


def lookup_asset_metadata(
    value: str,
    *,
    session: requests.Session | None = None,
) -> dict[str, str]:
    """Look up a numeric asset ID through public Roblox developer and catalog endpoints."""
    asset_id = normalize_asset_id(value)
    client = session or _new_session()
    owns_session = session is None
    metadata: dict[str, str] = {}
    sources: list[str] = []
    errors: list[str] = []

    try:
        try:
            response = client.get(
                _DEVELOP_ASSETS_URL,
                params={'assetIds': asset_id},
                timeout=8,
            )
            response.raise_for_status()
            developed = _metadata_from_develop(response.json(), asset_id)
            metadata.update({key: value for key, value in developed.items() if value})
            if developed:
                sources.append('Roblox developer metadata')
        except (requests.RequestException, ValueError, TypeError) as exc:
            errors.append(f'developer metadata: {type(exc).__name__}')

        try:
            response = client.post(
                _CATALOG_DETAILS_URL,
                json={'items': [{'itemType': 'Asset', 'id': int(asset_id)}]},
                timeout=8,
            )
            response.raise_for_status()
            catalog = _metadata_from_catalog(response.json(), asset_id)
            for key, value in catalog.items():
                if value:
                    metadata[key] = value
            if catalog:
                sources.append('Roblox catalog')
        except (requests.RequestException, ValueError, TypeError) as exc:
            errors.append(f'catalog metadata: {type(exc).__name__}')
    finally:
        if owns_session:
            client.close()

    if not metadata:
        detail = f" ({'; '.join(errors)})" if errors else ''
        raise AssetLookupError(
            f'Roblox did not return public metadata for asset {asset_id}. '
            'The ID may be invalid, private, moderated, deleted, or not represented in the catalog.'
            + detail
        )

    metadata['asset_id'] = asset_id
    metadata['asset_url'] = f'https://www.roblox.com/library/{asset_id}'
    metadata['source'] = ', '.join(dict.fromkeys(sources)) or 'Roblox'
    return metadata


def _trusted_asset_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or '').casefold()
    return parsed.scheme == 'https' and (
        host == 'roblox.com'
        or host.endswith('.roblox.com')
        or host == 'rbxcdn.com'
        or host.endswith('.rbxcdn.com')
    )


def _follow_cdn_redirects(
    client: requests.Session,
    url: str,
    *,
    method: str,
    headers: dict[str, str] | None = None,
    stream: bool = False,
    timeout: tuple[int, int] = (6, 20),
) -> requests.Response:
    if not _trusted_asset_url(url):
        raise AssetLookupError('The cache URL is not hosted by Roblox; request blocked.')
    current_url = url
    current_headers = dict(headers or {})
    for _attempt in range(5):
        request = client.head if method == 'HEAD' else client.get
        response = request(
            current_url,
            headers=current_headers,
            timeout=timeout,
            stream=stream,
            allow_redirects=False,
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        redirect = urljoin(current_url, response.headers.get('Location', ''))
        response.close()
        if not redirect or not _trusted_asset_url(redirect):
            raise AssetLookupError('Roblox redirected the request to an unexpected host.')
        current_url = redirect
        current_headers = {'User-Agent': _USER_AGENT}
    raise AssetLookupError('Roblox returned too many redirects.')


def _asset_id_from_text(value: str) -> str:
    match = _ASSET_ID_RE.search(value)
    return match.group(1) if match else ''


def _asset_type_label(value: str) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ''
    name = _ASSET_TYPE_NAMES.get(number)
    return f'{name} (ID {number})' if name else f'Unknown type (ID {number})'


def _cache_header_metadata(payload: bytes) -> dict[str, str]:
    """Read Roblox asset headers and useful surrounding CDN response headers."""
    metadata: dict[str, str] = {}
    sample = payload[:65536]
    for raw_key, raw_value in _CACHE_HEADER_RE.findall(sample):
        key = raw_key.decode('ascii', errors='ignore').casefold().replace('-', '_')
        value = raw_value.decode('ascii', errors='ignore').strip()
        if key == 'assetid' and value.isdigit():
            metadata['asset_id'] = value
        elif key == 'assetversionnumber' and value.isdigit():
            metadata['asset_version'] = value
        elif key == 'assettypeid' and value.isdigit():
            metadata['asset_type_id'] = value
            metadata['asset_type_label'] = _asset_type_label(value)
        elif key == 'asset_recordable':
            metadata['asset_recordable'] = value
        elif key == 'assetname':
            metadata['name'] = value
        elif key == 'creatorid' and value.isdigit():
            metadata['creator_id'] = value

    header_names = {
        'content-type': 'cdn_content_type',
        'content-encoding': 'cdn_content_encoding',
        'content-length': 'cdn_content_length',
        'last-modified': 'cdn_last_modified',
        'x-rbx-uncompressed-size-bytes': 'uncompressed_size',
        'x-rbx-zstd-dict-sha': 'zstd_dictionary_sha256',
        'cache-control': 'cache_control',
        'etag': 'etag',
        'date': 'response_date',
        'server': 'cdn_server',
    }
    for raw_key, raw_value in _HTTP_METADATA_HEADER_RE.findall(sample):
        key = raw_key.decode('ascii', errors='ignore').casefold()
        target = header_names.get(key)
        if target and target not in metadata:
            metadata[target] = raw_value.decode('latin-1', errors='replace').strip()
    return metadata


def format_asset_metadata_preview(
    payload: bytes,
    *,
    asset_id: str = "",
    resolved_metadata: dict[str, Any] | None = None,
    source_url: str = "",
) -> str:
    """Render embedded asset metadata plus already-resolved public/CDN fields."""
    metadata = dict(resolved_metadata or {})
    # Embedded response headers are the most direct evidence for the cache entry.
    metadata.update({
        key: value for key, value in _cache_header_metadata(payload).items() if value
    })
    lines = ["Roblox asset metadata", ""]

    asset_type_id = str(metadata.get("asset_type_id") or "").strip()
    asset_type = str(metadata.get("asset_type_label") or "").strip()
    if not asset_type and asset_type_id:
        asset_type = _asset_type_label(asset_type_id)
    if asset_type:
        lines.append(f"Asset type: {asset_type}")
    elif metadata.get("asset_type"):
        lines.append(f"Asset type: {metadata['asset_type']}")
    else:
        lines.append("Asset type: not present in this record")

    numeric_id = str(metadata.get("asset_id") or asset_id or "").strip()
    lines.append(f"Roblox Asset ID: {numeric_id or 'not present in this record'}")

    for key, label in (
        ("name", "Name"),
        ("creator_name", "Creator"),
        ("creator_type", "Creator type"),
        ("creator_id", "Creator ID"),
        ("asset_version", "Asset version"),
        ("created_at", "Created"),
        ("updated_at", "Updated"),
        ("price", "Price"),
        ("price_status", "Price status"),
        ("favorite_count", "Favorites"),
        ("asset_recordable", "Recordable"),
    ):
        value = str(metadata.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")

    lines.append("")
    encoding = str(metadata.get("cdn_content_encoding") or "").strip()
    content_type = str(metadata.get("cdn_content_type") or "").strip()
    if content_type:
        lines.append(f"Response content type: {content_type}")
    if encoding:
        lines.append(f"Response content encoding: {encoding}")
        lowered_encoding = encoding.casefold()
        if "dcz" in lowered_encoding:
            lines.append(
                "Compression: Roblox DCZ / dictionary-based Zstandard. "
                "The matching dictionary is required for full decompression."
            )
        elif "zstd" in lowered_encoding:
            lines.append("Compression: Zstandard (Zstd).")
        elif "gzip" in lowered_encoding:
            lines.append("Compression: GZip.")
    for key, label in (
        ("cdn_content_length", "Compressed response size"),
        ("uncompressed_size", "Uncompressed size"),
        ("cdn_last_modified", "Last modified"),
        ("etag", "ETag"),
        ("zstd_dictionary_sha256", "Zstandard dictionary SHA-256"),
        ("cdn_probe_status", "CDN metadata probe"),
    ):
        value = str(metadata.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")

    display_url = ""
    if source_url:
        parsed = urlparse(source_url)
        display_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if display_url:
            lines.append(f"CDN resource: {display_url}")
    cdn_hash = str(metadata.get("cdn_hash") or "").strip()
    if not cdn_hash and source_url:
        match = _CDN_HASH_RE.search(source_url)
        cdn_hash = match.group(1).casefold() if match else ""
    if cdn_hash:
        lines.append(f"CDN content hash: {cdn_hash}")
    if metadata.get("cdn_host"):
        lines.append(f"CDN host: {metadata['cdn_host']}")
    if metadata.get("cdn_url_expires_at"):
        expiry_text = str(metadata["cdn_url_expires_at"])
        try:
            expiry = datetime.fromisoformat(expiry_text.replace("Z", "+00:00"))
            expired = expiry <= datetime.now(timezone.utc)
        except (TypeError, ValueError):
            expired = False
        lines.append(
            f"Stored CDN URL: {'expired' if expired else 'expires'} at {expiry_text}"
        )
    if metadata.get("cache_payload_size"):
        lines.append(f"Cached response record size: {metadata['cache_payload_size']} bytes")

    description = str(metadata.get("description") or "").strip()
    if description:
        lines.extend(("", "Description:", description[:2000]))

    source = str(metadata.get("source") or "").strip()
    if source:
        lines.append(f"Metadata source: {source}")
    note = str(metadata.get("metadata_lookup_note") or metadata.get("cdn_probe_note") or "").strip()
    if note:
        lines.append(f"Lookup note: {note}")

    lines.extend([
        "",
        "This cache entry contains metadata only (response headers/status), not the asset's geometry or image data.",
        "For a model or mesh preview, open the matching RBXM, XML or Mesh cache slot.",
    ])
    if not numeric_id:
        lines.append(
            "The 32-character local cache hash or CDN path hash cannot be converted back into "
            "an Asset ID by itself. Roblox's reverse-delivery lookup requires a separate MAR "
            "checksum, which is not present in this cache record."
        )
    return "\n".join(lines)


def _expiry_from_url(url: str) -> str:
    match = re.search(r'(?:[?&]|~)Expires=(\d+)|(?:[?&]|~)exp=(\d+)', url, re.IGNORECASE)
    if match is None:
        return ''
    timestamp = next((value for value in match.groups() if value), '')
    try:
        return datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return ''


def _probe_cached_url(
    client: requests.Session,
    url: str,
) -> dict[str, str]:
    """Read asset-identifying HTTP response headers without downloading the body."""
    if not url:
        return {}
    response: requests.Response | None = None
    try:
        response = _follow_cdn_redirects(
            client,
            url,
            method='HEAD',
            headers={'User-Agent': _USER_AGENT},
        )
        if response.status_code == 405:
            response.close()
            response = _follow_cdn_redirects(
                client,
                url,
                method='GET',
                headers={'User-Agent': _USER_AGENT, 'Range': 'bytes=0-0'},
                stream=True,
            )
        if response.status_code in {401, 403, 404, 410}:
            return {
                'cdn_probe_status': f'HTTP {response.status_code}',
                'cdn_probe_note': (
                    'The stored signed CDN URL is expired or no longer accessible.'
                    if response.status_code in {401, 403, 410}
                    else 'The cached CDN URL no longer resolves.'
                ),
            }
        response.raise_for_status()
        headers = {str(key).casefold(): str(value) for key, value in response.headers.items()}
        result: dict[str, str] = {}
        header_map = {
            'roblox-assetid': 'asset_id',
            'roblox-assetversionnumber': 'asset_version',
            'roblox-assettypeid': 'asset_type_id',
            'x-rbx-uncompressed-size-bytes': 'uncompressed_size',
            'content-type': 'cdn_content_type',
            'content-length': 'cdn_content_length',
            'last-modified': 'cdn_last_modified',
        }
        for source, target in header_map.items():
            value = headers.get(source, '').strip()
            if value:
                result[target] = value
        if result.get('asset_type_id'):
            result['asset_type_label'] = _asset_type_label(result['asset_type_id'])
        result['cdn_probe_status'] = f'HTTP {response.status_code}'
        result['cdn_probe_note'] = 'Asset metadata read from the Roblox CDN response headers.'
        return result
    except (requests.RequestException, AssetLookupError) as exc:
        return {
            'cdn_probe_status': 'Unavailable',
            'cdn_probe_note': f'CDN header lookup failed: {type(exc).__name__}',
        }
    finally:
        if response is not None:
            response.close()


def inspect_cache_entry(
    entry_id: str,
    store: Any,
    *,
    session: requests.Session | None = None,
) -> dict[str, str]:
    """Inspect one selected cache slot for embedded metadata and CDN response headers."""
    client = session or _new_session()
    owns_session = session is None
    metadata: dict[str, str] = {}
    source_url = ''
    try:
        with tempfile.TemporaryDirectory(prefix='fleasion-cache-asset-info-') as folder:
            raw_path = Path(folder) / 'payload.raw'
            source_url, raw_size = store.copy_entry_payload_to_file(entry_id, raw_path)
            metadata['cache_entry_id'] = entry_id
            metadata['cache_payload_size'] = str(raw_size)
            if source_url:
                match = _CDN_HASH_RE.search(source_url)
                if match:
                    metadata['cdn_hash'] = match.group(1).casefold()
                metadata['cdn_host'] = (urlparse(source_url).hostname or '').casefold()
                expiry = _expiry_from_url(source_url)
                if expiry:
                    metadata['cdn_url_expires_at'] = expiry
            with raw_path.open('rb') as handle:
                raw_prefix = handle.read(65536)
            from ..database.roblox_cache_store import decompress_roblox_payload_prefix

            decoded_prefix = decompress_roblox_payload_prefix(
                raw_prefix,
                max_output_bytes=65536,
            )
            metadata.update(_cache_header_metadata(decoded_prefix))

        if source_url:
            probed = _probe_cached_url(client, source_url)
            for key, value in probed.items():
                if value and (not metadata.get(key) or key in {'asset_id', 'asset_version', 'asset_type_id'}):
                    metadata[key] = value

        asset_id = metadata.get('asset_id') or _asset_id_from_text(source_url)
        if asset_id:
            metadata['asset_id'] = asset_id
            try:
                resolved = lookup_asset_metadata(asset_id, session=client)
                metadata.update(resolved)
            except AssetLookupError as exc:
                metadata['metadata_lookup_note'] = str(exc)
        else:
            metadata['metadata_lookup_note'] = (
                'This cache slot exposes a CDN content hash but no numeric Asset ID. '
                'Roblox metadata APIs require an Asset ID; the hash alone is not a reversible ID.'
            )

        metadata['source_url'] = source_url
        metadata['source'] = 'Local cache payload and Roblox CDN headers'
        return metadata
    except Exception as exc:
        if isinstance(exc, AssetLookupError):
            raise
        raise AssetLookupError(
            f'Could not inspect cache entry {entry_id}: {type(exc).__name__}: {exc}'
        ) from exc
    finally:
        if owns_session:
            client.close()


def _extract_asset_location(response: requests.Response) -> str:
    content_type = response.headers.get('Content-Type', '').casefold()
    content_length = response.headers.get('Content-Length', '')
    if 'json' not in content_type:
        return ''
    try:
        if content_length and int(content_length) > 1_000_000:
            return ''
        body = response.json()
    except (ValueError, TypeError):
        return ''
    if not isinstance(body, dict):
        return ''
    locations = body.get('locations')
    if isinstance(locations, list):
        for entry in locations:
            if isinstance(entry, dict) and entry.get('location'):
                return str(entry['location'])
    location = body.get('location')
    return str(location) if location else ''


def _get_asset_response(
    session: requests.Session,
    url: str,
    headers: dict[str, str],
) -> requests.Response:
    return _follow_cdn_redirects(
        session,
        url,
        method='GET',
        headers=headers,
        stream=True,
        timeout=(10, 60),
    )


def _stream_response_to_file(
    response: requests.Response,
    target: Path,
) -> AssetDownloadResult:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{target.name}.',
        suffix='.part',
        dir=str(target.parent),
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        digest = hashlib.sha256()
        size = 0
        with temporary_path.open('wb') as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if size == 0:
            raise AssetLookupError('Roblox returned an empty asset; no file was saved.')
        temporary_path.replace(target)
        return AssetDownloadResult(
            path=target,
            size=size,
            sha256=digest.hexdigest(),
            content_type=response.headers.get('Content-Type', 'application/octet-stream'),
        )
    finally:
        temporary_path.unlink(missing_ok=True)


def _download_response(
    response: requests.Response,
    destination: str | Path,
) -> AssetDownloadResult:
    target = Path(destination).expanduser()
    if target.exists() and target.is_dir():
        raise AssetLookupError('Choose a file name, not a directory, for the downloaded asset.')
    if not _trusted_asset_url(response.url):
        raise AssetLookupError('Roblox redirected the download to an unexpected host.')
    target.parent.mkdir(parents=True, exist_ok=True)
    return _stream_response_to_file(response, target)


def download_asset(
    value: str,
    destination: str | Path,
    *,
    session: requests.Session | None = None,
) -> AssetDownloadResult:
    """Download an asset using normal Roblox Asset Delivery, without an Open Cloud key."""
    asset_id = normalize_asset_id(value)
    client = session or _new_session()
    owns_session = session is None
    response: requests.Response | None = None
    try:
        response = _get_asset_response(
            client,
            _ASSET_DELIVERY_URL.format(asset_id=asset_id),
            {'User-Agent': _USER_AGENT},
        )
        if response.status_code in {401, 403}:
            raise AssetLookupError(
                'Roblox denied access to this asset. It may be private, restricted, or unavailable '
                'through normal Asset Delivery.'
            )
        response.raise_for_status()
        location = _extract_asset_location(response)
        if location:
            response.close()
            response = _get_asset_response(
                client,
                location,
                {'User-Agent': _USER_AGENT},
            )
            response.raise_for_status()
        return _download_response(response, destination)
    except AssetLookupError:
        raise
    except requests.RequestException as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        if status in {401, 403}:
            raise AssetLookupError(
                'Roblox denied access to this asset. It may be private or restricted.'
            ) from exc
        raise AssetLookupError(
            f'Roblox asset download failed: {type(exc).__name__}: {exc}'
        ) from exc
    finally:
        if response is not None:
            response.close()
        if owns_session:
            client.close()


def download_cached_asset(
    url: str,
    destination: str | Path,
    *,
    session: requests.Session | None = None,
) -> AssetDownloadResult:
    """Download a cached asset through its original signed Roblox CDN URL."""
    client = session or _new_session()
    owns_session = session is None
    response: requests.Response | None = None
    try:
        response = _get_asset_response(
            client,
            url,
            {'User-Agent': _USER_AGENT},
        )
        if response.status_code in {401, 403, 410}:
            raise AssetLookupError(
                'The cached CDN link has expired. Fleasion cannot renew a hash-only signed URL '
                'without a numeric Asset ID.'
            )
        response.raise_for_status()
        return _download_response(response, destination)
    except AssetLookupError:
        raise
    except requests.RequestException as exc:
        raise AssetLookupError(
            f'Cached CDN download failed: {type(exc).__name__}: {exc}'
        ) from exc
    finally:
        if response is not None:
            response.close()
        if owns_session:
            client.close()
