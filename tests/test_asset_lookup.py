"""Tests for Roblox asset lookup and download helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import pytest
import requests

from fleasion.utils.asset_lookup import (
    AssetLookupError,
    download_asset,
    lookup_asset_metadata,
    normalize_asset_id,
)


class _FakeResponse:
    def __init__(
        self,
        body: dict[str, Any] | None = None,
        *,
        chunks: tuple[bytes, ...] = (),
        url: str = 'https://assetdelivery.roblox.com/v2/assetId/12345',
        headers: dict[str, str] | None = None,
        status_code: int = 200,
    ):
        self._body = body or {}
        self._chunks = chunks
        self.url = url
        self.headers = headers or {'Content-Type': 'application/octet-stream'}
        self.status_code = status_code
        self.closed = False

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 1024 * 1024):
        del chunk_size
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


class _FakeSession:
    def __init__(self, *, get_responses: list[_FakeResponse], post_response: _FakeResponse):
        self.get_responses = list(get_responses)
        self.post_response = post_response
        self.get_calls: list[tuple[str, dict[str, Any]]] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.get_calls.append((url, kwargs))
        return self.get_responses.pop(0)

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.post_calls.append((url, kwargs))
        return self.post_response


def test_normalize_asset_id_accepts_numeric_ids_and_rejects_urls() -> None:
    assert normalize_asset_id(' 123456789 ') == '123456789'
    for value in ('', '0', '-12', '123abc', 'https://www.roblox.com/library/123'):
        with pytest.raises(AssetLookupError):
            normalize_asset_id(value)


def test_lookup_merges_developer_and_catalog_metadata() -> None:
    session = _FakeSession(
        get_responses=[
            _FakeResponse(
                {
                    'data': [
                        {
                            'id': 12345,
                            'name': 'Example model',
                            'created': '2024-01-01T00:00:00Z',
                            'creator': {'name': 'Example creator', 'id': 42, 'type': 'User'},
                        }
                    ]
                }
            )
        ],
        post_response=_FakeResponse(
            {
                'data': [
                    {
                        'id': 12345,
                        'itemType': 'Asset',
                        'assetType': 10,
                        'description': 'A model for testing.',
                        'creatorName': 'Example creator',
                        'creatorTargetId': 42,
                        'creatorType': 'User',
                        'price': 0,
                        'priceStatus': 'Free',
                    }
                ]
            }
        ),
    )
    metadata = lookup_asset_metadata('12345', session=cast(requests.Session, session))

    assert metadata['name'] == 'Example model'
    assert metadata['description'] == 'A model for testing.'
    assert metadata['creator_name'] == 'Example creator'
    assert metadata['creator_id'] == '42'
    assert metadata['asset_type'] == '10'
    assert metadata['price_status'] == 'Free'
    assert 'Roblox developer metadata' in metadata['source']
    assert 'Roblox catalog' in metadata['source']
    assert len(session.get_calls) == 1
    assert session.post_calls[0][1]['json']['items'][0]['id'] == 12345


def test_lookup_reports_when_roblox_has_no_public_metadata() -> None:
    session = _FakeSession(
        get_responses=[_FakeResponse({'data': []})],
        post_response=_FakeResponse({'data': []}),
    )
    with pytest.raises(AssetLookupError, match='did not return public metadata'):
        lookup_asset_metadata('12345', session=cast(requests.Session, session))


def test_download_streams_original_bytes_and_returns_sha256(tmp_path: Path) -> None:
    payload = (b'Roblox asset payload\x00' * 200)
    response = _FakeResponse(
        chunks=(payload[:300], payload[300:]),
        headers={'Content-Type': 'application/octet-stream'},
    )
    session = _FakeSession(get_responses=[response], post_response=_FakeResponse())
    destination = tmp_path / 'asset_12345.bin'

    result = download_asset('12345', destination, session=cast(requests.Session, session))

    assert destination.read_bytes() == payload
    assert result.size == len(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.path == destination
    assert session.get_calls[0][0].endswith('/v2/assetId/12345')


def test_download_refuses_untrusted_signed_location(tmp_path: Path) -> None:
    response = _FakeResponse(
        {'locations': [{'location': 'https://example.invalid/asset.bin'}]},
        headers={'Content-Type': 'application/json', 'Content-Length': '100'},
    )
    session = _FakeSession(get_responses=[response], post_response=_FakeResponse())

    with pytest.raises(AssetLookupError, match='unexpected download host'):
        download_asset('12345', tmp_path / 'asset.bin', session=cast(requests.Session, session))



def test_open_cloud_key_is_not_forwarded_to_cdn_redirect(tmp_path: Path) -> None:
    redirect = _FakeResponse(
        headers={
            'Location': 'https://cdn.rbxcdn.com/assets/12345.bin',
            'Content-Type': 'text/plain',
        },
        status_code=302,
    )
    payload = b'protected asset bytes'
    final = _FakeResponse(
        chunks=(payload,),
        url='https://cdn.rbxcdn.com/assets/12345.bin',
    )
    session = _FakeSession(
        get_responses=[redirect, final],
        post_response=_FakeResponse(),
    )

    result = download_asset(
        '12345',
        tmp_path / 'protected.bin',
        api_key='secret-test-key',
        session=cast(requests.Session, session),
    )

    assert result.path.read_bytes() == payload
    assert session.get_calls[0][1]['headers']['x-api-key'] == 'secret-test-key'
    assert session.get_calls[0][1]['allow_redirects'] is False
    assert session.get_calls[1][1]['headers'] == {
        'User-Agent': 'Fleasion/2.x (Roblox asset lookup)'
    }
    assert 'x-api-key' not in session.get_calls[1][1]['headers']



def test_download_uses_open_cloud_endpoint_when_key_is_supplied(tmp_path: Path) -> None:
    payload = b'private asset bytes'
    response = _FakeResponse(chunks=(payload,))
    session = _FakeSession(get_responses=[response], post_response=_FakeResponse())

    result = download_asset(
        '12345',
        tmp_path / 'private_asset.bin',
        api_key='test-key',
        session=cast(requests.Session, session),
    )

    assert result.size == len(payload)
    url, options = session.get_calls[0]
    assert '/asset-delivery-api/v1/assetId/12345' in url
    assert options['headers']['x-api-key'] == 'test-key'
