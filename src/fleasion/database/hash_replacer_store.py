"""Local cache-hash replacement profiles and a content-addressed asset library.

The legacy Asset-ID Replacer keeps its own configuration store.  This module
stores raw replacement payloads by SHA-256 and maps Roblox cache-slot hashes to
those payloads without changing the Roblox cache until the user applies a rule.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable

from ..utils.paths import CONFIG_DIR

_CACHE_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
_ASSET_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_RE = re.compile(r"^[^\\/:*?\"<>|]{1,80}$")
_CHUNK_SIZE = 1024 * 1024
_IGNORED_DIRS = {".git", "__pycache__", ".venv", "venv"}


class HashReplacerError(RuntimeError):
    """A user-displayable error from the local hash replacement library."""


class HashReplacerStore:
    """Manage isolated hash profiles and imported raw payloads."""

    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = Path(root) if root is not None else Path(CONFIG_DIR) / "HashReplacer"
        self.root = self.root.expanduser()
        self.configs_dir = self.root / "configs"
        self.assets_dir = self.root / "assets"
        self.index_path = self.assets_dir / "index.json"
        self.configs_dir.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        if not self._profile_path("Default").is_file():
            self.save_config("Default", {"rules": []})
        if not self.index_path.is_file():
            self._save_index({"assets": {}})

    @staticmethod
    def validate_cache_hash(value: str) -> str:
        result = str(value or "").strip().casefold()
        if _CACHE_HASH_RE.fullmatch(result) is None:
            raise HashReplacerError("A Roblox cache hash must contain exactly 32 hexadecimal characters.")
        return result

    @staticmethod
    def validate_asset_hash(value: str) -> str:
        result = str(value or "").strip().casefold()
        if _ASSET_HASH_RE.fullmatch(result) is None:
            raise HashReplacerError("An imported asset hash must contain exactly 64 hexadecimal characters.")
        return result

    def _profile_path(self, name: str) -> Path:
        name = str(name or "").strip()
        if not _PROFILE_RE.fullmatch(name) or name in {".", ".."}:
            raise HashReplacerError("Profile name contains unsupported characters.")
        return self.configs_dir / f"{name}.json"

    def list_configs(self) -> list[str]:
        self.configs_dir.mkdir(parents=True, exist_ok=True)
        names = sorted(
            path.stem for path in self.configs_dir.glob("*.json")
            if path.is_file() and _PROFILE_RE.fullmatch(path.stem)
        )
        return names or ["Default"]

    def load_config(self, name: str) -> dict:
        path = self._profile_path(name)
        if not path.is_file():
            return {"schema": 1, "name": name, "rules": []}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HashReplacerError(f"Could not read hash profile '{name}': {exc}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("rules", []), list):
            raise HashReplacerError(f"Hash profile '{name}' has an invalid format.")
        rules = []
        for rule in value.get("rules", []):
            if not isinstance(rule, dict):
                continue
            try:
                target = self.validate_cache_hash(rule.get("target_hash", ""))
                source = self.validate_asset_hash(rule.get("source_hash", ""))
            except HashReplacerError:
                continue
            rules.append({
                "target_hash": target,
                "source_hash": source,
                "enabled": bool(rule.get("enabled", True)),
            })
        return {"schema": 1, "name": name, "rules": rules}

    def save_config(self, name: str, config: dict) -> None:
        path = self._profile_path(name)
        rules = []
        for rule in config.get("rules", []):
            if not isinstance(rule, dict):
                continue
            rules.append({
                "target_hash": self.validate_cache_hash(rule.get("target_hash", "")),
                "source_hash": self.validate_asset_hash(rule.get("source_hash", "")),
                "enabled": bool(rule.get("enabled", True)),
            })
        payload = {"schema": 1, "name": name, "rules": rules}
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(path.name + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_path, path)

    def create_config(self, name: str) -> bool:
        name = str(name or "").strip()
        path = self._profile_path(name)
        if path.exists():
            return False
        self.save_config(name, {"rules": []})
        return True

    def delete_config(self, name: str) -> bool:
        names = self.list_configs()
        if name not in names or len(names) <= 1 or name == "Default":
            return False
        self._profile_path(name).unlink(missing_ok=True)
        return True

    def add_rule(self, name: str, target_hash: str, source_hash: str) -> None:
        target = self.validate_cache_hash(target_hash)
        source = self.validate_asset_hash(source_hash)
        self.asset_path(source)
        config = self.load_config(name)
        config["rules"] = [
            rule for rule in config["rules"] if rule["target_hash"] != target
        ]
        config["rules"].append({
            "target_hash": target,
            "source_hash": source,
            "enabled": True,
        })
        self.save_config(name, config)

    def remove_rule(self, name: str, target_hash: str) -> bool:
        target = self.validate_cache_hash(target_hash)
        config = self.load_config(name)
        before = len(config["rules"])
        config["rules"] = [rule for rule in config["rules"] if rule["target_hash"] != target]
        if len(config["rules"]) == before:
            return False
        self.save_config(name, config)
        return True

    def _load_index(self) -> dict:
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"assets": {}}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HashReplacerError(f"Could not read the imported-asset index: {exc}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("assets"), dict):
            raise HashReplacerError("The imported-asset index has an invalid format.")
        return value

    def _save_index(self, value: dict) -> None:
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.index_path.with_name(self.index_path.name + ".tmp")
        temp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_path, self.index_path)

    @staticmethod
    def _safe_extension(path: Path) -> str:
        suffix = path.suffix.casefold()
        return suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ".bin"

    def _collect_files(self, paths: Iterable[str | os.PathLike[str]]) -> list[Path]:
        found: list[Path] = []
        seen: set[str] = set()
        for raw in paths:
            candidate = Path(raw).expanduser()
            if candidate.is_symlink():
                continue
            if candidate.is_file():
                candidates = [candidate]
            elif candidate.is_dir():
                candidates = []
                for root, directories, filenames in os.walk(candidate, followlinks=False):
                    root_path = Path(root)
                    directories[:] = [
                        directory for directory in directories
                        if directory not in _IGNORED_DIRS
                        and not (root_path / directory).is_symlink()
                    ]
                    for filename in filenames:
                        file_path = root_path / filename
                        if filename in {"Thumbs.db", ".DS_Store"} or file_path.is_symlink():
                            continue
                        candidates.append(file_path)
            else:
                continue
            for file_path in candidates:
                try:
                    resolved = str(file_path.resolve())
                except OSError:
                    resolved = str(file_path.absolute())
                if resolved not in seen and file_path.is_file():
                    seen.add(resolved)
                    found.append(file_path)
        return found

    def import_paths(self, paths: Iterable[str | os.PathLike[str]]) -> list[dict]:
        """Stream files into the content-addressed library; folders are recursive."""
        files = self._collect_files(paths)
        if not files:
            raise HashReplacerError("No regular files were found in the selected path(s).")
        index = self._load_index()
        records = index["assets"]
        imported: list[dict] = []
        errors: list[str] = []

        for source in files:
            temp_path: Path | None = None
            try:
                if source.stat().st_size <= 0:
                    errors.append(f"{source.name}: empty file skipped")
                    continue
                digest = hashlib.sha256()
                size = 0
                fd, temp_name = tempfile.mkstemp(prefix=".import-", suffix=".tmp", dir=self.assets_dir)
                temp_path = Path(temp_name)
                with os.fdopen(fd, "wb") as destination, source.open("rb") as incoming:
                    prefix = incoming.read(65536)
                    if prefix.startswith(b"RBXH"):
                        raise HashReplacerError(
                            f"{source.name}: this is a complete RBXH cache entry. Import the raw asset payload instead."
                        )
                    digest.update(prefix)
                    destination.write(prefix)
                    size += len(prefix)
                    while True:
                        chunk = incoming.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        digest.update(chunk)
                        destination.write(chunk)
                        size += len(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())

                asset_hash = digest.hexdigest()
                extension = self._safe_extension(source)
                filename = asset_hash + extension
                target_path = self.assets_dir / filename
                existing = records.get(asset_hash, {})
                if target_path.exists():
                    temp_path.unlink(missing_ok=True)
                    temp_path = None
                else:
                    os.replace(temp_path, target_path)
                    temp_path = None

                source_names = list(existing.get("source_names", [])) if isinstance(existing, dict) else []
                if source.name not in source_names:
                    source_names.append(source.name)
                try:
                    from .roblox_cache_store import detect_roblox_payload_kind
                    with target_path.open("rb") as handle:
                        kind = detect_roblox_payload_kind(handle.read(65536))
                except Exception:
                    kind = "unknown"
                record = {
                    "sha256": asset_hash,
                    "file": filename,
                    "name": source.name,
                    "source_names": source_names[-20:],
                    "kind": kind,
                    "size": size,
                }
                records[asset_hash] = record
                imported.append(record)
            except (OSError, HashReplacerError) as exc:
                errors.append(f"{source.name}: {exc}")
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)

        self._save_index(index)
        if not imported and errors:
            raise HashReplacerError("No files were imported. " + " | ".join(errors[:10]))
        for record in imported:
            record["import_errors"] = errors
        return imported

    def list_assets(self) -> list[dict]:
        records = self._load_index()["assets"]
        result = []
        for asset_hash, record in records.items():
            if not isinstance(record, dict):
                continue
            try:
                asset_hash = self.validate_asset_hash(asset_hash)
                path = self.asset_path(asset_hash)
            except HashReplacerError:
                continue
            result.append({**record, "sha256": asset_hash, "path": path})
        result.sort(key=lambda item: (str(item.get("name", "")).casefold(), item["sha256"]))
        return result

    def asset_path(self, asset_hash: str) -> Path:
        digest = self.validate_asset_hash(asset_hash)
        record = self._load_index()["assets"].get(digest)
        if not isinstance(record, dict):
            raise HashReplacerError(f"Imported asset {digest} is not in the library.")
        filename = str(record.get("file", ""))
        if not filename or Path(filename).name != filename:
            raise HashReplacerError("Imported-asset index contains an unsafe file path.")
        path = (self.assets_dir / filename).resolve()
        try:
            path.relative_to(self.assets_dir.resolve())
        except ValueError as exc:
            raise HashReplacerError("Imported-asset path escapes the library folder.") from exc
        if not path.is_file():
            raise HashReplacerError(f"Imported file is missing: {path.name}")
        return path
