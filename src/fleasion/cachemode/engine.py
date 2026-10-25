"""Turn Fleasion replacement rules into cache edits."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .store import CacheEntry, CacheModeError, RobloxCacheStore, split_entry

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    FetchAsset = Callable[[int], bytes | None]
    FetchUrl = Callable[[str], bytes | None]


@dataclass
class PlannedReplacement:
    """One asset ID to replace, with where its new content comes from."""

    asset_id: int
    source: str  # 'id', 'local' or 'cdn'
    value: str  # Replacement asset ID, file path or URL
    entries: list[CacheEntry] = field(default_factory=lambda: list[CacheEntry]())
    note: str = ''  # Why it cannot be applied (empty when it can)

    @property
    def origin(self) -> str:
        return f'{self.source} {self.value}'


@dataclass
class ApplyReport:
    applied: list[str] = field(default_factory=lambda: list[str]())
    skipped: list[str] = field(default_factory=lambda: list[str]())
    errors: list[str] = field(default_factory=lambda: list[str]())

    def summary(self) -> str:
        return (
            f'{len(self.applied)} applied, {len(self.skipped)} skipped, {len(self.errors)} failed'
        )


class CacheReplacer:
    """Applies and restores replacements in a :class:`RobloxCacheStore`."""

    def __init__(
        self,
        store: RobloxCacheStore,
        *,
        fetch_asset: FetchAsset | None = None,
        fetch_url: FetchUrl | None = None,
    ) -> None:
        self.store = store
        self._fetch_asset = fetch_asset
        self._fetch_url = fetch_url

    def plan(
        self,
        replacements: Mapping[int | str, int],
        removals: set[int | str] | frozenset[int | str],
        cdn: Mapping[int | str, str],
        local: Mapping[int | str, str],
    ) -> list[PlannedReplacement]:
        """Build the plan from ``ConfigManager.get_all_replacements()`` output."""
        out: list[PlannedReplacement] = []
        sources: list[tuple[str, Mapping[int | str, object]]] = [
            ('id', replacements),
            ('cdn', cdn),
            ('local', local),
        ]
        for source, table in sources:
            for key, value in table.items():
                if not isinstance(key, int):
                    out.append(
                        PlannedReplacement(
                            0,
                            source,
                            str(value),
                            note=f'rule for {key!r}: only asset IDs are supported',
                        )
                    )
                    continue
                item = PlannedReplacement(key, source, str(value))
                item.entries = self.store.find_by_asset_id(key)
                if not item.entries:
                    item.note = (
                        'not in the cache yet (load it in-game once, close Roblox, then apply)'
                    )
                out.append(item)
        out.extend(
            PlannedReplacement(
                k if isinstance(k, int) else 0, 'remove', '', note='removal rules are not supported'
            )
            for k in removals
        )
        return out

    def _original(self, ident: str) -> bytes:
        """The genuine Roblox bytes of an entry (the saved original when we replaced it)."""
        if self.store.swap_status(ident) == 'swapped':
            orig = self.store.state / 'originals' / ident
            if orig.is_file():
                return orig.read_bytes()
        return self.store.path(ident).read_bytes()

    def _content_for(self, item: PlannedReplacement, template: bytes) -> bytes:
        """Full entry bytes that should replace ``template``'s file."""
        if item.source == 'id':
            sources = [e for e in self.store.find_by_asset_id(int(item.value)) if e.has_header]
            if sources:  # Whole-entry copy, the method proven to work
                return self._original(sources[0].ident)
            if self._fetch_asset is None:
                raise CacheModeError(
                    f'Asset {item.value} is not in the cache and downloading is not available.'
                )
            payload = self._fetch_asset(int(item.value))
        elif item.source == 'cdn':
            payload = self._fetch_url(item.value) if self._fetch_url else None
        else:
            try:
                payload = Path(item.value).read_bytes()
            except OSError as exc:
                raise CacheModeError(f'Cannot read {item.value}: {exc}') from exc
        if not payload:
            raise CacheModeError(f'Could not get the replacement content ({item.origin}).')
        fields = self.store.detect_length_fields()
        if not fields:
            raise CacheModeError('Could not work out the cache header layout from your entries.')
        # Strip a header if the downloaded file somehow has one
        _url, payload, _has = split_entry(payload)
        return self.store.build_entry(template, payload, fields)

    def apply(self, plan: list[PlannedReplacement]) -> ApplyReport:
        report = ApplyReport()
        for item in plan:
            label = f'{item.asset_id or "?"} ← {item.origin}'
            if item.note:
                report.skipped.append(f'{label}: {item.note}')
                continue
            for entry in item.entries:
                try:
                    info = self.store.manifest().get(entry.ident)
                    if (
                        info
                        and info.get('origin') == item.origin
                        and self.store.swap_status(entry.ident) == 'swapped'
                    ):
                        report.skipped.append(f'{label}: already applied')
                        continue
                    template = self._original(entry.ident)
                    self.store.write_entry(
                        entry.ident, self._content_for(item, template), item.origin
                    )
                    report.applied.append(label)
                except CacheModeError as exc:
                    report.errors.append(f'{label}: {exc}')
        return report

    def restore_all(self) -> list[str]:
        return self.store.restore_all()
