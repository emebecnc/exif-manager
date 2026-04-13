"""Date editing dialog for video files — mirrors date_editor.py patterns."""
import calendar
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QObject, QThread, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QSpinBox, QCheckBox, QPushButton,
    QRadioButton, QButtonGroup, QGroupBox,
    QTableWidget, QTableWidgetItem, QDialogButtonBox,
    QApplication, QProgressDialog, QAbstractItemView,
    QHeaderView, QScrollArea, QFrame, QWidget,
)

from core.video_handler import (
    get_video_metadata, get_best_date, write_video_date,
    scan_video_folder, make_dated_filename,
    backup_video_metadata, format_duration, is_invalid_date,
)
from core.exif_handler import parse_date_from_filename
from core.backup_manager import append_historial
from ui.log_viewer import LogManager
from ui.styles import apply_button_style, apply_primary_button_style, mb_warning, mb_info, mb_question

# ── Constants ─────────────────────────────────────────────────────────────────

_MODE_KEEP   = 0
_MODE_CHANGE = 1

_OPT_PRESERVE = 0
_OPT_CUSTOM   = 1

_RENAME_DATE_ONLY = 0
_RENAME_DATE_PLUS = 1
_RENAME_KEEP_NAME = 2

_COL_FILE    = 0
_COL_DUR     = 1
_COL_CURRENT = 2
_COL_NEW     = 3
_COL_RENAME  = 4

_COLOR_NO_CHANGE = QColor(130, 130, 130)


# ── Background apply worker ───────────────────────────────────────────────────

class _ApplyWorker(QObject):
    """Background worker: writes date metadata + optional rename to videos."""
    progress = pyqtSignal(int, int, str)     # current, total, filename
    finished = pyqtSignal(int, int, list)    # ok, failed, errors

    def __init__(
        self,
        paths: List[Path],
        keep_mode: bool,
        chk_year: bool, chk_month: bool, chk_day: bool,
        year: int, month: int, day: int,
        use_custom_time: bool,
        hour: int, minute: int, second: int,
        rename: bool,
        rename_fmt: int,
        log_manager: LogManager,
    ):
        super().__init__()
        self._paths          = paths
        self._keep_mode      = keep_mode
        self._chk_year       = chk_year
        self._chk_month      = chk_month
        self._chk_day        = chk_day
        self._year           = year
        self._month          = month
        self._day            = day
        self._use_custom_time = use_custom_time
        self._hour           = hour
        self._minute         = minute
        self._second         = second
        self._rename         = rename
        self._rename_fmt     = rename_fmt
        self._log            = log_manager
        self.applied_renames: Dict[Path, Path] = {}

    def _resolve_dt(self, existing: Optional[datetime]) -> Optional[datetime]:
        """Compute the target datetime for one file."""
        if self._keep_mode:
            return existing

        year  = self._year  if self._chk_year  else (existing.year  if existing else datetime.now().year)
        month = self._month if self._chk_month else (existing.month if existing else datetime.now().month)
        day   = self._day   if self._chk_day   else (existing.day   if existing else datetime.now().day)
        if self._use_custom_time:
            h, m, s = self._hour, self._minute, self._second
        else:
            h = existing.hour   if existing else 12
            m = existing.minute if existing else 0
            s = existing.second if existing else 0
        day = min(day, calendar.monthrange(year, month)[1])
        try:
            return datetime(year, month, day, h, m, s)
        except ValueError:
            return None

    def run(self) -> None:
        ok = failed = 0
        errors: List[str] = []
        used: set = set()
        total = len(self._paths)

        for i, path in enumerate(self._paths):
            self.progress.emit(i + 1, total, path.name)
            try:
                meta     = get_video_metadata(path)
                existing = get_best_date(meta)
                new_dt   = self._resolve_dt(existing)

                if new_dt is None:
                    failed += 1
                    errors.append(f"{path.name}: no se pudo determinar la fecha")
                    continue

                old_str = existing.isoformat() if existing else ""
                if not self._keep_mode:
                    success = write_video_date(path, new_dt)
                    if not success:
                        # Format not supported (e.g. .3gp) or ffmpeg error —
                        # skip this file gracefully rather than crashing.
                        failed += 1
                        errors.append(
                            f"{path.name}: formato no soportado para "
                            f"edición de fecha ({path.suffix})"
                        )
                        continue
                    self._log.log(
                        str(path.parent), path.name, "write_exif",
                        old_str, new_dt.isoformat(),
                    )

                # Optional rename
                applied_new_name: Optional[str] = None
                if self._rename and self._rename_fmt != _RENAME_KEEP_NAME:
                    stem = path.stem if self._rename_fmt == _RENAME_DATE_PLUS else None
                    applied_new_name = make_dated_filename(
                        new_dt, path.parent, path.suffix, used, original_stem=stem
                    )
                    used.add(applied_new_name)
                    new_path = path.parent / applied_new_name
                    path.rename(new_path)
                    self._log.log(
                        str(path.parent), path.name, "rename", path.name, applied_new_name
                    )
                    self.applied_renames[path] = new_path

                # Historial entry — mirrors date_editor.py pattern.
                # Operation is "fecha_editada" when the date was changed,
                # "renombrado" when keep_mode (only a rename happened).
                try:
                    operation    = "renombrado" if self._keep_mode else "fecha_editada"
                    exif_before  = {"DateTimeOriginal": old_str}
                    exif_after_v: Optional[dict] = None
                    if not self._keep_mode and new_dt is not None:
                        exif_after_v = {
                            "DateTimeOriginal": new_dt.strftime("%Y:%m:%d %H:%M:%S")
                        }
                    append_historial(
                        path.parent, path.name, operation,
                        exif_before, exif_after_v, applied_new_name,
                    )
                except Exception:
                    pass

                ok += 1
            except Exception as e:
                failed += 1
                errors.append(f"{path.name}: {e}")

        self.finished.emit(ok, failed, errors)


# ── Dialog ────────────────────────────────────────────────────────────────────

class VideoDateEditorDialog(QDialog):
    """
    Modal dialog to edit video metadata dates.

    Modes
    -----
    'folder'    — all videos in target (a Path to a directory)
    'single'    — target is a Path to a single video file
    'selection' — explicit list via the paths= keyword argument
    """

    def __init__(
        self,
        mode: str,
        target: Path,
        log_manager: LogManager,
        parent=None,
        paths: Optional[List[Path]] = None,
        prefill_from_filename: bool = False,
    ):
        super().__init__(parent)
        self.setWindowTitle("Editar fecha de video")
        self._mode   = mode
        self._target = target
        self._log    = log_manager
        self.applied_renames: Dict[Path, Path] = {}
        self._worker: Optional[_ApplyWorker] = None
        self._thread: Optional[QThread]      = None

        # Collect the paths to operate on
        if mode == "folder":
            self._paths = scan_video_folder(target)
        elif mode == "single":
            self._paths = [target]
        else:
            self._paths = list(paths or [])

        self.setMinimumWidth(700)
        self.setMinimumHeight(600)
        screen = QApplication.primaryScreen().availableGeometry()
        self.setMaximumHeight(int(screen.height() * 0.90))

        self._build_ui(prefill_from_filename)
        self._populate_table()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self, prefill: bool) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner  = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(8)

        now = datetime.now()
        prefill_dt: Optional[datetime] = None
        if prefill and self._paths:
            prefill_dt = parse_date_from_filename(self._paths[0].stem)

        # ── Action radio ────────────────────────────────────────────────────
        grp_mode = QGroupBox("Acción")
        ml = QVBoxLayout(grp_mode)
        self._radio_keep   = QRadioButton("Conservar fecha de metadata")
        self._radio_change = QRadioButton("Cambiar fecha")
        self._radio_change.setChecked(True)
        self._bg_mode = QButtonGroup(self)
        self._bg_mode.addButton(self._radio_keep,   _MODE_KEEP)
        self._bg_mode.addButton(self._radio_change, _MODE_CHANGE)
        ml.addWidget(self._radio_keep)
        ml.addWidget(self._radio_change)
        layout.addWidget(grp_mode)

        # ── Date group ──────────────────────────────────────────────────────
        self._grp_date = QGroupBox("Fecha nueva")
        date_row = QHBoxLayout(self._grp_date)
        date_row.setSpacing(12)

        self._chk_year = QCheckBox("Año:")
        self._chk_year.setChecked(True)
        self._spin_year = QSpinBox()
        self._spin_year.setRange(1970, 2099)
        self._spin_year.setValue(prefill_dt.year if prefill_dt else now.year)
        self._spin_year.setFixedWidth(70)
        date_row.addWidget(self._chk_year)
        date_row.addWidget(self._spin_year)

        self._chk_month = QCheckBox("Mes:")
        self._spin_month = QSpinBox()
        self._spin_month.setRange(1, 12)
        self._spin_month.setValue(prefill_dt.month if prefill_dt else now.month)
        self._spin_month.setFixedWidth(55)
        date_row.addWidget(self._chk_month)
        date_row.addWidget(self._spin_month)

        self._chk_day = QCheckBox("Día:")
        self._spin_day = QSpinBox()
        self._spin_day.setRange(1, 31)
        self._spin_day.setValue(prefill_dt.day if prefill_dt else now.day)
        self._spin_day.setFixedWidth(55)
        date_row.addWidget(self._chk_day)
        date_row.addWidget(self._spin_day)
        date_row.addStretch()
        layout.addWidget(self._grp_date)

        # ── Time group — compact single-row layout ───────────────────────────
        grp_time = QGroupBox("Hora")
        time_row = QHBoxLayout(grp_time)
        time_row.setSpacing(6)
        self._radio_preserve_time = QRadioButton("Conservar original")
        self._radio_custom_time   = QRadioButton("Personalizada:")
        self._radio_preserve_time.setChecked(True)
        self._bg_time = QButtonGroup(self)
        self._bg_time.addButton(self._radio_preserve_time, _OPT_PRESERVE)
        self._bg_time.addButton(self._radio_custom_time,   _OPT_CUSTOM)

        self._spin_hour   = QSpinBox(); self._spin_hour.setRange(0, 23);   self._spin_hour.setFixedWidth(50)
        self._spin_minute = QSpinBox(); self._spin_minute.setRange(0, 59); self._spin_minute.setFixedWidth(50)
        self._spin_second = QSpinBox(); self._spin_second.setRange(0, 59); self._spin_second.setFixedWidth(50)
        if prefill_dt:
            self._spin_hour.setValue(prefill_dt.hour)
            self._spin_minute.setValue(prefill_dt.minute)
            self._spin_second.setValue(prefill_dt.second)

        time_row.addWidget(self._radio_preserve_time)
        time_row.addWidget(self._radio_custom_time)
        time_row.addWidget(self._spin_hour)
        time_row.addWidget(QLabel("h"))
        time_row.addWidget(self._spin_minute)
        time_row.addWidget(QLabel("m"))
        time_row.addWidget(self._spin_second)
        time_row.addWidget(QLabel("s"))
        time_row.addStretch()
        layout.addWidget(grp_time)

        # ── Rename group ────────────────────────────────────────────────────
        grp_rename = QGroupBox("Renombrar archivos")
        rl = QVBoxLayout(grp_rename)
        self._chk_rename      = QCheckBox("Renombrar archivos con la fecha")
        self._radio_date_only = QRadioButton("Solo fecha  (2007-09-29-02h47m07s.mp4)")
        self._radio_date_plus = QRadioButton("Fecha + nombre original  (…_nombre.mp4)")
        self._radio_keep_name = QRadioButton("Conservar nombre original")
        self._radio_date_only.setChecked(True)
        self._bg_rename = QButtonGroup(self)
        self._bg_rename.addButton(self._radio_date_only, _RENAME_DATE_ONLY)
        self._bg_rename.addButton(self._radio_date_plus, _RENAME_DATE_PLUS)
        self._bg_rename.addButton(self._radio_keep_name, _RENAME_KEEP_NAME)
        for w in (self._chk_rename, self._radio_date_only,
                  self._radio_date_plus, self._radio_keep_name):
            rl.addWidget(w)
        layout.addWidget(grp_rename)

        # ── Filename-date prefill ───────────────────────────────────────────
        btn_fn = QPushButton("📋 Leer fecha del nombre de archivo")
        btn_fn.setToolTip(
            "Pre-rellena los controles con la fecha detectada en el nombre del archivo."
        )
        btn_fn.clicked.connect(self._prefill_from_filename)
        apply_button_style(btn_fn)
        layout.addWidget(btn_fn)

        # ── Preview table ───────────────────────────────────────────────────
        grp_prev = QGroupBox("Vista previa")
        pl = QVBoxLayout(grp_prev)
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Archivo", "Duración", "Fecha actual", "Fecha nueva", "Nombre nuevo"]
        )
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setMinimumHeight(250)
        self._table.setMaximumHeight(400)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        pl.addWidget(self._table)

        btn_preview = QPushButton("Actualizar vista previa")
        btn_preview.setToolTip("Recalcula los valores de la tabla para verificar los cambios.")
        btn_preview.clicked.connect(self._populate_table)
        apply_button_style(btn_preview)
        pl.addWidget(btn_preview)
        layout.addWidget(grp_prev)

        layout.addStretch()
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        # ── Pinned button box ───────────────────────────────────────────────
        self._btn_box    = QDialogButtonBox()
        self._btn_apply  = QPushButton("Aplicar")
        self._btn_cancel = QPushButton("Cancelar")
        apply_primary_button_style(self._btn_apply)
        apply_button_style(self._btn_cancel)
        self._btn_box.addButton(self._btn_apply,  QDialogButtonBox.ButtonRole.AcceptRole)
        self._btn_box.addButton(self._btn_cancel, QDialogButtonBox.ButtonRole.RejectRole)
        self._btn_box.accepted.connect(self._on_apply)
        self._btn_box.rejected.connect(self.reject)
        outer.addWidget(self._btn_box)

        # Wire state
        self._radio_keep.toggled.connect(self._update_state)
        self._radio_custom_time.toggled.connect(self._update_state)
        self._chk_rename.toggled.connect(self._update_state)
        self._chk_year.toggled.connect(lambda _: self._spin_year.setEnabled(
            self._radio_change.isChecked() and self._chk_year.isChecked()
        ))
        self._chk_month.toggled.connect(lambda _: self._spin_month.setEnabled(
            self._radio_change.isChecked() and self._chk_month.isChecked()
        ))
        self._chk_day.toggled.connect(lambda _: self._spin_day.setEnabled(
            self._radio_change.isChecked() and self._chk_day.isChecked()
        ))
        self._update_state()

    def _update_state(self) -> None:
        keep     = self._radio_keep.isChecked()
        custom   = self._radio_custom_time.isChecked()
        renaming = self._chk_rename.isChecked()

        # Conservar → disable the whole date group AND uncheck all checkboxes
        # so it's visually clear nothing will be written.
        # Cambiar → enable the group; auto-check all three if all were off.
        self._grp_date.setEnabled(not keep)
        if keep:
            self._chk_year.setChecked(False)
            self._chk_month.setChecked(False)
            self._chk_day.setChecked(False)
        else:
            if not (self._chk_year.isChecked() or self._chk_month.isChecked()
                    or self._chk_day.isChecked()):
                self._chk_year.setChecked(True)
                self._chk_month.setChecked(True)
                self._chk_day.setChecked(True)

        self._spin_year.setEnabled(not keep and self._chk_year.isChecked())
        self._spin_month.setEnabled(not keep and self._chk_month.isChecked())
        self._spin_day.setEnabled(not keep and self._chk_day.isChecked())
        for w in (self._radio_preserve_time, self._radio_custom_time):
            w.setEnabled(not keep)
        for w in (self._spin_hour, self._spin_minute, self._spin_second):
            w.setEnabled(not keep and custom)
        for w in (self._radio_date_only, self._radio_date_plus, self._radio_keep_name):
            w.setEnabled(renaming)

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _prefill_from_filename(self) -> None:
        if not self._paths:
            return
        dt = parse_date_from_filename(self._paths[0].stem)
        if dt is None:
            mb_warning(self, "Sin fecha",
                       "No se detectó fecha en el nombre del archivo.")
            return
        self._radio_change.setChecked(True)
        self._chk_year.setChecked(True);  self._spin_year.setValue(dt.year)
        self._chk_month.setChecked(True); self._spin_month.setValue(dt.month)
        self._chk_day.setChecked(True);   self._spin_day.setValue(dt.day)
        self._radio_custom_time.setChecked(True)
        self._spin_hour.setValue(dt.hour)
        self._spin_minute.setValue(dt.minute)
        self._spin_second.setValue(dt.second)
        self._update_state()
        self._populate_table()

    def _populate_table(self) -> None:
        self._table.setRowCount(0)
        keep         = self._radio_keep.isChecked()
        use_custom   = self._radio_custom_time.isChecked()
        renaming     = self._chk_rename.isChecked()
        rename_fmt   = self._bg_rename.checkedId()
        used: set    = set()
        now          = datetime.now()

        for path in self._paths:
            meta       = get_video_metadata(path)
            current_dt = get_best_date(meta)
            current_str = (
                current_dt.strftime("%Y:%m:%d %H:%M:%S") if current_dt else "Sin fecha"
            )
            dur_str    = format_duration(meta.get("duration_seconds", 0))

            if keep:
                new_str = current_str
                rename_text = ""
            else:
                existing = current_dt
                year  = self._spin_year.value()  if self._chk_year.isChecked()  else (existing.year  if existing else now.year)
                month = self._spin_month.value() if self._chk_month.isChecked() else (existing.month if existing else 1)
                day   = self._spin_day.value()   if self._chk_day.isChecked()   else (existing.day   if existing else 1)
                h = self._spin_hour.value()   if use_custom else (existing.hour   if existing else 12)
                m = self._spin_minute.value() if use_custom else (existing.minute if existing else 0)
                s = self._spin_second.value() if use_custom else (existing.second if existing else 0)
                day = min(day, calendar.monthrange(year, month)[1])
                try:
                    new_dt      = datetime(year, month, day, h, m, s)
                    new_str     = new_dt.strftime("%Y:%m:%d %H:%M:%S")
                    if renaming and rename_fmt != _RENAME_KEEP_NAME:
                        stem = path.stem if rename_fmt == _RENAME_DATE_PLUS else None
                        rename_text = make_dated_filename(
                            new_dt, path.parent, path.suffix, used, stem
                        )
                        used.add(rename_text)
                    else:
                        rename_text = "— (sin cambio)" if renaming else ""
                except ValueError:
                    new_str = "Fecha inválida"
                    rename_text = ""

            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, _COL_FILE,    QTableWidgetItem(path.name))
            self._table.setItem(row, _COL_DUR,     QTableWidgetItem(dur_str))
            self._table.setItem(row, _COL_CURRENT, QTableWidgetItem(current_str))
            new_item = QTableWidgetItem(new_str)
            if new_str == current_str:
                new_item.setForeground(QBrush(_COLOR_NO_CHANGE))
            self._table.setItem(row, _COL_NEW,    new_item)
            self._table.setItem(row, _COL_RENAME, QTableWidgetItem(rename_text))

    def _on_apply(self) -> None:
        if not self._paths:
            self.accept()
            return

        # ── Pre-apply backup ───────────────────────────────────────────────────
        # Back up metadata for every file BEFORE any changes are written.
        # Only needed in Cambiar mode (keep_mode = rename-only, no date changes).
        if not self._radio_keep.isChecked():
            backup_failures: list[str] = []
            for path in self._paths:
                try:
                    meta = get_video_metadata(path)
                    backup_video_metadata(path.parent, path.name, meta)
                except Exception as e:
                    backup_failures.append(f"{path.name}: {e}")
            if backup_failures:
                reply = mb_question(
                    self, "Error en backup",
                    "No se pudo crear backup para "
                    f"{len(backup_failures)} archivo(s):\n\n"
                    + "\n".join(backup_failures[:5])
                    + ("\n…" if len(backup_failures) > 5 else "")
                    + "\n\n¿Continuar de todas formas?",
                )
                from PyQt6.QtWidgets import QMessageBox
                if reply != QMessageBox.StandardButton.Yes:
                    return

        progress = QProgressDialog(
            "Procesando videos…", "Cancelar", 0, len(self._paths), self
        )
        progress.setWindowTitle("Editando fechas de video")
        progress.setMinimumWidth(420)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        self._worker = _ApplyWorker(
            paths            = self._paths,
            keep_mode        = self._radio_keep.isChecked(),
            chk_year         = self._chk_year.isChecked(),
            chk_month        = self._chk_month.isChecked(),
            chk_day          = self._chk_day.isChecked(),
            year             = self._spin_year.value(),
            month            = self._spin_month.value(),
            day              = self._spin_day.value(),
            use_custom_time  = self._radio_custom_time.isChecked(),
            hour             = self._spin_hour.value(),
            minute           = self._spin_minute.value(),
            second           = self._spin_second.value(),
            rename           = self._chk_rename.isChecked(),
            rename_fmt       = self._bg_rename.checkedId(),
            log_manager      = self._log,
        )
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(
            lambda cur, tot, name: (
                progress.setValue(cur),
                progress.setLabelText(f"Procesando {cur}/{tot}:\n{name}"),
            )
        )
        self._worker.finished.connect(self._on_apply_finished)
        self._thread.start()

        progress.exec()
        if progress.wasCanceled() and self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait()

    def _on_apply_finished(self, ok: int, failed: int, errors: list) -> None:
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait()
        if self._worker:
            self.applied_renames = self._worker.applied_renames
        self._worker = None
        self._thread = None

        if errors:
            mb_warning(
                self, "Completado con errores",
                f"Procesados: {ok}   Errores: {failed}\n\n" +
                "\n".join(errors[:10]),
            )
        else:
            mb_info(self, "Completado", f"Se procesaron {ok} video(s) correctamente.")
        self.accept()
