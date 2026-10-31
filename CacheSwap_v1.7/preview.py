"""preview.py - parsers behind the preview pane (no Qt in here, so they are easy to test).

Everything is best effort: a parser that does not understand a file raises PreviewError and the
pane falls back to showing basic information. Nothing here ever writes to the Roblox cache.
"""
from __future__ import annotations

import gzip
import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import numpy as np

try:  # optional: fast LZ4 and zstd (pip install lz4 zstandard); pure-Python LZ4 is the fallback
    import lz4.block as _lz4
except Exception:  # pragma: no cover
    _lz4 = None
try:
    import zstandard as _zstd
except Exception:  # pragma: no cover
    _zstd = None

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


class PreviewError(Exception):
    pass


def decompress_chunk(body: bytes, usize: int) -> bytes:
    """Decompress one rbxm chunk body. zstd is recognised by its magic number, otherwise LZ4."""
    if body[:4] == ZSTD_MAGIC:
        if _zstd is None:
            raise PreviewError("zstd-compressed chunk: install the 'zstandard' package")
        try:
            return _zstd.ZstdDecompressor().decompress(body, max_output_size=max(usize, 1))
        except Exception as e:
            raise PreviewError(f"corrupt zstd data ({e})")
    if _lz4 is not None:
        try:
            return _lz4.decompress(body, uncompressed_size=usize)
        except Exception:
            pass  # fall through to the pure-Python decoder for a clearer error
    return lz4_block_decompress(body, usize)


# ------------------------------------------------------------------ LZ4 (block)
def lz4_block_decompress(src: bytes, usize: int) -> bytes:
    """Small pure-Python LZ4 block decoder (rbxm chunks are LZ4 compressed)."""
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = token & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        if off == 0 or off > len(out):
            raise PreviewError("corrupt LZ4 data")
        start = len(out) - off
        if off >= ml:
            out += out[start:start + ml]
        else:
            for k in range(ml):
                out.append(out[start + k])
        if len(out) > usize + 1024:
            raise PreviewError("LZ4 output larger than declared")
    if len(out) != usize:
        raise PreviewError("LZ4 output size mismatch")
    return bytes(out)


# ------------------------------------------------------------------ rbxm animation
PRIORITY = {0: "Idle", 1: "Movement", 2: "Action", 3: "Action2", 4: "Action3", 5: "Action4", 1000: "Core"}
RBXM_MAGIC = b"<roblox!\x89\xff\r\n\x1a\n"


@dataclass
class AnimInfo:
    kind: str = "unknown"          # KeyframeSequence | CurveAnimation | unknown
    name: str = ""
    priority: str = ""
    loop: bool | None = None
    length: float | None = None    # seconds (KeyframeSequence only)
    keyframes: int = 0
    keyframe_times: list = None
    classes: dict = None
    names: list = None             # pose / joint names found
    notes: list = None

    def lines(self) -> list:
        out = [f"Format: {self.kind}"]
        if self.name:
            out.append(f"Name: {self.name}")
        if self.priority:
            out.append(f"Priority: {self.priority}")
        if self.loop is not None:
            out.append(f"Looped: {'yes' if self.loop else 'no'}")
        if self.length is not None:
            out.append(f"Length: {self.length:.3f} s")
        if self.keyframes:
            out.append(f"Keyframes: {self.keyframes}")
        if self.keyframe_times:
            shown = ", ".join(f"{t:.2f}" for t in self.keyframe_times[:24])
            out.append("Keyframe times (s): " + shown + (" ..." if len(self.keyframe_times) > 24 else ""))
        out.append("Contains: " + ", ".join(f"{k} x{v}" for k, v in (self.classes or {}).items()))
        if self.names:
            out.append("Names: " + ", ".join(self.names[:40]) + (" ..." if len(self.names) > 40 else ""))
        out += self.notes or []
        return out


def _rstr(b: bytes, o: int):
    n = struct.unpack_from("<I", b, o)[0]
    if n > len(b):
        raise PreviewError("bad string length")
    return b[o + 4:o + 4 + n].decode("utf-8", "replace"), o + 4 + n


def _interleaved_u32(buf: bytes, count: int):
    if len(buf) < 4 * count:
        raise PreviewError("short property data")
    a = np.frombuffer(buf[:4 * count], dtype=np.uint8).reshape(4, count).astype(np.uint32)
    return (a[0] << 24) | (a[1] << 16) | (a[2] << 8) | a[3]


def _decode_float(u):
    u = u.astype(np.uint32)
    rot = ((u >> 1) | (u << 31)).astype(np.uint32)  # Roblox stores floats rotated left by one bit
    return rot.view(np.float32)


def parse_rbxm_animation(payload: bytes) -> AnimInfo:
    if payload[:14] != RBXM_MAGIC:
        raise PreviewError("not a binary Roblox model")
    if len(payload) < 32:
        raise PreviewError("truncated model")
    pos = 32
    classes: dict = {}
    info = AnimInfo(classes={}, names=[], notes=[])
    seen_names = set()
    while pos + 16 <= len(payload):
        tag = payload[pos:pos + 4]
        clen, ulen = struct.unpack_from("<II", payload, pos + 4)
        size = clen or ulen
        body = payload[pos + 16:pos + 16 + size]
        pos += 16 + size
        if tag.startswith(b"END"):
            break
        if clen:
            if ulen > 64_000_000:
                raise PreviewError("model too large")
            try:
                body = decompress_chunk(body, ulen)
            except (PreviewError, IndexError, struct.error) as e:
                info.notes.append(f"(skipped chunk {tag.decode('latin1').strip(chr(0))}: {e})")
                continue
        try:
            if tag == b"INST":
                cid = struct.unpack_from("<I", body, 0)[0]
                cname, o = _rstr(body, 4)
                count = struct.unpack_from("<I", body, o + 1)[0]
                classes[cid] = (cname, count)
                info.classes[cname] = info.classes.get(cname, 0) + count
            elif tag == b"PROP":
                cid = struct.unpack_from("<I", body, 0)[0]
                pname, o = _rstr(body, 4)
                ptype = body[o]
                vals = body[o + 1:]
                cname, count = classes[cid]
                if ptype == 0x01 and pname == "Name":
                    p = 0
                    for _ in range(count):
                        s, p = _rstr(vals, p)
                        if cname in ("CurveAnimation", "KeyframeSequence") and not info.name:
                            info.name = s
                        elif s and s not in seen_names and cname in ("Pose", "Folder", "Vector3Curve",
                                                                    "EulerRotationCurve", "FloatCurve"):
                            seen_names.add(s)
                            info.names.append(s)
                elif ptype == 0x12 and pname == "Priority" and cname in ("CurveAnimation", "KeyframeSequence"):
                    v = int(_interleaved_u32(vals, count)[0])
                    info.priority = PRIORITY.get(v, f"value {v}")
                elif ptype == 0x02 and pname == "Loop" and cname in ("CurveAnimation", "KeyframeSequence"):
                    info.loop = bool(vals[0])
                elif ptype == 0x04 and pname == "Time" and cname == "Keyframe":
                    times = sorted(float(x) for x in _decode_float(_interleaved_u32(vals, count)))
                    info.keyframe_times = times
                    info.keyframes = len(times)
                    info.length = times[-1] if times else 0.0
        except (struct.error, IndexError, KeyError):
            info.notes.append(f"(could not read property chunk {tag.decode('latin1')})")
    if "KeyframeSequence" in info.classes:
        info.kind = "KeyframeSequence"
    elif "CurveAnimation" in info.classes:
        info.kind = "CurveAnimation"
        info.notes.append("Curve timing is decoded by the dedicated animation preview when the curve data is valid.")
    return info


# ------------------------------------------------------------------ meshes
@dataclass
class Mesh:
    verts: np.ndarray   # (N, 3) float32
    faces: np.ndarray   # (M, 3) int
    version: str
    note: str = ""


_VER = re.compile(rb"version (\d+)\.(\d+)")


def parse_mesh(data: bytes) -> Mesh:
    m = _VER.match(data)
    nl = data.find(b"\n")
    if not m or nl < 0:
        raise PreviewError("not a Roblox mesh")
    major, minor = int(m.group(1)), int(m.group(2))
    ver = f"{major}.{minor:02d}"
    body = nl + 1
    try:
        if major == 1:
            return _mesh_v1(data, body, ver)
        if major == 2:
            h, vsize, fsize, nv, nf = struct.unpack_from("<HBBII", data, body)
            return _mesh_binary(data, body + h, vsize, fsize, nv, nf, ver)
        if major == 3:
            h, vsize, fsize, lodsize, nlods, nv, nf = struct.unpack_from("<HBBHHII", data, body)
            off = body + h
            lods = []
            lod_at = off + nv * vsize + nf * fsize
            for i in range(nlods):
                lods.append(struct.unpack_from("<I", data, lod_at + 4 * i)[0])
            return _mesh_binary(data, off, vsize, fsize, nv, nf, ver, lods)
        if major == 4:
            h, _lt, nv, nf, nlods, nbones, _nt, _ns, _hq, _u = struct.unpack_from("<HHIIHHIHBB", data, body)
            off = body + h
            vsize, fsize = 40, 12
            faces_at = off + nv * vsize + (8 * nv if nbones else 0)
            lods = [struct.unpack_from("<I", data, faces_at + nf * fsize + 4 * i)[0] for i in range(nlods)]
            return _mesh_binary(data, off, vsize, fsize, nv, nf, ver, lods, faces_at=faces_at)
        if major == 5:  # same body as v4 behind a 32-byte header (adds FACS fields at the end)
            h, _lt, nv, nf, nlods, nbones, _bn, _ns, _hq, _u, _fmt, _fsz = struct.unpack_from(
                "<HHIIHHIHBBII", data, body)
            off = body + h
            vsize, fsize = 40, 12
            faces_at = off + nv * vsize + (8 * nv if nbones else 0)
            lods = [struct.unpack_from("<I", data, faces_at + nf * fsize + 4 * i)[0] for i in range(nlods)]
            return _mesh_binary(data, off, vsize, fsize, nv, nf, ver, lods, faces_at=faces_at)
        if major in (6, 7):
            return _mesh_chunked(data, body, ver, major)
    except struct.error as e:
        raise PreviewError(f"mesh {ver} header unreadable: {e}")
    raise PreviewError(f"mesh version {ver} is not supported by the preview yet")


def _mesh_chunked(data: bytes, pos: int, ver: str, major: int) -> Mesh:
    """v6: plain chunks (8-byte name, u32 version, u32 size, data). v7: COREMESH holds Draco data."""
    lods = None
    core_mesh = None
    while pos + 16 <= len(data):
        name = data[pos:pos + 8].rstrip(b"\x00 ")
        cver, size = struct.unpack_from("<II", data, pos + 8)
        chunk = data[pos + 16:pos + 16 + size]
        pos += 16 + size
        if len(chunk) != size:
            raise PreviewError("mesh chunk is truncated")
        if name == b"COREMESH":
            core_mesh = (cver, chunk)
        elif name == b"LODS" and size >= 7:
            n = struct.unpack_from("<I", chunk, 3)[0]
            if 0 < n <= 64 and size >= 7 + 4 * n:
                lods = list(struct.unpack_from(f"<{n}I", chunk, 7))
    if core_mesh is None:
        raise PreviewError("mesh has no COREMESH chunk")
    cver, chunk = core_mesh
    if cver == 2 or major == 7:
        return _mesh_draco(chunk, ver)
    nv = struct.unpack_from("<I", chunk, 0)[0]
    if nv > 5_000_000:
        raise PreviewError("mesh is too large to preview")
    # vertex stride is 36 or 40 bytes; the chunk size tells which one is right
    rest = len(chunk) - 8
    vsize = next((v for v in (40, 36) if rest - nv * v >= 0 and (rest - nv * v) % 12 == 0), None)
    if vsize is None:
        raise PreviewError("COREMESH size does not fit 36- or 40-byte vertices")
    nf = struct.unpack_from("<I", chunk, 4 + nv * vsize)[0]
    blob = chunk[:4 + nv * vsize] + chunk[4 + nv * vsize + 4:]
    return _mesh_binary(blob, 4, vsize, 12, nv, nf, ver, lods)


def _mesh_draco(chunk: bytes, ver: str) -> Mesh:
    try:
        import DracoPy
    except Exception:
        raise PreviewError("mesh 7.x is Draco-compressed: install the 'DracoPy' package (pip install DracoPy)")
    n = struct.unpack_from("<I", chunk, 0)[0]
    stream = chunk[4:4 + n]
    if len(stream) != n:
        raise PreviewError("Draco stream is truncated")
    try:
        d = DracoPy.decode(stream)
    except Exception as e:
        raise PreviewError(f"Draco decode failed: {e}")
    verts = np.asarray(d.points, dtype=np.float32).reshape(-1, 3)
    faces = np.asarray(d.faces, dtype=np.int64).reshape(-1, 3)
    if len(faces) and faces.max() >= len(verts):
        raise PreviewError("mesh faces point outside the vertex list")
    return Mesh(verts, faces, ver, "Draco")


def _mesh_v1(data: bytes, body: int, ver: str) -> Mesh:
    end = data.find(b"\n", body)
    nfaces = int(data[body:end].strip())
    nums = re.findall(rb"\[([^\]]*)\]", data[end + 1:])
    if len(nums) < nfaces * 9:
        raise PreviewError("mesh 1.x vertex data is shorter than its face count")
    pos = np.array([[float(x) for x in nums[i].split(b",")[:3]] for i in range(0, nfaces * 9, 3)],
                   dtype=np.float32)
    if ver == "1.00":
        pos *= 0.5  # version 1.00 stores positions at double scale
    return Mesh(pos, np.arange(nfaces * 3, dtype=np.int64).reshape(nfaces, 3), ver)


def _mesh_binary(data, off, vsize, fsize, nv, nf, ver, lods=None, faces_at=None) -> Mesh:
    if vsize < 12 or fsize != 12:
        raise PreviewError(f"unexpected vertex/face size ({vsize}/{fsize})")
    if nv > 5_000_000 or nf > 10_000_000:
        raise PreviewError("mesh is too large to preview")
    faces_at = off + nv * vsize if faces_at is None else faces_at
    if faces_at + nf * 12 > len(data) or off + nv * vsize > len(data):
        raise PreviewError("mesh data is shorter than its header says")
    raw = np.frombuffer(data, dtype=np.uint8, count=nv * vsize, offset=off).reshape(nv, vsize)
    verts = raw[:, :12].copy().view("<f4").reshape(nv, 3).astype(np.float32)
    faces = np.frombuffer(data, dtype="<u4", count=nf * 3, offset=faces_at).reshape(nf, 3).astype(np.int64)
    note = ""
    if lods and len(lods) >= 2 and 0 <= lods[0] <= lods[1] <= nf:
        faces = faces[lods[0]:lods[1]]
        note = f"showing LOD 0 of {len(lods) - 1}"
    if len(faces) and faces.max() >= nv:
        raise PreviewError("mesh faces point outside the vertex list")
    if not np.isfinite(verts).all():
        raise PreviewError("mesh has invalid vertices")
    return Mesh(verts, faces, ver, note)


# ------------------------------------------------------------------ other types
def image_kind(payload: bytes) -> str:
    """Identify image/texture containers by their magic bytes."""
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
    return ""


def decode_ktx1_uncompressed(payload: bytes):
    """Decode the first 2D KTX1 mip when stored as uncompressed RGB/RGBA8.

    Returns an HxWx4 uint8 NumPy array, or None for formats requiring a GPU or
    a block-compression transcoder. The file is not modified.
    """
    if payload[:12] != b"\xabKTX 11\xbb\r\n\x1a\n" or len(payload) < 68:
        return None
    try:
        fields = struct.unpack_from("<13I", payload, 12)
        endian, gl_type, gl_type_size, gl_format, _internal, _base, width, height, depth, arrays, faces, mips, kv_bytes = fields
    except struct.error:
        return None
    if endian != 0x04030201 or gl_type != 0x1401 or gl_type_size != 1 or gl_format not in (0x1907, 0x1908):
        return None
    if width < 1 or height < 1 or depth not in (0, 1) or arrays not in (0, 1) or faces != 1 or mips < 1:
        return None
    pos = 64 + kv_bytes
    if pos + 4 > len(payload):
        return None
    image_size = struct.unpack_from("<I", payload, pos)[0]
    pos += 4
    channels = 4 if gl_format == 0x1908 else 3
    row_bytes = width * channels
    padded_row = (row_bytes + 3) & ~3
    needed = padded_row * height
    if image_size < needed or pos + min(image_size, needed) > len(payload):
        # Some exporters omit row padding from the imageSize field; try tightly packed data.
        needed = row_bytes * height
        padded_row = row_bytes
        if image_size < needed or pos + needed > len(payload):
            return None
    try:
        import numpy as np
        raw = payload[pos:pos + needed]
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(height, padded_row)[:, :row_bytes]
        arr = arr.reshape(height, width, channels)
        if channels == 4:
            return arr.copy()
        alpha = np.full((height, width, 1), 255, dtype=np.uint8)
        return np.concatenate((arr, alpha), axis=2)
    except (ValueError, ImportError):
        return None


def parse_texturepack(payload: bytes) -> list[dict[str, object]]:
    """Read map ID references from a Roblox TexturePack XML payload."""
    data = payload
    if data.startswith(b"\x1f\x8b"):
        try:
            data = gzip.decompress(data)
        except OSError:
            return []
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError):
        return []
    tag_to_slot = {
        "color": 0, "albedo": 0, "diffuse": 0, "basecolor": 0,
        "normal": 1, "normalmap": 1, "bumpmap": 1,
        "metalness": 2, "orm": 2, "roughness": 3,
        "emissive": 4, "emissivemap": 4, "height": 5,
        "heightmap": 5, "displacement": 5,
    }
    result = []
    for child in root:
        tag = child.tag.rsplit("}", 1)[-1].lower()
        slot = tag_to_slot.get(tag)
        value = (child.text or child.get("id") or "").strip()
        if slot is not None and value.isdigit() and value != "0":
            result.append({"name": tag.title(), "asset_id": value, "slot": slot})
    return result


def png_size(payload: bytes):
    if payload[:4] == b"\x89PNG" and len(payload) >= 24:
        return struct.unpack_from(">II", payload, 16)
    return None


def ktx_info(payload: bytes) -> str:
    if payload[:12] == b"\xabKTX 11\xbb\r\n\x1a\n" and len(payload) >= 64:
        w, h = struct.unpack_from("<II", payload, 36)
        return f"KTX 1 texture, {w}x{h}"
    if payload[:12] == b"\xabKTX 20\xbb\r\n\x1a\n" and len(payload) >= 28:
        w, h = struct.unpack_from("<II", payload, 20)
        return f"KTX 2 texture, {w}x{h}"
    return "KTX texture"


# ------------------------------------------------------------------ image channel views
CHANNELS = ("RGBA", "R", "G", "B", "A")


def channel_view(rgba: np.ndarray, mode: str) -> np.ndarray:
    """rgba: (H, W, 4) uint8. 'RGBA' returns it unchanged; R/G/B/A return that channel as opaque grey."""
    if mode == "RGBA":
        return rgba
    i = "RGBA".index(mode)
    g = rgba[:, :, i]
    return np.stack([g, g, g, np.full_like(g, 255)], axis=2)
