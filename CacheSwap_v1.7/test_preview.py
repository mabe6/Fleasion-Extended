"""Tests for the preview parsers and pane. Run:  py test_preview.py   (needs PySide6 + numpy)"""
import os
import sqlite3
import struct
import sys
import tempfile
import time
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["CACHESWAP_HOME"] = str(tmp / "app")
os.environ["CACHESWAP_ROBLOX_DIR"] = str(tmp / "Roblox")

import numpy as np  # noqa: E402

import core  # noqa: E402
import preview  # noqa: E402

REAL_ANIM = os.environ.get("REAL_ANIM", "")  # optional: a real downloaded .rbxm

# ---------------------------------------------------------------- mesh encoders
CUBE_V = np.array([[x, y, z] for x in (0, 1) for y in (0, 1) for z in (0, 1)], dtype="<f4")
CUBE_F = np.array([[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1],
                   [2, 3, 7], [2, 7, 6], [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]], dtype="<u4")


def vertex_block(vsize):
    out = bytearray()
    for v in CUBE_V:
        rec = struct.pack("<3f", *v) + struct.pack("<3f", 0, 1, 0) + struct.pack("<2f", 0, 0)
        out += rec.ljust(vsize, b"\x00")
    return bytes(out)


def mesh_v1(scale_double=True):
    tris = CUBE_V[CUBE_F]
    body = "".join(f"[{p[0]:g},{p[1]:g},{p[2]:g}][0,1,0][0,0,0]" for t in tris for p in t * (2 if scale_double else 1))
    return b"version 1.00\n12\n" + body.encode()


def mesh_v2():
    return (b"version 2.00\n" + struct.pack("<HBBII", 12, 40, 12, 8, 12) + vertex_block(40) + CUBE_F.tobytes())


def mesh_v3(lods=(0, 12, 12)):
    return (b"version 3.00\n" + struct.pack("<HBBHHII", 16, 40, 12, 4, len(lods), 8, 12)
            + vertex_block(40) + CUBE_F.tobytes() + struct.pack(f"<{len(lods)}I", *lods))


def mesh_v4(bones=0):
    return (b"version 4.00\n" + struct.pack("<HHIIHHIHBB", 24, 1, 8, 12, 2, bones, 0, 1, 1, 0)
            + vertex_block(40) + (b"\x00" * 64 if bones else b"") + CUBE_F.tobytes() + struct.pack("<2I", 0, 12))


for name, data in (("1.00", mesh_v1()), ("2.00", mesh_v2()), ("3.00", mesh_v3()), ("4.00", mesh_v4()),
                   ("4.00 with bones", mesh_v4(bones=2))):
    m = preview.parse_mesh(data)
    assert len(m.faces) == 12, (name, len(m.faces))
    ext = m.verts.max(axis=0) - m.verts.min(axis=0)
    assert np.allclose(ext, 1.0, atol=1e-5), (name, ext)  # v1.00 halves its doubled coordinates
    assert m.faces.max() < len(m.verts)
assert "LOD 0" in preview.parse_mesh(mesh_v3()).note

def mesh_v5(bones=0):
    return (b"version 5.00\n" + struct.pack("<HHIIHHIHBBII", 32, 1, 8, 12, 2, bones, 0, 1, 1, 0, 1, 0)
            + vertex_block(40) + (b"\x00" * 64 if bones else b"") + CUBE_F.tobytes() + struct.pack("<2I", 0, 12))


def mchunk(name, ver, data):
    return name.ljust(8, b"\x00") + struct.pack("<II", ver, len(data)) + data


def mesh_v6(vsize=40):
    core_ = struct.pack("<I", 8) + vertex_block(vsize) + struct.pack("<I", 12) + CUBE_F.tobytes()
    return (b"version 6.00\n" + mchunk(b"COREMESH", 1, core_)
            + mchunk(b"LODS", 1, struct.pack("<HBI", 0, 1, 2) + struct.pack("<2I", 0, 12)))


import DracoPy  # noqa: E402


def mesh_v7():
    enc = DracoPy.encode(CUBE_V.astype("f4"), CUBE_F.astype("int32"))
    return b"version 7.00\n" + mchunk(b"COREMESH", 2, struct.pack("<I", len(enc)) + enc)


for name, data in (("5.00", mesh_v5()), ("5.00 bones", mesh_v5(2)), ("6.00/40", mesh_v6(40)),
                   ("6.00/36", mesh_v6(36)), ("7.00 draco", mesh_v7())):
    m = preview.parse_mesh(data)
    assert len(m.faces) == 12, (name, len(m.faces))
    assert np.allclose(m.verts.max(axis=0) - m.verts.min(axis=0), 1.0, atol=1e-2), name
    assert m.faces.max() < len(m.verts)
assert preview.parse_mesh(mesh_v7()).note == "Draco"
try:
    preview.parse_mesh(b"version 6.00\n" + mchunk(b"LODS", 1, b"\x00" * 7))
except preview.PreviewError as e:
    assert "COREMESH" in str(e)
else:
    raise AssertionError("v6 without COREMESH must fail")

for bad, label in ((b"version 8.00\nxxxx", "unsupported version"), (b"notamesh", "not a mesh"),
                   (mesh_v2()[:-30], "truncated"), (b"version 2.00\n" + struct.pack("<HBBII", 12, 40, 12, 8, 99),
                                                    "faces missing")):
    try:
        preview.parse_mesh(bad)
    except preview.PreviewError:
        pass
    else:
        raise AssertionError("expected PreviewError: " + label)

# ----------------------------------------------------- rbxm (KeyframeSequence) builder
def rstr(s):
    b = s.encode()
    return struct.pack("<I", len(b)) + b


def chunk(tag, data):
    return tag + struct.pack("<III", 0, len(data), 0) + data


def interleave_u32(values):
    raw = [struct.pack(">I", v) for v in values]
    return b"".join(bytes(r[i] for r in raw) for i in range(4))


def enc_float(x):
    u = struct.unpack("<I", struct.pack("<f", x))[0]
    return ((u << 1) | (u >> 31)) & 0xFFFFFFFF


def keyframe_rbxm(times, priority=5, loop=True):
    n = len(times)
    out = preview.RBXM_MAGIC + struct.pack("<Hii", 0, 3, n + 2 + 1) + bytes(8)
    out += chunk(b"INST", struct.pack("<I", 0) + rstr("KeyframeSequence") + b"\x00" + struct.pack("<I", 1) + bytes(4))
    out += chunk(b"INST", struct.pack("<I", 1) + rstr("Keyframe") + b"\x00" + struct.pack("<I", n) + bytes(4 * n))
    out += chunk(b"INST", struct.pack("<I", 2) + rstr("Pose") + b"\x00" + struct.pack("<I", 2) + bytes(8))
    out += chunk(b"PROP", struct.pack("<I", 0) + rstr("Name") + b"\x01" + rstr("Wave"))
    out += chunk(b"PROP", struct.pack("<I", 0) + rstr("Priority") + b"\x12" + interleave_u32([priority]))
    out += chunk(b"PROP", struct.pack("<I", 0) + rstr("Loop") + b"\x02" + bytes([1 if loop else 0]))
    out += chunk(b"PROP", struct.pack("<I", 1) + rstr("Time") + b"\x04" + interleave_u32([enc_float(t) for t in times]))
    out += chunk(b"PROP", struct.pack("<I", 2) + rstr("Name") + b"\x01" + rstr("Head") + rstr("Torso"))
    out += b"END\x00" + struct.pack("<III", 0, 9, 0) + b"</roblox>"
    return out


ks = keyframe_rbxm([0.0, 0.5, 1.25])
info = preview.parse_rbxm_animation(ks)
assert info.kind == "KeyframeSequence" and info.name == "Wave" and info.priority == "Action4" and info.loop is True
assert info.keyframes == 3 and abs(info.length - 1.25) < 1e-6 and info.keyframe_times == [0.0, 0.5, 1.25], info.lines()
assert info.names == ["Head", "Torso"], info.names
assert keyframe_rbxm([0.0], priority=1000, loop=False) and \
    preview.parse_rbxm_animation(keyframe_rbxm([0.0], 1000, False)).priority == "Core"
for bad in (b"", b"<roblox!" + b"\x00" * 10, ks[:20]):
    try:
        preview.parse_rbxm_animation(bad)
    except preview.PreviewError:
        pass
    else:
        raise AssertionError("expected PreviewError for bad rbxm")

if REAL_ANIM and os.path.isfile(REAL_ANIM):
    real = preview.parse_rbxm_animation(open(REAL_ANIM, "rb").read())
    assert real.kind == "CurveAnimation" and real.priority == "Action4" and real.loop is True, real.lines()
    print("real animation file parsed:", real.kind, real.priority, "loop", real.loop)

# ---- compressed chunks: zstd and LZ4 both work, a broken chunk is skipped instead of failing
import lz4.block as lz4b  # noqa: E402
import zstandard  # noqa: E402


def compress_chunks(rbxm: bytes, how: str, corrupt_tag: bytes = b"") -> bytes:
    out, pos = rbxm[:32], 32
    while pos + 16 <= len(rbxm):
        tag = rbxm[pos:pos + 4]
        _c, ulen, _r = struct.unpack_from("<III", rbxm, pos + 4)
        body = rbxm[pos + 16:pos + 16 + ulen]
        pos += 16 + ulen
        if tag.startswith(b"END"):
            out += tag + struct.pack("<III", 0, ulen, 0) + body
            break
        if how == "zstd":
            comp = zstandard.ZstdCompressor().compress(body)
        else:
            comp = lz4b.compress(body, store_size=False)
        if tag == corrupt_tag:
            comp = comp[:4] + b"\xff" * 6
        out += tag + struct.pack("<III", len(comp), ulen, 0) + comp
    return out


ks = keyframe_rbxm([0.0, 0.5, 1.25])
for how in ("zstd", "lz4"):
    i2 = preview.parse_rbxm_animation(compress_chunks(ks, how))
    assert i2.keyframes == 3 and i2.priority == "Action4" and i2.name == "Wave", (how, i2.lines())
soft = preview.parse_rbxm_animation(compress_chunks(ks, "zstd", corrupt_tag=b"PROP"))
assert any("skipped chunk" in n for n in soft.notes), soft.notes   # no exception, a note instead
assert soft.classes.get("KeyframeSequence") == 1
soft = preview.parse_rbxm_animation(compress_chunks(ks, "lz4", corrupt_tag=b"INST"))
assert any("skipped chunk" in n for n in soft.notes)

# ---- channel views
px = np.zeros((2, 2, 4), dtype=np.uint8)
px[..., 0], px[..., 1], px[..., 2], px[..., 3] = 10, 20, 30, 40
assert preview.channel_view(px, "RGBA") is px
for mode, val in (("R", 10), ("G", 20), ("B", 30), ("A", 40)):
    v = preview.channel_view(px, mode)
    assert (v[..., :3] == val).all() and (v[..., 3] == 255).all(), mode

# ----------------------------------------------------------------------- GUI
from PySide6.QtGui import QColor, QGuiApplication, QImage  # noqa: E402
from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

qa = QApplication([])

root = tmp / "Roblox"
(root / "rbx-storage").mkdir(parents=True)
con = sqlite3.connect(root / "rbx-storage.db")
con.execute("CREATE TABLE files (id BLOB PRIMARY KEY, size INTEGER, hits INTEGER, atime INTEGER)")


def make_entry(ident, url, payload):
    u = url.encode()
    meta = bytearray(25)
    struct.pack_into("<I", meta, 13, len(payload))
    data = b"RBXH" + b"\x01\x00\x00\x00" + struct.pack("<I", len(u)) + u + bytes(meta) + payload
    p = root / "rbx-storage" / ident[:2] / ident
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(data)
    con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?)", (bytes.fromhex(ident), len(data), 1, 0))
    con.commit()


img = QImage(64, 32, QImage.Format_RGB32)
img.fill(QColor("red"))
ba = QByteArray()
buf = QBuffer(ba)
buf.open(QIODevice.WriteOnly)
img.save(buf, "PNG")
png_bytes = bytes(ba)

E = {k: f"{i:02x}" * 16 for i, k in enumerate(["png", "mesh", "anim", "ogg", "ktx", "bad", "junk", "json"], 1)}
make_entry(E["png"], "https://x/?id=1", png_bytes)
make_entry(E["mesh"], "https://x/?id=2", mesh_v2())
make_entry(E["anim"], "https://x/?id=3", ks)
make_entry(E["ogg"], "https://x/?id=4", b"OggS" + b"\x00" * 100)
make_entry(E["ktx"], "https://x/?id=5", b"\xabKTX 11\xbb\r\n\x1a\n" + b"\x00" * 24 + struct.pack("<II", 128, 64) + b"\x00" * 40)
make_entry(E["bad"], "https://x/?id=6", b"version 2.00\n" + struct.pack("<HBBII", 12, 40, 12, 8, 99))
make_entry(E["junk"], "https://x/?id=7", b"\x01\x02\x03\x04" * 20)
make_entry(E["json"], "https://x/?id=8", b'{"a": 1}')

from preview_ui import PreviewPane  # noqa: E402

store = core.Store()
pane = PreviewPane(lambda: store)
pane.resize(420, 520)
pane.show()
P = PreviewPane

pane.show_entry(E["png"])
assert pane.stack.currentIndex() == P.PAGE_IMAGE and pane.image._pm is not None
assert "64 x 32" in pane.detail.toPlainText()
img0 = pane.image.grab().toImage()
pane.image.set_mode("R")
assert pane.image.grab().toImage() != img0       # channel view changes the picture
pane.image.set_mode("RGBA")
pane.image.set_checker(False)
assert pane.image.grab().toImage() != img0       # checkerboard off changes the background
pane.image.set_checker(True)

pane.show_entry(E["mesh"])
assert pane.stack.currentIndex() == P.PAGE_MESH and "12 triangles" in pane.mesh_note.text(), pane.mesh_note.text()
pane.mesh.resize(300, 300)
before = pane.mesh.grab().toImage()
bg = QColor("#2b2b2b").rgb()
drawn = sum(1 for x in range(0, 300, 3) for y in range(0, 300, 3) if before.pixel(x, y) != bg)
assert drawn > 200, f"mesh not drawn ({drawn} sampled pixels)"
pane.mesh.yaw += 1.0  # rotating must change the picture
after = pane.mesh.grab().toImage()
assert before != after
pane.wire.setChecked(True)
assert pane.mesh.grab().toImage() != after

pane.show_entry(E["anim"])
txt = pane.info.toPlainText()
assert pane.stack.currentIndex() == P.PAGE_INFO and "Priority: Action4" in txt and "Length: 1.250 s" in txt, txt

pane.show_entry(E["ogg"])
assert pane.stack.currentIndex() == P.PAGE_AUDIO or "audio" in pane.info.toPlainText()

pane.show_entry(E["ktx"])
assert "128x64" in pane.info.toPlainText(), pane.info.toPlainText()

pane.show_entry(E["bad"])  # parser error -> message, no crash
assert pane.stack.currentIndex() == P.PAGE_INFO and "Could not preview" in pane.info.toPlainText()

pane.show_entry(E["junk"])
assert "No preview" in pane.info.toPlainText()
pane.show_entry(E["json"])
assert '"a"' in pane.info.toPlainText()
pane.show_entry("ff" * 16)  # not in the cache
assert "not found" in pane.info.toPlainText()
pane.show_entry(None)
assert "Click an entry" in pane.title.text()

# ---- the real window: clicking a row drives the pane
import app as gui  # noqa: E402

w = gui.MainWindow()
w.show()
w.worker.wait()
end = time.time() + 0.4
while time.time() < end:
    qa.processEvents()
    time.sleep(0.01)
for r in range(w.cache.rowCount()):
    if w.cache.item(r, w.C_ID).text() == E["mesh"]:
        w.cache.setCurrentCell(r, w.C_ID)
assert w.preview.stack.currentIndex() == P.PAGE_MESH
kinds = {w.cache.item(r, w.C_KIND).text() for r in range(w.cache.rowCount())}
assert {"png", "mesh", "anim", "ogg", "ktx", "json"} <= kinds, kinds
# sidebar: one row per kind with counts, selecting a kind filters the table
labels = [w.sidebar.item(i).text() for i in range(w.sidebar.count())]
assert labels[0].startswith("All (") and any(x.startswith("mesh (") for x in labels), labels
for i in range(w.sidebar.count()):
    if w.sidebar.item(i).data(gui.Qt.UserRole) == "mesh":
        w.sidebar.setCurrentRow(i)
assert {w.cache.item(r, w.C_KIND).text() for r in range(w.cache.rowCount())} == {"mesh"}
w.sidebar.setCurrentRow(0)
assert w.cache.rowCount() == len(w.entries)

# drop a file onto a row -> import path (confirmation dialog stubbed), shows up in Replacements
dropfile = tmp / "drop.bin"
dropfile.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x02" * 200)
gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)
gui.QMessageBox.warning = staticmethod(lambda *a, **k: print("WARNING SHOWN:", a[2]))
w.store.__class__._require_closed = staticmethod(lambda: None)
w.store.detect_length_field = lambda *a, **k: {"fields": [(13, 4)], "hits": {13: 3}, "total": 3}
for i in range(w.cache.rowCount()):
    if w.cache.item(i, w.C_ID).text() == E["png"]:
        row_png = i
w.cache.file_dropped.emit(row_png, str(dropfile))
for _ in range(30):
    qa.processEvents()
    time.sleep(0.01)
assert w.repl_table.rowCount() == 1 and "ACTIVE" in w.repl_table.item(0, 4).text(), w.repl_table.rowCount()
assert "drop.bin" in w.repl_table.item(0, 2).text()
assert w.hist_table.rowCount() == 1 and w.hist_table.item(0, 1).text() == "swap"
assert w.tabs.tabText(1) == "Replacements (1)"
w.revert(E["png"])
assert w.repl_table.rowCount() == 0 and w.hist_table.rowCount() == 2
assert w.hist_table.item(0, 1).text() == "restore"
w.close()
print("ALL PREVIEW TESTS PASSED")
