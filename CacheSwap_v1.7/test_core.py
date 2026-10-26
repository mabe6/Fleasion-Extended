"""Tests core.py against a synthetic cache. Run:  py test_core.py"""
import os
import sqlite3
import struct
import sys
import tempfile
import time
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["CACHESWAP_HOME"] = str(tmp / "app")
os.environ["CACHESWAP_ROBLOX_DIR"] = str(tmp / "Roblox")

import core  # noqa: E402

DB_SCHEMA = "CREATE TABLE files (id BLOB PRIMARY KEY, size INTEGER, hits INTEGER, atime INTEGER)"


def new_cache(base):
    (base / "rbx-storage").mkdir(parents=True)
    c = sqlite3.connect(base / "rbx-storage.db")
    c.execute(DB_SCHEMA)
    return c


root = tmp / "Roblox"
con = new_cache(root)


def make_entry(ident, url, payload, off=13, base=None, c=None):
    """off = where the payload length is stored in the 25-byte meta (None = zeros)."""
    base, c = base or root, c or con
    u = url.encode()
    meta = bytearray(25)
    meta[0] = 1
    if off is not None:
        struct.pack_into("<I", meta, off, len(payload))
    data = b"RBXH" + b"\x01\x00\x00\x00" + struct.pack("<I", len(u)) + u + bytes(meta) + payload
    p = base / "rbx-storage" / ident[:2] / ident
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(data)
    c.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?)", (bytes.fromhex(ident), len(data), 1, 0))
    c.commit()
    return data


def expect_error(fn, label):
    try:
        fn()
    except core.CacheError:
        return
    raise AssertionError("expected CacheError: " + label)


ANIM_A = b"<roblox!" + b"KeyframeSequence" + b"A" * 400
ANIM_B = b"<roblox!" + b"KeyframeSequence" + b"B" * 600
CURVE = b"<roblox!" + b"\x89\xff\r\n\x1a\n" + b"CurveAnimation" + b"Z" * 700
WAVE, JUMP, TEX, CRV = core.WAVE_ID, "27d15c439a553129768c5b294edd36c5", "ab" * 16, "ef" * 16
WAVE_URL = "https://assetdelivery.roblox.com/v1/asset/?id=111"
wave_orig = make_entry(WAVE, WAVE_URL, ANIM_A)
jump_data = make_entry(JUMP, "https://assetdelivery.roblox.com/v1/asset/?id=222", ANIM_B)
make_entry(TEX, "https://c0.rbxcdn.com/abc", b"\x89PNG" + b"x" * 300)
make_entry(CRV, "https://assetdelivery.roblox.com/v1/asset/?id=333", CURVE)

s = core.Store()
s.validate()
ents = {e.id: e for e in s.scan()}
assert ents[WAVE].kind == "anim" and ents[WAVE].asset_id == "111", ents[WAVE]
assert ents[TEX].kind == "png" and ents[TEX].asset_id == ""
assert ents[CRV].kind == "curve-anim", ents[CRV]
assert s.kind_warning(WAVE, TEX) != "" and s.kind_warning(WAVE, JUMP) == ""
assert "untested" in s.kind_warning(WAVE, CRV)

# swap
s.swap(WAVE, JUMP)
p = s.path(WAVE)
assert p.read_bytes() == jump_data
assert s.db_size(WAVE) == len(jump_data)
assert s.swap_status(WAVE) == "swapped"
assert (core.app_dir() / "library" / JUMP).read_bytes() == jump_data
assert (core.app_dir() / "originals" / WAVE).read_bytes() == wave_orig
assert list((core.app_dir() / "backups").glob("*/rbx-storage.db"))

# swapping again keeps the ORIGINAL, not the swapped file
s.swap(WAVE, JUMP)
assert (core.app_dir() / "originals" / WAVE).read_bytes() == wave_orig

# guards
for bad in (lambda: s.swap(WAVE, WAVE), lambda: s.swap("zz", JUMP), lambda: s.swap(JUMP, WAVE),
            lambda: s.swap(WAVE, "cd" * 16)):
    expect_error(bad, "swap guard")

# restore
s.restore(WAVE)
assert p.read_bytes() == wave_orig and s.db_size(WAVE) == len(wave_orig)
assert s.swap_status(WAVE) == "" and not s.manifest()

# Roblox reverts the swap -> status 'reverted', new swap saves the NEW genuine original
s.swap(WAVE, JUMP)
p.write_bytes(wave_orig + b"!")  # Roblox rewrote it
con.execute("UPDATE files SET size=? WHERE id=?", (len(wave_orig) + 1, bytes.fromhex(WAVE)))
con.commit()
assert s.swap_status(WAVE) == "reverted"
s.swap(WAVE, JUMP)
assert (core.app_dir() / "originals" / WAVE).read_bytes() == wave_orig + b"!"

# source deleted from cache -> library copy still works
s.restore_all()
s.path(JUMP).unlink()
s.swap(WAVE, JUMP)
assert p.read_bytes() == jump_data
msgs = s.restore_all()
assert s.swap_status(WAVE) == "", msgs
assert p.read_bytes() == wave_orig + b"!"
p.write_bytes(wave_orig)  # back to the pristine original for the import tests
con.execute("UPDATE files SET size=? WHERE id=?", (len(wave_orig), bytes.fromhex(WAVE)))
con.commit()

# names + presets
s.set_name(JUMP, "My jump")
assert s.name_of(JUMP) == "My jump" and s.name_of(WAVE).startswith("Wave")
s.save_preset("wave->jump", WAVE, JUMP)
assert s.presets()[0]["target"] == WAVE
s.apply_preset("wave->jump")
assert s.swap_status(WAVE) == "swapped"
s.restore_all()
s.delete_preset("wave->jump")
assert s.presets() == []

# ---- header layout detection ----
det = s.detect_length_field()
assert det and det["fields"] == [(13, 4)], det
assert "(13, 4)" in s.probe_header()

root2 = tmp / "Roblox2"
con2 = new_cache(root2)
for i in range(4):  # a different layout: length stored at offset 9
    make_entry(f"{i:02x}" * 16, f"https://x/?id={i}", ANIM_A + bytes([i]) * (50 * i), off=9, base=root2, c=con2)
det2 = core.Store(root2).detect_length_field()
assert det2 and det2["fields"] == [(9, 4)], det2

root3 = tmp / "Roblox3"
con3 = new_cache(root3)
for i in range(4):  # nothing in the header matches -> must refuse to guess
    make_entry(f"{i:02x}" * 16, f"https://x/?id={i}", ANIM_A + bytes([i]) * (50 * i), off=None, base=root3, c=con3)
assert core.Store(root3).detect_length_field() is None
assert "NOT found" in core.Store(root3).probe_header()

# ---- import a downloaded CurveAnimation file into Wave ----
f = tmp / "my_anim.rbxm"
f.write_bytes(CURVE)
assert "untested" in s.import_warning(WAVE, f)
s.import_file(WAVE, f)
got = p.read_bytes()
url, start = core.parse_header(got)
assert url == WAVE_URL, url  # the target's own URL is kept
assert got[start:] == CURVE
lo = start - 25 + 13
assert struct.unpack_from("<I", got, lo)[0] == len(CURVE)  # length field patched
assert got[:lo] == wave_orig[:lo] and got[lo + 4:start] == wave_orig[lo + 4:start]  # rest of header untouched
assert s.db_size(WAVE) == len(got) and s.swap_status(WAVE) == "swapped"
assert s.name_of(list(s.manifest().values())[0]["source"]) == "Import: my_anim"

s.import_file(WAVE, f)  # again: the template must still be the ORIGINAL header
assert (core.app_dir() / "originals" / WAVE).read_bytes() == wave_orig
s.restore(WAVE)
assert p.read_bytes() == wave_orig and s.db_size(WAVE) == len(wave_orig)

# import refusals
junk = tmp / "junk.rbxm"
junk.write_bytes(b"")
part = tmp / "part.rbxm"
part.write_bytes(b"<roblox!" + b"\x89\xff\r\n\x1a\n" + b"Part" * 100)
expect_error(lambda: s.import_file(WAVE, junk), "empty file")
real_detect = s.detect_length_field
s.detect_length_field = lambda *a, **k: None
expect_error(lambda: s.import_file(WAVE, f), "layout unknown")
s.detect_length_field = real_detect
assert p.read_bytes() == wave_orig and not s.manifest()  # refusals changed nothing

# learn
ls = core.LearnSession(s)
ls.mark("joined")
time.sleep(0.05)
NEW = "cd" * 16
make_entry(NEW, "https://assetdelivery.roblox.com/v1/asset/?id=120350549417076", ANIM_A)
ls.poll()
res = ls.results()
assert len(res) == 1 and res[0]["new"] and res[0]["entry"].asset_id == "120350549417076"
assert res[0]["phase"] == "after: joined"

# ---- v1.3 additions: header-less entries, export, history, profiles
RAW = "ee" * 16
rawp = root / "rbx-storage" / RAW[:2] / RAW
rawp.parent.mkdir(exist_ok=True)
rawp.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
d = s.describe(RAW)
assert d and d.kind == "png" and d.header is False, d
assert s.read_payload(RAW)[1][:4] == b"\x89PNG"
out = s.export_entry(RAW, tmp / "exp")
assert out.read_bytes() == rawp.read_bytes()
rep = (tmp / "exp" / (RAW + ".txt")).read_text()
assert "RBXH header: NO" in rep and "detected kind: png" in rep
out = s.export_entry(WAVE, tmp / "exp")
assert "RBXH header: yes" in (tmp / "exp" / (WAVE + ".txt")).read_text()

n_hist = len(s.history())
s.swap(WAVE, NEW, origin="test")
assert s.manifest()[WAVE]["origin"] == "test"
h = s.history()
assert len(h) == n_hist + 1 and h[-1]["action"] == "swap" and h[-1]["target"] == WAVE
core.save_profile("pf", s)
assert core.profile_names() == ["pf"]
core.export_profile("pf", tmp / "pf.cswap")
s.restore(WAVE)
assert s.history()[-1]["action"] == "restore"
shutil_lib = core.app_dir() / "library" / NEW
shutil_lib.unlink()                      # simulate another PC without the library file
assert core.import_profile(tmp / "pf.cswap") == "pf"
assert shutil_lib.is_file()
assert core.apply_profile("pf", s) and s.swap_status(WAVE) == "swapped"
bad = tmp / "bad.cswap"
bad.write_text("{}")
expect_error(lambda: core.import_profile(bad), "bad profile")
s.restore(WAVE)
core.delete_profile("pf")
expect_error(lambda: core.save_profile("x", s), "no active swaps")
imp = tmp / "tex.png"
imp.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x01" * 300)
s.import_file(WAVE, imp)  # any file type may be imported now
assert s.read_payload(WAVE)[1] == imp.read_bytes()
s.restore(WAVE)

# sanity: no rows were ever added or removed by swaps/imports (4 originals + the learn entry)
assert con.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 5
print("ALL CORE TESTS PASSED")
