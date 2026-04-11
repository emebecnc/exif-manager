"""Right panel: full metadata view + image preview + edit button."""
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, QObject, QThread, QSize
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QGroupBox, QFormLayout, QSizePolicy,
    QFrame, QMessageBox, QWidget,
    QRadioButton, QButtonGroup, QDialogButtonBox,
)

from core.exif_handler import (
    get_all_metadata, load_preview, read_exif, parse_exif_dt, make_dated_filename,
)
from ui.log_viewer import LogManager
from ui.styles import apply_button_style

# Rename format IDs — mirrors date_editor constants (kept local to avoid circular imports)
_RENAME_DATE_ONLY = 0   # Solo fecha         → 2011-12-24-15h40m46s.jpg
_RENAME_DATE_PLUS = 1   # Fecha + stem       → 2011-12-24-15h40m46s_IMG_2045.jpg
_RENAME_KEEP_NAME = 2   # No rename at all


class _RenameFormatDialog(QDialog):
    """Small dialog that asks the user which rename format to apply."""

    def __init__(self, path: Path, dt: datetime, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Renombrar con fecha EXIF")
        self._path = path
        self._dt   = dt
        self._fmt_group = QButtonGroup(self)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(QLabel(f"<b>Archivo actual:</b>  {self._path.name}"))

        # Pre-compute candidate names so the user can see the result
        name_date_only = make_dated_filename(
            self._dt, self._path.parent, self._path.suffix
        )
        name_date_plus = make_dated_filename(
            self._dt, self._path.parent, self._path.suffix,
            original_stem=self._path.stem
        )

        grp = QGroupBox("Formato del nombre nuevo")
        grp_layout = QVBoxLayout(grp)
        grp_layout.setSpacing(4)

        options = [
            (
                _RENAME_DATE_ONLY,
                "Solo fecha",
                name_date_only,
                "Renombra el archivo solo con la fecha:\n"
                f"  {name_date_only}",
            ),
            (
                _RENAME_DATE_PLUS,
                "Fecha + nombre original",
                name_date_plus,
                "Combina la fecha con el nombre original del archivo:\n"
                f"  {name_date_plus}",
            ),
            (
                _RENAME_KEEP_NAME,
                "Conservar nombre original",
                self._path.name,
                "No renombra el archivo.\n"
                "Útil si solo querés actualizar el EXIF sin cambiar el nombre.",
            ),
        ]

        for rid, label, preview, tooltip in options:
            radio = QRadioButton(f"{label}  →  {preview}")
            radio.setToolTip(tooltip)
            if rid == _RENAME_DATE_ONLY:
                radio.setChecked(True)
            self._fmt_group.addButton(radio, rid)
            grp_layout.addWidget(radio)

        layout.addWidget(grp)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def selected_format(self) -> int:
        return self._fmt_group.checkedId()


class _PreviewWorker(QObject):
    # Emits raw bytes (not QPixmap) — QPixmap must be created on the main thread
    image_ready = pyqtSignal(object)  # bytes or None

    def __init__(self, path: Path, max_w: int, max_h: int):
        super().__init__()
        self._path = path
        self._max_w = max_w
        self._max_h = max_h

    def run(self) -> None:
        data = load_preview(self._path, self._max_w, self._max_h)
        self.image_ready.emit(data)  # bytes or None — never QPixmap off main thread


class PhotoDetailPanel(QWidget):
    edit_photo_date = pyqtSignal(Path)
    photo_renamed   = pyqtSignal(Path, Path)  # old_path, new_path

    def __init__(self, log_manager: LogManager, parent=None):
        super().__init__(parent)
        self._log = log_manager
        self._current_path: Optional[Path] = None
        self._original_pixmap: Optional[QPixmap] = None
        self._preview_thread: Optional[QThread] = None
        self._preview_worker: Optional[_PreviewWorker] = None
        self._build_ui()

    def _build_ui(self) -> None:
        self.setMinimumWidth(320)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        # Preview image label
        self._preview = QLabel("Sin imagen")
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setMinimumHeight(220)
        self._preview.setMaximumHeight(320)
        self._preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._preview.setStyleSheet("background: #1e1e23; border: 1px solid #44444e;")
        layout.addWidget(self._preview)

        # Scrollable metadata area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        self._meta_layout = QVBoxLayout(content)
        self._meta_layout.setContentsMargins(0, 0, 0, 0)
        self._meta_layout.setSpacing(8)

        # EXIF section
        self._grp_exif = QGroupBox("EXIF")
        self._form_exif = QFormLayout(self._grp_exif)
        self._form_exif.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._meta_layout.addWidget(self._grp_exif)

        # File section
        self._grp_file = QGroupBox("Archivo")
        self._form_file = QFormLayout(self._grp_file)
        self._form_file.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._meta_layout.addWidget(self._grp_file)

        self._meta_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        # Action buttons row
        btn_row = QHBoxLayout()

        self._btn_edit = QPushButton("Editar fecha de esta foto")
        self._btn_edit.setEnabled(False)
        self._btn_edit.setToolTip(
            "Abre el editor de fecha para modificar el EXIF de esta foto individualmente.\n"
            "El estado anterior se guarda para poder deshacer con Ctrl+Z."
        )
        self._btn_edit.clicked.connect(self._on_edit)
        apply_button_style(self._btn_edit)
        btn_row.addWidget(self._btn_edit)

        self._btn_rename = QPushButton("Renombrar con fecha EXIF")
        self._btn_rename.setEnabled(False)
        self._btn_rename.setToolTip(
            "Renombra el archivo usando la fecha EXIF actual en formato\n"
            "2011-12-24-15h40m46s.jpg. Si ya existe un archivo con ese nombre,\n"
            "agrega _1, _2, etc. para evitar colisiones."
        )
        self._btn_rename.clicked.connect(self._on_rename)
        apply_button_style(self._btn_rename)
        btn_row.addWidget(self._btn_rename)

        layout.addLayout(btn_row)

    # ── Public API ─────────────────────────────────────────────────────────

    def load_photo(self, path: Path) -> None:
        self._current_path = path
        self._btn_edit.setEnabled(True)
        self._btn_rename.setEnabled(True)
        self._load_metadata(path)
        self._load_preview_async(path)

    def clear(self) -> None:
        """Reset panel to empty state (e.g. after selected photo is deleted)."""
        self._stop_preview_worker()
        self._current_path = None
        self._original_pixmap = None
        self._preview.setText("Sin imagen")
        self._grp_exif.setTitle("EXIF")
        self._grp_file.setTitle("Archivo")
        self._clear_form(self._form_exif)
        self._clear_form(self._form_file)
        self._btn_edit.setEnabled(False)
        self._btn_rename.setEnabled(False)

    def show_selection(self, pairs: list) -> None:
        """Display a summary for multiple selected photos.

        ``pairs`` is a list of ``(Path, date_str)`` tuples where ``date_str``
        is the cached EXIF date (already read by the thumbnail worker — no
        additional disk access required here).
        """
        self._stop_preview_worker()
        self._current_path = None
        self._original_pixmap = None
        self._btn_edit.setEnabled(False)
        self._btn_rename.setEnabled(False)

        n = len(pairs)
        self._preview.clear()
        self._preview.setText(f"{n} fotos\nseleccionadas")

        self._grp_exif.setTitle(f"Selección  ({n} fotos)")
        self._grp_file.setTitle("Archivos")
        self._clear_form(self._form_exif)
        self._clear_form(self._form_file)

        if not pairs:
            return

        # ── Total size (stat only — fast) ──────────────────────────────────
        total_bytes = 0
        for path, _ in pairs:
            try:
                total_bytes += path.stat().st_size
            except OSError:
                pass
        if total_bytes >= 1_048_576:
            size_str = f"{total_bytes / 1_048_576:.1f} MB"
        elif total_bytes > 0:
            size_str = f"{total_bytes / 1024:.0f} KB"
        else:
            size_str = "N/D"

        # ── Date range from cached item dates (no disk reads) ──────────────
        dates = sorted(d for _, d in pairs if d)
        if dates:
            lo = dates[0][:10].replace(":", "/")
            hi = dates[-1][:10].replace(":", "/")
            date_str = lo if lo == hi else f"{lo}  →  {hi}"
        else:
            date_str = "Sin fecha EXIF"

        self._add_row(self._form_exif, "Rango de fechas", date_str)
        self._add_row(self._form_exif, "Tamaño total", size_str)

        # ── File list (capped to keep the panel from growing huge) ─────────
        MAX_SHOWN = 40
        for path, _ in pairs[:MAX_SHOWN]:
            name_lbl = QLabel(path.name)
            name_lbl.setWordWrap(True)
            name_lbl.setStyleSheet("font-size: 9pt;")
            self._form_file.addRow(name_lbl)
        if n > MAX_SHOWN:
            self._add_row(self._form_file, "", f"… y {n - MAX_SHOWN} más")

    # ── Internal ───────────────────────────────────────────────────────────

    def _load_metadata(self, path: Path) -> None:
        meta = get_all_metadata(path)

        # Reset group titles (may have been changed by show_selection)
        self._grp_exif.setTitle("EXIF")
        self._grp_file.setTitle("Archivo")

        # Clear forms
        self._clear_form(self._form_exif)
        self._clear_form(self._form_file)

        # EXIF fields
        exif = meta.get("exif", {})
        fields = exif.get("fields", {})
        display = exif.get("display", {})
        gps = exif.get("gps")

        for label, key in [
            ("Fecha original", "DateTimeOriginal"),
            ("Fecha digitalizada", "DateTimeDigitized"),
            ("Fecha sistema", "DateTime"),
        ]:
            val = fields.get(key, "—")
            self._add_row(self._form_exif, label, val)

        for label, val in display.items():
            self._add_row(self._form_exif, label, val or "—")

        if gps:
            self._add_row(self._form_exif, "GPS", gps)

        err = exif.get("error")
        if err == "no_exif":
            self._add_row(self._form_exif, "Estado", "Sin EXIF")
        elif not exif.get("writable", True):
            self._add_row(self._form_exif, "Escritura", "No soportada")

        # File fields
        file_info = meta.get("file", {})
        for label, key in [
            ("Nombre", "nombre"),
            ("Tamaño", "tamaño"),
            ("Dimensiones", "dimensiones"),
            ("Modificado", "modificado"),
            ("Creado", "creado"),
            ("MD5", "md5"),
        ]:
            val = file_info.get(key, "—")
            self._add_row(self._form_file, label, val, selectable=(key == "md5"))

        # Full path (wider)
        path_label = QLabel(str(path))
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._form_file.addRow("Ruta:", path_label)

    def _load_preview_async(self, path: Path) -> None:
        # Stop any previous worker cleanly before starting a new one
        self._stop_preview_worker()

        self._original_pixmap = None
        self._preview.setText("Cargando…")

        self._preview_worker = _PreviewWorker(path, 760, 300)
        self._preview_thread = QThread()
        self._preview_worker.moveToThread(self._preview_thread)
        self._preview_thread.started.connect(self._preview_worker.run)
        # image_ready is connected only to the main-thread slot; cleanup is
        # handled in _on_image_ready to keep thread/worker alive until done.
        self._preview_worker.image_ready.connect(self._on_image_ready)
        self._preview_thread.start()

    def _stop_preview_worker(self) -> None:
        """Cleanly stop any running preview thread before starting a new one."""
        thread = self._preview_thread
        worker = self._preview_worker
        self._preview_thread = None
        self._preview_worker = None

        if thread is None:
            return
        try:
            if thread.isRunning():
                thread.quit()
                thread.wait()
        except RuntimeError:
            # C++ object already deleted — nothing to do
            return
        if worker is not None:
            worker.deleteLater()
        thread.deleteLater()

    def _on_image_ready(self, data) -> None:
        # Running on the main thread (queued connection) — safe to create QPixmap here
        # First clean up the thread/worker that just finished
        thread = self._preview_thread
        worker = self._preview_worker
        self._preview_thread = None
        self._preview_worker = None

        if thread is not None:
            thread.quit()
            thread.wait()
            if worker is not None:
                worker.deleteLater()
            thread.deleteLater()

        if data:
            pix = QPixmap()
            pix.loadFromData(data)
            if not pix.isNull():
                self._original_pixmap = pix
                self._update_preview_size()
                return
        self._preview.setText("No se pudo cargar la imagen")

    def _update_preview_size(self) -> None:
        if not self._original_pixmap:
            return
        w = self._preview.width() or 400
        h = self._preview.height() or 280
        scaled = self._original_pixmap.scaled(
            w, h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._preview.setPixmap(scaled)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_preview_size()

    def _on_rename(self) -> None:
        path = self._current_path
        if not path:
            return

        exif = read_exif(path)
        dt_str = (
            exif["fields"].get("DateTimeOriginal")
            or exif["fields"].get("DateTime")
        )
        dt = parse_exif_dt(dt_str) if dt_str else None
        if not dt:
            QMessageBox.warning(
                self, "Sin fecha EXIF",
                "La foto no tiene una fecha EXIF válida para generar el nombre."
            )
            return

        # Ask the user which rename format to use
        fmt_dlg = _RenameFormatDialog(path, dt, parent=self)
        if fmt_dlg.exec() != QDialog.DialogCode.Accepted:
            return

        fmt = fmt_dlg.selected_format()
        if fmt == _RENAME_KEEP_NAME:
            return  # "Conservar nombre original" — nothing to do

        # Build the target filename based on the chosen format
        stem = path.stem if fmt == _RENAME_DATE_PLUS else None
        new_name = make_dated_filename(dt, path.parent, path.suffix, original_stem=stem)
        new_path = path.parent / new_name

        # Reuse the already-read EXIF (no second disk access)
        original_exif = exif["fields"]
        try:
            path.rename(new_path)
        except OSError as e:
            QMessageBox.warning(self, "Error al renombrar", str(e))
            return

        self._log.log(str(path.parent), path.name, "rename", path.name, new_name)
        from core.backup_manager import rename_backup_entry, append_historial
        append_historial(path.parent, path.name, new_name, original_exif, "renombrado")
        rename_backup_entry(path.parent, path.name, new_name)
        self._current_path = new_path
        self.photo_renamed.emit(path, new_path)

    def _on_edit(self) -> None:
        if self._current_path:
            self.edit_photo_date.emit(self._current_path)

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _clear_form(form: QFormLayout) -> None:
        while form.rowCount():
            form.removeRow(0)

    @staticmethod
    def _add_row(
        form: QFormLayout,
        label: str,
        value: str,
        selectable: bool = False,
    ) -> None:
        lbl = QLabel(value)
        lbl.setWordWrap(True)
        if selectable:
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(f"{label}:", lbl)
