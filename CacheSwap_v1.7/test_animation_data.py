"""Headless tests for supported Roblox animation decoding and interpolation."""
import struct

from animation_data import (parse_curve_animation, parse_float_curve, parse_keyframe_sequence, AnimationFormatError, _special_cframe_rotation, _decode_cframe_array)
from preview import RBXM_MAGIC


def _interleave_u32(values):
    raw = [struct.pack(">I", v & 0xFFFFFFFF) for v in values]
    return b"".join(bytes(item[i] for item in raw) for i in range(4))


def _encode_refs(refs):
    last = 0
    encoded = []
    for ref in refs:
        delta = ref - last
        last = ref
        encoded.append(((delta << 1) ^ (delta >> 31)) & 0xFFFFFFFF)
    return _interleave_u32(encoded)


def _rstr(data):
    return struct.pack("<I", len(data)) + data


def _chunk(tag, body):
    return tag + struct.pack("<III", 0, len(body), 0) + body


def _inst(cid, cname, refs):
    bname = cname.encode()
    return struct.pack("<I", cid) + _rstr(bname) + b"\x00" + struct.pack("<I", len(refs)) + _encode_refs(refs)


def _prop_strings(cid, name, values):
    return struct.pack("<I", cid) + _rstr(name.encode()) + b"\x01" + b"".join(_rstr(v) for v in values)


def _prop_bools(cid, name, values):
    return struct.pack("<I", cid) + _rstr(name.encode()) + b"\x02" + bytes(values)


def _prop_enums(cid, name, values):
    return struct.pack("<I", cid) + _rstr(name.encode()) + b"\x12" + _interleave_u32(values)


def _values_and_times(keys):
    # ValuesAndTimes: v2, count, n * (interpolation, tangent flags, value, left, right),
    # then time section v1, count, and tick times.
    out = bytearray(struct.pack("<II", 2, len(keys)))
    for t, v, interpolation in keys:
        out += struct.pack("<BBfff", interpolation, 0, v, 0.0, 0.0)
    out += struct.pack("<II", 1, len(keys))
    out += struct.pack(f"<{len(keys)}I", *(round(t * 14400) for t, _v, _i in keys)) if keys else b""
    return bytes(out)


def _build_curve_animation():
    # refs: 0 animation; 1 HumanoidRootPart folder; 2 LowerTorso folder;
    # 3 Rotation EulerRotationCurve; 4,5,6 its X/Y/Z FloatCurve children.
    chunks = [
        _chunk(b"INST", _inst(0, "CurveAnimation", [0])),
        _chunk(b"INST", _inst(1, "Folder", [1, 2])),
        _chunk(b"INST", _inst(2, "EulerRotationCurve", [3])),
        _chunk(b"INST", _inst(3, "FloatCurve", [4, 5, 6])),
        _chunk(b"PROP", _prop_strings(0, "Name", [b"TestWave"])),
        _chunk(b"PROP", _prop_enums(0, "Priority", [5])),
        _chunk(b"PROP", _prop_bools(0, "Loop", [1])),
        _chunk(b"PROP", _prop_strings(1, "Name", [b"HumanoidRootPart", b"LowerTorso"])),
        _chunk(b"PROP", _prop_strings(2, "Name", [b"Rotation"])),
        _chunk(b"PROP", _prop_strings(3, "Name", [b"X", b"Y", b"Z"])),
        _chunk(b"PROP", _prop_strings(3, "ValuesAndTimes", [
            _values_and_times([(0.0, 0.0, 1), (1.0, 0.5, 1)]),
            _values_and_times([(0.0, 0.0, 1), (1.0, 0.0, 1)]),
            _values_and_times([(0.0, 0.0, 1), (1.0, 0.0, 1)]),
        ])),
    ]
    # PRNT child refs and corresponding parent refs; root is unparented.
    children = [1, 2, 3, 4, 5, 6]
    parents = [0, 1, 2, 3, 3, 3]
    prnt = b"\x00" + struct.pack("<I", len(children)) + _encode_refs(children) + _encode_refs(parents)
    chunks.append(_chunk(b"PRNT", prnt))
    end = b"END\x00" + struct.pack("<III", 0, len(b"</roblox>"), 0) + b"</roblox>"
    # Header's total-chunk count is ignored by the current readers; keep a sane value.
    return RBXM_MAGIC + struct.pack("<Hii", 0, 3, len(chunks) + 1) + bytes(8) + b"".join(chunks) + end


def test_parse_curve_animation_hierarchy_and_metadata():
    data = parse_curve_animation(_build_curve_animation())
    assert data.name == "TestWave"
    assert data.loop is True
    assert data.priority == "Action4"
    assert data.duration == 1.0
    assert "HumanoidRootPart" in data.joint_paths
    assert "HumanoidRootPart/LowerTorso" in data.joint_paths
    assert data.euler_rotations["LowerTorso"]["X"].sample(0.5) == 0.25
    assert data.has_motion


def test_float_curve_constant_and_linear_interpolation():
    raw = _values_and_times([(0.0, 2.0, 0), (1.0, 8.0, 1)])
    curve = parse_float_curve(raw)
    assert curve.sample(0.5) == 2.0
    raw = _values_and_times([(0.0, 2.0, 1), (1.0, 8.0, 1)])
    curve = parse_float_curve(raw)
    assert abs(curve.sample(0.5) - 5.0) < 1e-5


def test_bad_curve_rejected():
    try:
        parse_float_curve(b"bad")
    except AnimationFormatError:
        return
    raise AssertionError("truncated curve should be rejected")




def test_float_curve_extreme_finite_value_is_preserved():
    raw = _values_and_times([(0.0, -1.0e32, 1)])
    curve = parse_float_curve(raw)
    assert len(curve.keys) == 1
    assert abs(curve.keys[0].value / -1.0e32 - 1.0) < 1e-6
    assert abs(curve.sample(0.0) / -1.0e32 - 1.0) < 1e-6


def _encode_float(v):
    bits = struct.unpack("<I", struct.pack("<f", float(v)))[0]
    return ((bits << 1) | (bits >> 31)) & 0xFFFFFFFF


def _prop_floats(cid, name, values):
    return struct.pack("<I", cid) + _rstr(name.encode()) + b"\x04" + _interleave_u32([_encode_float(v) for v in values])


def _prop_cframes(cid, name, values):
    # Each value: (sparse special rotation ID, x, y, z). This fixture deliberately
    # uses ID 0x20 and checks the gap at 0x04 is not treated as id-minus-two.
    encoded = bytearray(v[0] for v in values)
    for axis in (1, 2, 3):
        encoded += _interleave_u32([_encode_float(v[axis]) for v in values])
    return struct.pack("<I", cid) + _rstr(name.encode()) + b"\x10" + bytes(encoded)


def _build_keyframe_sequence():
    # Root 0; Keyframes 1/2; same named Pose 3/4, one pose per keyframe.
    chunks = [
        _chunk(b"INST", _inst(0, "KeyframeSequence", [0])),
        _chunk(b"INST", _inst(1, "Keyframe", [1, 2])),
        _chunk(b"INST", _inst(2, "Pose", [3, 4])),
        _chunk(b"PROP", _prop_strings(0, "Name", [b"LegacyWave"])),
        _chunk(b"PROP", _prop_bools(0, "Loop", [1])),
        _chunk(b"PROP", _prop_enums(0, "Priority", [5])),
        _chunk(b"PROP", _prop_strings(1, "Name", [b"Keyframe0", b"Keyframe1"])),
        _chunk(b"PROP", _prop_floats(1, "Time", [0.0, 1.0])),
        _chunk(b"PROP", _prop_strings(2, "Name", [b"LeftShoulder", b"LeftShoulder"])),
        _chunk(b"PROP", _prop_floats(2, "Weight", [1.0, 1.0])),
        _chunk(b"PROP", _prop_cframes(2, "CFrame", [(0x02, 0, 0, 0), (0x20, 0, 1, 0)])),
    ]
    children = [1, 2, 3, 4]
    parents = [0, 0, 1, 2]
    prnt = b"\x00" + struct.pack("<I", len(children)) + _encode_refs(children) + _encode_refs(parents)
    chunks.append(_chunk(b"PRNT", prnt))
    end = b"END\x00" + struct.pack("<III", 0, len(b"</roblox>"), 0) + b"</roblox>"
    return RBXM_MAGIC + struct.pack("<Hii", 0, 3, len(chunks) + 1) + bytes(8) + b"".join(chunks) + end


def test_parse_keyframe_sequence_pose_cframes_and_interpolation():
    data = parse_keyframe_sequence(_build_keyframe_sequence())
    assert data.name == "LegacyWave"
    assert data.loop is True
    assert data.priority == "Action4"
    assert data.duration == 1.0
    assert data.joint_names == ["LeftShoulder"]
    at_start = data.sample_joint("LeftShoulder", 0.0)
    at_mid = data.sample_joint("LeftShoulder", 0.5)
    at_end = data.sample_joint("LeftShoulder", 1.0)
    assert at_start is not None and at_mid is not None and at_end is not None
    assert at_start[1] == (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    assert abs(at_mid[0][1] - 0.5) < 1e-5
    # Between identity and 90-degree Y rotation, slerp should be near 45 degrees.
    assert abs(at_mid[1][2] - 2 ** -0.5) < 1e-5
    assert abs(at_end[0][1] - 1.0) < 1e-5


def test_cframe_special_rotation_ids_are_sparse():
    mat = _special_cframe_rotation(0x20)
    assert abs(mat[2] - 1.0) < 1e-8
    try:
        _special_cframe_rotation(0x04)
    except AnimationFormatError:
        pass
    else:
        raise AssertionError("an unused CFrame rotation ID must not be accepted")



def test_cframe_full_matrix_and_position_array_decode():
    identity = struct.pack("<9f", 1, 0, 0, 0, 1, 0, 0, 0, 1)
    # Full matrix for first frame, then a sparse ID for the second frame.
    raw = b"\x00" + identity + b"\x20"
    for values in ((1.25, 4.0), (-2.5, 5.0), (3.75, 6.0)):
        raw += _interleave_u32([_encode_float(value) for value in values])
    first, second = _decode_cframe_array(raw, 2)
    assert first.rotation == (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    assert all(abs(a - b) < 1e-6 for a, b in zip(first.position, (1.25, -2.5, 3.75)))
    assert all(abs(a - b) < 1e-6 for a, b in zip(second.position, (4.0, 5.0, 6.0)))
    assert abs(second.rotation[2] - 1.0) < 1e-8

if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print("PASS", test.__name__)
    print("ALL ANIMATION DATA TESTS PASSED")
