"""Locate Roblox's rbx-storage.db and rbx-storage/ folder from whatever path the user picked."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

DB_NAME = 'rbx-storage.db'
ASSETS_NAME = 'rbx-storage'
SOBER_APP_ID = 'org.vinegarhq.Sober'
MAX_SEARCH_DEPTH = 6
MAX_SEARCH_DIRS = 20000
# Directories that never hold a Roblox cache but can be huge
_SKIP_DIRS = frozenset(
    {'.git', 'node_modules', '__pycache__', 'dosdevices', 'windows', 'proc', 'sys'}
)


@dataclass(frozen=True)
class CacheLocation:
    """Resolved cache files. ``assets_path`` is None when no external storage folder exists."""

    database_path: Path
    assets_path: Path | None
    searched: tuple[Path, ...] = field(default=(), compare=False)

    @property
    def root(self) -> Path:
        return self.database_path.parent


def _lookup(directory: Path, name: str) -> Path | None:
    """Find ``name`` inside ``directory`` ignoring case (Linux filenames are case-sensitive)."""
    exact = directory / name
    if exact.exists():
        return exact
    try:
        for child in directory.iterdir():
            if child.name.casefold() == name.casefold():
                return child
    except OSError:
        pass
    return None


def sober_cache_dirs(home: Path | None = None) -> list[Path]:
    """Sober keeps rbx-storage.db under data/.../appData but the asset folder under cache/."""
    base = (home or Path.home()) / '.var' / 'app' / SOBER_APP_ID
    return [base / 'cache' / 'sober' / ASSETS_NAME, base / 'data' / 'sober' / ASSETS_NAME]


def candidate_cache_roots(home: Path | None = None) -> list[Path]:
    """Usual places for the Roblox cache on this machine, most likely first."""
    home = home or Path.home()
    roots: list[Path] = []
    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        roots.append(Path(local_app_data) / 'Roblox')
    roots.append(home / 'Library' / 'Roblox')
    sober = home / '.var' / 'app' / SOBER_APP_ID / 'data' / 'sober'
    roots += [sober / 'appData', sober]
    # Wine/Vinegar/Lutris style prefixes
    for prefix in (
        home / '.var' / 'app' / 'org.vinegarhq.Vinegar' / 'data' / 'vinegar' / 'prefixes',
        home / '.local' / 'share' / 'vinegar' / 'prefixes',
        home / '.wine',
        home / 'Games',
    ):
        roots.append(prefix)
    return roots


def _iter_dirs(top: Path, max_depth: int) -> Iterator[Path]:
    """Breadth-first directory walk that also enters hidden folders such as ~/.var."""
    level = [top]
    visited = 0
    for _ in range(max_depth + 1):
        next_level: list[Path] = []
        for directory in level:
            visited += 1
            if visited > MAX_SEARCH_DIRS:
                return
            yield directory
            try:
                children = sorted(
                    c for c in directory.iterdir() if c.is_dir() and not c.is_symlink()
                )
            except OSError:
                continue
            next_level.extend(c for c in children if c.name.casefold() not in _SKIP_DIRS)
        level = next_level
        if not level:
            return


def _find_database(path: Path, searched: list[Path]) -> Path | None:
    if path.is_file():
        return path
    if not path.is_dir():
        return None
    # The user may have picked the rbx-storage folder itself, so look next to it first
    if path.name.casefold() == ASSETS_NAME:
        found = _lookup(path.parent, DB_NAME)
        if found is not None:
            return found
    for directory in _iter_dirs(path, MAX_SEARCH_DEPTH):
        searched.append(directory)
        found = _lookup(directory, DB_NAME)
        if found is not None and found.is_file():
            return found
    return None


def _find_assets(database_path: Path, picked: Path) -> Path | None:
    """Look for the storage folder beside the database, in the picked tree, then Sober's cache."""
    options: list[Path] = [database_path.parent]
    if picked.is_dir():
        options.append(picked)
        if picked.name.casefold() == ASSETS_NAME:
            options.insert(0, picked.parent)
    for directory in options:
        found = _lookup(directory, ASSETS_NAME)
        if found is not None and found.is_dir():
            return found
    if picked.is_dir() and picked.name.casefold() == ASSETS_NAME:
        return picked
    if SOBER_APP_ID in database_path.parts:
        for candidate in sober_cache_dirs():
            if candidate.is_dir():
                return candidate
    return None


def resolve_cache_location(path: str | os.PathLike[str]) -> CacheLocation:
    """Resolve a database file, its folder, the rbx-storage folder or any parent folder.

    Raises FileNotFoundError listing what was searched when no database exists.
    """
    picked = Path(path).expanduser()
    searched: list[Path] = []
    database = _find_database(picked, searched)
    if database is None:
        where = ', '.join(str(p) for p in searched[:8]) or str(picked)
        message = (
            f'No {DB_NAME} found in {picked} (searched {len(searched)} folder(s) up to '
            f'{MAX_SEARCH_DEPTH} levels deep: {where}). Pick the folder that contains {DB_NAME}.'
        )
        raise FileNotFoundError(message)
    database = database.resolve()
    assets = _find_assets(database, picked)
    return CacheLocation(database, assets.resolve() if assets else None, tuple(searched))


def find_default_cache_location() -> CacheLocation | None:
    """Return the first existing cache found in the usual places, or None."""
    for root in candidate_cache_roots():
        if not root.exists():
            continue
        try:
            return resolve_cache_location(root)
        except FileNotFoundError:
            continue
    return None
