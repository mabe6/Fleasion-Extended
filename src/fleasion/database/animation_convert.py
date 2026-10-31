"""Convert an animation payload to the type and rig of the cache slot it replaces.

This mirrors the rule Fleasion's proxy follows when it serves an animation
replacement: the client must receive the same animation type it originally asked
for. A CurveAnimation slot gets a CurveAnimation, a KeyframeSequence slot gets a
KeyframeSequence, and a player rig (R6/R15) that differs from the slot's rig is
converted. When the payload already matches, its bytes are returned untouched.
"""

from __future__ import annotations

import io
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass

log = logging.getLogger(__name__)

_BINARY_MAGIC = b'<roblox!'


class AnimationConversionError(Exception):
    """Raised when an animation cannot be converted to the target slot's format."""


@dataclass(frozen=True)
class AnimationConversion:
    """Result of matching an animation to a cache slot."""

    payload: bytes
    notes: tuple[str, ...] = ()
    changed: bool = False


def _strip_cdn_prefix(payload: bytes) -> bytes:
    """Drop any bytes before the Roblox document, as the proxy does for raw replacements."""
    head = payload.lstrip(b'\xef\xbb\xbf \t\r\n')
    if head.startswith((b'<roblox', b'<?xml')):
        return payload
    start = payload.find(b'<roblox')
    return payload[start:] if start > 0 else payload


def _rig_convert(xml_data: bytes, target_rig: str) -> bytes:
    """Convert every Keyframe in a KeyframeSequence between the R6 and R15 rigs."""
    from ..utils.r15_to_r6 import (
        convert_keyframe_r6_to_r15,
        convert_keyframe_r15_to_r6,
        sanitize_xml,
    )
    from ..utils.rig_data import R6_JOINTS, R6_PARTS, R15_JOINTS, R15_PARTS

    root = ET.fromstring(sanitize_xml(xml_data))
    sequence = root.find(".//Item[@class='KeyframeSequence']")
    if sequence is None:
        raise AnimationConversionError('No KeyframeSequence was found to convert between rigs.')
    keyframes = sequence.findall("Item[@class='Keyframe']")
    if not keyframes:
        raise AnimationConversionError('The KeyframeSequence has no Keyframes to convert between rigs.')
    for keyframe in keyframes:
        if target_rig == 'R6':
            convert_keyframe_r15_to_r6(keyframe, R6_PARTS, R6_JOINTS, R15_PARTS, R15_JOINTS)
        else:
            convert_keyframe_r6_to_r15(keyframe, R6_PARTS, R6_JOINTS, R15_PARTS, R15_JOINTS)
    buffer = io.BytesIO()
    ET.ElementTree(root).write(buffer, encoding='utf-8', xml_declaration=True)
    return buffer.getvalue()


def convert_animation_for_slot(payload: bytes, target_payload: bytes) -> AnimationConversion:
    """Return payload converted to the type and rig of target_payload.

    Parameters
    ----------
    payload
        Raw RBXM/RBXMX bytes of the replacement animation.
    target_payload
        Raw payload of the cache slot being replaced (without its RBXH header).

    Returns
    -------
    AnimationConversion
        The bytes to write plus human-readable notes about what was converted.

    Raises
    ------
    AnimationConversionError
        If a conversion step fails or does not produce the requested type.
    """
    notes: list[str] = []
    try:
        from ..utils.anim_converter import (
            detect_player_rig,
            detect_rig,
            is_curve_animation,
            rbxm_to_rbxmx,
        )
        from ..utils.r15_to_r6 import curve_anim_to_keyframe_xml, keyframe_to_curve_anim

        source = _strip_cdn_prefix(payload)
        source_is_curve = is_curve_animation(source)
        target_is_curve = is_curve_animation(target_payload)

        source_rig = detect_rig(source)
        target_rig = detect_rig(target_payload)
        if target_rig == 'unknown':
            # Mixed or tool animations still target a player rig, so use the lenient detector
            target_rig = detect_player_rig(target_payload)
        if target_is_curve and target_rig == 'unknown':
            target_rig = source_rig if source_rig != 'unknown' else 'R15'

        needs_rig = target_rig != 'unknown' and source_rig != 'unknown' and source_rig != target_rig
        needs_type = source_is_curve != target_is_curve
        if not needs_rig and not needs_type:
            return AnimationConversion(payload=payload)

        data = rbxm_to_rbxmx(source) if source[:8] == _BINARY_MAGIC else source
        if source_is_curve:
            data = curve_anim_to_keyframe_xml(data)
        if needs_rig:
            data = _rig_convert(data, target_rig)
            notes.append(f'Converted the {source_rig} rig to {target_rig} to match the cache slot.')
        if target_is_curve:
            data = keyframe_to_curve_anim(data)
    except AnimationConversionError:
        raise
    except Exception as exc:
        raise AnimationConversionError(f'{type(exc).__name__}: {exc}') from exc

    if is_curve_animation(data) != target_is_curve:
        raise AnimationConversionError(
            'The conversion did not produce the animation type the cache slot uses.'
        )
    if needs_type:
        notes.insert(
            0,
            'Converted '
            + ('CurveAnimation to KeyframeSequence' if source_is_curve else 'KeyframeSequence to CurveAnimation')
            + ' to match the cache slot.',
        )
    log.info('Animation converted for cache slot: %s', '; '.join(notes))
    return AnimationConversion(payload=data, notes=tuple(notes), changed=True)

