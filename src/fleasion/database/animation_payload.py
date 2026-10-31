"""Preflight checks for importing Roblox animation clips into the local cache.

This module is deliberately diagnostic: it never rewrites animation keys or
silently repairs an asset. A suspicious payload requires an explicit override.
"""

from __future__ import annotations

import base64
import math
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any


# The existing Fleasion CurveAnimation preview parser uses this unit conversion.
# Keep wording in the report explicit because this is an estimate, not engine playback.
_CURVE_TICKS_PER_SECOND = 14400.0
_MAX_CURVE_KEYS = 1_000_000
_EXTREME_VALUE_LIMIT = 1.0e12
_EXTREME_POSITION_LIMIT = 10_000.0

_PRIORITY_NAMES = {
    0: "Core",
    1: "Idle",
    2: "Movement",
    3: "Action",
    4: "Action2",
    5: "Action3",
    6: "Action4",
}


@dataclass(frozen=True)
class AnimationPayloadInspection:
    """Readable summary of an imported CurveAnimation or KeyframeSequence."""

    root_class: str
    name: str = ""
    loop: bool | None = None
    priority: str = ""
    duration_seconds: float | None = None
    keyframe_count: int = 0
    curve_count: int = 0
    pose_count: int = 0
    rig_data_present: bool = False
    joint_paths: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    advisories: tuple[str, ...] = ()

    @property
    def requires_override(self) -> bool:
        """Only findings (damaged or unreadable data) block an import; advisories never do."""
        return bool(self.findings)

    @property
    def kind(self) -> str:
        return "curve-anim" if self.root_class == "CurveAnimation" else "anim"

    def format(self) -> str:
        """Return a GUI-safe report for the user to review before writing."""
        lines = [
            f"Format: {self.root_class}",
            f"Name: {self.name or '(unnamed)'}",
            f"Loop: {'yes' if self.loop else 'no' if self.loop is not None else 'unknown'}",
            f"Priority: {self.priority or 'unknown'}",
        ]
        if self.duration_seconds is not None:
            lines.append(
                f"Duration estimate: {self.duration_seconds:.6g} s "
                "(based on Fleasion's current CurveAnimation time conversion)"
            )
        if self.root_class == "CurveAnimation":
            lines.extend(
                [
                    f"Float curves: {self.curve_count}",
                    f"AnimationRigData: {'present' if self.rig_data_present else 'not found'}",
                ]
            )
        else:
            lines.extend(
                [
                    f"Keyframes: {self.keyframe_count}",
                    f"Poses: {self.pose_count}",
                ]
            )
        if self.joint_paths:
            lines.append("Joint paths: " + ", ".join(self.joint_paths[:12]))
            if len(self.joint_paths) > 12:
                lines.append(f"... and {len(self.joint_paths) - 12} more")
        if self.findings:
            lines.append("")
            lines.append("Warnings requiring your review:")
            lines.extend(f"• {finding}" for finding in self.findings)
        else:
            lines.append("")
            lines.append("No obvious structural or numeric problem was detected.")
        if self.advisories:
            lines.append("")
            lines.append("Notes (advisory only, these do not block the import):")
            lines.extend(f"• {note}" for note in self.advisories)
        lines.append("")
        lines.append(
            "This inspection checks the file data only. It cannot prove that Roblox's "
            "runtime will accept or play the animation in the selected cache slot."
        )
        return "\n".join(lines)


@dataclass
class _Node:
    class_name: str
    properties: dict[str, Any]
    children: list["_Node"]


def _value(node: _Node, key: str, default: Any = None) -> Any:
    value = node.properties.get(key, default)
    return getattr(value, "value", value)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip()


def _as_bytes(value: Any) -> bytes | None:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, str):
        # The current RBXM deserializer returns str only after a successful UTF-8
        # decode, so UTF-8 re-encoding preserves those original bytes.
        return value.encode("utf-8")
    return None


def _from_binary(payload: bytes) -> list[_Node]:
    from ..cache.tools.solidmodel_converter.rbxm.deserializer import RbxmDeserializer

    document = RbxmDeserializer().deserialize(payload)

    def convert(instance: Any) -> _Node:
        return _Node(
            class_name=str(instance.class_name),
            properties=dict(instance.properties),
            children=[convert(child) for child in instance.children],
        )

    return [convert(root) for root in document.roots]


def _parse_xml_property(element: ET.Element) -> Any:
    text = element.text or ""
    tag = element.tag.casefold()
    if tag in {"binarystring", "sharedstring"}:
        try:
            return base64.b64decode(text.strip(), validate=False)
        except (ValueError, TypeError):
            return text.encode("utf-8")
    if tag == "bool":
        return text.strip().casefold() == "true"
    if tag in {"float", "double"}:
        try:
            return float(text.strip())
        except ValueError:
            return math.nan
    if tag in {"int", "int64", "token", "uint64"}:
        try:
            return int(text.strip())
        except ValueError:
            return text.strip()
    return text.strip()


def _from_xml(payload: bytes) -> list[_Node]:
    root = ET.fromstring(payload.decode("utf-8-sig", errors="replace"))

    def convert(element: ET.Element) -> _Node:
        class_name = str(element.get("class") or "")
        properties: dict[str, Any] = {}
        prop_container = element.find("Properties")
        if prop_container is not None:
            for prop in prop_container:
                prop_name = prop.get("name")
                if prop_name:
                    properties[str(prop_name)] = _parse_xml_property(prop)
        children = [
            convert(child)
            for child in element
            if child.tag == "Item"
        ]
        return _Node(class_name=class_name, properties=properties, children=children)

    return [convert(child) for child in root if child.tag == "Item"]


def _walk(nodes: list[_Node]):
    """Yield each node with its ancestors from the document root."""
    def visit(node: _Node, ancestors: tuple[_Node, ...]):
        yield node, ancestors
        for child in node.children:
            yield from visit(child, ancestors + (node,))

    for node in nodes:
        yield from visit(node, ())


def _decode_values_and_times(raw_value: Any) -> tuple[list[float], list[int], str | None]:
    """Read the ValuesAndTimes blob enough to validate counts and numeric values."""
    raw = _as_bytes(raw_value)
    if raw is None or len(raw) < 16:
        return [], [], "ValuesAndTimes data is missing or truncated"
    try:
        _value_version, value_count = struct.unpack_from("<II", raw, 0)
        if value_count > _MAX_CURVE_KEYS:
            return [], [], f"ValuesAndTimes contains an implausible key count ({value_count:,})"
        time_section = 8 + value_count * 14
        if time_section + 8 > len(raw):
            return [], [], "ValuesAndTimes value section is truncated"
        _time_version, time_count = struct.unpack_from("<II", raw, time_section)
        if time_count > _MAX_CURVE_KEYS or time_section + 8 + time_count * 4 > len(raw):
            return [], [], "ValuesAndTimes time section is truncated or has an implausible count"
        values = [
            float(struct.unpack_from("<f", raw, 8 + i * 14 + 2)[0])
            for i in range(value_count)
        ]
        times = [
            int(struct.unpack_from("<I", raw, time_section + 8 + i * 4)[0])
            for i in range(time_count)
        ]
    except (struct.error, OverflowError, ValueError):
        return [], [], "ValuesAndTimes could not be decoded safely"

    if value_count != time_count:
        return values, times, (
            f"ValuesAndTimes has {value_count} values but {time_count} timestamps"
        )
    if not values:
        return values, times, "ValuesAndTimes has no animation keys"
    return values, times, None


def inspect_animation_payload(payload: bytes) -> AnimationPayloadInspection | None:
    """Inspect a raw RBXM/RBXMX animation without modifying its bytes.

    Returns None for assets that do not appear to be animations. Parse failures
    become visible findings when the bytes look like an animation, so imports do
    not silently succeed with an unreadable payload.
    """
    stripped = payload.lstrip(b"\xef\xbb\xbf \t\r\n")
    looks_binary = payload.startswith(b"<roblox!")
    looks_xml = stripped.startswith(b"<roblox") or (
        stripped.startswith(b"<?xml") and b"<roblox" in stripped[:1024]
    )

    if not (looks_binary or looks_xml):
        return None

    try:
        roots = _from_binary(payload) if looks_binary else _from_xml(stripped)
    except Exception as exc:
        preview = payload[:128].decode("ascii", errors="replace")
        if b"CurveAnimation" not in payload and b"KeyframeSequence" not in payload:
            return None
        return AnimationPayloadInspection(
            root_class="Unparsed animation",
            findings=(
                "The file contains an animation class marker but Fleasion could not parse "
                f"the serialized asset ({type(exc).__name__}: {exc}).",
                f"File header: {preview!r}",
            ),
        )

    all_nodes = list(_walk(roots))
    classes = [node.class_name for node, _ancestors in all_nodes]
    animation_roots = [
        node
        for node in roots
        if node.class_name in {"CurveAnimation", "KeyframeSequence"}
    ]
    if not animation_roots:
        if "CurveAnimation" not in classes and "KeyframeSequence" not in classes:
            return None
        return AnimationPayloadInspection(
            root_class="Unparsed animation",
            findings=("The file contains animation instances, but no animation root was found.",),
        )

    animation_root = animation_roots[0]
    scoped = list(_walk([animation_root]))
    name = _as_text(_value(animation_root, "Name"))
    loop_value = _value(animation_root, "Loop")
    loop = loop_value if isinstance(loop_value, bool) else None
    priority_value = _value(animation_root, "Priority")
    if isinstance(priority_value, int):
        priority = _PRIORITY_NAMES.get(priority_value, f"Unknown ({priority_value})")
    else:
        priority = _as_text(priority_value)

    findings: list[str] = []
    advisories: list[str] = []
    joint_paths: list[str] = []
    duration_seconds: float | None = None
    curve_count = 0
    keyframe_count = 0
    pose_count = 0
    rig_data_present = any(cls == "AnimationRigData" for cls in classes)

    if animation_root.class_name == "CurveAnimation":
        all_times: list[int] = []
        for node, ancestors in scoped:
            if node.class_name == "Folder":
                folder_name = _as_text(_value(node, "Name"))
                # Only call a folder an animated joint when it owns transform
                # curves, rather than listing every organizational folder.
                has_transform_curves = any(
                    child.class_name in {"Vector3Curve", "EulerRotationCurve", "RotationCurve"}
                    for child in node.children
                )
                if folder_name and has_transform_curves:
                    path_parts = [
                        _as_text(_value(parent, "Name"))
                        for parent in ancestors
                        if parent.class_name == "Folder" and _as_text(_value(parent, "Name"))
                    ]
                    full_path = "/".join(path_parts + [folder_name])
                    if full_path not in joint_paths:
                        joint_paths.append(full_path)
            if node.class_name != "FloatCurve":
                continue
            raw_values = _value(node, "ValuesAndTimes")
            if raw_values is None:
                continue
            curve_count += 1
            values, times, issue = _decode_values_and_times(raw_values)
            context = [
                _as_text(_value(parent, "Name")) or parent.class_name
                for parent in ancestors
            ]
            context.append(_as_text(_value(node, "Name")) or "FloatCurve")
            track_path = "/".join(part for part in context if part)
            in_position_curve = any(
                ancestor.class_name == "Vector3Curve"
                and _as_text(_value(ancestor, "Name")).casefold() == "position"
                for ancestor in ancestors
            )
            if issue:
                findings.append(f"{track_path}: {issue}.")
            if times:
                all_times.extend(times)
                curve_duration = max(times) / _CURVE_TICKS_PER_SECOND
                duration_seconds = (
                    curve_duration if duration_seconds is None
                    else max(duration_seconds, curve_duration)
                )
                if any(a >= b for a, b in zip(times, times[1:])):
                    findings.append(f"{track_path}: timestamps are not strictly increasing.")
            for value in values:
                if not math.isfinite(value):
                    findings.append(f"{track_path}: contains a NaN or infinite key value.")
                    break
                limit = _EXTREME_POSITION_LIMIT if in_position_curve else _EXTREME_VALUE_LIMIT
                if abs(value) > limit:
                    findings.append(
                        f"{track_path}: contains an extreme value ({value:.6g}); "
                        "that can send a body part far outside the rig or make the clip unusable."
                    )
                    break

        if curve_count == 0:
            findings.append("No FloatCurve data with ValuesAndTimes was found.")
        if duration_seconds is None or duration_seconds <= 0:
            findings.append("The animation has no positive-duration curve data.")
        elif duration_seconds < 0.1:
            advisories.append(
                f"The estimated duration is very short ({duration_seconds:.6g} s). Fleasion "
                "assumes 14400 time ticks per second, so check the preview speed if this "
                "clip is meant to be longer."
            )
        if not rig_data_present:
            advisories.append(
                "No AnimationRigData instance was found. Some emote/import "
                "workflows may reject the clip."
            )
        if not joint_paths:
            advisories.append("No named joint folders with transform curves were found under CurveAnimation.")
        elif not any(
            token in path.casefold()
            for path in joint_paths
            for token in ("arm", "hand", "shoulder")
        ):
            advisories.append(
                "No arm, hand, or shoulder transform curves were found. If this file is intended "
                "to replace a wave gesture, the arm-waving motion appears to be absent from the asset."
            )
    else:
        keyframe_times: list[float] = []
        for node, ancestors in scoped:
            if node.class_name == "Keyframe":
                keyframe_count += 1
                value = _value(node, "Time")
                try:
                    parsed_time = float(value)
                    if math.isfinite(parsed_time):
                        keyframe_times.append(parsed_time)
                    else:
                        findings.append("A Keyframe has a non-finite Time value.")
                except (TypeError, ValueError):
                    findings.append("A Keyframe is missing a numeric Time value.")
            elif node.class_name == "Pose":
                pose_count += 1
        if keyframe_times:
            duration_seconds = max(keyframe_times)
        if keyframe_count == 0:
            findings.append("The KeyframeSequence contains no Keyframe instances.")
        if pose_count == 0:
            findings.append("The KeyframeSequence contains no Pose instances.")
        if duration_seconds is None or duration_seconds <= 0:
            findings.append("The KeyframeSequence has no positive-duration keyframes.")
        elif duration_seconds < 0.1:
            advisories.append(
                f"The parsed duration is very short ({duration_seconds:.6g} s); "
                "the clip may be effectively a still frame."
            )

    # Deduplicate equivalent findings from curves that share the same bad condition.
    unique_findings = tuple(dict.fromkeys(findings))
    return AnimationPayloadInspection(
        root_class=animation_root.class_name,
        name=name,
        loop=loop,
        priority=priority,
        duration_seconds=duration_seconds,
        keyframe_count=keyframe_count,
        curve_count=curve_count,
        pose_count=pose_count,
        rig_data_present=rig_data_present,
        joint_paths=tuple(joint_paths),
        findings=unique_findings,
        advisories=tuple(dict.fromkeys(advisories)),
    )
