"""Convert legacy Asset-ID Replacer profiles into local cache-hash profiles.

The source JSON files are read-only inputs: this module never renames, edits or
deletes them. A conversion only creates a separate HashReplacer profile and
imports the replacement payloads into its content-addressed asset library.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from ..config.manager import resolve_local_replacement_path
from .hash_replacer_store import HashReplacerError, HashReplacerStore
from .roblox_cache_store import RobloxCacheStore, RobloxCacheEntry


_UNSUPPORTED_TARGET_KINDS = {
    "asset-metadata",
    "http-response",
    "roblox-settings",
    "video-metadata",
    "video-manifest",
}


def _iter_rules(rules: list[Any]):
    """Flatten the nested profile tree without mutating the source objects."""
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        children = rule.get("children")
        if isinstance(children, list):
            yield from _iter_rules(children)
        elif "replace_ids" in rule or "mode" in rule or "local_path" in rule:
            yield rule


def _numeric_ids(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if re.fullmatch(r"[1-9][0-9]{0,19}", text) and text not in result:
            result.append(text)
    return result


def _cache_hashes_by_asset_id(
    entries: list[RobloxCacheEntry],
    cached_metadata: dict[str, Any] | None,
    resolved_cdn_hashes: dict[str, str] | None = None,
) -> dict[str, list[RobloxCacheEntry]]:
    """Index cache slots by Asset IDs or resolved Roblox CDN hashes."""
    metadata = cached_metadata if isinstance(cached_metadata, dict) else {}
    result: dict[str, list[RobloxCacheEntry]] = {}
    by_cdn_hash: dict[str, list[RobloxCacheEntry]] = {}

    for entry in entries:
        if entry.kind in _UNSUPPORTED_TARGET_KINDS:
            continue
        try:
            HashReplacerStore.validate_cache_hash(entry.entry_id)
        except HashReplacerError:
            continue

        slot_metadata = metadata.get(entry.entry_id, {})
        slot_metadata = slot_metadata if isinstance(slot_metadata, dict) else {}
        asset_id = str(entry.asset_id or slot_metadata.get("asset_id") or "").strip()
        if re.fullmatch(r"[1-9][0-9]{0,19}", asset_id):
            result.setdefault(asset_id, []).append(entry)

        match = re.search(
            r"/(?:v[0-9]+/)?sc[0-9]+/([0-9a-f]{32})(?:[/?]|$)",
            str(entry.url or ""),
            re.IGNORECASE,
        )
        if match:
            by_cdn_hash.setdefault(match.group(1).casefold(), []).append(entry)

    for asset_id, cdn_hash in (resolved_cdn_hashes or {}).items():
        normalized_hash = str(cdn_hash or "").casefold()
        for entry in by_cdn_hash.get(normalized_hash, []):
            matches = result.setdefault(str(asset_id), [])
            if all(existing.entry_id != entry.entry_id for existing in matches):
                matches.append(entry)
    return result


def _unique_profile_name(store: HashReplacerStore, requested: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|]', "_", str(requested or "").strip()).strip(" .")
    safe = (safe or "Converted profile")[:80]
    candidate = safe
    suffix = 2
    existing = set(store.list_configs())
    while candidate in existing:
        tail = f" (hash {suffix})"
        candidate = safe[: 80 - len(tail)] + tail
        suffix += 1
    return candidate


def _replacement_path(value: str) -> Path:
    """Resolve portable Configs paths as well as ordinary absolute paths."""
    return resolve_local_replacement_path(str(value or "").strip())


def convert_legacy_replacer_profile(
    name: str,
    rules: list[Any],
    *,
    cache_store: RobloxCacheStore,
    asset_store: HashReplacerStore,
    cached_metadata: dict[str, Any] | None = None,
    cdn_hash_resolver: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Create a new hash profile from a legacy Asset-ID profile.

    Asset IDs are matched to 32-character cache-slot hashes using the local
    cache index and metadata that the user has already resolved in Scraper DB.
    OBJ replacements are converted to Roblox's V2 mesh payload before import.
    Unsupported/absent mappings are reported rather than guessed.
    """
    if not isinstance(rules, list):
        raise HashReplacerError("The selected legacy profile does not contain a rules list.")

    entries = cache_store.scan()
    rules_to_convert = list(_iter_rules(rules))
    requested_asset_ids = {
        asset_id
        for rule in rules_to_convert
        for asset_id in _numeric_ids(rule.get("replace_ids", []))
    }
    by_asset_id = _cache_hashes_by_asset_id(entries, cached_metadata)
    resolved_cdn_hashes: dict[str, str] = {}
    resolution_errors: dict[str, str] = {}
    if cdn_hash_resolver is not None:
        for asset_id in sorted(requested_asset_ids):
            if by_asset_id.get(asset_id):
                continue
            try:
                resolved_cdn_hashes[asset_id] = str(
                    cdn_hash_resolver(asset_id)
                ).casefold()
            except Exception as exc:
                resolution_errors[asset_id] = f"{type(exc).__name__}: {exc}"
        if resolved_cdn_hashes:
            by_asset_id = _cache_hashes_by_asset_id(
                entries, cached_metadata, resolved_cdn_hashes
            )
    imported_by_source: dict[str, str] = {}
    imported_asset_ids: dict[str, str] = {}
    converted_rules: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    matched_asset_ids: set[str] = set()

    for rule in rules_to_convert:
        label = str(rule.get("name") or "Unnamed rule")
        mode = str(rule.get("mode") or ("remove" if rule.get("remove") else "id")).casefold()
        target_ids = _numeric_ids(rule.get("replace_ids", []))
        if not target_ids:
            skipped.append(f"{label}: no numeric target Asset IDs")
            continue

        targets: list[RobloxCacheEntry] = []
        missing_ids: list[str] = []
        for asset_id in target_ids:
            matches = by_asset_id.get(asset_id, [])
            if matches:
                targets.extend(matches)
                matched_asset_ids.add(asset_id)
            else:
                missing_ids.append(asset_id)
        if missing_ids:
            missing_details = []
            for asset_id in missing_ids[:8]:
                detail = asset_id
                if asset_id in resolution_errors:
                    detail += f" (Asset Delivery lookup failed: {resolution_errors[asset_id]})"
                elif asset_id in resolved_cdn_hashes:
                    detail += (
                        f" (CDN hash {resolved_cdn_hashes[asset_id]} is not present "
                        "in this Scraper DB)"
                    )
                missing_details.append(detail)
            skipped.append(
                f"{label}: no local cache slot found for Asset ID(s) {', '.join(missing_details)}"
                + (f" and {len(missing_ids) - 8} more" if len(missing_ids) > 8 else "")
            )
        if not targets:
            continue

        source_hash = ""
        try:
            if mode == "local":
                raw_path = str(rule.get("local_path") or "").strip()
                if not raw_path:
                    raise HashReplacerError("local replacement path is empty")
                source_path = _replacement_path(raw_path)
                if not source_path.is_file():
                    raise HashReplacerError(f"replacement file does not exist: {source_path}")

                # HashReplacerStore converts OBJ sources to Roblox's
                # binary V2 Mesh format while retaining the original filename.
                cache_key = str(source_path.resolve())
                source_hash = imported_by_source.get(cache_key, "")
                if not source_hash:
                    imported = asset_store.import_paths([source_path])
                    if not imported:
                        raise HashReplacerError("replacement file was not imported")
                    source_hash = str(imported[0]["sha256"])
                    imported_by_source[cache_key] = source_hash
                    import_errors = imported[0].get("import_errors", [])
                    if import_errors:
                        skipped.extend(f"{label}: {item}" for item in import_errors)
            elif mode == "id":
                replacement_id = _numeric_ids(rule.get("with_id"))
                if not replacement_id:
                    raise HashReplacerError("ID replacement has no numeric with_id value")
                replacement_id = replacement_id[0]
                source_hash = imported_asset_ids.get(replacement_id, "")
                if not source_hash:
                    imported = asset_store.import_asset_id(replacement_id)
                    source_hash = str(imported["sha256"])
                    imported_asset_ids[replacement_id] = source_hash
            else:
                raise HashReplacerError(
                    f"mode '{mode}' cannot be represented as a direct local cache replacement"
                )
        except (OSError, RuntimeError, ValueError, HashReplacerError) as exc:
            skipped.append(f"{label}: {exc}")
            continue

        for entry in targets:
            try:
                target_hash = HashReplacerStore.validate_cache_hash(entry.entry_id)
            except HashReplacerError:
                continue
            converted_rules[target_hash] = {
                "target_hash": target_hash,
                "source_hash": source_hash,
                "enabled": bool(rule.get("enabled", True)),
            }

    if not converted_rules:
        raise HashReplacerError(
            "No rules could be converted. Scraper DB stores CDN content hashes, not reversible "
            "numeric Asset IDs. Asset Delivery could not match the selected IDs to entries in "
            "this cache. Confirm the assets are accessible and already present in this Scraper DB."
        )

    profile_name = _unique_profile_name(asset_store, name)
    asset_store.save_config(profile_name, {"rules": list(converted_rules.values())})
    return {
        "profile_name": profile_name,
        "rules_created": len(converted_rules),
        "asset_ids_matched": len(matched_asset_ids),
        "asset_ids_requested": len(requested_asset_ids),
        "asset_ids_resolved_by_cdn": len(resolved_cdn_hashes),
        "skipped": skipped,
    }
