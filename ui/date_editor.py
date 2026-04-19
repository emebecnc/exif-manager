"""Date editing dialog: folder, single-photo, or explicit-selection mode."""
import calendar
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QObject, QThread, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QSpinBox, QCheckBox, QPushButton, QGroupBox,
    QRadioButton, QButtonGroup,
    QTableWidget, QTableWidgetItem, QDialogButtonBox,
    QApplication, QProgressDialog, QMessageBox, QAbstractItemView,
    QHeaderView, QScrollArea, QFrame, QWidget,
)

from core.exif_handler import (
    read_exif, write_exif_date, parse_exif_dt,
    make_dated_filename, get_best_date_str, parse_date_from_filename,
)
from core.file_scanner import scan_folder
from core.backup_manager import create_backup, rename_backup_entry, append_historial
from ui.log_viewer import LogManager
from ui.styles import apply_button_style, apply_primary_button_style, mb_warning, mb_info, mb_question

_FIELD_NAMES = ["DateTimeOriginal", "DateTimeDigitized", "DateTime"]

# Outer "what to do" radio IDs
_MODE_KEEP      = 0   # conservar fecha EXIF
_MODE_CHANGE    = 1   # cambiar fecha EXIF
_MODE_USE_CTIME = 2   # use filesystem creation date (st_ctime / st_mtime fallback)
_MODE_USE_FNAME = 3   # use date parsed from each file's own filename

# Inner time radio IDs
_OPT_PRESERVE = 0  # keep original time per file
_OPT_CUSTOM   = 1  # use custom time

# Rename format radio IDs
_RENAME_DATE_ONLY = 0  # "Solo fecha"              → 2011-12-24-15h40m46s.jpg
_RENAME_DATE_PLUS = 1  # "Fecha + nombre original" → 2011-12-24-15h40m46s_IMG_2045.jpg
_RENAME_KEEP_NAME = 2  # "Conservar nombre original" → no rename at all

# Preview table column indices
_COL_FILE    = 0
_COL_CURRENT = 1
_COL_NEW     = 2
_COL_RENAME  = 3

# Grey used for "no change" cells
_COLOR_NO_CHANGE = QColor(130, 130, 130)


def _get_file_creation_dt(path: Path) -> Optional[datetime]:
    """Return the filesystem creation datetime for *path*.

    Uses ``st_ctime`` (creation time on Windows, metadata-change time on POSIX).
    Falls back to ``st_mtime`` when ``st_ctime`` appears invalid — timestamps
    before 1980-01-01 are treated as unreliable (common on FAT-formatted NAS
    drives and some POSIX systems where ctime is not the creation date).
    """
    try:
        st = path.stat()
        ts = st.st_ctime
        if ts < 315_532_800:   # 1980-01-01 00:00:00 UTC
            ts = st.st_mtime
        return datetime.fromtimestamp(ts)
    except OSError:
        return None


class _PreviewWorker(QObject):
    """Background worker for preview generation (used for 50+ photo folders)."""
    progress = pyqtSignal(int, int)    # current, total
    result   = pyqtSignal(list)        # list of (fname, current_str, new_str, rename_text, rename_gray)

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
        use_ctime: bool = False,
        use_fname: bool = False,
    ):
        super().__init__()
        self._paths = paths
        self._keep_mode = keep_mode
        self._use_ctime = use_ctime
        self._use_fname = use_fname
        self._chk_year = chk_year
        self._chk_month = chk_month
        self._chk_day = chk_day
        self._year = year
        self._month = month
        self._day = day
        self._use_custom_time = use_custom_time
        self._hour = hour
        self._minute = minute
        self._second = second
        self._rename = rename
        self._rename_fmt = rename_fmt
        self.stop_requested = False

    def _resolve_dt(self, path: Path) -> Optional[datetime]:
        # ── "Usar fecha del nombre" mode ──────────────────────────────────
        if self._use_fname:
            return parse_date_from_filename(path.stem)
        # ── "Usar fecha creación" mode ────────────────────────────────────
        if self._use_ctime:
            return _get_file_creation_dt(path)

        if self._keep_mode:
            exif = read_exif(path)
            return parse_exif_dt(get_best_date_str(exif["fields"]))

        need_existing = (
            not self._chk_year or not self._chk_month or not self._chk_day
            or not self._use_custom_time
        )
        existing = None
        if need_existing:
            exif = read_exif(path)
            existing = parse_exif_dt(get_best_date_str(exif["fields"]))

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
        print(f"[PREVIEW WORKER] Started — paths={len(self._paths)}, stop_requested={self.stop_requested}", flush=True)
        rows = []
        used: set = set()
        total = len(self._paths)
        for i, path in enumerate(self._paths):
            print(f"[PREVIEW WORKER] Item {i}/{total} ({path.name}), stop_requested={self.stop_requested}", flush=True)
            if self.stop_requested:
                print(f"[PREVIEW WORKER] STOP DETECTED — exiting at item {i} ({path.name})", flush=True)
                self.result.emit(rows)
                return
            
            self.progress.emit(i + 1, total)
            exif = read_exif(path)
            current = get_best_date_str(exif["fields"]) or "Sin fecha"
            new_dt = self._resolve_dt(path)
            if new_dt is None:
                if self._keep_mode:
                    new_str = "Sin fecha EXIF"
                elif self._use_fname:
                    new_str = "Sin fecha en nombre"
                else:
                    new_str = "Fecha inválida"
                rename_text = "— (conservar nombre)"
                rename_gray = True
            else:
                new_str = new_dt.strftime("%Y:%m:%d %H:%M:%S")
                if self._rename:
                    if self._rename_fmt == _RENAME_KEEP_NAME:
                        rename_text = "— (sin cambio)"
                        rename_gray = True
                    elif self._rename_fmt == _RENAME_DATE_PLUS:
                        rename_text = make_dated_filename(
                            new_dt, path.parent, path.suffix, used,
                            original_stem=path.stem,
                            exclude=path.name,
                        )
                        used.add(rename_text)
                        rename_gray = False
                    else:
                        rename_text = make_dated_filename(
                            new_dt, path.parent, path.suffix, used,
                            exclude=path.name,
                        )
                        used.add(rename_text)
                        rename_gray = False
                else:
                    # No rename — show placeholder so the column is never blank
                    rename_text = "— (conservar nombre)"
                    rename_gray = True
            rows.append((path.name, current, new_str, rename_text, rename_gray))
        print(f"[PREVIEW WORKER] Finished normally — {len(rows)} rows emitted", flush=True)
        self.result.emit(rows)


class _ApplyWorker(QObject):
    progress = pyqtSignal(int, int, str, str)   # step, total_steps, filename, phase_label
    # finished: ok, failed, errors, renames_dict {old_path_str → new_path_str}
    finished = pyqtSignal(int, int, list, object)

    def __init__(
        self,
        paths: List[Path],
        # Raw date parameters — resolved per-file inside the thread (zero main-thread I/O)
        keep_mode: bool,
        chk_year: bool, chk_month: bool, chk_day: bool,
        year: int, month: int, day: int,
        use_custom_time: bool,
        hour: int, minute: int, second: int,
        fields: List[str],
        rename: bool,
        rename_fmt: int,
        write_exif: bool = True,        # False → rename-only (Conservar mode)
        sync_mtime: bool = True,        # update filesystem mtime/atime
        sync_creation: bool = True,     # update Windows creation date (pywin32)
        use_ctime: bool = False,        # True → read st_ctime per-file (ignores spinbox values)
        use_fname: bool = False,        # True → parse date from each file's own filename
    ):
        super().__init__()
        self._paths          = paths
        self._keep_mode      = keep_mode
        self._use_ctime      = use_ctime
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
        self._fields         = fields
        self._rename         = rename
        self._rename_fmt     = rename_fmt
        self._write_exif     = write_exif
        self._sync_mtime     = sync_mtime
        self._sync_creation  = sync_creation
        self._use_fname      = use_fname
        self._cancelled      = False
        self.stop_requested  = False

    def cancel(self) -> None:
        self._cancelled = True
        self.stop_requested = True

    def _resolve_dt(self, existing_fields: dict,
                    path: Optional[Path] = None) -> Optional[datetime]:
        """Compute the target datetime for one file.

        ``existing_fields``  — EXIF fields already read by the caller (no extra I/O).
        ``path``             — needed only in *use_ctime* mode to read st_ctime.
        """
        # ── "Usar fecha del nombre" mode — parse from filename ───────────────
        if self._use_fname:
            if path is None:
                return None
            return parse_date_from_filename(path.stem)
        # ── "Usar fecha creación" mode — ignore EXIF, read from filesystem ──
        if self._use_ctime:
            if path is None:
                return None
            return _get_file_creation_dt(path)

        if self._keep_mode:
            return parse_exif_dt(get_best_date_str(existing_fields))

        need_existing = (
            not self._chk_year or not self._chk_month or not self._chk_day
            or not self._use_custom_time
        )
        existing = parse_exif_dt(get_best_date_str(existing_fields)) if need_existing else None

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
        ok = 0
        failed = 0
        errors: List[str] = []
        # Maps old_path_str → new_path_str for files successfully renamed
        renames: Dict[str, str] = {}
        total = len(self._paths)

        # Two-phase progress: EXIF write (phase 1) then rename (phase 2).
        # has_two_phases is deterministic from settings — no per-file EXIF needed.
        will_rename    = self._rename and self._rename_fmt != _RENAME_KEEP_NAME
        has_two_phases = self._write_exif and will_rename
        total_steps    = total * 2 if has_two_phases else total
        step           = 0
        used_names: set = set()

        print(f"[WORKER] _ApplyWorker started, stop_requested={self.stop_requested}, paths={len(self._paths)}", flush=True)
        for path in self._paths:
            print(f"[WORKER] _ApplyWorker iteration ({path.name}), stop_requested={self.stop_requested}", flush=True)
            if self._cancelled or self.stop_requested:
                print(f"[WORKER] CANCEL DETECTED — stopping _ApplyWorker at {path.name}", flush=True)
                break

            # Single EXIF read per file — supplies both date resolution and historial
            try:
                original_fields = read_exif(path)["fields"]
            except Exception as exc:
                failed += 1
                errors.append(f"{path.name}: error leyendo EXIF: {exc}")
                step += 2 if has_two_phases else 1
                continue

            new_dt           = self._resolve_dt(original_fields, path)
            new_name_for_log = None

            # ── Phase 1: write EXIF (or rename-only pseudo-phase) ──────────
            exif_ok = True
            if self._write_exif:
                step += 1
                self.progress.emit(step, total_steps, path.name, "Escribiendo EXIF")
                if new_dt is None:
                    failed += 1
                    if self._use_fname:
                        errors.append(f"{path.name}: sin fecha reconocible en el nombre")
                    else:
                        errors.append(f"{path.name}: sin fecha válida para escribir")
                    exif_ok = False
                    if has_two_phases:
                        step += 1   # consume the rename step slot for this file
                else:
                    try:
                        write_exif_date(
                            path,
                            new_dt.year, new_dt.month, new_dt.day,
                            self._fields,
                            new_dt.hour, new_dt.minute, new_dt.second,
                            sync_mtime=self._sync_mtime,
                            sync_creation=self._sync_creation,
                        )
                        ok += 1
                    except Exception as exc:
                        failed += 1
                        errors.append(f"{path.name}: {exc}")
                        exif_ok = False
                        if has_two_phases:
                            step += 1   # consume the rename step slot for this file
            else:
                # rename-only mode (Conservar): no EXIF write
                step += 1
                self.progress.emit(step, total_steps, path.name, "Renombrando")
                ok += 1

            # ── Phase 2: rename ─────────────────────────────────────────────
            if exif_ok and will_rename and new_dt is not None:
                stem     = path.stem if self._rename_fmt == _RENAME_DATE_PLUS else None
                new_name = make_dated_filename(
                    new_dt, path.parent, path.suffix, used_names,
                    original_stem=stem, exclude=path.name,
                )
                used_names.add(new_name)
                new_name_for_log = new_name
                if has_two_phases:
                    step += 1
                    self.progress.emit(step, total_steps, path.name, "Renombrando")
                try:
                    path.rename(path.parent / new_name)
                    renames[str(path)] = str(path.parent / new_name)
                except Exception as exc:
                    errors.append(f"Renombrar {path.name}: {exc}")

            # ── Historial ───────────────────────────────────────────────────
            operation = "fecha_editada" if self._write_exif else "renombrado"
            try:
                # Build exif_after from the target datetime we just wrote
                exif_after: Optional[dict] = None
                if self._write_exif and exif_ok and new_dt is not None:
                    new_val  = new_dt.strftime("%Y:%m:%d %H:%M:%S")
                    exif_after = {field: new_val for field in self._fields}
                append_historial(
                    path.parent, path.name, operation,
                    original_fields, exif_after, new_name_for_log,
                )
            except Exception:
                pass

        self.finished.emit(ok, failed, errors, renames)


class DateEditorDialog(QDialog):
    """Edit EXIF date for a single file, a whole folder, or an explicit selection."""

    changes_applied = pyqtSignal()   # emitted just before accept(); main window can connect for reload

    def __init__(
        self,
        mode: str,                              # 'single' | 'folder' | 'selection'
        target: Path,                           # file (single) or folder (folder/selection)
        log_manager: LogManager,
        parent=None,
        paths: Optional[List[Path]] = None,     # explicit list for 'selection' mode
        prefill_from_filename: bool = False,    # auto-read date from filename on open
    ):
        super().__init__(parent)
        self.setWindowIcon(QApplication.instance().windowIcon())
        self._mode = mode
        self._target = target
        self._log = log_manager
        self._explicit_paths = paths or []
        # Apply worker / thread (renamed from _worker/_thread for clarity)
        self._apply_worker: Optional[_ApplyWorker] = None
        self._apply_thread: Optional[QThread] = None
        # Preview worker / thread
        self._preview_worker: Optional[_PreviewWorker] = None
        self._preview_thread: Optional[QThread] = None
        # Progress dialogs stored on self so Python's GC can't collect them
        # while a worker thread still holds a reference via a queued signal.
        self._progress_dlg: Optional[QProgressDialog] = None
        self._preview_progress_dlg: Optional[QProgressDialog] = None
        self._preview_populated: bool = False
        # Context captured from _on_apply so slot methods can access it without closures
        self._apply_keep_mode: bool = False
        self._apply_paths: List[Path] = []
        self._apply_rename: bool = False
        self._apply_rename_fmt: int = _RENAME_DATE_ONLY
        self._apply_log_date_str: str = ""
        self._apply_total_steps: int = 0
        # Populated after apply: old_path → new_path for any renamed files
        self.applied_renames: Dict[Path, Path] = {}

        # Window title
        if mode == "selection":
            n = len(self._explicit_paths)
            self.setWindowTitle(f"Editar fecha — {n} foto{'s' if n != 1 else ''} seleccionada{'s' if n != 1 else ''}")
        else:
            self.setWindowTitle("Editar fecha — " + target.name)

        self.setMinimumWidth(700)
        self.setMinimumHeight(600)
        self._build_ui()
        screen = QApplication.primaryScreen().availableGeometry()
        self.setMaximumHeight(int(screen.height() * 0.90))
        self._prefill_date()
        if prefill_from_filename:
            self._try_apply_filename_date(show_warning=False)
        # Preview is NOT auto-generated on open — the table stays empty until the
        # user explicitly clicks "Vista previa de cambios".  This keeps the dialog
        # instant to open even for large folders.

    # ── UI ─────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Outer layout: scrollable content area + pinned button row at bottom
        outer = QVBoxLayout(self)
        outer.setSpacing(0)
        outer.setContentsMargins(0, 0, 0, 0)

        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _scroll.setFrameShape(QFrame.Shape.NoFrame)
        _content = QWidget()
        layout = QVBoxLayout(_content)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 8)
        _scroll.setWidget(_content)
        outer.addWidget(_scroll, 1)

        # ── Outer mode: Conservar vs Cambiar ───────────────────────────────
        exif_mode_grp = QGroupBox("Acción sobre la fecha EXIF")
        exif_mode_row = QHBoxLayout(exif_mode_grp)

        self._radio_mode_keep   = QRadioButton("Conservar fecha EXIF original")
        self._radio_mode_change = QRadioButton("Cambiar fecha EXIF")
        self._radio_mode_ctime  = QRadioButton("Usar fecha creación")
        self._radio_mode_fname  = QRadioButton("Usar fecha del nombre")
        self._radio_mode_keep.setChecked(True)
        self._radio_mode_keep.setToolTip(
            "No modifica la fecha EXIF de ningún archivo.\n"
            "Solo disponible para renombrar archivos usando su fecha EXIF actual."
        )
        self._radio_mode_change.setToolTip(
            "Sobreescribe la fecha EXIF de los archivos con la nueva fecha indicada."
        )
        self._radio_mode_ctime.setToolTip(
            "Lee la fecha de creación del archivo en el sistema operativo\n"
            "(st_ctime en Windows; cae en st_mtime si st_ctime es inválido)\n"
            "y la escribe como fecha EXIF.  Cada archivo usa su propia fecha.\n"
            "Útil para fotos sin fecha EXIF pero con fecha de archivo correcta."
        )
        self._radio_mode_fname.setToolTip(
            "Extrae la fecha del nombre de cada archivo por separado\n"
            "y la escribe como fecha EXIF.  Cada archivo usa su propia fecha.\n"
            "Patrones reconocidos: 2011-12-24-15h40m46s, 20111224_154046,\n"
            "2011-12-24_15-40-46, 2011-12-24 15.40.46, 2011-12-24, 20111224.\n"
            "Archivos sin fecha reconocible en el nombre no se modifican."
        )

        # Note: idToggled is connected AFTER all widgets are built (see end of _build_ui)
        # to avoid triggering _update_apply_state before _lbl_hint exists.
        self._exif_mode_group = QButtonGroup(self)
        self._exif_mode_group.addButton(self._radio_mode_keep,   _MODE_KEEP)
        self._exif_mode_group.addButton(self._radio_mode_change, _MODE_CHANGE)
        self._exif_mode_group.addButton(self._radio_mode_ctime,  _MODE_USE_CTIME)
        self._exif_mode_group.addButton(self._radio_mode_fname,  _MODE_USE_FNAME)

        exif_mode_row.addWidget(self._radio_mode_keep)
        exif_mode_row.addWidget(self._radio_mode_change)
        exif_mode_row.addWidget(self._radio_mode_ctime)
        exif_mode_row.addWidget(self._radio_mode_fname)
        exif_mode_row.addStretch()
        layout.addWidget(exif_mode_grp)

        # ── Date fields (single horizontal row) ────────────────────────────
        self._date_grp = QGroupBox("Fecha nueva")
        date_row = QHBoxLayout(self._date_grp)
        date_row.setSpacing(12)

        # Checkboxes inline with spinboxes — allow selecting which date components
        # to modify (e.g. change only the year, keep month/day from each file's EXIF).
        # _apply_exif_mode_state() and _ApplyWorker/_PreviewWorker all read isChecked().
        self._chk_year = QCheckBox("Año:")
        self._chk_year.setChecked(True)
        self._chk_year.setToolTip(
            "Marcado: reemplaza el año de cada foto con el valor del spinbox.\n"
            "Desmarcado: conserva el año original de cada foto."
        )
        self._chk_month = QCheckBox("Mes:")
        self._chk_month.setChecked(True)
        self._chk_month.setToolTip(
            "Marcado: reemplaza el mes.\n"
            "Desmarcado: conserva el mes original de cada foto."
        )
        self._chk_day = QCheckBox("Día:")
        self._chk_day.setChecked(True)
        self._chk_day.setToolTip(
            "Marcado: reemplaza el día.\n"
            "Desmarcado: conserva el día original de cada foto."
        )

        self._spin_year = QSpinBox()
        self._spin_year.setRange(1900, 2099)
        self._spin_year.setValue(datetime.now().year)
        self._spin_year.setFixedWidth(70)
        self._spin_year.setToolTip("Nuevo año (1900–2099).")
        self._chk_year.toggled.connect(self._spin_year.setEnabled)
        self._chk_year.toggled.connect(lambda _: self._on_date_component_toggled())

        self._spin_month = QSpinBox()
        self._spin_month.setRange(1, 12)
        self._spin_month.setValue(datetime.now().month)
        self._spin_month.setFixedWidth(55)
        self._spin_month.setToolTip("Nuevo mes (1–12).")
        self._chk_month.toggled.connect(self._spin_month.setEnabled)
        self._chk_month.toggled.connect(lambda _: self._on_date_component_toggled())

        self._spin_day = QSpinBox()
        self._spin_day.setRange(1, 31)
        self._spin_day.setValue(datetime.now().day)
        self._spin_day.setFixedWidth(55)
        self._spin_day.setToolTip("Nuevo día (1–31). Se recorta al último día válido del mes si es necesario.")
        self._chk_day.toggled.connect(self._spin_day.setEnabled)
        self._chk_day.toggled.connect(lambda _: self._on_date_component_toggled())

        date_row.addWidget(self._chk_year)
        date_row.addWidget(self._spin_year)
        date_row.addWidget(self._chk_month)
        date_row.addWidget(self._spin_month)
        date_row.addWidget(self._chk_day)
        date_row.addWidget(self._spin_day)
        date_row.addStretch()

        layout.addWidget(self._date_grp)

        # ── Time mode — compact single-row layout ─────────────────────────
        self._time_grp = QGroupBox("Hora")
        time_row = QHBoxLayout(self._time_grp)
        time_row.setSpacing(6)

        self._radio_preserve = QRadioButton("Conservar original")
        self._radio_preserve.setToolTip(
            "Mantiene la hora, minutos y segundos originales de cada foto.\n"
            "Solo cambia el día, mes y año."
        )
        self._radio_custom = QRadioButton("Personalizada:")
        self._radio_custom.setToolTip(
            "Aplica la misma hora a todas las fotos del lote.\n"
            "Útil cuando las fotos no tienen hora EXIF o querés unificarla."
        )
        self._radio_preserve.setChecked(True)

        self._time_btn_group = QButtonGroup(self)
        self._time_btn_group.addButton(self._radio_preserve, _OPT_PRESERVE)
        self._time_btn_group.addButton(self._radio_custom,   _OPT_CUSTOM)
        self._time_btn_group.idToggled.connect(self._on_time_option_changed)

        self._spin_hour = QSpinBox()
        self._spin_hour.setRange(0, 23)
        self._spin_hour.setFixedWidth(50)
        self._spin_hour.setToolTip("Hora del día en formato 24 h (0–23).")
        self._spin_hour.setEnabled(False)   # enabled only when Personalizada is selected

        self._spin_minute = QSpinBox()
        self._spin_minute.setRange(0, 59)
        self._spin_minute.setFixedWidth(50)
        self._spin_minute.setToolTip("Minutos (0–59).")
        self._spin_minute.setEnabled(False)

        self._spin_second = QSpinBox()
        self._spin_second.setRange(0, 59)
        self._spin_second.setFixedWidth(50)
        self._spin_second.setToolTip("Segundos (0–59).")
        self._spin_second.setEnabled(False)

        time_row.addWidget(self._radio_preserve)
        time_row.addWidget(self._radio_custom)
        time_row.addWidget(self._spin_hour)
        time_row.addWidget(QLabel("h"))
        time_row.addWidget(self._spin_minute)
        time_row.addWidget(QLabel("m"))
        time_row.addWidget(self._spin_second)
        time_row.addWidget(QLabel("s"))
        time_row.addStretch()

        layout.addWidget(self._time_grp)

        # ── Fields to update — visible groupbox between Renombrar and Vista previa ──
        # All 5 checkboxes checked by default.  The first 3 control which EXIF
        # timestamp tags are written; the last 2 control filesystem timestamps.
        _field_tooltips = {
            "DateTimeOriginal": (
                "Campo EXIF principal que usan Immich, Google Photos y la\n"
                "mayoría de los visores para mostrar la fecha de la foto.\n"
                "Recomendado: siempre activado."
            ),
            "DateTimeDigitized": (
                "Fecha en que la imagen fue digitalizada.\n"
                "Generalmente igual a DateTimeOriginal en fotos de celular."
            ),
            "DateTime": (
                "Fecha de última modificación del archivo según EXIF.\n"
                "Se actualiza automáticamente al editar la foto con muchos programas."
            ),
            "Timestamp": (
                "Actualiza la fecha de modificación del archivo en el sistema de archivos\n"
                "(mtime / atime) para que coincida con la nueva fecha EXIF."
            ),
            "Fecha creación": (
                "Actualiza la fecha de creación del archivo en Windows\n"
                "(requiere pywin32).  No tiene efecto en macOS/Linux."
            ),
        }
        self._fields_grp = QGroupBox("Campos a actualizar")
        fields_row = QHBoxLayout(self._fields_grp)
        fields_row.setSpacing(12)
        self._field_checks = {}
        for fname in (*_FIELD_NAMES, "Timestamp", "Fecha creación"):
            chk = QCheckBox(fname)
            chk.setChecked(True)
            chk.setToolTip(_field_tooltips.get(fname, ""))
            fields_row.addWidget(chk)
            self._field_checks[fname] = chk
        fields_row.addStretch()

        # ── Hint label (Conservar + no effective rename) ───────────────────
        self._lbl_hint = QLabel("Activá 'Renombrar archivos' para poder aplicar cambios.")
        self._lbl_hint.setStyleSheet("color: #e0a040; font-style: italic; padding: 2px 0;")
        self._lbl_hint.setVisible(True)   # Conservar is default
        # NOTE: _lbl_hint is intentionally NOT added to layout — hidden from UI
        # but kept alive because _update_apply_state() calls setText/setVisible on it.

        # ── Leer fecha del nombre — standalone row below Hora ─────────────
        # (video editor has this inside Renombrar groupbox; photo editor places it
        # here so it is visible without scrolling and above Vista previa button)
        _fn_row = QHBoxLayout()
        self._btn_read_filename = QPushButton("📋 Leer fecha del nombre")
        self._btn_read_filename.setToolTip(
            "Intenta extraer la fecha del nombre del archivo\n"
            "y pre-rellena los controles de fecha con el resultado.\n"
            "Patrones reconocidos: 2011-12-24-15h40m46s, 20111224_154046,\n"
            "2011-12-24_15-40-46, 2011-12-24 15.40.46, 2011-12-24, 20111224."
        )
        self._btn_read_filename.clicked.connect(
            lambda: self._try_apply_filename_date(show_warning=True)
        )
        apply_button_style(self._btn_read_filename)
        self._lbl_filename_date = QLabel("")
        self._lbl_filename_date.setStyleSheet("color: #60c060; font-style: italic;")
        self._lbl_filename_date.setVisible(False)
        _fn_row.addWidget(self._btn_read_filename)
        _fn_row.addWidget(self._lbl_filename_date)
        _fn_row.addStretch()
        layout.addLayout(_fn_row)

        # ── Renombrar archivos section (consolidated QGroupBox) ────────────
        grp_rename = QGroupBox("Renombrar archivos")
        rl = QVBoxLayout(grp_rename)
        rl.setSpacing(4)
        rl.setContentsMargins(8, 6, 8, 6)

        # Checkbox — master toggle for the rename section
        self._chk_rename = QCheckBox("Renombrar archivos con la fecha")
        self._chk_rename.setChecked(True)
        self._chk_rename.setToolTip(
            "Renombra cada archivo con su nueva fecha en formato\n"
            "2011-12-24-15h40m46s.jpg después de escribir el EXIF.\n"
            "Si ya existe un archivo con ese nombre, agrega _1, _2, etc."
        )
        self._chk_rename.toggled.connect(self._on_rename_toggled)
        rl.addWidget(self._chk_rename)

        # Format radio buttons — directly in the groupbox layout (enabled/disabled with checkbox)
        self._radio_rename_date_only = QRadioButton(
            "Solo fecha  →  2011-12-24-15h40m46s.jpg"
        )
        self._radio_rename_date_plus = QRadioButton(
            "Fecha + nombre original  →  2011-12-24-15h40m46s_nombre.jpg"
        )
        self._radio_rename_keep_name = QRadioButton(
            "Conservar nombre original"
        )

        self._radio_rename_date_only.setChecked(True)
        self._radio_rename_date_only.setToolTip(
            "Renombra el archivo solo con la fecha:\n"
            "2011-12-24-15h40m46s.jpg\n"
            "Si ya existe un archivo con ese nombre, agrega _1, _2, etc."
        )
        self._radio_rename_date_plus.setToolTip(
            "Combina la fecha con el nombre original del archivo:\n"
            "2011-12-24-15h40m46s_nombre_original.jpg"
        )
        self._radio_rename_keep_name.setToolTip(
            "No renombra el archivo aunque el checkbox esté marcado.\n"
            "Útil para corregir la fecha EXIF sin cambiar el nombre del archivo."
        )

        self._rename_fmt_group = QButtonGroup(self)
        self._rename_fmt_group.addButton(self._radio_rename_date_only, _RENAME_DATE_ONLY)
        self._rename_fmt_group.addButton(self._radio_rename_date_plus, _RENAME_DATE_PLUS)
        self._rename_fmt_group.addButton(self._radio_rename_keep_name, _RENAME_KEEP_NAME)

        rl.addWidget(self._radio_rename_date_only)
        rl.addWidget(self._radio_rename_date_plus)
        rl.addWidget(self._radio_rename_keep_name)

        layout.addWidget(grp_rename)

        # ── Fields to update (between Renombrar and Vista previa) ─────────────
        layout.addWidget(self._fields_grp)

        # ── Vista previa de cambios — BELOW Renombrar section ───────────────
        self._btn_preview = QPushButton("Vista previa de cambios")
        self._btn_preview.setToolTip(
            "Muestra cómo quedarán las fechas antes de aplicar los cambios.\n"
            "No modifica ningún archivo."
        )
        self._btn_preview.clicked.connect(self._on_preview)
        apply_button_style(self._btn_preview)
        layout.addWidget(self._btn_preview)

        # ── Preview table (4 cols; all always visible) ──────────────────────
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["Archivo", "Fecha actual", "Fecha nueva", "Nombre nuevo"]
        )
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(_COL_FILE,    QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(_COL_CURRENT, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_COL_NEW,     QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_COL_RENAME,  QHeaderView.ResizeMode.ResizeToContents)
        # _COL_RENAME is always visible — shows calculated name or "conservar nombre"
        self._table.setMinimumHeight(250)
        self._table.setMaximumHeight(400)
        layout.addWidget(self._table)

        # ── Apply / Cancel ─────────────────────────────────────────────────
        self._btn_box = QDialogButtonBox()
        self._btn_apply = QPushButton("Aplicar")
        self._btn_apply.setEnabled(False)
        self._btn_apply.setToolTip(
            "Escribe los cambios de fecha en los archivos seleccionados.\n"
            "Se crea un backup automático antes de modificar (modo carpeta)."
        )
        self._btn_apply.clicked.connect(self._on_apply)
        apply_primary_button_style(self._btn_apply)
        self._btn_cancel = QPushButton("Cancelar")
        self._btn_cancel.setToolTip("Cierra el diálogo sin realizar ningún cambio.")
        self._btn_cancel.clicked.connect(self.reject)
        apply_button_style(self._btn_cancel)
        self._btn_box.addButton(self._btn_apply, QDialogButtonBox.ButtonRole.AcceptRole)
        self._btn_box.addButton(self._btn_cancel, QDialogButtonBox.ButtonRole.RejectRole)
        # Pinned outside the scroll area so Apply/Cancel are always visible
        outer.addWidget(self._btn_box)

        # Connect mode-change signal now that all widgets exist, then apply initial state
        self._exif_mode_group.idToggled.connect(self._on_exif_mode_changed)
        # Connect rename-format change signal
        self._rename_fmt_group.idToggled.connect(self._on_rename_fmt_changed)
        self._apply_exif_mode_state()

    # ── Pre-fill helpers ──────────────────────────────────────────────────

    def _get_first_path(self) -> Optional[Path]:
        """Return the first photo path relevant to this dialog."""
        if self._mode == "single":
            return self._target
        if self._mode == "selection" and self._explicit_paths:
            return self._explicit_paths[0]
        images = scan_folder(self._target)
        return images[0] if images else None

    def _prefill_date(self) -> None:
        """Set year/month/day spinboxes from the first available photo's EXIF date."""
        first_path = self._get_first_path()
        if first_path is None:
            return

        exif = read_exif(first_path)
        dt = parse_exif_dt(get_best_date_str(exif["fields"]))
        if dt:
            self._spin_year.setValue(dt.year)
            self._spin_month.setValue(dt.month)
            self._spin_day.setValue(dt.day)
            # Also pre-fill time spinboxes in case user switches to custom time
            self._spin_hour.setValue(dt.hour)
            self._spin_minute.setValue(dt.minute)
            self._spin_second.setValue(dt.second)

    def _try_apply_filename_date(self, show_warning: bool = True) -> None:
        """Switch to 'Usar fecha del nombre' per-file mode.

        Each file's EXIF date is set from the date found in its own filename.
        The spinboxes show the first file's extracted date as a read-only display.

        Validates only the first file's name upfront (gives immediate feedback
        without scanning every file).  Files whose names contain no recognizable
        date are skipped gracefully at apply time (counted as errors).
        """
        first_path = self._get_first_path()
        if first_path is None:
            return

        # Check first file as a quick sanity guard — the mode still works even
        # if some other files don't match, but if the very first one already has
        # no date the user has probably clicked the button on the wrong folder.
        dt = parse_date_from_filename(first_path.stem)
        if dt is None:
            if show_warning:
                mb_warning(
                    self, "Sin fecha en el nombre",
                    f"No se encontró una fecha reconocible en:\n{first_path.name}\n\n"
                    "Patrones soportados: 2011-12-24-15h40m46s, 20111224_154046,\n"
                    "2011-12-24_15-40-46, 2011-12-24 15.40.46, 2011-12-24, 20111224."
                )
            return

        # Switch to per-file filename mode.  The radio change triggers
        # _on_exif_mode_changed → _apply_exif_mode_state → _prefill_fname_date
        # which fills the (disabled) spinboxes with the first file's date.
        self._radio_mode_fname.setChecked(True)

        self._lbl_filename_date.setText(
            "Fecha del nombre de cada archivo (por archivo)"
        )
        self._lbl_filename_date.setVisible(True)
        self._preview_populated = False
        self._update_apply_state()

    # ── Slots ──────────────────────────────────────────────────────────────

    def _force_stop_preview_thread(self) -> None:
        """Actively stop any running preview background thread.

        Called before starting a new preview or switching modes.  This is
        distinct from _cleanup_preview_thread() (which is connected to
        thread.finished as a post-stop callback) — here we *cause* the thread
        to stop, then disconnect the finished signal so _cleanup_preview_thread
        won't clobber the new thread references that will be assigned afterwards.
        """
        if self._preview_worker is not None:
            self._preview_worker.stop_requested = True
        if self._preview_thread is not None:
            if self._preview_thread.isRunning():
                self._preview_thread.quit()
                self._preview_thread.wait(2000)
            # Disconnect so _cleanup_preview_thread won't fire and zero-out
            # self._preview_worker/_thread after we hand them to the new thread.
            try:
                self._preview_thread.finished.disconnect(self._cleanup_preview_thread)
            except Exception:
                pass
            self._preview_thread.deleteLater()
            self._preview_thread = None
        if self._preview_worker is not None:
            self._preview_worker.deleteLater()
            self._preview_worker = None
        if self._preview_progress_dlg is not None:
            self._preview_progress_dlg.close()
            self._preview_progress_dlg = None
        self.setEnabled(True)

    def _on_exif_mode_changed(self, btn_id: int, checked: bool) -> None:
        if not checked:
            return
        # Stop any running preview before changing mode — prevents
        # "QThread: Destroyed while thread is still running" when the user
        # switches Conservar ↔ Cambiar quickly on large folders.
        self._force_stop_preview_thread()
        self._apply_exif_mode_state()
        # Preview is only generated on explicit button click — no auto-refresh here.

    def _apply_exif_mode_state(self) -> None:
        """Enable/disable groups and columns to match current mode."""
        keep      = self._radio_mode_keep.isChecked()
        use_ctime = self._radio_mode_ctime.isChecked()
        use_fname = self._radio_mode_fname.isChecked()
        # In both per-file modes the date/time spinboxes are read-only displays
        per_file  = use_ctime or use_fname

        # Date group: editable only in manual Cambiar mode
        self._date_grp.setEnabled(not keep and not per_file)
        if keep:
            # Conservar mode: uncheck all date-component checkboxes so the user
            # sees clearly that no date fields will be modified.
            self._chk_year.setChecked(False)
            self._chk_month.setChecked(False)
            self._chk_day.setChecked(False)
        elif use_ctime:
            # Ctime mode: show first file's creation date in spinboxes (read-only
            # display — the group is disabled so the user cannot edit the values).
            self._chk_year.setChecked(True)
            self._chk_month.setChecked(True)
            self._chk_day.setChecked(True)
            self._prefill_creation_date()
        elif use_fname:
            # Fname mode: show first file's name date in spinboxes (read-only display).
            self._chk_year.setChecked(True)
            self._chk_month.setChecked(True)
            self._chk_day.setChecked(True)
            self._prefill_fname_date()
        else:
            # Cambiar mode: ensure at least year is checked so there's a visible
            # target date to edit.  Only auto-check if all three are off (i.e. the
            # user just switched from Conservar) to avoid clobbering deliberate unchecks.
            if not (self._chk_year.isChecked() or self._chk_month.isChecked() or self._chk_day.isChecked()):
                self._chk_year.setChecked(True)
                self._chk_month.setChecked(True)
                self._chk_day.setChecked(True)
            # Re-apply per-checkbox enabled state (setEnabled(True) on the group
            # re-enables ALL children indiscriminately).
            self._sync_date_spinbox_state()

        # Time group: disabled in Conservar and per-file modes (date comes from
        # the file, so there is nothing to configure manually).
        self._time_grp.setEnabled(not keep and not per_file)
        if not keep and not per_file:
            # Re-apply time spinbox enabled state after the group is re-enabled.
            is_custom = self._radio_custom.isChecked()
            self._spin_hour.setEnabled(is_custom)
            self._spin_minute.setEnabled(is_custom)
            self._spin_second.setEnabled(is_custom)

        # Fields groupbox: active whenever we're writing something (not Conservar)
        self._fields_grp.setEnabled(not keep)
        self._update_apply_state()

    def _prefill_creation_date(self) -> None:
        """Pre-fill date/time spinboxes with the first file's creation date.

        Called when the user selects "Usar fecha creación" so the spinboxes
        show an informative (read-only) preview of the date that will be applied.
        """
        first_path = self._get_first_path()
        if first_path is None:
            return
        dt = _get_file_creation_dt(first_path)
        if dt is None:
            return
        self._spin_year.setValue(dt.year)
        self._spin_month.setValue(dt.month)
        self._spin_day.setValue(dt.day)
        self._spin_hour.setValue(dt.hour)
        self._spin_minute.setValue(dt.minute)
        self._spin_second.setValue(dt.second)

    def _prefill_fname_date(self) -> None:
        """Pre-fill date/time spinboxes with the first file's filename date.

        Called when the user selects "Usar fecha del nombre" so the spinboxes
        show an informative (read-only) preview of the date that will be applied
        to the first file.  Other files may have different dates in their names.
        """
        first_path = self._get_first_path()
        if first_path is None:
            return
        dt = parse_date_from_filename(first_path.stem)
        if dt is None:
            return
        self._spin_year.setValue(dt.year)
        self._spin_month.setValue(dt.month)
        self._spin_day.setValue(dt.day)
        self._spin_hour.setValue(dt.hour)
        self._spin_minute.setValue(dt.minute)
        self._spin_second.setValue(dt.second)

    def _sync_date_spinbox_state(self) -> None:
        """Sync each date spinbox's enabled state from its checkbox."""
        self._spin_year.setEnabled(self._chk_year.isChecked())
        self._spin_month.setEnabled(self._chk_month.isChecked())
        self._spin_day.setEnabled(self._chk_day.isChecked())

    def _on_date_component_toggled(self) -> None:
        """Called when any date-component checkbox (Año/Mes/Día) changes."""
        self._preview_populated = False
        self._update_apply_state()

    def _on_time_option_changed(self, btn_id: int, checked: bool) -> None:
        if btn_id == _OPT_CUSTOM:
            custom = checked
            self._spin_hour.setEnabled(custom)
            self._spin_minute.setEnabled(custom)
            self._spin_second.setEnabled(custom)

    def _on_rename_toggled(self, checked: bool) -> None:
        # Enable/disable the format radios to match the checkbox state
        # (mirrors video editor behaviour — radios stay visible, just gray when off)
        for w in (self._radio_rename_date_only,
                  self._radio_rename_date_plus,
                  self._radio_rename_keep_name):
            w.setEnabled(checked)
        # _COL_RENAME stays visible; content changes on next preview run.
        self._update_apply_state()

    def _on_rename_fmt_changed(self, btn_id: int, checked: bool) -> None:
        if not checked:
            return
        self._preview_populated = False
        self._update_apply_state()

    def _get_rename_fmt(self) -> int:
        """Return the currently selected rename format ID."""
        return self._rename_fmt_group.checkedId()

    def _update_apply_state(self) -> None:
        """Set Aplicar/Vista previa enabled/disabled and show/hide the hint label."""
        keep_mode  = self._radio_mode_keep.isChecked()
        rename_on  = self._chk_rename.isChecked()
        rename_fmt = self._get_rename_fmt()
        will_rename = rename_on and rename_fmt != _RENAME_KEEP_NAME

        use_ctime = self._radio_mode_ctime.isChecked()
        use_fname = self._radio_mode_fname.isChecked()

        # In Cambiar mode: at least one date component must be checked.
        # In per-file modes (ctime, fname) all components are always used
        # (the full date comes from the file), so the check is skipped.
        no_component = (
            not keep_mode
            and not use_ctime
            and not use_fname
            and not self._chk_year.isChecked()
            and not self._chk_month.isChecked()
            and not self._chk_day.isChecked()
        )

        if no_component:
            self._btn_apply.setEnabled(False)
            self._btn_preview.setEnabled(False)
            self._lbl_hint.setText("Seleccioná al menos un campo de fecha para modificar.")
            self._lbl_hint.setVisible(True)
        elif keep_mode and not will_rename:
            # Conservar + nothing effective to do
            self._btn_apply.setEnabled(False)
            self._btn_preview.setEnabled(True)
            self._lbl_hint.setText("Activá 'Renombrar archivos' para poder aplicar cambios.")
            self._lbl_hint.setVisible(True)
        elif self._preview_populated:
            self._btn_apply.setEnabled(True)
            self._btn_preview.setEnabled(True)
            self._lbl_hint.setVisible(False)
        else:
            self._btn_apply.setEnabled(False)
            self._btn_preview.setEnabled(True)
            self._lbl_hint.setVisible(False)

    # ── Preview helpers ───────────────────────────────────────────────────

    def _populate_preview_table(self, rows: list) -> None:
        """Populate the preview table from a list of row tuples.

        Each tuple: (fname, current_str, new_str, rename_text, rename_gray).
        """
        self._table.setRowCount(0)
        for fname, current, new_str, rename_text, rename_gray in rows:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, _COL_FILE,    QTableWidgetItem(fname))
            self._table.setItem(row, _COL_CURRENT, QTableWidgetItem(current))
            new_item = QTableWidgetItem(new_str)
            if new_str == current:          # gray when date won't change (Conservar mode)
                new_item.setForeground(QBrush(_COLOR_NO_CHANGE))
            self._table.setItem(row, _COL_NEW, new_item)
            rename_item = QTableWidgetItem(rename_text)
            if rename_gray:
                rename_item.setForeground(QBrush(_COLOR_NO_CHANGE))
            self._table.setItem(row, _COL_RENAME, rename_item)
        self._preview_populated = True
        self._table.setVisible(True)
        self._update_apply_state()
        self.adjustSize()

    _PREVIEW_THRESHOLD = 50  # use background worker above this photo count

    def _on_preview(self) -> None:
        keep_mode  = self._radio_mode_keep.isChecked()
        use_ctime  = self._radio_mode_ctime.isChecked()
        use_fname  = self._radio_mode_fname.isChecked()
        rename_fmt = self._get_rename_fmt()

        # Validate the manually-entered date only when in Cambiar mode.
        # In per-file modes (ctime, fname) the date comes from each file — no
        # spinbox values to validate.
        if not keep_mode and not use_ctime and not use_fname and not self._validate_date():
            return

        paths = self._get_target_paths()
        if not paths:
            mb_warning(self, "Sin archivos", "No hay imágenes para procesar.")
            return

        show_rename = self._chk_rename.isChecked()

        if len(paths) < self._PREVIEW_THRESHOLD:
            # ── Synchronous path (fast for small folders) ──────────────────
            rows: list = []
            used: set  = set()
            for path in paths:
                exif    = read_exif(path)
                current = get_best_date_str(exif["fields"]) or "Sin fecha"
                new_dt  = self._resolve_new_dt(path)
                if new_dt is None:
                    new_str     = "Sin fecha EXIF" if keep_mode else "Fecha inválida"
                    rename_text = "— (conservar nombre)"
                    rename_gray = True
                else:
                    new_str = new_dt.strftime("%Y:%m:%d %H:%M:%S")
                    if show_rename:
                        if rename_fmt == _RENAME_KEEP_NAME:
                            rename_text = "— (sin cambio)"
                            rename_gray = True
                        elif rename_fmt == _RENAME_DATE_PLUS:
                            rename_text = make_dated_filename(
                                new_dt, path.parent, path.suffix, used,
                                original_stem=path.stem,
                                exclude=path.name,
                            )
                            used.add(rename_text)
                            rename_gray = False
                        else:
                            rename_text = make_dated_filename(
                                new_dt, path.parent, path.suffix, used,
                                exclude=path.name,
                            )
                            used.add(rename_text)
                            rename_gray = False
                    else:
                        rename_text = "— (conservar nombre)"
                        rename_gray = True
                rows.append((path.name, current, new_str, rename_text, rename_gray))
            self._populate_preview_table(rows)
        else:
            # ── Background worker path (50+ photos) ────────────────────────
            # Stop any previously running preview before starting a new one.
            # This handles mode/format/component changes while a large-folder
            # preview is already in progress.
            print(f"[PREVIEW DEBUG] 1. Preview button clicked — {len(paths)} paths, entering async branch", flush=True)
            self._force_stop_preview_thread()
            # Store progress dialog on self — prevents GC while thread is live
            self._preview_progress_dlg = QProgressDialog("Preparando…", "❌ Cancelar", 0, len(paths), self)
            self._preview_progress_dlg.setWindowTitle("Generando vista previa…")
            self._preview_progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
            self._preview_progress_dlg.setMinimumDuration(0)
            print(f"[PREVIEW DEBUG] 2. Progress dialog created", flush=True)

            # CRITICAL: Connect canceled BEFORE showing
            self._preview_progress_dlg.canceled.connect(self._on_cancel_preview)
            print(f"[PREVIEW DEBUG] 3. canceled.connect(_on_cancel_preview) done", flush=True)

            use_custom = self._radio_custom.isChecked()
            self._preview_worker = _PreviewWorker(
                paths, keep_mode,
                self._chk_year.isChecked(),
                self._chk_month.isChecked(),
                self._chk_day.isChecked(),
                self._spin_year.value(),
                self._spin_month.value(),
                self._spin_day.value(),
                use_custom,
                self._spin_hour.value(),
                self._spin_minute.value(),
                self._spin_second.value(),
                show_rename,
                rename_fmt,
                use_ctime=use_ctime,
                use_fname=use_fname,
            )
            self._preview_thread = QThread()
            self._preview_worker.moveToThread(self._preview_thread)

            self._preview_thread.started.connect(self._preview_worker.run)
            self._preview_worker.progress.connect(self._on_preview_progress)
            self._preview_worker.result.connect(self._on_preview_result)
            self._preview_worker.result.connect(self._preview_thread.quit)
            self._preview_thread.finished.connect(self._cleanup_preview_thread)
            print(f"[PREVIEW DEBUG] 4. All signals connected", flush=True)

            self._preview_progress_dlg.show()
            self.setEnabled(False)
            # Re-enable the progress dialog explicitly: setEnabled(False) on a parent
            # propagates to all child widgets, which would disable the Cancel button.
            self._preview_progress_dlg.setEnabled(True)
            print(f"[PREVIEW DEBUG] 5. Dialog shown, setEnabled(False) called — dialog.isEnabled()={self._preview_progress_dlg.isEnabled()}", flush=True)
            QApplication.processEvents()   # force paint before thread starts

            self._preview_thread.start()
            print(f"[PREVIEW DEBUG] 6. Thread started", flush=True)

    # ── Preview worker slots ──────────────────────────────────────────────

    def _on_preview_progress(self, current: int, total: int) -> None:
        if self._preview_progress_dlg:
            self._preview_progress_dlg.setValue(current)
            self._preview_progress_dlg.setLabelText(
                f"Generando vista previa…\n{current} de {total} fotos"
            )

    def _on_cancel_preview(self) -> None:
        """Cancel preview generation."""
        print(f"[PREVIEW DEBUG] >>> CANCEL PREVIEW CLICKED <<<", flush=True)
        print(f"[PREVIEW DEBUG] worker={self._preview_worker is not None}, thread={self._preview_thread is not None}", flush=True)
        if self._preview_worker is not None:
            print(f"[PREVIEW DEBUG] Setting worker.stop_requested = True", flush=True)
            self._preview_worker.stop_requested = True
            print(f"[PREVIEW DEBUG] Confirmed: stop_requested={self._preview_worker.stop_requested}", flush=True)
        else:
            print(f"[PREVIEW DEBUG] ERROR: _preview_worker is None!", flush=True)
        if self._preview_thread is not None and self._preview_thread.isRunning():
            print(f"[PREVIEW DEBUG] Calling thread.quit()", flush=True)
            self._preview_thread.quit()
        else:
            print(f"[PREVIEW DEBUG] Thread not running (or None)", flush=True)
        if self._preview_progress_dlg:
            self._preview_progress_dlg.close()
            self._preview_progress_dlg = None
        self.setEnabled(True)

    def _on_preview_result(self, rows: list) -> None:
        """Receive completed rows, stop the thread, populate the table.

        Calls quit() + wait() before touching the table so the thread is fully
        stopped and self._preview_thread cannot be GC'd mid-flight.
        """
        if self._preview_thread and self._preview_thread.isRunning():
            self._preview_thread.quit()
            self._preview_thread.wait()
        if self._preview_progress_dlg:
            self._preview_progress_dlg.close()
            self._preview_progress_dlg = None
        self.setEnabled(True)
        self._populate_preview_table(rows)

    def _cleanup_preview_thread(self) -> None:
        if self._preview_worker:
            self._preview_worker.deleteLater()
            self._preview_worker = None
        if self._preview_thread:
            self._preview_thread.deleteLater()
            self._preview_thread = None

    def _on_apply(self) -> None:
        keep_mode  = self._radio_mode_keep.isChecked()
        use_ctime  = self._radio_mode_ctime.isChecked()
        use_fname  = self._radio_mode_fname.isChecked()
        rename_fmt = self._get_rename_fmt()

        # Skip manual-date validation in per-file modes — date comes from each file
        if not keep_mode and not use_ctime and not use_fname and not self._validate_date():
            return

        paths = self._get_target_paths()
        if not paths:
            return

        # Separate EXIF timestamp fields from filesystem timestamp flags.
        # _field_checks contains 5 keys: the 3 EXIF field names + "Timestamp" +
        # "Fecha creación".  Only the EXIF names are passed to write_exif_date;
        # the filesystem flags are forwarded as bool kwargs.
        _FS_FLAGS = {"Timestamp", "Fecha creación"}
        sync_mtime     = self._field_checks["Timestamp"].isChecked()
        sync_creation  = self._field_checks["Fecha creación"].isChecked()

        # In Cambiar mode: verify at least one EXIF field is selected
        fields: List[str] = []
        if not keep_mode:
            fields = [
                f for f, chk in self._field_checks.items()
                if chk.isChecked() and f not in _FS_FLAGS
            ]
            if not fields:
                mb_warning(self, "Sin campos", "Selecciona al menos un campo EXIF.")
                return

        # Backup before writing (all modes; single-file → backup parent folder)
        if not keep_mode:
            backup_folder = (
                self._target
                if self._mode in ("folder", "selection")
                else self._target.parent
            )
            try:
                # Collect current EXIF for the files about to be modified.
                # Reuse `paths` (already computed above) — avoids a second
                # scan_folder() call for folder mode.
                files_data: Dict[str, dict] = {}
                for p in paths:
                    try:
                        files_data[p.name] = read_exif(p)["fields"]
                    except Exception:
                        pass  # unreadable files are skipped; not catastrophic
                n = create_backup(backup_folder, files_data)
                self._log.log(str(backup_folder), "", "create_backup", "", f"{n} archivos")
            except Exception as e:
                reply = mb_question(
                    self, "Error en backup",
                    f"No se pudo crear backup:\n{e}\n\n¿Continuar de todas formas?",
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return

        # ── Capture apply context (zero disk I/O — only UI values read here) ──
        rename = self._chk_rename.isChecked()

        # Build a human-readable log summary (no EXIF reads needed)
        if use_ctime:
            log_date_str = "fecha-creacion"
        elif use_fname:
            log_date_str = "fecha-nombre"
        elif not keep_mode:
            y_str = str(self._spin_year.value())        if self._chk_year.isChecked()  else "*"
            m_str = f"{self._spin_month.value():02d}"   if self._chk_month.isChecked() else "*"
            d_str = f"{self._spin_day.value():02d}"     if self._chk_day.isChecked()   else "*"
            log_date_str = f"{y_str}:{m_str}:{d_str}"
        else:
            log_date_str = ""

        # Total steps computable from settings alone — no per-file EXIF reads needed
        will_rename    = rename and rename_fmt != _RENAME_KEEP_NAME
        has_two_phases = not keep_mode and will_rename

        self._apply_keep_mode    = keep_mode
        self._apply_paths        = paths
        self._apply_rename       = rename
        self._apply_rename_fmt   = rename_fmt
        self._apply_log_date_str = log_date_str
        self._apply_total_steps  = len(paths) * 2 if has_two_phases else len(paths)

        # ── Show progress dialog BEFORE thread starts ──────────────────────
        # With cancel button to allow cancellation even mid-apply
        self._progress_dlg = QProgressDialog("Iniciando…", "❌ Cancelar", 0, self._apply_total_steps, self)
        self._progress_dlg.setWindowTitle("Aplicando cambios…")
        self._progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress_dlg.setMinimumDuration(0)   # always show immediately
        self._progress_dlg.setMinimumWidth(400)
        self._progress_dlg.setAutoReset(False)
        self._progress_dlg.setAutoClose(False)
        self._progress_dlg.canceled.connect(self._on_cancel_apply)
        self._progress_dlg.show()
        QApplication.processEvents()   # force paint before thread starts

        # Disable dialog so the user cannot fire a second apply while running.
        # Re-enable the progress dialog explicitly after: setEnabled(False) on a
        # parent propagates to all child widgets and would disable the Cancel button.
        self.setEnabled(False)
        self._progress_dlg.setEnabled(True)
        self._btn_apply.setEnabled(False)

        # ── Start worker thread — ALL EXIF reads happen here, not above ────
        # _ApplyWorker computes datetime and new filenames on the fly, reading
        # each file's EXIF exactly once inside the thread.
        use_custom = self._radio_custom.isChecked()
        self._apply_worker = _ApplyWorker(
            paths,
            keep_mode,
            self._chk_year.isChecked(), self._chk_month.isChecked(), self._chk_day.isChecked(),
            self._spin_year.value(), self._spin_month.value(), self._spin_day.value(),
            use_custom,
            self._spin_hour.value(), self._spin_minute.value(), self._spin_second.value(),
            fields,
            rename,
            rename_fmt,
            write_exif=not keep_mode,
            sync_mtime=sync_mtime,
            sync_creation=sync_creation,
            use_ctime=use_ctime,
            use_fname=use_fname,
        )
        self._apply_thread = QThread()
        self._apply_worker.moveToThread(self._apply_thread)

        # Thread lifetime pattern (CLAUDE.md): do NOT also connect finished→thread.quit
        # here; _on_apply_finished calls quit()+wait() directly to avoid double-quit.
        self._apply_thread.started.connect(self._apply_worker.run)
        self._apply_worker.progress.connect(self._on_apply_progress)
        self._apply_worker.finished.connect(self._on_apply_finished)
        self._apply_thread.finished.connect(self._cleanup_apply_thread)

        self._apply_thread.start()

    # ── Apply worker slots ────────────────────────────────────────────────

    def _on_apply_progress(self, step: int, steps: int, fname: str, phase: str) -> None:
        """Update the apply progress dialog (runs in main thread via queued signal)."""
        if self._progress_dlg:
            self._progress_dlg.setValue(step)
            self._progress_dlg.setLabelText(
                f"{phase}: {fname}\n{step} de {steps} pasos"
            )

    def _on_cancel_apply(self) -> None:
        """Cancel apply operation."""
        print(f"[DIALOG] _on_cancel_apply fired — worker={self._apply_worker is not None}", flush=True)
        if self._apply_worker is not None:
            self._apply_worker.stop_requested = True
            print(f"[DIALOG] apply stop_requested is now: {self._apply_worker.stop_requested}", flush=True)
        if self._apply_thread is not None and self._apply_thread.isRunning():
            self._apply_thread.quit()
        if self._progress_dlg:
            self._progress_dlg.close()
            self._progress_dlg = None
        self.setEnabled(True)

    def _on_apply_finished(self, ok: int, failed: int, errors: list, renames_dict: object) -> None:
        """Handle apply completion: close progress, log, show result, accept dialog.

        ``renames_dict`` is a plain dict {old_path_str → new_path_str} emitted by
        the worker for files that were actually renamed successfully.

        Calls quit() + wait() on the thread BEFORE accepting the dialog so the
        QThread object is not destroyed while the OS thread is still running —
        that was the cause of the 'QThread: Destroyed while still running' crash.
        """
        # Re-enable the dialog first so message boxes are interactive
        self.setEnabled(True)

        if self._progress_dlg:
            self._progress_dlg.setValue(self._apply_total_steps)
            self._progress_dlg.close()
            self._progress_dlg = None

        # Stop the thread and wait for it to fully exit BEFORE the dialog can
        # be closed and garbage-collected (which would destroy self._apply_thread).
        if self._apply_thread and self._apply_thread.isRunning():
            self._apply_thread.quit()
            self._apply_thread.wait()

        # Log EXIF writes
        if not self._apply_keep_mode:
            for p in self._apply_paths:
                self._log.log(str(p.parent), p.name, "write_exif", "",
                              self._apply_log_date_str)

        # Log renames using the worker-reported dict (only includes successful renames)
        for old_str, new_str in (renames_dict or {}).items():
            old_path = Path(old_str)
            new_path = Path(new_str)
            self.applied_renames[old_path] = new_path
            self._log.log(str(old_path.parent), old_path.name, "rename",
                          old_path.name, new_path.name)
            # Keep backup JSON in sync with the new filename so that
            # restore_backup() can locate the file after a rename.
            rename_backup_entry(old_path.parent, old_path.name, new_path.name)

        if errors:
            mb_warning(
                self, "Aplicado con errores",
                f"Correctos: {ok}\nErrores: {failed}\n\n" + "\n".join(errors[:10])
            )
        else:
            mb_info(self, "Completado", f"Se procesaron {ok} archivos.")
        self.changes_applied.emit()
        self.accept()

    def _cleanup_apply_thread(self) -> None:
        """Schedule worker and thread for deletion once the thread has fully stopped."""
        if self._apply_worker:
            self._apply_worker.deleteLater()
            self._apply_worker = None
        if self._apply_thread:
            self._apply_thread.deleteLater()
            self._apply_thread = None

    # ── Helpers ────────────────────────────────────────────────────────────

    def _resolve_new_dt(self, path: Path) -> Optional[datetime]:
        """Compute the per-file datetime to use for EXIF writing or renaming.

        Conservar mode → each file's own current EXIF date, unchanged.
        Cambiar mode   → replace only the checked date components; preserve the
                         rest from each file's current EXIF.  If the resulting
                         day exceeds the last valid day of that month (e.g. day=31
                         in February) it is clamped automatically via
                         calendar.monthrange().
        """
        # ── "Usar fecha del nombre" mode ──────────────────────────────────────
        if self._radio_mode_fname.isChecked():
            return parse_date_from_filename(path.stem)
        # ── "Usar fecha creación" mode ────────────────────────────────────────
        if self._radio_mode_ctime.isChecked():
            return _get_file_creation_dt(path)

        if self._radio_mode_keep.isChecked():
            exif = read_exif(path)
            return parse_exif_dt(get_best_date_str(exif["fields"]))

        # Read existing EXIF when any component will be preserved from the file
        need_existing = (
            not self._chk_year.isChecked()
            or not self._chk_month.isChecked()
            or not self._chk_day.isChecked()
            or not self._radio_custom.isChecked()
        )
        existing = None
        if need_existing:
            exif = read_exif(path)
            existing = parse_exif_dt(get_best_date_str(exif["fields"]))

        year  = (self._spin_year.value()  if self._chk_year.isChecked()
                 else (existing.year  if existing else datetime.now().year))
        month = (self._spin_month.value() if self._chk_month.isChecked()
                 else (existing.month if existing else datetime.now().month))
        day   = (self._spin_day.value()   if self._chk_day.isChecked()
                 else (existing.day   if existing else datetime.now().day))

        if self._radio_custom.isChecked():
            h = self._spin_hour.value()
            m = self._spin_minute.value()
            s = self._spin_second.value()
        else:
            h = existing.hour   if existing else 12
            m = existing.minute if existing else 0
            s = existing.second if existing else 0

        # Clamp day to last valid day of the resolved month/year
        day = min(day, calendar.monthrange(year, month)[1])
        try:
            return datetime(year, month, day, h, m, s)
        except ValueError:
            return None

    def _validate_date(self) -> bool:
        """Validate the fixed date components.

        Only performs a static check when all three components are checked —
        partial dates are resolved per-file and automatically clamped so there
        is nothing to validate upfront in that case.
        """
        if not (self._chk_year.isChecked() and
                self._chk_month.isChecked() and
                self._chk_day.isChecked()):
            return True   # per-file resolution with day clamping handles validity
        y = self._spin_year.value()
        m = self._spin_month.value()
        d = self._spin_day.value()
        try:
            datetime(y, m, d)
            return True
        except ValueError as e:
            mb_warning(self, "Fecha inválida", str(e))
            return False

    def _get_target_paths(self) -> List[Path]:
        if self._mode == "single":
            return [self._target]
        if self._mode == "selection":
            return list(self._explicit_paths)
        return scan_folder(self._target)  # folder mode
