"""Read supported Roblox animation data from a binary .rbxm payload.

This defensive parser handles CurveAnimation tracks and legacy KeyframeSequence
pose CFrames for CacheSwap's local previewer. It never writes to Roblox files.
"""
from __future__ import annotations

import bisect
import math
import struct
from dataclasses import dataclass, field
from typing import Any

RBXM_MAGIC = b"<roblox!\x89\xff\r\n\x1a\n"
# Fleasion current parser uses 14400 ticks per second for ValuesAndTimes.
# Keep the rate in one place and test it against real CurveAnimation samples.
TICKS_PER_SECOND = 14400.0
# Diagnostic threshold only: large finite values are preserved, not rejected.
EXTREME_CURVE_DIAGNOSTIC_THRESHOLD = 1.0e12
MAX_CHUNK_BYTES = 128 * 1024 * 1024
MAX_INSTANCES = 1_000_000

try:
    import lz4.block as _lz4
except Exception:  # pragma: no cover - optional dependency
    _lz4 = None
try:
    import zstandard as _zstd
except Exception:  # pragma: no cover - optional dependency
    _zstd = None


class AnimationFormatError(ValueError):
    """The model is not a usable/recognised CurveAnimation payload."""


@dataclass(frozen=True)
class CurveKey:
    time: float
    value: float
    interpolation: int = 1  # 0 constant, 1 linear, 2 cubic Hermite
    left_tangent: float = 0.0
    right_tangent: float = 0.0


@dataclass
class FloatCurve:
    keys: list[CurveKey] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.keys[-1].time if self.keys else 0.0

    def sample(self, t: float) -> float | None:
        if not self.keys:
            return None
        if len(self.keys) == 1 or t <= self.keys[0].time:
            return self.keys[0].value
        if t >= self.keys[-1].time:
            return self.keys[-1].value
        times = [k.time for k in self.keys]
        right = bisect.bisect_right(times, t)
        left = max(0, right - 1)
        a, b = self.keys[left], self.keys[right]
        dt = b.time - a.time
        if dt <= 1e-12:
            return b.value
        u = max(0.0, min(1.0, (t - a.time) / dt))
        if a.interpolation == 0:
            return a.value
        if a.interpolation == 2:
            # Roblox tangents are slopes with respect to time.
            u2, u3 = u * u, u * u * u
            h00 = 2 * u3 - 3 * u2 + 1
            h10 = u3 - 2 * u2 + u
            h01 = -2 * u3 + 3 * u2
            h11 = u3 - u2
            return (h00 * a.value + h10 * dt * a.right_tangent
                    + h01 * b.value + h11 * dt * b.left_tangent)
        return a.value + (b.value - a.value) * u


@dataclass(frozen=True)
class QuaternionKey:
    time: float
    value: tuple[float, float, float, float]  # x, y, z, w
    interpolation: int = 1


@dataclass
class QuaternionCurve:
    keys: list[QuaternionKey] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.keys[-1].time if self.keys else 0.0

    def sample(self, t: float) -> tuple[float, float, float, float] | None:
        if not self.keys:
            return None
        if len(self.keys) == 1 or t <= self.keys[0].time:
            return _qnorm(self.keys[0].value)
        if t >= self.keys[-1].time:
            return _qnorm(self.keys[-1].value)
        times = [k.time for k in self.keys]
        right = bisect.bisect_right(times, t)
        left = max(0, right - 1)
        a, b = self.keys[left], self.keys[right]
        if a.interpolation == 0:
            return _qnorm(a.value)
        span = b.time - a.time
        u = 1.0 if span <= 1e-12 else max(0.0, min(1.0, (t - a.time) / span))
        # RotationCurve's linear mode uses spherical interpolation. Cubic is
        # currently displayed with the same robust slerp fallback.
        return _qslerp(a.value, b.value, u)


@dataclass
class CurveAnimationData:
    name: str = ""
    priority: str = ""
    loop: bool = False
    duration: float = 0.0
    positions: dict[str, dict[str, FloatCurve]] = field(default_factory=dict)
    euler_rotations: dict[str, dict[str, FloatCurve]] = field(default_factory=dict)
    quaternion_rotations: dict[str, QuaternionCurve] = field(default_factory=dict)
    rotation_orders: dict[str, str] = field(default_factory=dict)
    joint_paths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_motion(self) -> bool:
        return bool(self.positions or self.euler_rotations or self.quaternion_rotations) and self.duration > 0


@dataclass(frozen=True)
class CFrameValue:
    position: tuple[float, float, float]
    rotation: tuple[float, ...]  # 3x3 row-major matrix


@dataclass(frozen=True)
class PoseKey:
    time: float
    position: tuple[float, float, float]
    quaternion: tuple[float, float, float, float]
    weight: float = 1.0


@dataclass
class KeyframeSequenceData:
    name: str = ""
    priority: str = ""
    loop: bool = False
    duration: float = 0.0
    tracks: dict[str, list[PoseKey]] = field(default_factory=dict)
    joint_paths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_motion(self) -> bool:
        return bool(self.tracks) and self.duration > 0

    @property
    def joint_names(self) -> list[str]:
        return sorted(self.tracks)

    def sample_joint(self, name: str, t: float):
        """Return (local position, 3x3 rotation matrix) at time t."""
        keys = self.tracks.get(name) or []
        if not keys:
            return None
        if len(keys) == 1 or t <= keys[0].time:
            key = keys[0]
            return key.position, _qmatrix_tuple(key.quaternion)
        if t >= keys[-1].time:
            key = keys[-1]
            return key.position, _qmatrix_tuple(key.quaternion)
        times = [k.time for k in keys]
        right = bisect.bisect_right(times, t)
        left = max(0, right - 1)
        a, b = keys[left], keys[right]
        span = b.time - a.time
        u = 1.0 if span <= 1e-12 else max(0.0, min(1.0, (t - a.time) / span))
        pos = tuple(x + (y - x) * u for x, y in zip(a.position, b.position))
        q = _qslerp(a.quaternion, b.quaternion, u)
        return pos, _qmatrix_tuple(q)


@dataclass
class _Instance:
    ref: int
    class_name: str
    name: str = ""
    props: dict[str, Any] = field(default_factory=dict)
    parent: int | None = None


def _u32(buf: bytes, off: int) -> int:
    if off < 0 or off + 4 > len(buf):
        raise AnimationFormatError("truncated 32-bit value")
    return struct.unpack_from("<I", buf, off)[0]


def _read_string(buf: bytes, off: int) -> tuple[bytes, int]:
    n = _u32(buf, off)
    if n > len(buf) or off + 4 + n > len(buf):
        raise AnimationFormatError("string length exceeds its chunk")
    return buf[off + 4:off + 4 + n], off + 4 + n


def _decode_interleaved_u32(buf: bytes, count: int) -> list[int]:
    if count < 0 or count > MAX_INSTANCES or len(buf) < count * 4:
        raise AnimationFormatError("short interleaved integer array")
    out: list[int] = []
    for i in range(count):
        v = (buf[i] << 24) | (buf[count + i] << 16) | (buf[count * 2 + i] << 8) | buf[count * 3 + i]
        out.append(v)
    return out


def _decode_refs(buf: bytes, count: int) -> list[int]:
    values = _decode_interleaved_u32(buf, count)
    total = 0
    refs: list[int] = []
    for v in values:
        delta = (v >> 1) ^ -(v & 1)  # zig-zag signed delta
        total += delta
        refs.append(total)
    return refs


def _decode_float_array(buf: bytes, count: int) -> list[float]:
    vals = _decode_interleaved_u32(buf, count)
    out = []
    for u in vals:
        bits = ((u >> 1) | ((u & 1) << 31)) & 0xFFFFFFFF
        f = struct.unpack("<f", struct.pack("<I", bits))[0]
        out.append(float(f))
    return out


# CFrame special rotation IDs from the public RBXM binary format specification.
# Angles are (X, Y, Z) degrees; these compact IDs are sparse, not id-minus-two indexed.
_CFRAME_SPECIAL_EULER_DEG = {
    0x02: (0, 0, 0), 0x03: (90, 0, 0), 0x05: (0, 180, 180),
    0x06: (-90, 0, 0), 0x07: (0, 180, 90), 0x09: (0, 90, 90),
    0x0A: (0, 0, 90), 0x0C: (0, -90, 90), 0x0D: (-90, -90, 0),
    0x0E: (0, -90, 0), 0x10: (90, -90, 0), 0x11: (0, 90, 180),
    0x14: (0, 180, 0), 0x15: (-90, -180, 0), 0x17: (0, 0, 180),
    0x18: (90, 180, 0), 0x19: (0, 0, -90), 0x1B: (0, -90, -90),
    0x1C: (0, -180, -90), 0x1E: (0, 90, -90), 0x1F: (90, 90, 0),
    0x20: (0, 90, 0), 0x22: (-90, 90, 0), 0x23: (0, -90, 180),
}


def _mat_mul(a, b):
    return tuple(sum(a[r * 3 + k] * b[k * 3 + c] for k in range(3)) for r in range(3) for c in range(3))


def _axis_matrix(axis: str, angle: float):
    c, s = math.cos(angle), math.sin(angle)
    if axis == "X":
        return (1.0, 0.0, 0.0, 0.0, c, -s, 0.0, s, c)
    if axis == "Y":
        return (c, 0.0, s, 0.0, 1.0, 0.0, -s, 0.0, c)
    return (c, -s, 0.0, s, c, 0.0, 0.0, 0.0, 1.0)


def _special_cframe_rotation(rotation_id: int):
    angles = _CFRAME_SPECIAL_EULER_DEG.get(rotation_id)
    if angles is None:
        raise AnimationFormatError(f"unknown CFrame rotation ID 0x{rotation_id:02x}")
    x, y, z = (math.radians(v) for v in angles)
    # The format specifies the compact rotations in Y -> X -> Z application order.
    return _mat_mul(_mat_mul(_axis_matrix("Z", z), _axis_matrix("X", x)), _axis_matrix("Y", y))


def _decode_cframe_array(buf: bytes, count: int) -> list[CFrameValue]:
    # Rotation IDs are interspersed with their optional 9-float matrices: each
    # value is encoded as ID, then matrix only when ID == 0. Positions follow
    # the complete sequence of rotation records as three interleaved float arrays.
    off = 0
    rotations: list[tuple[float, ...]] = []
    for _ in range(count):
        if off >= len(buf):
            raise AnimationFormatError("CFrame rotation IDs are truncated")
        rotation_id = buf[off]
        off += 1
        if rotation_id == 0:
            if off + 36 > len(buf):
                raise AnimationFormatError("full CFrame rotation matrix is truncated")
            matrix = struct.unpack_from("<9f", buf, off)
            off += 36
            if not all(math.isfinite(v) for v in matrix):
                raise AnimationFormatError("CFrame contains a non-finite rotation")
            rotations.append(tuple(float(v) for v in matrix))
        else:
            rotations.append(_special_cframe_rotation(rotation_id))
    axis_bytes = count * 4
    if off + axis_bytes * 3 > len(buf):
        raise AnimationFormatError("CFrame position arrays are truncated")
    xs = _decode_float_array(buf[off:off + axis_bytes], count)
    off += axis_bytes
    ys = _decode_float_array(buf[off:off + axis_bytes], count)
    off += axis_bytes
    zs = _decode_float_array(buf[off:off + axis_bytes], count)
    out = []
    for i, rotation in enumerate(rotations):
        position = (xs[i], ys[i], zs[i])
        if not all(math.isfinite(v) for v in position):
            raise AnimationFormatError("CFrame contains a non-finite position")
        out.append(CFrameValue(position, rotation))
    return out


def _decompress(body: bytes, unpacked_size: int) -> bytes:
    if unpacked_size < 0 or unpacked_size > MAX_CHUNK_BYTES:
        raise AnimationFormatError("chunk is too large")
    if body.startswith(b"\x28\xb5\x2f\xfd"):
        if _zstd is None:
            raise AnimationFormatError("zstd-compressed chunk needs the 'zstandard' package")
        try:
            result = _zstd.ZstdDecompressor().decompress(body, max_output_size=max(1, unpacked_size))
        except Exception as e:
            raise AnimationFormatError(f"invalid zstd chunk: {e}") from e
    elif _lz4 is not None:
        try:
            result = _lz4.decompress(body, uncompressed_size=unpacked_size)
        except Exception:
            result = _lz4_block(body, unpacked_size)
    else:
        result = _lz4_block(body, unpacked_size)
    if len(result) != unpacked_size:
        raise AnimationFormatError("decompressed chunk size mismatch")
    return result


def _lz4_block(src: bytes, expected: int) -> bytes:
    out = bytearray()
    i = 0
    while i < len(src):
        token = src[i]
        i += 1
        literals = token >> 4
        if literals == 15:
            while True:
                if i >= len(src):
                    raise AnimationFormatError("truncated LZ4 literal length")
                b = src[i]
                i += 1
                literals += b
                if b != 255:
                    break
        if i + literals > len(src):
            raise AnimationFormatError("truncated LZ4 literals")
        out.extend(src[i:i + literals])
        i += literals
        if i >= len(src):
            break
        if i + 2 > len(src):
            raise AnimationFormatError("truncated LZ4 offset")
        distance = src[i] | (src[i + 1] << 8)
        i += 2
        length = token & 0x0F
        if length == 15:
            while True:
                if i >= len(src):
                    raise AnimationFormatError("truncated LZ4 match length")
                b = src[i]
                i += 1
                length += b
                if b != 255:
                    break
        length += 4
        if distance <= 0 or distance > len(out):
            raise AnimationFormatError("invalid LZ4 back-reference")
        for j in range(length):
            out.append(out[-distance])
        if len(out) > expected:
            raise AnimationFormatError("LZ4 data expands past the declared size")
    if len(out) != expected:
        raise AnimationFormatError("LZ4 output size mismatch")
    return bytes(out)


def _model_chunks(payload: bytes):
    if not payload.startswith(RBXM_MAGIC):
        raise AnimationFormatError("not a binary Roblox model")
    if len(payload) < 32:
        raise AnimationFormatError("truncated Roblox model header")
    pos = 32
    while pos < len(payload):
        if pos + 16 > len(payload):
            raise AnimationFormatError("truncated chunk header")
        tag = payload[pos:pos + 4]
        clen, ulen = struct.unpack_from("<II", payload, pos + 4)
        size = clen or ulen
        if size > MAX_CHUNK_BYTES or pos + 16 + size > len(payload):
            raise AnimationFormatError(f"invalid {tag!r} chunk length")
        raw = payload[pos + 16:pos + 16 + size]
        pos += 16 + size
        if tag.startswith(b"END"):
            break
        if clen:
            try:
                raw = _decompress(raw, ulen)
            except AnimationFormatError:
                # Parsing a pose without a corrupt optional chunk is sometimes
                # still useful, but losing INST/PRNT would make the hierarchy
                # unreliable. Raise for all chunks; the caller will show this.
                raise
        yield tag, raw


def _parse_model(payload: bytes) -> dict[int, _Instance]:
    class_refs: dict[int, list[int]] = {}
    class_names: dict[int, str] = {}
    class_ids: dict[int, int] = {}
    instances: dict[int, _Instance] = {}
    parents: list[tuple[int, int]] = []

    for tag, body in _model_chunks(payload):
        if tag == b"INST":
            if len(body) < 9:
                raise AnimationFormatError("short INST chunk")
            cid = _u32(body, 0)
            raw_name, off = _read_string(body, 4)
            cname = raw_name.decode("utf-8", "replace")
            if off + 5 > len(body):
                raise AnimationFormatError("truncated INST metadata")
            _service = body[off]
            count = _u32(body, off + 1)
            refs_off = off + 5
            if count > MAX_INSTANCES or refs_off + 4 * count > len(body):
                raise AnimationFormatError("invalid INST referent count")
            refs = _decode_refs(body[refs_off:refs_off + 4 * count], count)
            class_names[cid] = cname
            class_refs[cid] = refs
            for ref in refs:
                if ref in instances:
                    raise AnimationFormatError(f"duplicate instance reference {ref}")
                instances[ref] = _Instance(ref=ref, class_name=cname)
                class_ids[ref] = cid

        elif tag == b"PROP":
            if len(body) < 5:
                continue
            cid = _u32(body, 0)
            raw_prop, off = _read_string(body, 4)
            prop = raw_prop.decode("utf-8", "replace")
            if off >= len(body) or cid not in class_refs:
                continue
            ptype = body[off]
            vals = body[off + 1:]
            refs = class_refs[cid]
            count = len(refs)
            values: list[Any] | None = None
            if ptype == 0x01:  # String / BinaryString; ValuesAndTimes is a byte string
                values = []
                p = 0
                try:
                    for _ in range(count):
                        v, p = _read_string(vals, p)
                        values.append(v)
                except AnimationFormatError:
                    continue
            elif ptype == 0x02:  # Bool
                if len(vals) >= count:
                    values = [bool(x) for x in vals[:count]]
            elif ptype == 0x12:  # Enum
                if len(vals) >= count * 4:
                    values = _decode_interleaved_u32(vals, count)
            elif ptype == 0x04:  # Float32, Roblox bit-rotation encoding
                if len(vals) >= count * 4:
                    values = _decode_float_array(vals, count)
            elif ptype == 0x10:  # CFrame: sparse rotation IDs, then position arrays
                try:
                    values = _decode_cframe_array(vals, count)
                except AnimationFormatError:
                    values = None
            if values is not None:
                for ref, value in zip(refs, values):
                    instances[ref].props[prop] = value

        elif tag == b"PRNT":
            if len(body) < 5:
                raise AnimationFormatError("short PRNT chunk")
            count = _u32(body, 1)
            if count > MAX_INSTANCES or len(body) < 5 + 8 * count:
                raise AnimationFormatError("invalid PRNT referent count")
            child = _decode_refs(body[5:5 + 4 * count], count)
            parent = _decode_refs(body[5 + 4 * count:5 + 8 * count], count)
            parents.extend(zip(child, parent))

    for ref, inst in instances.items():
        raw_name = inst.props.get("Name", b"")
        if isinstance(raw_name, bytes):
            inst.name = raw_name.decode("utf-8", "replace")
        else:
            inst.name = str(raw_name or "")
    for child, parent in parents:
        if child in instances:
            instances[child].parent = parent if parent >= 0 else None
    return instances


def _curve_payload(raw: Any) -> bytes | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        return raw.encode("latin1", "replace")
    return None


def parse_float_curve(raw: bytes) -> FloatCurve:
    """Parse the FloatCurve ValuesAndTimes binary string (14-byte value keys)."""
    if len(raw) < 16:
        raise AnimationFormatError("FloatCurve ValuesAndTimes is truncated")
    values_version, count = struct.unpack_from("<II", raw, 0)
    if values_version not in (1, 2):
        raise AnimationFormatError(f"unsupported FloatCurve values version {values_version}")
    if count > 2_000_000:
        raise AnimationFormatError("FloatCurve contains too many keys")
    values_end = 8 + count * 14
    if values_end + 8 > len(raw):
        raise AnimationFormatError("FloatCurve value keys are truncated")
    keys: list[tuple[int, float, float, float]] = []
    off = 8
    for _ in range(count):
        interpolation, tangent_flags = raw[off], raw[off + 1]
        value, left, right = struct.unpack_from("<fff", raw, off + 2)
        if not all(math.isfinite(v) for v in (value, left, right)):
            raise AnimationFormatError("FloatCurve contains non-finite values")
        keys.append((interpolation, value, left, right))
        off += 14
    times_version, time_count = struct.unpack_from("<II", raw, values_end)
    if times_version not in (1, 2) or time_count != count:
        raise AnimationFormatError("FloatCurve time section does not match its values")
    times_off = values_end + 8
    if times_off + time_count * 4 > len(raw):
        raise AnimationFormatError("FloatCurve time keys are truncated")
    ticks = struct.unpack_from(f"<{time_count}I", raw, times_off) if time_count else ()
    curve_keys = []
    for (interp, value, left, right), tick in zip(keys, ticks):
        t = tick / TICKS_PER_SECOND
        if not math.isfinite(t):
            raise AnimationFormatError("FloatCurve contains invalid time")
        curve_keys.append(CurveKey(t, value, int(interp), left, right))
    curve_keys.sort(key=lambda k: k.time)
    return FloatCurve(curve_keys)


def parse_quaternion_curve(raw: bytes) -> QuaternionCurve:
    """Parse RotationCurve ValuesAndTimes (25-byte quaternion value keys)."""
    if len(raw) < 16:
        raise AnimationFormatError("RotationCurve ValuesAndTimes is truncated")
    values_version, count = struct.unpack_from("<II", raw, 0)
    if values_version not in (1, 2):
        raise AnimationFormatError(f"unsupported RotationCurve values version {values_version}")
    if count > 2_000_000:
        raise AnimationFormatError("RotationCurve contains too many keys")
    values_end = 8 + count * 25
    if values_end + 8 > len(raw):
        raise AnimationFormatError("RotationCurve values are truncated")
    vals: list[tuple[int, tuple[float, float, float, float]]] = []
    off = 8
    for _ in range(count):
        interpolation = raw[off]
        q = struct.unpack_from("<4f", raw, off + 1)
        if not all(math.isfinite(x) for x in q):
            raise AnimationFormatError("RotationCurve contains non-finite quaternion")
        vals.append((interpolation - 12 if 12 <= interpolation <= 14 else interpolation, tuple(float(x) for x in q)))
        off += 25
    times_version, time_count = struct.unpack_from("<II", raw, values_end)
    if times_version not in (1, 2) or time_count != count:
        raise AnimationFormatError("RotationCurve time section does not match its values")
    times_off = values_end + 8
    if times_off + time_count * 4 > len(raw):
        raise AnimationFormatError("RotationCurve times are truncated")
    ticks = struct.unpack_from(f"<{time_count}I", raw, times_off) if time_count else ()
    pairs = sorted(((tick / TICKS_PER_SECOND, it, q) for (it, q), tick in zip(vals, ticks)), key=lambda x: x[0])
    return QuaternionCurve([QuaternionKey(t, q, int(it)) for t, it, q in pairs])


def _ancestor_joint(inst: _Instance, instances: dict[int, _Instance], root_ref: int) -> str | None:
    p = inst.parent
    visited: set[int] = set()
    while p is not None and p not in visited:
        visited.add(p)
        if p == root_ref:
            return None
        node = instances.get(p)
        if node is None:
            return None
        if node.class_name == "Folder":
            return node.name or None
        p = node.parent
    return None


def _float_children(
    inst: _Instance, instances: dict[int, _Instance], notes: list[str] | None = None, label: str = "curve"
) -> dict[str, FloatCurve]:
    out: dict[str, FloatCurve] = {}
    for child in instances.values():
        if child.parent != inst.ref or child.class_name != "FloatCurve":
            continue
        raw = _curve_payload(child.props.get("ValuesAndTimes"))
        if raw is None:
            continue
        axis = child.name.strip().upper()
        if axis not in ("X", "Y", "Z"):
            continue
        try:
            curve = parse_float_curve(raw)
            out[axis] = curve
            # Keep finite source values exactly as serialized. An extreme value can
            # move the corresponding part outside a simplified preview camera; it
            # is not enough evidence to label the source curve corrupt.
            if notes is not None and any(
                abs(v) > EXTREME_CURVE_DIAGNOSTIC_THRESHOLD
                for key in curve.keys
                for v in (key.value, key.left_tangent, key.right_tangent)
            ):
                notes.append(
                    f"{label}.{axis}: very large finite curve values are preserved; "
                    "the corresponding part may be outside the simplified preview view."
                )
        except AnimationFormatError as exc:
            if notes is not None:
                notes.append(f"{label}.{axis}: curve ignored ({exc})")
            continue
    return out


def parse_curve_animation(payload: bytes) -> CurveAnimationData:
    """Decode a CurveAnimation for the CacheSwap software-rendered rig preview."""
    instances = _parse_model(payload)
    roots = [n for n in instances.values() if n.class_name == "CurveAnimation"]
    if not roots:
        raise AnimationFormatError("this file is not a CurveAnimation")
    root = roots[0]
    data = CurveAnimationData()
    data.name = root.name or "CurveAnimation"
    data.loop = bool(root.props.get("Loop", False))
    p = root.props.get("Priority")
    try:
        p = int(p)
        data.priority = {0: "Idle", 1: "Movement", 2: "Action", 3: "Action2", 4: "Action3", 5: "Action4", 1000: "Core"}.get(p, f"value {p}")
    except (TypeError, ValueError):
        data.priority = ""

    folder_names = []
    for inst in instances.values():
        if inst.class_name == "Folder" and inst.name:
            p = inst.parent
            chain: list[str] = []
            seen: set[int] = set()
            while p is not None and p not in seen:
                seen.add(p)
                ancestor = instances.get(p)
                if ancestor is None or p == root.ref:
                    break
                if ancestor.class_name == "Folder" and ancestor.name:
                    chain.append(ancestor.name)
                p = ancestor.parent
            path = "/".join(list(reversed(chain)) + [inst.name]) if chain else inst.name
            folder_names.append(path)

    data.joint_paths = sorted(set(folder_names))
    duration = 0.0
    for inst in instances.values():
        if inst.class_name not in ("Vector3Curve", "EulerRotationCurve"):
            continue
        joint = _ancestor_joint(inst, instances, root.ref)
        if not joint:
            continue
        axes = _float_children(inst, instances, data.notes, joint)
        if not axes:
            continue
        if inst.class_name == "Vector3Curve":
            data.positions.setdefault(joint, {}).update(axes)
        else:
            data.euler_rotations.setdefault(joint, {}).update(axes)
            # Serialized enum values: XYZ=0, YZX=1, ZXY=2, XZY=3, YXZ=4, ZYX=5.
            raw_order = inst.props.get("RotationOrder", 0)
            try:
                order_code = int(raw_order)
            except (TypeError, ValueError):
                order_code = 0
            data.rotation_orders[joint] = {0: "XYZ", 1: "YZX", 2: "ZXY", 3: "XZY", 4: "YXZ", 5: "ZYX"}.get(order_code, "XYZ")
        for curve in axes.values():
            duration = max(duration, curve.duration)

    for inst in instances.values():
        if inst.class_name != "RotationCurve":
            continue
        joint = _ancestor_joint(inst, instances, root.ref)
        raw = _curve_payload(inst.props.get("ValuesAndTimes"))
        if not joint or raw is None:
            continue
        try:
            curve = parse_quaternion_curve(raw)
        except AnimationFormatError as e:
            data.notes.append(f"{joint}: rotation curve not previewed ({e})")
            continue
        data.quaternion_rotations[joint] = curve
        duration = max(duration, curve.duration)

    if not (data.positions or data.euler_rotations or data.quaternion_rotations):
        raise AnimationFormatError("no supported Vector3Curve, EulerRotationCurve or RotationCurve tracks found")
    data.duration = max(0.0, duration)
    if data.duration <= 0:
        data.notes.append("No curve contains timed keys; playback is static.")
    elif data.duration < 0.05:
        data.notes.append(
            f"Short serialized duration ({data.duration:.6f} s); CacheSwap preserves the key times "
            "encoded in this file."
        )
    return data


def _matrix_to_quaternion(m: tuple[float, ...]) -> tuple[float, float, float, float]:
    # Matrix is row-major; output quaternion uses (x, y, z, w).
    m00, m01, m02, m10, m11, m12, m20, m21, m22 = m
    trace = m00 + m11 + m22
    if trace > 0:
        s = math.sqrt(max(0.0, trace + 1.0)) * 2
        q = ((m21 - m12) / s, (m02 - m20) / s, (m10 - m01) / s, 0.25 * s) if s else (0, 0, 0, 1)
    elif m00 > m11 and m00 > m22:
        s = math.sqrt(max(0.0, 1.0 + m00 - m11 - m22)) * 2
        q = (0.25 * s, (m01 + m10) / s, (m02 + m20) / s, (m21 - m12) / s) if s else (0, 0, 0, 1)
    elif m11 > m22:
        s = math.sqrt(max(0.0, 1.0 + m11 - m00 - m22)) * 2
        q = ((m01 + m10) / s, 0.25 * s, (m12 + m21) / s, (m02 - m20) / s) if s else (0, 0, 0, 1)
    else:
        s = math.sqrt(max(0.0, 1.0 + m22 - m00 - m11)) * 2
        q = ((m02 + m20) / s, (m12 + m21) / s, 0.25 * s, (m10 - m01) / s) if s else (0, 0, 0, 1)
    return _qnorm(tuple(float(v) for v in q))


def _qmatrix_tuple(q):
    x, y, z, w = _qnorm(q)
    return (
        1 - 2 * (y*y + z*z), 2 * (x*y - z*w), 2 * (x*z + y*w),
        2 * (x*y + z*w), 1 - 2 * (x*x + z*z), 2 * (y*z - x*w),
        2 * (x*z - y*w), 2 * (y*z + x*w), 1 - 2 * (x*x + y*y),
    )


def parse_keyframe_sequence(payload: bytes) -> KeyframeSequenceData:
    """Decode legacy KeyframeSequence pose CFrames for the same rig preview."""
    instances = _parse_model(payload)
    roots = [node for node in instances.values() if node.class_name == "KeyframeSequence"]
    if not roots:
        raise AnimationFormatError("this file is not a KeyframeSequence")
    root = roots[0]
    data = KeyframeSequenceData(name=root.name or "KeyframeSequence", loop=bool(root.props.get("Loop", False)))
    priority = root.props.get("Priority", 0)
    try:
        p = int(priority)
        data.priority = {0: "Idle", 1: "Movement", 2: "Action", 3: "Action2", 4: "Action3", 5: "Action4", 1000: "Core"}.get(p, f"value {p}")
    except (TypeError, ValueError):
        data.priority = ""

    keyframe_times: dict[int, float] = {}
    for node in instances.values():
        if node.class_name != "Keyframe":
            continue
        p = node.parent
        seen: set[int] = set()
        while p is not None and p not in seen:
            seen.add(p)
            ancestor = instances.get(p)
            if ancestor is None or ancestor.ref == root.ref:
                break
            p = ancestor.parent
        else:
            continue
        if p == root.ref or (instances.get(p) and instances[p].ref == root.ref):
            try:
                t = float(node.props.get("Time", 0.0))
            except (TypeError, ValueError):
                t = 0.0
            if math.isfinite(t) and t >= 0:
                keyframe_times[node.ref] = t
    if not keyframe_times:
        raise AnimationFormatError("KeyframeSequence has no readable keyframes")

    track_keys: dict[str, list[PoseKey]] = {}
    path_names: set[str] = set()
    for node in instances.values():
        if node.class_name != "Pose" or not node.name:
            continue
        p = node.parent
        kf_ref = None
        visited: set[int] = set()
        while p is not None and p not in visited:
            visited.add(p)
            ancestor = instances.get(p)
            if ancestor is None:
                break
            if ancestor.class_name == "Keyframe":
                kf_ref = ancestor.ref
                break
            if ancestor.ref == root.ref:
                break
            p = ancestor.parent
        if kf_ref not in keyframe_times:
            continue
        weight = node.props.get("Weight", 1.0)
        try:
            weight = float(weight)
        except (TypeError, ValueError):
            weight = 1.0
        if not math.isfinite(weight) or weight <= 0:
            continue
        cf_values = node.props.get("CFrame")
        if isinstance(cf_values, CFrameValue):
            cf = cf_values
        elif isinstance(cf_values, list) and cf_values and isinstance(cf_values[0], CFrameValue):
            cf = cf_values[0]
        else:
            cf = CFrameValue((0.0, 0.0, 0.0), (1.0,0.0,0.0,0.0,1.0,0.0,0.0,0.0,1.0))
        t = keyframe_times[kf_ref]
        quat = _matrix_to_quaternion(cf.rotation)
        track_keys.setdefault(node.name, []).append(PoseKey(t, cf.position, quat, weight))

        parts = [node.name]
        p = node.parent
        visited.clear()
        while p is not None and p not in visited:
            visited.add(p)
            ancestor = instances.get(p)
            if ancestor is None or ancestor.ref == kf_ref or ancestor.class_name == "Keyframe":
                break
            if ancestor.class_name == "Pose" and ancestor.name:
                parts.append(ancestor.name)
            p = ancestor.parent
        path_names.add("/".join(reversed(parts)))

    if not track_keys:
        raise AnimationFormatError("KeyframeSequence contains no poses with readable CFrame values")
    for name, keys in track_keys.items():
        # If duplicate names exist in a frame, preserve stable order and keep the last value.
        by_time = {}
        for key in keys:
            by_time[key.time] = key
        data.tracks[name] = [by_time[t] for t in sorted(by_time)]
    data.duration = max(keyframe_times.values())
    data.joint_paths = sorted(path_names)
    data.notes.append("Legacy pose interpolation uses linear position blending and quaternion slerp.")
    if data.duration <= 0:
        data.notes.append("No animation duration: the preview shows a static pose.")
    return data


def _qnorm(q: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    n = math.sqrt(sum(v * v for v in q))
    if n < 1e-12:
        return (0.0, 0.0, 0.0, 1.0)
    return tuple(v / n for v in q)


def _qslerp(a: tuple[float, float, float, float], b: tuple[float, float, float, float], t: float) -> tuple[float, float, float, float]:
    qa, qb = _qnorm(a), _qnorm(b)
    dot = sum(x * y for x, y in zip(qa, qb))
    if dot < 0.0:
        qb = tuple(-v for v in qb)
        dot = -dot
    if dot > 0.9995:
        return _qnorm(tuple(x + t * (y - x) for x, y in zip(qa, qb)))
    dot = max(-1.0, min(1.0, dot))
    theta = math.acos(dot)
    sin_theta = math.sin(theta)
    if abs(sin_theta) < 1e-10:
        return qa
    w0 = math.sin((1 - t) * theta) / sin_theta
    w1 = math.sin(t * theta) / sin_theta
    return _qnorm(tuple(w0 * x + w1 * y for x, y in zip(qa, qb)))
