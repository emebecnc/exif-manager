"""Right panel: video metadata display with async thumbnail preview."""
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, QObject, QThread
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QFormLayout, QFrame, QScrollArea,
)

from core.video_handler import (
    get_video_metadata, get_best_date, get_video_thumbnail,
    format_duration, format_size, is_invalid_date,
)
from ui.log_viewer import LogManager
from ui.styles import apply_button_style, apply_primary_button_style


# ── Background thumbnail loader ───────────────────────────────────────────────

class _ThumbWorker(QObject):
    """Load a video thumbnail in the background (ffmpeg required)."""
    thumbnail_ready = pyqtSignal(object)   # bytes | None
    finished        = pyqtSignal()

    def __init__(self, path: Path):
        super().__init__()
        self._path      = path
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        if not self._cancelled:
            data = get_video_thumbnail(self._path)
            if not self._cancelled:
                self.thumbnail_ready.emit(data)
        self.finished.emit()


# ── Detail panel ─────────────────────────────────────────────────────────────

class VideoDetailPanel(QWidget):
    """Right panel showing full video metadata + first-frame preview + actions."""

    edit_video_date = pyqtSignal(Path)
    video_renamed   = pyqtSignal(Path, Path)   # old, new

    def __init__(self, log_manager: LogManager, ffmpeg_available: bool = True,
                 parent=None):
        super().__init__(parent)
        self._log              = log_manager
        self._ffmpeg_available = ffmpeg_available
        self._current_path: Optional[Path] = None
        self._worker: Optional[_ThumbWorker] = None
        self._thread: Optional[QThread]      = None
        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        # Scrollable metadata area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner  = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(4, 0, 4, 4)
        layout.setSpacing(8)

        # Thumbnail preview
        self._preview = QLabel("Sin video")
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setMinimumHeight(180)
        self._preview.setMaximumHeight(240)
        self._preview.setStyleSheet(
            "background-color: #1a1a1e; border-radius: 4px; color: #666;"
        )
        layout.addWidget(self._preview)

        # Video technical info
        self._grp_video = QGroupBox("Video")
        self._form_video = QFormLayout(self._grp_video)
        self._form_video.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self._grp_video)

        # Date info
        self._grp_fecha = QGroupBox("Fecha")
        self._form_fecha = QFormLayout(self._grp_fecha)
        self._form_fecha.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self._grp_fecha)

        # File info
        self._grp_file = QGroupBox("Archivo")
        self._form_file = QFormLayout(self._grp_file)
        self._form_file.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self._grp_file)

        layout.addStretch()
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        # Pinned action buttons
        btn_row = QHBoxLayout()
        self._btn_edit = QPushButton("Editar fecha")
        self._btn_edit.setEnabled(False)
        self._btn_edit.setToolTip("Abre el editor de fecha para este video.")
        self._btn_edit.clicked.connect(self._on_edit)
        apply_primary_button_style(self._btn_edit)
        btn_row.addWidget(self._btn_edit)
        btn_row.addStretch()
        outer.addLayout(btn_row)

    # ── Public API ────────────────────────────────────────────────────────────

    def on_folder_changed(self, folder: Path) -> None:
        """Slot connected to MainWindow.folder_changed (via VideoPanel).

        Clears the detail panel when the user navigates to a different folder
        so stale video metadata from the previous folder is not displayed.
        """
        self._current_path = None
        self.clear()

    def load_video(self, path: Path) -> None:
        self._current_path = path
        self._btn_edit.setEnabled(True)
        self._load_metadata(path)
        if self._ffmpeg_available:
            self._load_preview_async(path)
        else:
            self._preview.setText("🎬 (ffmpeg no disponible)")

    def clear(self) -> None:
        self._stop_worker()
        self._current_path = None
        self._preview.clear()
        self._preview.setText("Sin video")
        self._btn_edit.setEnabled(False)
        self._clear_form(self._form_video)
        self._clear_form(self._form_fecha)
        self._clear_form(self._form_file)
        self._grp_video.setTitle("Video")
        self._grp_fecha.setTitle("Fecha")
        self._grp_file.setTitle("Archivo")

    def show_selection(self, paths: list) -> None:
        """Display a summary for a multi-video selection."""
        self._stop_worker()
        self._current_path = None
        self._btn_edit.setEnabled(False)
        self._clear_form(self._form_video)
        self._clear_form(self._form_fecha)
        self._clear_form(self._form_file)
        n = len(paths)
        self._preview.clear()
        self._preview.setText(f"{n} videos\nseleccionados")
        self._grp_video.setTitle(f"Selección  ({n} videos)")
        self._grp_fecha.setTitle("Fecha")
        self._grp_file.setTitle("Archivos")

        total_bytes = 0
        for path in paths:
            try:
                total_bytes += path.stat().st_size
            except OSError:
                pass
        self._add_row(self._form_file, "Tamaño total:", format_size(total_bytes))
        for path in paths[:25]:
            self._add_row(self._form_file, "", path.name)

    # ── Private ───────────────────────────────────────────────────────────────

    def _load_metadata(self, path: Path) -> None:
        self._clear_form(self._form_video)
        self._clear_form(self._form_fecha)
        self._clear_form(self._form_file)

        meta = get_video_metadata(path)

        # Video section
        self._add_row(self._form_video, "Duración:",
                      format_duration(meta["duration_seconds"]))
        if meta["width"] and meta["height"]:
            self._add_row(self._form_video, "Resolución:",
                          f"{meta['width']} × {meta['height']} px")
        if meta["fps"]:
            self._add_row(self._form_video, "FPS:", str(meta["fps"]))
        if meta["codec_video"]:
            self._add_row(self._form_video, "Codec video:", meta["codec_video"])
        if meta["codec_audio"]:
            self._add_row(self._form_video, "Codec audio:", meta["codec_audio"])
        if meta["bitrate"]:
            self._add_row(self._form_video, "Bitrate:",
                          f"{meta['bitrate'] / 1_000_000:.1f} Mbps")
        if meta["rotation"]:
            self._add_row(self._form_video, "Rotación:", f"{meta['rotation']}°")
        if meta["format_name"]:
            self._add_row(self._form_video, "Formato:", meta["format_name"])
        cam = f"{meta['make']} {meta['model']}".strip()
        if cam:
            self._add_row(self._form_video, "Cámara:", cam)

        # Fecha section
        best    = get_best_date(meta)
        invalid = is_invalid_date(best)
        if meta["creation_time"]:
            ct_str = meta["creation_time"].strftime("%Y:%m:%d %H:%M:%S")
            ct_lbl = QLabel(ct_str)
            if invalid:
                ct_lbl.setStyleSheet("color: #dc5050;")
            self._form_fecha.addRow("Metadata:", ct_lbl)
        else:
            self._add_row(self._form_fecha, "Metadata:", "— (sin dato)")
        if meta["date_modified"]:
            self._add_row(self._form_fecha, "Modificado:",
                          meta["date_modified"].strftime("%d/%m/%Y %H:%M:%S"))
        if meta["date_created"]:
            self._add_row(self._form_fecha, "Creado (SO):",
                          meta["date_created"].strftime("%d/%m/%Y"))

        # File section
        self._add_row(self._form_file, "Nombre:", path.name, selectable=True)
        self._add_row(self._form_file, "Tamaño:", format_size(meta["size_bytes"]))
        self._add_row(self._form_file, "Ruta:", str(path.parent), selectable=True)

    def _load_preview_async(self, path: Path) -> None:
        self._stop_worker()
        self._preview.setText("⏳")
        self._worker = _ThumbWorker(path)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.thumbnail_ready.connect(self._on_thumbnail_ready)
        self._worker.finished.connect(self._on_worker_finished)
        self._thread.start()

    def _stop_worker(self) -> None:
        if self._worker:
            self._worker.cancel()
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait()
        self._worker = None
        self._thread = None

    def _on_thumbnail_ready(self, data) -> None:
        if not data:
            self._preview.setText("🎬 Sin miniatura")
            return
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        if pixmap.isNull():
            self._preview.setText("🎬 Sin miniatura")
            return
        w = self._preview.width() or 300
        h = self._preview.maximumHeight()
        self._preview.setPixmap(
            pixmap.scaled(w, h,
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
        )

    def _on_worker_finished(self) -> None:
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait()
        self._worker = None
        self._thread = None

    def _on_edit(self) -> None:
        if self._current_path:
            self.edit_video_date.emit(self._current_path)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Re-scale the preview image when the panel is resized
        if self._current_path and self._preview.pixmap():
            pm = self._preview.pixmap()
            if pm and not pm.isNull():
                w = self._preview.width() or 300
                h = self._preview.maximumHeight()
                self._preview.setPixmap(
                    pm.scaled(w, h,
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
                )

    @staticmethod
    def _clear_form(form: QFormLayout) -> None:
        while form.rowCount():
            form.removeRow(0)

    @staticmethod
    def _add_row(form: QFormLayout, label: str, value: str,
                 selectable: bool = False) -> None:
        lbl = QLabel(value)
        lbl.setWordWrap(True)
        if selectable:
            lbl.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
        form.addRow(label, lbl)
