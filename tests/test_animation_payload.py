"""Tests for animation-asset preflight before local cache imports."""

from __future__ import annotations

import base64
import struct

from fleasion.database.animation_payload import inspect_animation_payload
from fleasion.database.roblox_cache_store import detect_roblox_payload_kind


def _values_and_times(keys: list[tuple[int, float]]) -> str:
    raw = bytearray(struct.pack("<II", 2, len(keys)))
    for _tick, value in keys:
        raw.extend(struct.pack("<BBfff", 1, 0, value, 0.0, 0.0))
    raw.extend(struct.pack("<II", 1, len(keys)))
    if keys:
        raw.extend(struct.pack(f"<{len(keys)}I", *(tick for tick, _value in keys)))
    return base64.b64encode(raw).decode("ascii")


def _curve_animation_xml(
    *,
    ticks: tuple[int, int] = (0, 14400),
    z_values: tuple[float, float] = (0.0, 0.5),
    include_arm_track: bool = True,
) -> bytes:
    arm_track = f"""
      <Item class="Folder">
        <Properties><string name="Name">RightUpperArm</string></Properties>
        <Item class="EulerRotationCurve">
          <Properties><string name="Name">Rotation</string></Properties>
          <Item class="FloatCurve">
            <Properties>
              <string name="Name">X</string>
              <BinaryString name="ValuesAndTimes">{_values_and_times(list(zip(ticks, (0.0, 0.5))))}</BinaryString>
            </Properties>
          </Item>
        </Item>
      </Item>
""" if include_arm_track else ""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<roblox version="4">
  <Item class="CurveAnimation">
    <Properties>
      <string name="Name">Preflight Test Animation</string>
      <bool name="Loop">true</bool>
      <token name="Priority">6</token>
    </Properties>
    <Item class="AnimationRigData">
      <Properties><string name="Name">R15 DummyAnimationRigData</string></Properties>
    </Item>
    <Item class="Folder">
      <Properties><string name="Name">HumanoidRootPart</string></Properties>
      <Item class="Folder">
        <Properties><string name="Name">LowerTorso</string></Properties>
        <Item class="Vector3Curve">
          <Properties><string name="Name">Position</string></Properties>
          <Item class="FloatCurve">
            <Properties>
              <string name="Name">Z</string>
              <BinaryString name="ValuesAndTimes">{_values_and_times(list(zip(ticks, z_values)))}</BinaryString>
            </Properties>
          </Item>
        </Item>
      </Item>
      {arm_track}
    </Item>
  </Item>
</roblox>
""".encode("utf-8")


def test_curve_animation_preflight_accepts_structurally_sane_clip():
    payload = _curve_animation_xml()
    assert detect_roblox_payload_kind(payload) == "curve-anim"
    report = inspect_animation_payload(payload)

    assert report is not None
    assert report.root_class == "CurveAnimation"
    assert report.name == "Preflight Test Animation"
    assert report.loop is True
    assert report.priority == "Action4"
    assert report.rig_data_present is True
    assert report.curve_count == 2
    assert report.duration_seconds == 1.0
    assert report.joint_paths == (
        "HumanoidRootPart/LowerTorso",
        "HumanoidRootPart/RightUpperArm",
    )
    assert report.findings == ()


def test_curve_animation_preflight_flags_extreme_position_and_tiny_duration():
    report = inspect_animation_payload(
        _curve_animation_xml(ticks=(0, 24), z_values=(0.0, -1.0e32))
    )

    assert report is not None
    assert report.duration_seconds is not None
    assert abs(report.duration_seconds - (1 / 600)) < 1e-12
    assert any("extreme value" in finding for finding in report.findings)
    # A short duration is advisory only, but the extreme value still blocks
    assert any("very short" in note for note in report.advisories)
    assert not any("short" in finding for finding in report.findings)
    assert report.requires_override
    assert "Warnings requiring your review:" in report.format()
    assert "advisory only" in report.format()


def test_curve_animation_warns_when_wave_has_no_arm_or_hand_tracks():
    report = inspect_animation_payload(_curve_animation_xml(include_arm_track=False))

    assert report is not None
    assert report.joint_paths == ("HumanoidRootPart/LowerTorso",)
    assert any("No arm, hand, or shoulder" in note for note in report.advisories)
    # Heuristics alone must not block an otherwise readable animation
    assert not report.requires_override


def test_malformed_animation_marker_does_not_look_like_a_clean_import():
    report = inspect_animation_payload(b"<roblox!not-a-complete-rbxm-CurveAnimation")

    assert report is not None
    assert report.root_class == "Unparsed animation"
    assert report.requires_override
    assert any("could not parse" in finding for finding in report.findings)



def _binary_rbxm_from_xml(payload: bytes) -> bytes:
    from fleasion.cache.roblox_document import _xml_to_document
    from fleasion.cache.tools.solidmodel_converter.rbxm.serializer import write_rbxm

    return write_rbxm(_xml_to_document(payload))


def test_binary_curve_animation_is_classified_and_playable():
    from fleasion.cache.animation_viewer import load_animation_data

    binary = _binary_rbxm_from_xml(_curve_animation_xml())

    assert detect_roblox_payload_kind(binary) == "curve-anim"
    keyframes = load_animation_data(binary)
    assert len(keyframes) >= 2
    assert keyframes[-1].time == 1.0
    assert "LowerTorso" in keyframes[-1].pose_by_part_name


def test_binary_keyframe_sequence_is_classified_as_animation_and_parsed():
    from fleasion.cache.animation_viewer import load_animation_data

    xml = b'''<?xml version="1.0"?>
<roblox version="4">
  <Item class="KeyframeSequence" referent="sequence">
    <Properties><string name="Name">Binary animation test</string></Properties>
    <Item class="Keyframe" referent="key">
      <Properties><float name="Time">0</float></Properties>
      <Item class="Pose" referent="pose">
        <Properties>
          <string name="Name">Torso</string>
          <float name="Weight">1</float>
          <CoordinateFrame name="CFrame">
            <X>0</X><Y>0</Y><Z>0</Z>
            <R00>1</R00><R01>0</R01><R02>0</R02>
            <R10>0</R10><R11>1</R11><R12>0</R12>
            <R20>0</R20><R21>0</R21><R22>1</R22>
          </CoordinateFrame>
        </Properties>
      </Item>
    </Item>
  </Item>
</roblox>
'''
    binary = _binary_rbxm_from_xml(xml)

    assert detect_roblox_payload_kind(binary) == "anim"
    keyframes = load_animation_data(binary)
    assert len(keyframes) == 1
    assert "Torso" in keyframes[0].pose_by_part_name


def test_xml_without_referent_attributes_keeps_every_instance_distinct():
    import xml.etree.ElementTree as ET

    from fleasion.cache.roblox_document import _xml_to_document

    payload = _curve_animation_xml()
    xml_instance_count = sum(
        1 for element in ET.fromstring(payload).iter("Item")
    )
    document = _xml_to_document(payload)

    assert len(document.instances) == xml_instance_count
    assert len({instance.referent for instance in document.instances.values()}) == xml_instance_count
