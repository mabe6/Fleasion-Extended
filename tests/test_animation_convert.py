"""Tests for converting animation payloads to the cache slot's format and rig."""

from __future__ import annotations

import base64
import struct

import pytest

from fleasion.database.animation_convert import AnimationConversionError, convert_animation_for_slot
from fleasion.utils import anim_converter, r15_to_r6


def _values_and_times(keys: list[tuple[int, float]]) -> str:
    raw = bytearray(struct.pack('<II', 2, len(keys)))
    for _tick, value in keys:
        raw.extend(struct.pack('<BBfff', 1, 0, value, 0.0, 0.0))
    raw.extend(struct.pack('<II', 1, len(keys)))
    if keys:
        raw.extend(struct.pack(f'<{len(keys)}I', *(tick for tick, _value in keys)))
    return base64.b64encode(raw).decode('ascii')


def _curve_animation_xml() -> bytes:
    arm_values = _values_and_times([(0, 0.0), (14400, 0.5)])
    position_values = _values_and_times([(0, 0.0), (14400, 0.5)])
    return f'''<?xml version="1.0" encoding="utf-8"?>
<roblox version="4">
  <Item class="CurveAnimation">
    <Properties>
      <string name="Name">Conversion Test</string>
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
              <BinaryString name="ValuesAndTimes">{position_values}</BinaryString>
            </Properties>
          </Item>
        </Item>
      </Item>
      <Item class="Folder">
        <Properties><string name="Name">RightUpperArm</string></Properties>
        <Item class="EulerRotationCurve">
          <Properties><string name="Name">Rotation</string></Properties>
          <Item class="FloatCurve">
            <Properties>
              <string name="Name">X</string>
              <BinaryString name="ValuesAndTimes">{arm_values}</BinaryString>
            </Properties>
          </Item>
        </Item>
      </Item>
    </Item>
  </Item>
</roblox>
'''.encode('utf-8')


def _keyframe_sequence_xml(body_part: str = 'LowerTorso', marker: str = '') -> bytes:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<roblox version="4">
  <Item class="KeyframeSequence" referent="sequence">
    <Properties>
      <string name="Name">Conversion Test</string>
      <bool name="Loop">true</bool>
      <token name="Priority">6</token>
    </Properties>
    <Item class="Keyframe" referent="keyframe">
      <Properties><float name="Time">0</float></Properties>
      <Item class="Pose" referent="root-pose">
        <Properties><string name="Name">HumanoidRootPart</string></Properties>
        <Item class="Pose" referent="body-pose">
          <Properties><string name="Name">{body_part}</string></Properties>
        </Item>
      </Item>
    </Item>
  </Item>
{marker}
</roblox>
'''.encode('utf-8')


def _empty_curve_slot() -> bytes:
    return b'<roblox version="4"><Item class="CurveAnimation"/></roblox>'


def test_curve_animation_converts_to_keyframe_sequence(monkeypatch):
    monkeypatch.setattr(anim_converter, 'detect_rig', lambda _data: 'unknown')
    monkeypatch.setattr(anim_converter, 'detect_player_rig', lambda _data: 'unknown')

    result = convert_animation_for_slot(
        _curve_animation_xml(),
        b'<roblox version="4"><Item class="KeyframeSequence"/></roblox>',
    )

    assert result.changed is True
    assert b'KeyframeSequence' in result.payload
    assert b'CurveAnimation' not in result.payload
    assert any('CurveAnimation to KeyframeSequence' in note for note in result.notes)


def test_keyframe_sequence_converts_to_curve_animation(monkeypatch):
    monkeypatch.setattr(anim_converter, 'detect_rig', lambda _data: 'unknown')
    monkeypatch.setattr(anim_converter, 'detect_player_rig', lambda _data: 'unknown')

    result = convert_animation_for_slot(
        _keyframe_sequence_xml(),
        _empty_curve_slot(),
    )

    assert result.changed is True
    assert b'CurveAnimation' in result.payload
    assert b'KeyframeSequence' not in result.payload
    assert any('KeyframeSequence to CurveAnimation' in note for note in result.notes)


def test_r6_animation_is_converted_to_r15_rig(monkeypatch):
    source = _keyframe_sequence_xml('Torso', marker='<!-- SOURCE_R6 -->')
    target = _keyframe_sequence_xml('LowerTorso', marker='<!-- TARGET_R15 -->')

    def detect_rig(data: bytes) -> str:
        return 'R6' if b'SOURCE_R6' in data else 'R15'

    monkeypatch.setattr(anim_converter, 'detect_rig', detect_rig)

    result = convert_animation_for_slot(source, target)

    assert result.changed is True
    assert b'LowerTorso' in result.payload
    assert b'UpperTorso' in result.payload
    assert any('R6 rig to R15' in note for note in result.notes)


def test_matching_animation_payload_is_returned_unchanged(monkeypatch):
    monkeypatch.setattr(anim_converter, 'detect_rig', lambda _data: 'R15')

    payload = b'\xef\xbb\xbf' + _keyframe_sequence_xml()
    result = convert_animation_for_slot(payload, _keyframe_sequence_xml())

    assert result.payload == payload
    assert result.changed is False
    assert result.notes == ()


def test_conversion_failure_is_reported_as_animation_conversion_error(monkeypatch):
    monkeypatch.setattr(anim_converter, 'detect_rig', lambda _data: 'unknown')
    monkeypatch.setattr(anim_converter, 'detect_player_rig', lambda _data: 'unknown')

    def fail_conversion(_data: bytes) -> bytes:
        raise ValueError('broken conversion')

    monkeypatch.setattr(r15_to_r6, 'curve_anim_to_keyframe_xml', fail_conversion)

    with pytest.raises(AnimationConversionError, match='ValueError: broken conversion'):
        convert_animation_for_slot(
            _curve_animation_xml(),
            b'<roblox version="4"><Item class="KeyframeSequence"/></roblox>',
        )
