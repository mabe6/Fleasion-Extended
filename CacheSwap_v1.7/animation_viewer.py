"""PySide6 software-rendered R6/R15 rig preview for Roblox animation assets.

The viewer intentionally avoids OpenGL so it can run inside CacheSwap's existing
PySide6 pane. The rig is a simplified box mannequin: this makes joint motion
inspectable without downloading avatar meshes or running Roblox.
"""
from __future__ import annotations

import math
import time

import numpy as np
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton, QSlider,
    QSizePolicy, QVBoxLayout, QWidget,
)

from animation_data import CurveAnimationData, KeyframeSequenceData


# name, parent, joint offset, segment centre relative to joint, dimensions
_R15 = [
    ("lower_torso", "root", (0.0, 0.12, 0.0), (0.0, 0.15, 0.0), (0.68, 0.55, 0.36), "#4c78a8"),
    ("upper_torso", "lower_torso", (0.0, 0.48, 0.0), (0.0, 0.32, 0.0), (0.88, 0.72, 0.42), "#4c78a8"),
    ("head", "upper_torso", (0.0, 0.68, 0.0), (0.0, 0.24, 0.0), (0.54, 0.54, 0.54), "#e7bf91"),
    ("left_upper_arm", "upper_torso", (-0.55, 0.53, 0.0), (0.0, -0.29, 0.0), (0.27, 0.64, 0.30), "#e7bf91"),
    ("left_lower_arm", "left_upper_arm", (0.0, -0.58, 0.0), (0.0, -0.28, 0.0), (0.23, 0.58, 0.26), "#e7bf91"),
    ("left_hand", "left_lower_arm", (0.0, -0.55, 0.0), (0.0, -0.10, 0.0), (0.23, 0.20, 0.25), "#e7bf91"),
    ("right_upper_arm", "upper_torso", (0.55, 0.53, 0.0), (0.0, -0.29, 0.0), (0.27, 0.64, 0.30), "#e7bf91"),
    ("right_lower_arm", "right_upper_arm", (0.0, -0.58, 0.0), (0.0, -0.28, 0.0), (0.23, 0.58, 0.26), "#e7bf91"),
    ("right_hand", "right_lower_arm", (0.0, -0.55, 0.0), (0.0, -0.10, 0.0), (0.23, 0.20, 0.25), "#e7bf91"),
    ("left_upper_leg", "lower_torso", (-0.22, -0.12, 0.0), (0.0, -0.32, 0.0), (0.31, 0.67, 0.33), "#4c78a8"),
    ("left_lower_leg", "left_upper_leg", (0.0, -0.65, 0.0), (0.0, -0.30, 0.0), (0.28, 0.61, 0.30), "#4c78a8"),
    ("left_foot", "left_lower_leg", (0.0, -0.60, 0.05), (0.0, -0.08, 0.10), (0.30, 0.20, 0.48), "#444b55"),
    ("right_upper_leg", "lower_torso", (0.22, -0.12, 0.0), (0.0, -0.32, 0.0), (0.31, 0.67, 0.33), "#4c78a8"),
    ("right_lower_leg", "right_upper_leg", (0.0, -0.65, 0.0), (0.0, -0.30, 0.0), (0.28, 0.61, 0.30), "#4c78a8"),
    ("right_foot", "right_lower_leg", (0.0, -0.60, 0.05), (0.0, -0.08, 0.10), (0.30, 0.20, 0.48), "#444b55"),
]

_R6 = [
    ("torso", "root", (0.0, 0.12, 0.0), (0.0, 0.45, 0.0), (1.35, 1.15, 0.50), "#4c78a8"),
    ("head", "torso", (0.0, 1.05, 0.0), (0.0, 0.24, 0.0), (0.56, 0.56, 0.56), "#e7bf91"),
    ("left_upper_arm", "torso", (-0.80, 0.83, 0.0), (0.0, -0.48, 0.0), (0.38, 1.00, 0.42), "#e7bf91"),
    ("right_upper_arm", "torso", (0.80, 0.83, 0.0), (0.0, -0.48, 0.0), (0.38, 1.00, 0.42), "#e7bf91"),
    ("left_upper_leg", "torso", (-0.34, -0.08, 0.0), (0.0, -0.49, 0.0), (0.45, 1.00, 0.45), "#4c78a8"),
    ("right_upper_leg", "torso", (0.34, -0.08, 0.0), (0.0, -0.49, 0.0), (0.45, 1.00, 0.45), "#4c78a8"),
]


def _rx(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=float)


def _ry(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=float)


def _rz(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=float)


def _euler_matrix(xyz, order="XYZ"):
    mats = {"X": _rx(xyz[0]), "Y": _ry(xyz[1]), "Z": _rz(xyz[2])}
    result = np.eye(3)
    for axis in order.upper():
        result = result @ mats.get(axis, np.eye(3))
    return result


def _quaternion_matrix(q):
    x, y, z, w = q
    n = math.sqrt(x*x + y*y + z*z + w*w)
    if n < 1e-12:
        return np.eye(3)
    x, y, z, w = x/n, y/n, z/n, w/n
    return np.array([
        [1 - 2*(y*y + z*z), 2*(x*y - z*w), 2*(x*z + y*w)],
        [2*(x*y + z*w), 1 - 2*(x*x + z*z), 2*(y*z - x*w)],
        [2*(x*z - y*w), 2*(y*z + x*w), 1 - 2*(x*x + y*y)],
    ], dtype=float)


def _target_for_joint(name: str, rig: str) -> str | None:
    n = "".join(ch.lower() for ch in name if ch.isalnum())
    common = {
        "humanoidrootpart": "root", "root": "lower_torso" if rig == "R15" else "torso",
        "rootjoint": "torso", "lowertorso": "lower_torso" if rig == "R15" else "torso",
        "uppertorso": "upper_torso" if rig == "R15" else "torso", "torso": "torso",
        "waist": "upper_torso" if rig == "R15" else "torso", "neck": "head", "head": "head",
        "leftshoulder": "left_upper_arm", "leftupperarm": "left_upper_arm", "leftarm": "left_upper_arm",
        "leftelbow": "left_lower_arm", "leftlowerarm": "left_lower_arm", "leftwrist": "left_hand", "lefthand": "left_hand",
        "rightshoulder": "right_upper_arm", "rightupperarm": "right_upper_arm", "rightarm": "right_upper_arm",
        "rightelbow": "right_lower_arm", "rightlowerarm": "right_lower_arm", "rightwrist": "right_hand", "righthand": "right_hand",
        "lefthip": "left_upper_leg", "leftupperleg": "left_upper_leg", "leftleg": "left_upper_leg",
        "leftknee": "left_lower_leg", "leftlowerleg": "left_lower_leg", "leftankle": "left_foot", "leftfoot": "left_foot",
        "righthip": "right_upper_leg", "rightupperleg": "right_upper_leg", "rightleg": "right_upper_leg",
        "rightknee": "right_lower_leg", "rightlowerleg": "right_lower_leg", "rightankle": "right_foot", "rightfoot": "right_foot",
    }
    if n in common:
        result = common[n]
        if rig == "R6":
            if result in ("left_lower_arm", "left_hand"):
                return "left_upper_arm"
            if result in ("right_lower_arm", "right_hand"):
                return "right_upper_arm"
            if result == "lower_torso":
                return "torso"
            if result == "upper_torso":
                return "torso"
        return result
    return None


def _box_geometry(size):
    x, y, z = (float(v) / 2.0 for v in size)
    verts = np.array([
        [-x,-y,-z], [x,-y,-z], [x,y,-z], [-x,y,-z],
        [-x,-y,z], [x,-y,z], [x,y,z], [-x,y,z],
    ], dtype=float)
    faces = [(0,1,2,3), (4,7,6,5), (0,4,5,1), (3,2,6,7), (0,3,7,4), (1,5,6,2)]
    return verts, faces


class _RigCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.animation: CurveAnimationData | KeyframeSequenceData | None = None
        self.current_time = 0.0
        self.rig = "R15"
        self.wire = False
        self.grid = True
        self.yaw = 0.55
        self.pitch = 0.18
        self.zoom = 1.0
        self._last_mouse = None
        self.setMinimumSize(220, 250)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_state(self, animation, current_time, rig, wire, grid):
        self.animation = animation
        self.current_time = current_time
        self.rig = rig
        self.wire = wire
        self.grid = grid
        self.update()

    def mousePressEvent(self, e):
        self._last_mouse = e.position()

    def mouseMoveEvent(self, e):
        if self._last_mouse is not None and e.buttons() & Qt.MouseButton.LeftButton:
            delta = e.position() - self._last_mouse
            self.yaw += delta.x() * 0.01
            self.pitch = max(-1.1, min(1.1, self.pitch + delta.y() * 0.01))
            self._last_mouse = e.position()
            self.update()

    def mouseReleaseEvent(self, _e):
        self._last_mouse = None

    def wheelEvent(self, e):
        self.zoom = max(0.35, min(4.0, self.zoom * (1.1 if e.angleDelta().y() > 0 else 1 / 1.1)))
        self.update()

    def _pose_by_part(self):
        data = self.animation
        poses = {}
        if data is None:
            return poses
        t = self.current_time
        if isinstance(data, KeyframeSequenceData):
            for name in data.joint_names:
                target = _target_for_joint(name, self.rig)
                if not target:
                    continue
                sample = data.sample_joint(name, t)
                if sample is None:
                    continue
                pos, matrix = sample
                poses[target] = (np.asarray(pos, dtype=float), np.asarray(matrix, dtype=float).reshape(3, 3))
            return poses

        names = set(data.positions) | set(data.euler_rotations) | set(data.quaternion_rotations)
        for name in sorted(names):
            target = _target_for_joint(name, self.rig)
            if not target:
                continue
            pos = np.zeros(3, dtype=float)
            for i, axis in enumerate(("X", "Y", "Z")):
                curve = data.positions.get(name, {}).get(axis)
                value = curve.sample(t) if curve else None
                if value is not None:
                    pos[i] = value
            rot = np.eye(3)
            qcurve = data.quaternion_rotations.get(name)
            q = qcurve.sample(t) if qcurve else None
            if q is not None:
                rot = _quaternion_matrix(q)
            else:
                vals = [0.0, 0.0, 0.0]
                for i, axis in enumerate(("X", "Y", "Z")):
                    curve = data.euler_rotations.get(name, {}).get(axis)
                    value = curve.sample(t) if curve else None
                    if value is not None:
                        vals[i] = value
                rot = _euler_matrix(vals, data.rotation_orders.get(name, "XYZ"))
            # If multiple CurveAnimation folders map to the same box part, the
            # last track provides a deterministic fallback for name variants.
            poses[target] = (pos, rot)
        return poses

    def _scene(self):
        specs = _R15 if self.rig == "R15" else _R6
        poses = self._pose_by_part()
        world = {"root": (np.zeros(3), np.eye(3))}
        if "root" in poses:
            world["root"] = (poses["root"][0], poses["root"][1])
        boxes = []
        for name, parent, offset, center, size, color in specs:
            parent_pos, parent_rot = world.get(parent, world["root"])
            pos, local_rot = poses.get(name, (np.zeros(3), np.eye(3)))
            joint_pos = parent_pos + parent_rot @ (np.array(offset, dtype=float) + pos)
            world_rot = parent_rot @ local_rot
            world[name] = (joint_pos, world_rot)
            local_verts, faces = _box_geometry(size)
            verts = (local_verts + np.asarray(center, dtype=float)) @ world_rot.T + joint_pos
            boxes.append((verts, faces, color, name))
        return boxes

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), QColor("#20252b"))
        yaw, pitch = self.yaw, self.pitch
        camera = _rx(pitch) @ _ry(yaw)
        scale = min(self.width(), self.height()) * 0.30 * self.zoom
        cx, cy = self.width() * 0.5, self.height() * 0.54

        def project(v):
            cam = np.asarray(v) @ camera.T
            return QPointF(cx + cam[0] * scale, cy - cam[1] * scale), float(cam[2])

        if self.grid:
            p.setPen(QPen(QColor("#343b44"), 1))
            for x in range(-5, 6):
                a, _ = project((x * 0.5, -2.75, -2.5))
                b, _ = project((x * 0.5, -2.75, 2.5))
                c, _ = project((-2.5, -2.75, x * 0.5))
                d, _ = project((2.5, -2.75, x * 0.5))
                p.drawLine(a, b)
                p.drawLine(c, d)

        boxes = self._scene()
        rendered = []
        light = np.array([-0.35, 0.8, -0.55], dtype=float)
        light /= np.linalg.norm(light)
        for verts, faces, color, name in boxes:
            projected = [project(v) for v in verts]
            for face in faces:
                pts = [projected[i][0] for i in face]
                depth = sum(projected[i][1] for i in face) / len(face)
                tri = verts[list(face)]
                normal = np.cross(tri[1] - tri[0], tri[2] - tri[0])
                nn = np.linalg.norm(normal)
                if nn > 1e-9:
                    normal /= nn
                shade = max(0.25, min(1.0, 0.48 + 0.52 * abs(float(np.dot(normal, light)))))
                base = QColor(color)
                base.setRed(int(base.red() * shade))
                base.setGreen(int(base.green() * shade))
                base.setBlue(int(base.blue() * shade))
                rendered.append((depth, pts, base, name))
        # Farther geometry is painted first; camera uses +z as the near direction.
        rendered.sort(key=lambda item: item[0])
        for _depth, pts, color, _name in rendered:
            if self.wire:
                p.setPen(QPen(QColor("#c7d4e3"), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
            else:
                p.setPen(QPen(QColor("#15191e"), 1))
                p.setBrush(QBrush(color))
            p.drawPolygon(QPolygonF(pts))

        if self.animation is None:
            p.setPen(QColor("#b7c0cc"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Select an animation cache entry")
        elif not self.animation.has_motion:
            p.setPen(QColor("#ffcc80"))
            p.drawText(10, 22, "No timed keys — static pose")


class AnimationRigView(QWidget):
    """Playback controls and a mouse-orbit software-rendered rig canvas."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.animation: CurveAnimationData | KeyframeSequenceData | None = None
        self.current_time = 0.0
        self.playing = False
        self.speed = 1.0
        self._last_tick = time.monotonic()
        self._scrubbing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.heading = QLabel("Animation preview")
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)

        self.canvas = _RigCanvas()
        layout.addWidget(self.canvas, 1)

        controls = QHBoxLayout()
        self.play_button = QPushButton("▶ Play")
        self.play_button.clicked.connect(self.toggle_play)
        controls.addWidget(self.play_button)
        self.time_slider = QSlider(Qt.Orientation.Horizontal)
        self.time_slider.setRange(0, 1000)
        self.time_slider.setEnabled(False)
        self.time_slider.sliderPressed.connect(self._slider_pressed)
        self.time_slider.sliderReleased.connect(self._slider_released)
        self.time_slider.valueChanged.connect(self._slider_changed)
        controls.addWidget(self.time_slider, 1)
        self.time_label = QLabel("0.00 / 0.00 s")
        self.time_label.setMinimumWidth(105)
        controls.addWidget(self.time_label)
        layout.addLayout(controls)

        options = QHBoxLayout()
        options.addWidget(QLabel("Rig"))
        self.rig_box = QComboBox()
        self.rig_box.addItems(["R15", "R6"])
        self.rig_box.currentTextChanged.connect(self._settings_changed)
        options.addWidget(self.rig_box)
        options.addWidget(QLabel("Speed"))
        self.speed_box = QComboBox()
        self.speed_box.addItems(["0.25×", "0.5×", "1×", "1.5×", "2×"])
        self.speed_box.setCurrentText("1×")
        self.speed_box.currentTextChanged.connect(self._speed_changed)
        options.addWidget(self.speed_box)
        self.wire_box = QCheckBox("Wireframe")
        self.wire_box.toggled.connect(self._settings_changed)
        options.addWidget(self.wire_box)
        self.grid_box = QCheckBox("Grid")
        self.grid_box.setChecked(True)
        self.grid_box.toggled.connect(self._settings_changed)
        options.addWidget(self.grid_box)
        options.addStretch(1)
        layout.addLayout(options)

        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.set_animation(None)

    def set_animation(self, animation: CurveAnimationData | KeyframeSequenceData | None):
        self.animation = animation
        self.current_time = 0.0
        self.playing = bool(animation and animation.duration > 0)
        self._last_tick = time.monotonic()
        if animation is None:
            self.heading.setText("Animation preview")
        else:
            if isinstance(animation, KeyframeSequenceData):
                track_names = animation.joint_names
            else:
                track_names = sorted(set(animation.positions) | set(animation.euler_rotations) | set(animation.quaternion_rotations))
            self.heading.setText(
                f"{animation.name}  ·  {animation.priority or 'Unknown priority'}  ·  "
                f"{'looped' if animation.loop else 'not looped'}\n"
                f"{len(track_names)} animated joint(s)  ·  {animation.duration:.3f} s"
            )
        self.time_slider.blockSignals(True)
        self.time_slider.setValue(0)
        self.time_slider.setEnabled(bool(animation and animation.duration > 0))
        self.time_slider.blockSignals(False)
        self.play_button.setEnabled(bool(animation and animation.duration > 0))
        self._update_play_button()
        self._refresh()

    def toggle_play(self):
        if self.animation is None or self.animation.duration <= 0:
            return
        self.playing = not self.playing
        if self.playing and self.current_time >= self.animation.duration:
            self.current_time = 0.0
        self._last_tick = time.monotonic()
        self._update_play_button()
        self._refresh()

    def _update_play_button(self):
        self.play_button.setText("❚❚ Pause" if self.playing else "▶ Play")

    def _tick(self):
        now = time.monotonic()
        dt = min(0.1, max(0.0, now - self._last_tick))
        self._last_tick = now
        if self.playing and not self._scrubbing and self.animation and self.animation.duration > 0:
            self.current_time += dt * self.speed
            if self.current_time >= self.animation.duration:
                if self.animation.loop:
                    self.current_time %= self.animation.duration
                else:
                    self.current_time = self.animation.duration
                    self.playing = False
                    self._update_play_button()
            self._refresh()

    def _slider_pressed(self):
        self._scrubbing = True

    def _slider_released(self):
        self._scrubbing = False
        self._refresh()

    def _slider_changed(self, value):
        if self.animation and self.animation.duration > 0:
            self.current_time = self.animation.duration * value / 1000.0
            self._refresh(update_slider=False)

    def _speed_changed(self, text):
        self.speed = float(text.replace("×", ""))

    def _settings_changed(self, *_):
        self._refresh()

    def _refresh(self, update_slider=True):
        duration = self.animation.duration if self.animation else 0.0
        if update_slider and not self._scrubbing:
            self.time_slider.blockSignals(True)
            self.time_slider.setValue(round(1000 * self.current_time / duration) if duration > 0 else 0)
            self.time_slider.blockSignals(False)
        self.time_label.setText(f"{self.current_time:.2f} / {duration:.2f} s")
        self.canvas.set_state(self.animation, self.current_time, self.rig_box.currentText(),
                              self.wire_box.isChecked(), self.grid_box.isChecked())
        self._update_play_button()
