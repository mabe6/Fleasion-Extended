"""preview_ui.py - the preview pane shown next to the entry tables (Qt widgets)."""
from __future__ import annotations

import hashlib
import json
import io
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPointF, Qt, QUrl
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPixmap, QPolygonF, QPen
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QRadioButton,
    QSizePolicy, QStackedWidget, QVBoxLayout, QWidget, QMessageBox, QTableWidget,
    QTableWidgetItem, QHeaderView,
)

import core
import preview
import animation_data
from animation_viewer import AnimationRigView
from fleasion_port.cache.rbxm_preview import RbxmPreviewWidget, is_rbx_model_data
from fleasion_port.cache.cache_json_viewer import CacheJsonViewer
from fleasion_port.cache.font_viewer import FontViewerWidget
try:
    from fleasion_port.cache import mesh_processing as fleasion_mesh_processing
except Exception:
    fleasion_mesh_processing = None

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    HAVE_AUDIO = True
except Exception:  # some Qt builds ship without multimedia
    HAVE_AUDIO = False

MAX_TRIS = 12000  # triangles drawn per frame (software renderer)


class ImageView(QWidget):
    """Image on a checkerboard, with R / G / B / A channel views (normal and packed maps are common)."""

    def __init__(self):
        super().__init__()
        self._rgba: np.ndarray | None = None
        self._mode = "RGBA"
        self._pm: QPixmap | None = None
        self.checker = True
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)

    def set_image(self, img: QImage | None):
        if img is None or img.isNull():
            self._rgba = None
        else:
            img = img.convertToFormat(QImage.Format_RGBA8888)
            w, h = img.width(), img.height()
            buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=img.bytesPerLine() * h)
            self._rgba = buf.reshape(h, img.bytesPerLine())[:, :w * 4].reshape(h, w, 4).copy()
        self._rebuild()

    def set_mode(self, mode: str):
        self._mode = mode
        self._rebuild()

    def set_checker(self, on: bool):
        self.checker = on
        self.update()

    def _rebuild(self):
        self._pm = None
        if self._rgba is not None:
            v = np.ascontiguousarray(preview.channel_view(self._rgba, self._mode))
            h, w = v.shape[:2]
            self._pm = QPixmap.fromImage(QImage(v.data, w, h, w * 4, QImage.Format_RGBA8888).copy())
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        r = self.rect()
        if self.checker:
            sq = 12
            p.fillRect(r, QColor("#c8c8c8"))
            dark = QColor("#9a9a9a")
            for y in range(0, r.height(), sq):
                for x in range(0, r.width(), sq):
                    if (x // sq + y // sq) % 2:
                        p.fillRect(x, y, sq, sq, dark)
        else:
            p.fillRect(r, QColor("#2b2b2b"))
        if self._pm is not None and not self._pm.isNull():
            sc = self._pm.scaled(r.size(), Qt.KeepAspectRatio, Qt.FastTransformation
                                 if self._pm.width() * 2 <= r.width() else Qt.SmoothTransformation)
            p.drawPixmap((r.width() - sc.width()) // 2, (r.height() - sc.height()) // 2, sc)


class MeshView(QWidget):
    """Tiny software 3D viewer: drag to rotate, wheel to zoom."""

    def __init__(self):
        super().__init__()
        self.mesh: preview.Mesh | None = None
        self.yaw, self.pitch, self.zoom = 0.6, 0.35, 1.0
        self._last = None
        self.wire = False
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_mesh(self, mesh: preview.Mesh | None):
        self.mesh = mesh
        self.yaw, self.pitch, self.zoom = 0.6, 0.35, 1.0
        if mesh is not None:
            v = mesh.verts
            lo, hi = v.min(axis=0), v.max(axis=0)
            self._center = (lo + hi) / 2
            self._scale = float(max((hi - lo).max(), 1e-6))
            f = mesh.faces
            if len(f) > MAX_TRIS:
                f = f[:: int(np.ceil(len(f) / MAX_TRIS))]
            self._faces = f
        self.update()

    def set_wire(self, on: bool):
        self.wire = on
        self.update()

    def mousePressEvent(self, e):
        self._last = e.position()

    def mouseMoveEvent(self, e):
        if self._last is not None and e.buttons() & Qt.LeftButton:
            d = e.position() - self._last
            self.yaw += d.x() * 0.01
            self.pitch = max(-1.5, min(1.5, self.pitch + d.y() * 0.01))
            self._last = e.position()
            self.update()

    def mouseReleaseEvent(self, e):
        self._last = None

    def wheelEvent(self, e):
        self.zoom = max(0.2, min(8.0, self.zoom * (1.1 if e.angleDelta().y() > 0 else 1 / 1.1)))
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#2b2b2b"))
        if self.mesh is None or len(self._faces) == 0:
            return
        cy, sy, cp, sp = np.cos(self.yaw), np.sin(self.yaw), np.cos(self.pitch), np.sin(self.pitch)
        ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
        rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]])
        v = ((self.mesh.verts - self._center) / self._scale) @ (rx @ ry).T
        side = min(self.width(), self.height()) * 0.8 * self.zoom
        sx = self.width() / 2 + v[:, 0] * side
        sy_ = self.height() / 2 - v[:, 1] * side
        tri = v[self._faces]                       # (M, 3, 3)
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-9)
        shade = np.abs(n[:, 2])                    # light from the camera
        order = np.argsort(tri[:, :, 2].mean(axis=1))  # far to near (camera looks down -z... z toward viewer)
        p.setRenderHint(QPainter.Antialiasing, False)
        if self.wire:
            p.setPen(QPen(QColor("#8ab4f8"), 1))
            p.setBrush(Qt.NoBrush)
        else:
            p.setPen(Qt.NoPen)
        for i in order:
            a, b, c = self._faces[i]
            if not self.wire:
                g = int(60 + 170 * shade[i])
                p.setBrush(QBrush(QColor(g, g, min(255, g + 20))))
            p.drawPolygon(QPolygonF([QPointF(sx[a], sy_[a]), QPointF(sx[b], sy_[b]), QPointF(sx[c], sy_[c])]))


class TexturePackView(QWidget):
    """Structured view of texture-pack map IDs, following Fleasion's map slots."""
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("TexturePack maps found in this asset"))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Map", "Asset ID", "Global slot"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.copy_id_btn = QPushButton("Copy selected asset ID")
        self.copy_all_btn = QPushButton("Copy all map IDs")
        self.copy_id_btn.clicked.connect(self.copy_selected_id)
        self.copy_all_btn.clicked.connect(self.copy_all_ids)
        row.addWidget(self.copy_id_btn)
        row.addWidget(self.copy_all_btn)
        row.addStretch(1)
        layout.addLayout(row)
        self.note = QLabel("These are referenced texture asset IDs; their image bytes are not embedded in the pack XML.")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

    def set_maps(self, maps):
        self.table.setRowCount(len(maps))
        for row, item in enumerate(maps):
            values = [str(item["name"]), str(item["asset_id"]), str(item["slot"])]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                self.table.setItem(row, col, cell)
        if maps:
            self.table.selectRow(0)

    def copy_selected_id(self):
        row = self.table.currentRow()
        cell = self.table.item(row, 1) if row >= 0 else None
        if cell:
            from PySide6.QtGui import QGuiApplication
            QGuiApplication.clipboard().setText(cell.text())

    def copy_all_ids(self):
        values = [self.table.item(row, 1).text() for row in range(self.table.rowCount()) if self.table.item(row, 1)]
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText("\n".join(values))


class PreviewPane(QWidget):
    PAGE_INFO, PAGE_IMAGE, PAGE_MESH, PAGE_AUDIO, PAGE_ANIM = range(5)

    def __init__(self, store_getter):
        super().__init__()
        self._store = store_getter
        self._player = None
        self._audio_file = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel("Click an entry to preview it.")
        self.title.setWordWrap(True)
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.title)

        self.stack = QStackedWidget()
        self.info = QPlainTextEdit()
        self.info.setReadOnly(True)
        self.json_view = CacheJsonViewer()
        self.rbxm_view = RbxmPreviewWidget()
        self.texturepack_view = TexturePackView()
        self.font_view = None
        self._font_page = None
        self._current_payload: bytes | None = None
        self._current_ident = ""
        self.image = ImageView()
        image_page = QWidget()
        il = QVBoxLayout(image_page)
        il.setContentsMargins(0, 0, 0, 0)
        il.addWidget(self.image, 1)
        irow = QHBoxLayout()
        self._chan_group = QButtonGroup(self)
        for ch in preview.CHANNELS:
            rb = QRadioButton(ch)
            rb.setChecked(ch == "RGBA")
            rb.toggled.connect(lambda on, c=ch: on and self.image.set_mode(c))
            self._chan_group.addButton(rb)
            irow.addWidget(rb)
        self.checker_box = QCheckBox("Checkerboard")
        self.checker_box.setChecked(True)
        self.checker_box.toggled.connect(self.image.set_checker)
        irow.addStretch(1)
        irow.addWidget(self.checker_box)
        il.addLayout(irow)
        self.mesh = MeshView()
        mesh_page = QWidget()
        ml = QVBoxLayout(mesh_page)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.addWidget(self.mesh, 1)
        row = QHBoxLayout()
        self.wire = QCheckBox("Wireframe")
        self.wire.toggled.connect(self.mesh.set_wire)
        self.mesh_note = QLabel()
        self.export_obj_btn = QPushButton("Export OBJ…")
        self.export_obj_btn.clicked.connect(self._export_mesh_obj)
        self.export_obj_btn.setEnabled(fleasion_mesh_processing is not None)
        row.addWidget(self.wire)
        row.addWidget(self.mesh_note, 1)
        row.addWidget(self.export_obj_btn)
        ml.addLayout(row)
        audio_page = QWidget()
        al = QVBoxLayout(audio_page)
        self.audio_label = QLabel()
        self.play_btn = QPushButton("Play")
        self.stop_btn = QPushButton("Stop")
        self.play_btn.clicked.connect(self._play)
        self.stop_btn.clicked.connect(self._stop)
        al.addWidget(self.audio_label)
        ar = QHBoxLayout()
        ar.addWidget(self.play_btn)
        ar.addWidget(self.stop_btn)
        ar.addStretch(1)
        al.addLayout(ar)
        al.addStretch(1)
        self.animation = AnimationRigView()
        for w in (self.info, image_page, mesh_page, audio_page, self.animation, self.json_view, self.rbxm_view, self.texturepack_view):
            self.stack.addWidget(w)
        lay.addWidget(self.stack, 1)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(130)
        lay.addWidget(self.detail)

    # ---------------------------------------------------------------- audio
    def _ensure_player(self):
        if self._player is None and HAVE_AUDIO:
            self._player = QMediaPlayer(self)
            self._out = QAudioOutput(self)
            self._player.setAudioOutput(self._out)
        return self._player

    def _play(self):
        if self._audio_file and self._ensure_player():
            self._player.setSource(QUrl.fromLocalFile(str(self._audio_file)))
            self._player.play()

    def _stop(self):
        if self._player:
            self._player.stop()

    def _export_mesh_obj(self):
        if not self._current_payload:
            return
        if fleasion_mesh_processing is None:
            QMessageBox.warning(self, "OBJ export unavailable", "The Fleasion mesh converter could not be loaded.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export mesh as OBJ", "mesh.obj", "Wavefront OBJ (*.obj)")
        if not path:
            return
        if not path.lower().endswith(".obj"):
            path += ".obj"
        try:
            content = fleasion_mesh_processing.convert(self._current_payload, path)
        except Exception as err:
            QMessageBox.warning(self, "OBJ export failed", str(err))
            return
        if not content:
            QMessageBox.warning(self, "OBJ export failed", "The supplied Fleasion converter could not decode this mesh. Check whether DracoPy is installed for v6/v7 meshes.")
            return
        self.detail.appendPlainText(f"\nOBJ exported to: {path}\nOBJ size: {len(content):,} characters")
        QMessageBox.information(self, "OBJ export", f"Exported mesh to:\n{path}")

    # ---------------------------------------------------------------- show
    def clear(self, text="Click an entry to preview it."):
        self._stop()
        self.animation.set_animation(None)
        self._current_payload = None
        self._current_ident = ""
        self.title.setText(text)
        self.detail.setPlainText("")
        self.info.setPlainText("")
        self.stack.setCurrentIndex(self.PAGE_INFO)

    def show_file(self, path: str | Path, display_name: str | None = None, origin: str = ""):
        """Preview an external file without modifying the Roblox cache."""
        self._stop()
        file_path = Path(path)
        self.detail.setPlainText("")
        try:
            size = file_path.stat().st_size
            if size <= 0:
                raise OSError("The file is empty.")
            if size > 100_000_000:
                raise OSError("Files larger than 100 MB cannot be previewed.")
            payload = file_path.read_bytes()
        except OSError as err:
            self.title.setText(display_name or file_path.name)
            self.info.setPlainText(f"Could not read external file.\n{err}")
            self.stack.setCurrentIndex(self.PAGE_INFO)
            return
        name = display_name or file_path.name
        kind = core.kind_of(payload)
        self.title.setText(f"{name}\nExternal asset | {kind} | {len(payload):,} bytes")
        details = [f"Source: {origin or 'Local file'}", f"Path: {file_path}",
                   f"Size: {len(payload):,} bytes", f"First bytes: {payload[:24].hex(' ')}"]
        self.detail.setPlainText("\n".join(details))
        ident = hashlib.sha256(payload).hexdigest()
        try:
            self._show_payload(ident, payload)
        except preview.PreviewError as err:
            self.info.setPlainText(f"Could not preview this external file.\n{err}")
            self.stack.setCurrentIndex(self.PAGE_INFO)
        except Exception as err:  # malformed user-provided files must not crash the app
            self.info.setPlainText(f"External preview failed: {err}")
            self.stack.setCurrentIndex(self.PAGE_INFO)

    def show_entry(self, ident: str | None):
        self._stop()
        if not ident:
            self.clear()
            return
        store = self._store()
        name = store.name_of(ident)
        e = store.describe(ident)
        head = (name + "  " if name else "") + f"[{ident}]"
        if e:
            head += f"\n{e.kind}  |  {e.size:,} bytes" + (f"  |  asset {e.asset_id}" if e.asset_id else "")
        self.title.setText(head)
        self.detail.setPlainText("")
        try:
            url, payload = store.read_payload(ident)
        except core.CacheError as err:
            self.info.setPlainText(str(err))
            self.stack.setCurrentIndex(self.PAGE_INFO)
            return
        note = "" if (e is None or e.header) else "No RBXH header: showing the raw file.\n"
        self.detail.setPlainText(f"{note}URL: {url}\nPayload: {len(payload):,} bytes\nFirst bytes: {payload[:24].hex(' ')}")
        try:
            self._show_payload(ident, payload)
        except preview.PreviewError as err:
            self.info.setPlainText(f"Could not preview this entry.\n{err}")
            self.stack.setCurrentIndex(self.PAGE_INFO)
        except Exception as err:  # a bad file must never crash the app
            self.info.setPlainText(f"Preview failed: {err}")
            self.stack.setCurrentIndex(self.PAGE_INFO)

    def _show_payload(self, ident, payload: bytes):
        self._current_payload = bytes(payload)
        self._current_ident = str(ident)
        kind = core.kind_of(payload)
        img = preview.image_kind(payload)
        if img:
            qi = QImage()
            if img == "ktx":
                rgba = preview.decode_ktx1_uncompressed(payload)
                if rgba is None:
                    self.info.setPlainText(
                        preview.ktx_info(payload)
                        + "\n\nThis KTX compression/layout is not decoded yet. Uncompressed 2D RGB/RGBA KTX1 is supported."
                    )
                    self.stack.setCurrentWidget(self.info)
                    return
                h, w = rgba.shape[:2]
                qi = QImage(rgba.data, w, h, w * 4, QImage.Format_RGBA8888).copy()
            elif not qi.loadFromData(payload):
                # Qt image plugins do not exist for every format on every platform.
                try:
                    from PIL import Image as PILImage
                    image = PILImage.open(io.BytesIO(payload)).convert("RGBA")
                    rgba = np.asarray(image, dtype=np.uint8).copy()
                    h, w = rgba.shape[:2]
                    qi = QImage(rgba.data, w, h, w * 4, QImage.Format_RGBA8888).copy()
                except Exception as err:
                    raise preview.PreviewError(f"{img.upper()} image could not be decoded: {err}") from err
            if qi.isNull():
                raise preview.PreviewError(f"{img.upper()} image could not be decoded")
            self.image.set_image(qi)
            self.detail.appendPlainText(f"Image/texture ({img.upper()}): {qi.width()} x {qi.height()}"
                                        + ("  (has transparency)" if qi.hasAlphaChannel() else ""))
            self.stack.setCurrentIndex(self.PAGE_IMAGE)
        elif kind == "mesh":
            mesh = preview.parse_mesh(payload)
            self.mesh.set_mesh(mesh)
            self.mesh_note.setText(f"mesh v{mesh.version}: {len(mesh.verts):,} vertices, "
                                   f"{len(mesh.faces):,} triangles {mesh.note}  (drag to rotate, wheel to zoom)")
            self.stack.setCurrentIndex(self.PAGE_MESH)
        elif kind == "font":
            self._stop()
            if self._font_page is not None:
                self.stack.removeWidget(self._font_page)
                self._font_page.deleteLater()
            self.font_view = FontViewerWidget(payload, self)
            self._font_page = self.font_view
            self.stack.addWidget(self._font_page)
            self.stack.setCurrentWidget(self._font_page)
            self.detail.appendPlainText("Font preview: TrueType / OpenType family information and sample text.")
        elif kind in ("ogg", "mp3"):
            if not HAVE_AUDIO:
                raise preview.PreviewError("this build has no audio support")
            f = core.app_dir() / "preview"
            f.mkdir(exist_ok=True)
            self._audio_file = f / f"{ident}.{kind}"
            self._audio_file.write_bytes(payload)
            self.audio_label.setText(f"{kind.upper()} sound, {len(payload):,} bytes")
            self.stack.setCurrentIndex(self.PAGE_AUDIO)
        elif kind not in ("anim", "curve-anim") and is_rbx_model_data(payload):
            try:
                self.rbxm_view.load_bytes(payload, asset_label=str(ident))
            except Exception as err:
                self.info.setPlainText("Fleasion RBXM preview could not parse this model.\n" + str(err))
                self.stack.setCurrentWidget(self.info)
            else:
                self.detail.appendPlainText("Structured RBXM/RBXMX preview: browse instances and properties.")
                self.stack.setCurrentWidget(self.rbxm_view)
        elif kind in ("anim", "curve-anim", "rbxm"):
            info = preview.parse_rbxm_animation(payload)
            if info.kind in ("CurveAnimation", "KeyframeSequence"):
                try:
                    if info.kind == "CurveAnimation":
                        animation = animation_data.parse_curve_animation(payload)
                    else:
                        animation = animation_data.parse_keyframe_sequence(payload)
                except animation_data.AnimationFormatError as err:
                    lines = [line for line in info.lines() if "3D playback is not included" not in line]
                    lines += ["", "3D preview unavailable:", str(err)]
                    self.info.setPlainText("\n".join(lines))
                    self.stack.setCurrentIndex(self.PAGE_INFO)
                else:
                    self.animation.set_animation(animation)
                    self.detail.appendPlainText(
                        f"\nAnimation: {animation.name} | {animation.duration:.3f} s | "
                        f"{len(animation.joint_paths)} rig folder(s)"
                    )
                    if animation.notes:
                        self.detail.appendPlainText("Notes: " + " ".join(animation.notes))
                    self.stack.setCurrentIndex(self.PAGE_ANIM)
            else:
                self.info.setPlainText("\n".join(info.lines()))
                self.stack.setCurrentIndex(self.PAGE_INFO)
        elif kind == "json":
            try:
                obj = json.loads(payload.decode("utf-8-sig", "replace"))
            except (ValueError, UnicodeError) as err:
                self.info.setPlainText("JSON preview parse error: " + str(err) + "\n\n" + payload[:20000].decode("utf-8", "replace"))
                self.stack.setCurrentWidget(self.info)
            else:
                self.json_view.load_json(obj)
                self.stack.setCurrentWidget(self.json_view)
                self.detail.appendPlainText("Structured JSON tree preview with search and expand/collapse.")
        elif kind == "xml":
            # Prefer a structured RBXMX tree, then a TexturePack map list, then raw XML.
            if is_rbx_model_data(payload):
                try:
                    self.rbxm_view.load_bytes(payload, asset_label=str(ident))
                except Exception as err:
                    self.info.setPlainText("Fleasion RBXMX preview could not parse this model.\n" + str(err))
                    self.stack.setCurrentWidget(self.info)
                else:
                    self.stack.setCurrentWidget(self.rbxm_view)
            else:
                maps = preview.parse_texturepack(payload)
                if maps:
                    self.texturepack_view.set_maps(maps)
                    self.stack.setCurrentWidget(self.texturepack_view)
                    self.detail.appendPlainText(f"TexturePack contains {len(maps)} referenced texture map(s).")
                else:
                    self.info.setPlainText(payload[:20000].decode("utf-8", "replace"))
                    self.stack.setCurrentIndex(self.PAGE_INFO)
        else:
            self.info.setPlainText("No preview for this type. The first bytes are shown below.")
            self.stack.setCurrentIndex(self.PAGE_INFO)
