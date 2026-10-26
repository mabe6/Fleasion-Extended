"""core.py - Roblox cache logic for CacheSwap (no GUI in here).

Method (proven on Jump -> Wave): copy a WHOLE cache entry (RBXH header + payload)
of a source asset over the target entry file, then set the target's `size` row in
rbx-storage.db to the new file length.

Safety rules enforced here:
  * never touches anything while Roblox is running
  * only ever UPDATEs one `size` cell of rbx-storage.db (never deletes the database)
  * database is backed up before every swap
  * the target's original file is saved once and kept until you restore it
  * every source used is copied into the app's own library, so a swap can be redone
    even after Roblox deletes the source entry from its cache
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HEX32 = re.compile(r"[0-9a-f]{32}")
META = 25  # bytes between the URL and the payload in the RBXH header
PROCS = ("robloxplayerbeta.exe", "robloxstudiobeta.exe")
WAVE_ID = "47a6a89e49946fbfd8d2e1d607ff13bf"
DEFAULT_NAMES = {WAVE_ID: "Wave (default emote)"}
HEAD_READ = 262144  # bytes read per entry while scanning


class CacheError(Exception):
    """Something the user can act on; message is shown in the UI."""


# ---------------------------------------------------------------- paths / json
def app_dir() -> Path:
    env = os.environ.get("CACHESWAP_HOME")
    base = Path(env) if env else Path(os.environ.get("APPDATA", str(Path.home()))) / "CacheSwap"
    base.mkdir(parents=True, exist_ok=True)
    return base


def default_roblox_dir() -> Path:
    env = os.environ.get("CACHESWAP_ROBLOX_DIR")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "Roblox" if base else Path.home() / "Roblox"


def _load(name, default):
    try:
        return json.loads((app_dir() / name).read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _save(name, obj):
    p = app_dir() / name
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2), "utf-8")
    os.replace(tmp, p)


def roblox_running() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        out = subprocess.run(
            ["tasklist"], capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.lower()
    except OSError:
        return False
    return any(n in out for n in PROCS)


# ------------------------------------------------------------- entry parsing
def parse_header(data: bytes):
    """Return (url, payload_start). Raises CacheError if this is not an RBXH entry."""
    if data[:4] != b"RBXH" or len(data) < 12:
        raise CacheError("not an RBXH cache entry")
    url_len = struct.unpack_from("<I", data, 8)[0]
    start = 12 + url_len + META
    if url_len > 4096 or start > len(data):
        raise CacheError("malformed RBXH header")
    return data[12:12 + url_len].decode("utf-8", "replace"), start


def split_entry(data: bytes):
    """(url, payload, has_header). Entries without an RBXH header are treated as a raw payload."""
    try:
        url, start = parse_header(data)
        return url, data[start:], True
    except CacheError:
        return "", data, False


def kind_of(payload: bytes) -> str:
    # Font payload signatures used by the Fleasion font preview.
    if payload.startswith((b"\x00\x01\x00\x00", b"OTTO", b"ttcf", b"wOFF", b"wOF2")):
        return "font"
    if payload[:8] == b"<roblox!":
        if b"KeyframeSequence" in payload:
            return "anim"
        if b"CurveAnimation" in payload:
            return "curve-anim"
        return "rbxm"
    # Texture containers and common raster image formats supported by the extended preview.
    if payload.startswith(b"\x89PNG"):
        return "png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if payload[:12] in (b"\xabKTX 11\xbb\r\n\x1a\n", b"\xabKTX 20\xbb\r\n\x1a\n"):
        return "ktx"
    if payload.startswith(b"BM"):
        return "bmp"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "webp"
    if payload.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        return "tiff"
    if payload.startswith(b"\x00\x00\x01\x00"):
        return "ico"
    if payload.startswith(b"DDS "):
        return "dds"
    if payload.startswith(b"8BPS"):
        return "psd"
    if payload[:8] == b"version ":
        return "mesh"
    if payload[:4] == b"OggS":
        return "ogg"
    if payload[:3] == b"ID3" or payload[:2] == b"\xff\xfb":
        return "mp3"
    if payload[:6] == b"<roblox":
        return "xml"
    if payload[:1] in (b"{", b"["):
        return "json"
    return "other"


ANIM_KINDS = {"anim": "KeyframeSequence", "curve-anim": "CurveAnimation (newer clip format)"}


def kind_message(target_kind: str, source_kind: str) -> str:
    """Warning text for a target/source kind combination ('' when they match)."""
    if target_kind == source_kind:
        return ""
    if target_kind in ANIM_KINDS and source_kind in ANIM_KINDS:
        return (f"Target is a {ANIM_KINDS[target_kind]}, source is a {ANIM_KINDS[source_kind]}. "
                "Both are animations and should play, but this combination is untested.")
    return (f"Target looks like '{target_kind}' but source looks like '{source_kind}'. "
            "Roblox may ignore it or fail to play it.")


def build_entry(template: bytes, payload: bytes, fields) -> bytes:
    """New cache entry = template's RBXH header (length field(s) rewritten) + payload."""
    _url, start = parse_header(template)
    header = bytearray(template[:start])
    for off, width in fields:
        struct.pack_into("<I" if width == 4 else "<Q", header, start - META + off, len(payload))
    return bytes(header) + payload


_ASSET_RE = re.compile(r"[?&]id=(\d+)|assetId[=/](\d+)|/asset/(\d+)", re.I)


def asset_id_of(url: str) -> str:
    m = _ASSET_RE.search(url)
    return next((g for g in m.groups() if g), "") if m else ""


@dataclass
class Entry:
    id: str
    size: int
    mtime: float
    kind: str
    url: str
    asset_id: str
    header: bool = True  # False: the file has no RBXH header (raw payload)


# --------------------------------------------------------------------- store
class Store:
    def __init__(self, roblox_dir: str | os.PathLike | None = None):
        self.root = Path(roblox_dir) if roblox_dir else default_roblox_dir()
        self.db = self.root / "rbx-storage.db"
        self.dir = self.root / "rbx-storage"
        self._memo: dict = {}

    # -- basics
    def validate(self):
        if not self.dir.is_dir():
            raise CacheError(f"Cache folder not found:\n{self.dir}")
        if not self.db.is_file():
            raise CacheError(f"Database not found:\n{self.db}")

    def path(self, ident: str) -> Path:
        return self.dir / ident[:2] / ident

    @staticmethod
    def _check_id(ident: str):
        if not ident or not HEX32.fullmatch(ident):
            raise CacheError("Entry IDs are 32 lowercase hex characters.")

    @staticmethod
    def _require_closed():
        if roblox_running():
            raise CacheError("Roblox is running. Close it completely (tray icon too) and try again.")

    # -- database (only ever the `size` cell)
    def db_size(self, ident: str):
        with contextlib.closing(sqlite3.connect(self.db, timeout=5)) as con:
            r = con.execute("SELECT size FROM files WHERE id = ?", (bytes.fromhex(ident),)).fetchone()
        return r[0] if r else None

    def set_db_size(self, ident: str, size: int):
        with contextlib.closing(sqlite3.connect(self.db, timeout=5)) as con:
            with con:
                cur = con.execute("UPDATE files SET size = ? WHERE id = ?", (size, bytes.fromhex(ident)))
            if cur.rowcount != 1:
                raise CacheError(f"Entry {ident} has no row in the database.")

    def backup_db(self) -> Path:
        d = app_dir() / "backups" / time.strftime("%Y%m%d-%H%M%S")
        d.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(self.db, timeout=5)) as src, \
                contextlib.closing(sqlite3.connect(d / self.db.name)) as dst:
            src.backup(dst)
        old = sorted(p for p in (app_dir() / "backups").iterdir() if p.is_dir())
        for p in old[:-15]:  # keep the 15 newest
            shutil.rmtree(p, ignore_errors=True)
        return d

    # -- scanning
    def snapshot(self) -> dict:
        """Cheap: {id: (size, mtime_ns)} from file stats only."""
        out = {}
        for dp, _dn, fns in os.walk(self.dir):
            for fn in fns:
                if HEX32.fullmatch(fn):
                    try:
                        st = (Path(dp) / fn).stat()
                    except OSError:
                        continue
                    out[fn] = (st.st_size, st.st_mtime_ns)
        return out

    def _describe_path(self, p: Path, ident: str, st) -> Entry:
        try:
            with open(p, "rb") as f:
                head = f.read(HEAD_READ)
            url, payload, has_header = split_entry(head)
            kind = kind_of(payload)
        except OSError:
            url, kind, has_header = "", "?", True
        return Entry(ident, st.st_size, st.st_mtime, kind, url, asset_id_of(url), has_header)

    def read_payload(self, ident: str, max_bytes: int = 40_000_000):
        """(url, payload) of an entry in the cache or the app library (read-only, for previews)."""
        for p in (self.path(ident), app_dir() / "library" / ident):
            if p.is_file():
                if p.stat().st_size > max_bytes:
                    raise CacheError("Entry is too large to preview.")
                url, payload, _has = split_entry(p.read_bytes())
                return url, payload
        raise CacheError("Entry file not found.")

    def export_entry(self, ident: str, dest_dir) -> Path:
        """Copy the raw entry file plus a small text report into dest_dir (for sending to someone)."""
        self._check_id(ident)
        p = next((q for q in (self.path(ident), app_dir() / "library" / ident) if q.is_file()), None)
        if p is None:
            raise CacheError("Entry file not found.")
        data = p.read_bytes()
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"{ident}.bin"
        out.write_bytes(data)
        url, payload, has_header = split_entry(data)
        try:
            dbsize = self.db_size(ident) if self.db.is_file() else None
        except sqlite3.Error:
            dbsize = None
        lines = [f"entry: {ident}", f"file size: {len(data)}", f"db size column: {dbsize}",
                 f"RBXH header: {'yes' if has_header else 'NO'}", f"url: {url}",
                 f"detected kind: {kind_of(payload)}",
                 f"first 96 bytes: {data[:96].hex(' ')}",
                 f"modified: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(p.stat().st_mtime))}"]
        (dest / f"{ident}.txt").write_text("\n".join(lines) + "\n", "utf-8")
        return out

    def describe(self, ident: str):
        p = self.path(ident)
        try:
            return self._describe_path(p, ident, p.stat())
        except OSError:
            return None

    def scan(self) -> list:
        out = []
        for dp, _dn, fns in os.walk(self.dir):
            for fn in fns:
                if not HEX32.fullmatch(fn):
                    continue
                p = Path(dp) / fn
                try:
                    st = p.stat()
                except OSError:
                    continue
                key = (fn, st.st_size, st.st_mtime_ns)
                e = self._memo.get(key)
                if e is None:
                    e = self._memo[key] = self._describe_path(p, fn, st)
                out.append(e)
        return out

    # -- names
    def name_of(self, ident: str) -> str:
        return _load("names.json", {}).get(ident) or DEFAULT_NAMES.get(ident, "")

    @staticmethod
    def set_name(ident: str, name: str):
        names = _load("names.json", {})
        if name.strip():
            names[ident] = name.strip()
        else:
            names.pop(ident, None)
        _save("names.json", names)

    # -- history (persistent log of every swap / restore)
    @staticmethod
    def history() -> list:
        return _load("history.json", [])

    @staticmethod
    def _log_history(action: str, target: str, source: str = "", detail: str = ""):
        h = _load("history.json", [])
        h.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action, "target": target,
                  "source": source, "detail": detail})
        _save("history.json", h[-1000:])

    # -- swap state
    @staticmethod
    def manifest() -> dict:
        return _load("manifest.json", {})

    def swap_status(self, ident: str) -> str:
        """'' (untouched), 'swapped' (still ours) or 'reverted' (Roblox rewrote it)."""
        info = self.manifest().get(ident)
        if not info:
            return ""
        try:
            return "swapped" if self.path(ident).stat().st_size == info["size"] else "reverted"
        except OSError:
            return "reverted"

    def kind_warning(self, target: str, source: str) -> str:
        t, s = self.describe(target), self.describe(source)
        if not s:
            lib = app_dir() / "library" / source
            if lib.is_file():
                s = self._describe_path(lib, source, lib.stat())
        return kind_message(t.kind, s.kind) if t and s else ""

    def _source_bytes(self, source: str) -> bytes:
        lib = app_dir() / "library" / source
        for p in (self.path(source), lib):
            if p.is_file():
                data = p.read_bytes()
                parse_header(data)
                return data
        raise CacheError("Source entry is not in the cache or in the app library.")

    # -- header layout / import of a downloaded file
    def detect_length_field(self, limit: int = 400):
        """Find where the 25-byte header stores the payload length, using real entries.

        Returns {"fields": [(offset, width), ...], "hits": {offset: n}, "total": n} or None
        when no little-endian 4/8-byte field matches the payload length in >= 90% of the
        sampled entries (then importing a file is refused instead of guessing).
        """
        votes: dict = {}
        total = 0
        for dp, _dn, fns in os.walk(self.dir):
            for fn in fns:
                if total >= limit:
                    break
                if not HEX32.fullmatch(fn):
                    continue
                p = Path(dp) / fn
                try:
                    if p.stat().st_size > 1_000_000:
                        continue
                    data = p.read_bytes()
                    _url, start = parse_header(data)
                except (OSError, CacheError):
                    continue
                plen = len(data) - start
                if plen < 256:  # tiny payloads match unrelated small fields by accident
                    continue
                meta = data[start - META:start]
                total += 1
                for width, fmt in ((4, "<I"), (8, "<Q")):
                    for off in range(0, META - width + 1):
                        if struct.unpack_from(fmt, meta, off)[0] == plen:
                            votes[(off, width)] = votes.get((off, width), 0) + 1
            if total >= limit:
                break
        if total < 3:
            return None
        best: dict = {}  # offset -> (width, hits); keep the narrowest width per offset
        for (off, width), hits in votes.items():
            if hits / total >= 0.9 and (off not in best or width < best[off][0]):
                best[off] = (width, hits)
        if not best:
            return None
        return {"fields": sorted((off, w) for off, (w, _h) in best.items()),
                "hits": {off: h for off, (_w, h) in best.items()}, "total": total}

    def probe_header(self, samples: int = 6) -> str:
        """Plain-text report of the header layout, safe to paste back for debugging."""
        det = self.detect_length_field()
        lines = ["CacheSwap header probe " + time.strftime("%Y-%m-%d %H:%M:%S"), f"cache folder: {self.dir}"]
        if det:
            lines.append(f"payload-length field(s) found: {det['fields']} "
                         f"(hits per offset {det['hits']} of {det['total']} sampled entries)")
        else:
            lines.append("payload-length field: NOT found (no little-endian 4/8-byte field matches the "
                         "payload length in >=90% of sampled entries)")
        lines += ["", "sample entries (meta = the 25 bytes right before the payload):"]
        shown = 0
        for e in sorted(self.scan(), key=lambda e: (e.kind not in ANIM_KINDS, e.size)):
            if shown >= samples:
                break
            if e.size > 1_000_000:
                continue
            try:
                data = self.path(e.id).read_bytes()
                _u, start = parse_header(data)
            except (OSError, CacheError):
                continue
            lines.append(f"  {e.id} {e.kind:10} file={len(data)} payload={len(data) - start} "
                         f"meta={data[start - META:start].hex()} url={e.url[:120]}")
            shown += 1
        return "\n".join(lines)

    def import_warning(self, target: str, path) -> str:
        t = self.describe(target)
        head = Path(path).read_bytes()[:HEAD_READ]
        return kind_message(t.kind, kind_of(head)) if t else ""

    def import_file(self, target: str, path, name: str = "", origin: str = "") -> str:
        """Put a file (animation .rbxm, image, mesh, sound...) into `target`'s cache slot.

        The entry is the target's own header (URL and other fields kept, length field(s)
        rewritten) plus the file as payload, then swapped in like any other source.
        """
        self._require_closed()
        self.validate()
        self._check_id(target)
        payload = Path(path).read_bytes()
        if not payload:
            raise CacheError("That file is empty.")
        if len(payload) > 100_000_000:
            raise CacheError("That file is too large.")
        tp = self.path(target)
        if not tp.is_file():
            raise CacheError("Target entry file is not in the cache (join an experience / use the emote once first).")
        det = self.detect_length_field()
        if not det:
            raise CacheError("Could not work out the cache header layout from your entries. "
                             "Click 'Probe header' and send me the log.")
        orig = app_dir() / "originals" / target
        template = orig.read_bytes() if self.swap_status(target) == "swapped" and orig.is_file() else tp.read_bytes()
        entry = build_entry(template, payload, det["fields"])
        ident = hashlib.md5(entry).hexdigest()
        lib = app_dir() / "library"
        lib.mkdir(exist_ok=True)
        (lib / ident).write_bytes(entry)
        if name or not self.name_of(ident):
            Store.set_name(ident, name or ("Import: " + Path(path).stem))
        return self.swap(target, ident, origin=origin or ("file " + Path(path).name))

    # -- operations
    def swap(self, target: str, source: str, origin: str = "") -> str:
        self._require_closed()
        self.validate()
        self._check_id(target)
        self._check_id(source)
        if target == source:
            raise CacheError("Target and source are the same entry.")
        tp = self.path(target)
        if not tp.is_file():
            raise CacheError("Target entry file is not in the cache (join an experience / use the emote once first).")
        if self.db_size(target) is None:
            raise CacheError("Target has no row in the database.")
        if self.swap_status(source) == "swapped":
            raise CacheError("Source is currently swapped itself. Restore it first.")
        new = self._source_bytes(source)

        self.backup_db()
        orig_dir = app_dir() / "originals"
        orig_dir.mkdir(exist_ok=True)
        if self.swap_status(target) != "swapped":  # current file is a genuine Roblox copy
            shutil.copy2(tp, orig_dir / target)
        lib = app_dir() / "library"
        lib.mkdir(exist_ok=True)
        if not (lib / source).is_file():
            (lib / source).write_bytes(new)

        tmp = tp.with_name(tp.name + ".tmp")
        tmp.write_bytes(new)
        os.replace(tmp, tp)
        self.set_db_size(target, len(new))

        m = self.manifest()
        m[target] = {"source": source, "size": len(new), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "origin": origin or ("entry " + source[:8])}
        _save("manifest.json", m)
        self._log_history("swap", target, source, origin)
        return f"Swapped {target[:8]}… <- {source[:8]}… ({len(new)} bytes). Launch Roblox to test."

    def restore(self, target: str) -> str:
        self._require_closed()
        self.validate()
        self._check_id(target)
        m = self.manifest()
        if target not in m:
            raise CacheError("That entry has no swap to restore.")
        status = self.swap_status(target)
        tp = self.path(target)
        orig = app_dir() / "originals" / target
        if status == "swapped":
            if not orig.is_file():
                raise CacheError("Original copy is missing from the app's originals folder.")
            data = orig.read_bytes()
            tmp = tp.with_name(tp.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, tp)
            self.set_db_size(target, len(data))
            msg = f"Restored {target[:8]}… to the original ({len(data)} bytes)."
        else:
            with contextlib.suppress(OSError, CacheError):
                self.set_db_size(target, tp.stat().st_size)  # keep db consistent with the real file
            msg = f"{target[:8]}… was already rewritten by Roblox; nothing to restore."
        self._log_history("restore", target, m[target].get("source", ""), status)
        m.pop(target, None)
        _save("manifest.json", m)
        with contextlib.suppress(OSError):
            orig.unlink()
        return msg

    def restore_all(self) -> list:
        msgs = []
        for t in list(self.manifest()):
            try:
                msgs.append(self.restore(t))
            except CacheError as e:
                msgs.append(f"{t[:8]}…: {e}")
                if "Roblox is running" in str(e):
                    break
        return msgs or ["Nothing to restore."]

    # -- presets
    @staticmethod
    def presets() -> list:
        return _load("presets.json", [])

    @staticmethod
    def save_preset(name: str, target: str, source: str):
        items = [p for p in _load("presets.json", []) if p["name"] != name]
        items.append({"name": name, "target": target, "source": source})
        _save("presets.json", items)

    @staticmethod
    def delete_preset(name: str):
        _save("presets.json", [p for p in _load("presets.json", []) if p["name"] != name])

    def apply_preset(self, name: str) -> str:
        for p in self.presets():
            if p["name"] == name:
                return self.swap(p["target"], p["source"])
        raise CacheError(f"No preset named '{name}'.")


# --------------------------------------------------------------- profiles
def _profiles() -> dict:
    return _load("profiles.json", {})


def profile_names() -> list:
    return sorted(_profiles())


def save_profile(name: str, store: "Store"):
    """Remember every currently active swap as a named profile."""
    items = [{"target": t, "source": i["source"]} for t, i in store.manifest().items()
             if store.swap_status(t) == "swapped"]
    if not items:
        raise CacheError("There are no active replacements to save.")
    p = _profiles()
    p[name] = items
    _save("profiles.json", p)


def delete_profile(name: str):
    p = _profiles()
    p.pop(name, None)
    _save("profiles.json", p)


def apply_profile(name: str, store: "Store") -> list:
    items = _profiles().get(name)
    if items is None:
        raise CacheError(f"No profile named '{name}'.")
    return [store.swap(i["target"], i["source"], origin=f"profile {name}") for i in items]


def export_profile(name: str, path) -> int:
    """Write a profile (with the source files, so it works on another PC) to a .cswap JSON file."""
    import base64
    items = _profiles().get(name)
    if items is None:
        raise CacheError(f"No profile named '{name}'.")
    names = _load("names.json", {})
    out = {"format": "cacheswap-profile", "version": 1, "name": name, "items": []}
    for i in items:
        lib = app_dir() / "library" / i["source"]
        if not lib.is_file():
            raise CacheError(f"Source {i['source'][:8]}… is not in the app library, so it cannot be exported.")
        out["items"].append({**i, "label": names.get(i["source"], ""), "tlabel": names.get(i["target"], ""),
                             "data": base64.b64encode(lib.read_bytes()).decode(),
                             "sha256": hashlib.sha256(lib.read_bytes()).hexdigest()})
    Path(path).write_text(json.dumps(out), "utf-8")
    return len(items)


def import_profile(path) -> str:
    import base64
    try:
        obj = json.loads(Path(path).read_text("utf-8"))
        assert obj["format"] == "cacheswap-profile"
        items = obj["items"]
    except (OSError, ValueError, KeyError, AssertionError):
        raise CacheError("That is not a CacheSwap profile file.")
    lib = app_dir() / "library"
    lib.mkdir(exist_ok=True)
    clean = []
    for i in items:
        t, src = i.get("target", ""), i.get("source", "")
        if not (HEX32.fullmatch(t) and HEX32.fullmatch(src)):
            raise CacheError("Profile contains an invalid entry ID.")
        data = base64.b64decode(i["data"])
        if hashlib.sha256(data).hexdigest() != i.get("sha256"):
            raise CacheError("Profile data does not match its source ID (file damaged).")
        (lib / src).write_bytes(data)
        if i.get("label"):
            Store.set_name(src, i["label"])
        if i.get("tlabel") and not DEFAULT_NAMES.get(t):
            Store.set_name(t, i["tlabel"])
        clean.append({"target": t, "source": src})
    name = str(obj.get("name") or "imported")
    p = _profiles()
    p[name] = clean
    _save("profiles.json", p)
    return name


# --------------------------------------------------------------- learn mode
class LearnSession:
    """Watches cache file stats (read-only) and reports what Roblox wrote or rewrote."""

    def __init__(self, store: Store):
        self.store = store
        self.base = store.snapshot()
        self.seen = dict(self.base)
        self.first: dict = {}
        self.marks: list = []
        self.t0 = time.time()

    def poll(self):
        for ident, st in self.store.snapshot().items():
            if self.seen.get(ident) != st:
                self.first.setdefault(ident, time.time() - self.t0)
                self.seen[ident] = st

    def mark(self, label: str):
        self.marks.append((time.time() - self.t0, label))

    def results(self) -> list:
        self.poll()
        rows = []
        for ident, t in sorted(self.first.items(), key=lambda kv: -kv[1]):
            e = self.store.describe(ident)
            if e is None:
                continue
            phase = "before any mark"
            for mt, label in self.marks:
                if mt <= t:
                    phase = "after: " + label
            rows.append({"t": t, "phase": phase, "new": ident not in self.base, "entry": e})
        return rows
