"""Roblox asset metadata lookup and streamed asset downloads."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from .roblox_auth import get_roblosecurity


_ASSET_ID_RE = re.compile(r'^[1-9][0-9]{0,19}$')
_USER_AGENT = 'Fleasion/2.x (Roblox asset lookup)'
_DEVELOP_ASSETS_URL = 'https://develop.roblox.com/v1/assets'
_CATALOG_DETAILS_URL = 'https://catalog.roblox.com/v1/catalog/items/details'
_ASSET_DELIVERY_URL = 'https://assetdelivery.roblox.com/v2/assetId/{asset_id}'
_OPEN_CLOUD_DELIVERY_URL = 'https://apis.roblox.com/asset-delivery-api/v1/assetId/{asset_id}'


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
    if not _ASSET_ID_RE.fullmatch(asset_id):
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
        if not isinstance(record, dict):
            continue
        if str(record.get('id') or '') == asset_id:
            return record
    return None


def _metadata_text(value: Any) -> str:
    if value is None:
        return ''
    return str(value).strip()


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
    """Look up an asset through Roblox's developer and catalog metadata APIs."""
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
            if developed:
                metadata.update({key: value for key, value in developed.items() if value})
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
            if catalog:
                for key, value in catalog.items():
                    if value:
                        metadata[key] = value
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


def download_asset(
    value: str,
    destination: str | Path,
    *,
    api_key: str = '',
    session: requests.Session | None = None,
) -> AssetDownloadResult:
    """Download an asset in chunks, preserving the original bytes on disk."""
    asset_id = normalize_asset_id(value)
    target = Path(destination).expanduser()
    if target.exists() and target.is_dir():
        raise AssetLookupError('Choose a file name, not a directory, for the downloaded asset.')
    target.parent.mkdir(parents=True, exist_ok=True)

    client = session or _new_session()
    owns_session = session is None
    endpoint = (
        _OPEN_CLOUD_DELIVERY_URL.format(asset_id=asset_id)
        if api_key.strip()
        else _ASSET_DELIVERY_URL.format(asset_id=asset_id)
    )
    headers = {'x-api-key': api_key.strip()} if api_key.strip() else {}

    response: requests.Response | None = None
    temporary_path: Path | None = None
    try:
        response = client.get(
            endpoint,
            headers=headers,
            timeout=(10, 60),
            stream=True,
            allow_redirects=True,
        )
        if response.status_code in {401, 403}:
            raise AssetLookupError(
                'Roblox denied access to this asset. It may be private or restricted. '
                'Try an Open Cloud API key with Asset Delivery read permission.'
            )
        response.raise_for_status()

        location = _extract_asset_location(response)
        if location:
            response.close()
            if not _trusted_asset_url(location):
                raise AssetLookupError('Roblox returned an unexpected download host; download stopped.')
            response = client.get(
                location,
                headers={'User-Agent': _USER_AGENT},
                timeout=(10, 60),
                stream=True,
                allow_redirects=True,
            )
            response.raise_for_status()

        if not _trusted_asset_url(response.url):
            raise AssetLookupError('Roblox redirected the download to an unexpected host; download stopped.')

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f'.{target.name}.',
            suffix='.part',
            dir=str(target.parent),
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
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
        temporary_path = None
        return AssetDownloadResult(
            path=target,
            size=size,
            sha256=digest.hexdigest(),
            content_type=response.headers.get('Content-Type', 'application/octet-stream'),
        )
    except AssetLookupError:
        raise
    except requests.RequestException as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        if status in {401, 403}:
            raise AssetLookupError(
                'Roblox denied access to this asset. It may be private or restricted. '
                'Try an Open Cloud API key with Asset Delivery read permission.'
            ) from exc
        raise AssetLookupError(f'Roblox asset download failed: {type(exc).__name__}: {exc}') from exc
    except OSError as exc:
        raise AssetLookupError(f'Could not save the asset: {exc}') from exc
    finally:
        if response is not None:
            response.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        if owns_session:
            client.close()
