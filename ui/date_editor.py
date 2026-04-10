"""Date editing dialog: folder, single-photo, or explicit-selection mode."""
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
    QProgressDialog, QMessageBox, QAbstractItemView,
    QHeaderView,
)

from core.exif_handler import (
    read_exif, write_exif_date, parse_exif_dt,
    make_dated_filename, get_best_date_str, parse_date_from_filename,
)
from core.file_scanner import scan_folder
from core.backup_manager import create_backup, rename_backup_entry, append_historial
from ui.log_viewer import LogManager

_FIELD_NAMES = ["DateTimeOriginal", "DateTimeDigitized", "DateTime"]

# Outer "what to do" radio IDs
_MODE_KEEP   = 0   # conservar fecha EXIF
_MODE_CHANGE = 1   # cambiar fecha EXIF

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


class _ApplyWorker(QObject):
    progress = pyqtSignal(int, int, str)   # current, total, filename
    finished = pyqtSignal(int, int, list)  # ok, failed, errors

    def __init__(
        self,
        paths: List[Path],
        year: int, month: int, day: int,
        fields: List[str],
        hour: Optional[int],            # None → preserve original per-file time
        minute: Optional[int],
        second: Optional[int],
        rename: bool,
        new_names: Dict[Path, str],     # precomputed path → new filename mapping
        write_exif: bool = True,        # False → rename-only (Conservar mode)
    ):
        super().__init__()
        self._paths = paths
        self._year = year
        self._month = month
        self._day = day
        self._fields = fields
        self._hour = hour
        self._minute = minute
        self._second = second
        self._rename = rename
        self._new_names = new_names
        self._write_exif = write_exif
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        ok = 0
        failed = 0
        errors = []
        total = len(self._paths)

        for i, path in enumerate(self._paths):
            if self._cancelled:
                break
            self.progress.emit(i + 1, total, path.name)

            # Capture EXIF BEFORE any changes (historial needs the original state)
            original_exif = read_exif(path)["fields"]
            new_name_for_log = self._new_names.get(path) if self._rename else None

            if self._write_exif:
                try:
                    write_exif_date(
                        path,
                        self._year, self._month, self._day,
                        self._fields,
                        self._hour, self._minute, self._second,
                    )
                    ok += 1
                except Exception as e:
                    failed += 1
                    errors.append(f"{path.name}: {e}")
                    continue  # skip rename if EXIF write failed
            else:
                ok += 1  # in rename-only mode every file "succeeds" unless rename fails

            if self._rename and path in self._new_names:
                new_name = self._new_names[path]
                try:
                    path.rename(path.parent / new_name)
                except Exception as e:
                    errors.append(f"Renombrar {path.name}: {e}")

            # Append historial record (best-effort — never abort the main loop)
            operation = "fecha_editada" if self._write_exif else "renombrado"
            try:
                append_historial(
                    path.parent, path.name, new_name_for_log, original_exif, operation
                )
            except Exception:
                pass

        self.finished.emit(ok, failed, errors)


class DateEditorDialog(QDialog):
    """Edit EXIF date for a single file, a whole folder, or an explicit selection."""

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
        self._mode = mode
        self._target = target
        self._log = log_manager
        self._explicit_paths = paths or []
        self._worker: Optional[_ApplyWorker] = None
        self._thread: Optional[QThread] = None
        self._preview_populated: bool = False
        # Populated after apply: old_path → new_path for any renamed files
        self.applied_renames: Dict[Path, Path] = {}

        # Window title
        if mode == "selection":
            n = len(self._explicit_paths)
            self.setWindowTitle(f"Editar fecha — {n} foto{'s' if n != 1 else ''} seleccionada{'s' if n != 1 else ''}")
        else:
            self.setWindowTitle("Editar fecha — " + target.name)

        self.setMinimumWidth(700)
        self._build_ui()
        self._prefill_date()
        if prefill_from_filename:
            self._try_apply_filename_date(show_warning=False)

    # ── UI ─────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── Outer mode: Conservar vs Cambiar ───────────────────────────────
        exif_mode_grp = QGroupBox("Acción sobre la fecha EXIF")
        exif_mode_row = QHBoxLayout(exif_mode_grp)

        self._radio_mode_keep   = QRadioButton("Conservar fecha EXIF original")
        self._radio_mode_change = QRadioButton("Cambiar fecha EXIF")
        self._radio_mode_keep.setChecked(True)
        self._radio_mode_keep.setToolTip(
            "No modifica la fecha EXIF de ningún archivo.\n"
            "Solo disponible para renombrar archivos usando su fecha EXIF actual."
        )
        self._radio_mode_change.setToolTip(
            "Sobreescribe la fecha EXIF de los archivos con la nueva fecha indicada."
        )

        # Note: idToggled is connected AFTER all widgets are built (see end of _build_ui)
        # to avoid triggering _update_apply_state before _lbl_hint exists.
        self._exif_mode_group = QButtonGroup(self)
        self._exif_mode_group.addButton(self._radio_mode_keep,   _MODE_KEEP)
        self._exif_mode_group.addButton(self._radio_mode_change, _MODE_CHANGE)

        exif_mode_row.addWidget(self._radio_mode_keep)
        exif_mode_row.addWidget(self._radio_mode_change)
        exif_mode_row.addStretch()
        layout.addWidget(exif_mode_grp)

        # ── Date fields ────────────────────────────────────────────────────
        self._date_grp = QGroupBox("Fecha")
        date_row = QHBoxLayout(self._date_grp)

        date_row.addWidget(QLabel("Año:"))
        self._spin_year = QSpinBox()
        self._spin_year.setRange(1900, 2099)
        self._spin_year.setValue(datetime.now().year)
        self._spin_year.setFixedWidth(70)
        self._spin_year.setToolTip("Año de la nueva fecha (1900–2099).")
        date_row.addWidget(self._spin_year)

        date_row.addWidget(QLabel("Mes:"))
        self._spin_month = QSpinBox()
        self._spin_month.setRange(1, 12)
        self._spin_month.setValue(datetime.now().month)
        self._spin_month.setFixedWidth(55)
        self._spin_month.setToolTip("Mes de la nueva fecha (1–12).")
        date_row.addWidget(self._spin_month)

        date_row.addWidget(QLabel("Día:"))
        self._spin_day = QSpinBox()
        self._spin_day.setRange(1, 31)
        self._spin_day.setValue(datetime.now().day)
        self._spin_day.setFixedWidth(55)
        self._spin_day.setToolTip("Día de la nueva fecha (1–31).")
        date_row.addWidget(self._spin_day)
        date_row.addStretch()
        layout.addWidget(self._date_grp)

        # ── Time mode ──────────────────────────────────────────────────────
        self._time_grp = QGroupBox("Hora")
        time_outer = QVBoxLayout(self._time_grp)

        self._radio_preserve = QRadioButton("Conservar hora original de cada foto")
        self._radio_preserve.setToolTip(
            "Mantiene la hora, minutos y segundos originales de cada foto.\n"
            "Solo cambia el día, mes y año."
        )
        self._radio_custom = QRadioButton("Usar hora personalizada")
        self._radio_custom.setToolTip(
            "Aplica la misma hora a todas las fotos del lote.\n"
            "Útil cuando las fotos no tienen hora EXIF o querés unificarla."
        )
        self._radio_preserve.setChecked(True)

        self._time_btn_group = QButtonGroup(self)
        self._time_btn_group.addButton(self._radio_preserve, _OPT_PRESERVE)
        self._time_btn_group.addButton(self._radio_custom,   _OPT_CUSTOM)
        self._time_btn_group.idToggled.connect(self._on_time_option_changed)

        time_outer.addWidget(self._radio_preserve)
        time_outer.addWidget(self._radio_custom)

        # Custom time spinboxes (shown only when _OPT_CUSTOM is selected)
        self._custom_time_widget = QGroupBox()
        self._custom_time_widget.setFlat(True)
        self._custom_time_widget.setStyleSheet("QGroupBox { border: none; margin: 0; padding: 0; }")
        custom_row = QHBoxLayout(self._custom_time_widget)
        custom_row.setContentsMargins(20, 0, 0, 0)

        custom_row.addWidget(QLabel("Hora:"))
        self._spin_hour = QSpinBox()
        self._spin_hour.setRange(0, 23)
        self._spin_hour.setFixedWidth(55)
        self._spin_hour.setToolTip("Hora del día en formato 24 h (0–23).")
        custom_row.addWidget(self._spin_hour)

        custom_row.addWidget(QLabel("Minuto:"))
        self._spin_minute = QSpinBox()
        self._spin_minute.setRange(0, 59)
        self._spin_minute.setFixedWidth(55)
        self._spin_minute.setToolTip("Minutos (0–59).")
        custom_row.addWidget(self._spin_minute)

        custom_row.addWidget(QLabel("Segundo:"))
        self._spin_second = QSpinBox()
        self._spin_second.setRange(0, 59)
        self._spin_second.setFixedWidth(55)
        self._spin_second.setToolTip("Segundos (0–59).")
        custom_row.addWidget(self._spin_second)
        custom_row.addStretch()

        self._custom_time_widget.setVisible(False)
        time_outer.addWidget(self._custom_time_widget)
        layout.addWidget(self._time_grp)

        # ── Rename checkbox ────────────────────────────────────────────────
        self._chk_rename = QCheckBox("Renombrar archivos con la fecha  (ej. 2011-12-24-15h40m46s.jpg)")
        self._chk_rename.setChecked(False)
        self._chk_rename.setToolTip(
            "Renombra cada archivo con su nueva fecha en formato\n"
            "2011-12-24-15h40m46s.jpg después de escribir el EXIF.\n"
            "Si ya existe un archivo con ese nombre, agrega _1, _2, etc."
        )
        self._chk_rename.toggled.connect(self._on_rename_toggled)
        layout.addWidget(self._chk_rename)

        # ── Rename format options (indented, only visible when rename is on) ─
        self._rename_format_widget = QGroupBox()
        self._rename_format_widget.setFlat(True)
        self._rename_format_widget.setStyleSheet(
            "QGroupBox { border: none; margin: 0; padding: 0; }"
        )
        fmt_layout = QVBoxLayout(self._rename_format_widget)
        fmt_layout.setContentsMargins(20, 2, 0, 2)
        fmt_layout.setSpacing(3)

        self._radio_rename_date_only = QRadioButton(
            "Solo fecha  →  2011-12-24-15h40m46s.jpg"
        )
        self._radio_rename_date_plus = QRadioButton(
            "Fecha + nombre original  →  2011-12-24-15h40m46s_IMG_2045.jpg"
        )
        self._radio_rename_keep_name = QRadioButton(
            "Conservar nombre original  (solo cambia EXIF, sin renombrar)"
        )

        self._radio_rename_date_only.setChecked(True)
        self._radio_rename_date_only.setToolTip(
            "Renombra el archivo solo con la fecha:\n"
            "2011-12-24-15h40m46s.jpg\n"
            "Si ya existe un archivo con ese nombre, agrega _1, _2, etc."
        )
        self._radio_rename_date_plus.setToolTip(
            "Combina la fecha con el nombre original del archivo:\n"
            "2011-12-24-15h40m46s_nombre_original.jpg\n"
            "Ejemplo: 2011-12-24-15h40m46s_IMG_2045.jpg"
        )
        self._radio_rename_keep_name.setToolTip(
            "No renombra el archivo aunque el checkbox esté marcado.\n"
            "Útil para corregir la fecha EXIF sin cambiar el nombre del archivo."
        )

        self._rename_fmt_group = QButtonGroup(self)
        self._rename_fmt_group.addButton(self._radio_rename_date_only, _RENAME_DATE_ONLY)
        self._rename_fmt_group.addButton(self._radio_rename_date_plus, _RENAME_DATE_PLUS)
        self._rename_fmt_group.addButton(self._radio_rename_keep_name, _RENAME_KEEP_NAME)

        fmt_layout.addWidget(self._radio_rename_date_only)
        fmt_layout.addWidget(self._radio_rename_date_plus)
        fmt_layout.addWidget(self._radio_rename_keep_name)

        self._rename_format_widget.setVisible(False)
        layout.addWidget(self._rename_format_widget)

        # ── Filename date button (always enabled) ──────────────────────────
        fn_row = QHBoxLayout()
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
        fn_row.addWidget(self._btn_read_filename)
        self._lbl_filename_date = QLabel("")
        self._lbl_filename_date.setStyleSheet("color: #60c060; font-style: italic;")
        self._lbl_filename_date.setVisible(False)
        fn_row.addWidget(self._lbl_filename_date)
        fn_row.addStretch()
        layout.addLayout(fn_row)

        # ── EXIF fields to update ──────────────────────────────────────────
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
        }
        self._fields_grp = QGroupBox("Campos EXIF a actualizar")
        fields_row = QHBoxLayout(self._fields_grp)
        self._field_checks = {}
        for fname in _FIELD_NAMES:
            chk = QCheckBox(fname)
            chk.setChecked(True)
            chk.setToolTip(_field_tooltips.get(fname, ""))
            fields_row.addWidget(chk)
            self._field_checks[fname] = chk
        fields_row.addStretch()
        layout.addWidget(self._fields_grp)

        # ── Hint label (Conservar + no effective rename) ───────────────────
        self._lbl_hint = QLabel("Activá 'Renombrar archivos' para poder aplicar cambios.")
        self._lbl_hint.setStyleSheet("color: #e0a040; font-style: italic; padding: 2px 0;")
        self._lbl_hint.setVisible(True)   # Conservar is default
        layout.addWidget(self._lbl_hint)

        # ── Preview button ─────────────────────────────────────────────────
        self._btn_preview = QPushButton("Vista previa de cambios")
        self._btn_preview.setToolTip(
            "Muestra cómo quedarán las fechas antes de aplicar los cambios.\n"
            "No modifica ningún archivo."
        )
        self._btn_preview.clicked.connect(self._on_preview)
        layout.addWidget(self._btn_preview)

        # ── Preview table (4 cols; _COL_NEW hidden in Conservar mode) ──────
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
        self._table.setColumnHidden(_COL_NEW,    True)   # hidden in Conservar mode (default)
        self._table.setColumnHidden(_COL_RENAME, True)
        self._table.setMinimumHeight(150)
        self._table.setVisible(False)
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
        self._btn_cancel = QPushButton("Cancelar")
        self._btn_cancel.setToolTip("Cierra el diálogo sin realizar ningún cambio.")
        self._btn_cancel.clicked.connect(self.reject)
        self._btn_box.addButton(self._btn_apply, QDialogButtonBox.ButtonRole.AcceptRole)
        self._btn_box.addButton(self._btn_cancel, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(self._btn_box)

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
        """Extract a date from the first file's name and apply it to the spinboxes.

        Switches to Cambiar mode automatically.  Shows a warning when
        show_warning=True and no date pattern is found in the filename.
        """
        first_path = self._get_first_path()
        if first_path is None:
            return

        dt = parse_date_from_filename(first_path.stem)
        if dt is None:
            if show_warning:
                QMessageBox.warning(
                    self, "Sin fecha en el nombre",
                    f"No se encontró una fecha reconocible en:\n{first_path.name}\n\n"
                    "Patrones soportados: 2011-12-24-15h40m46s, 20111224_154046,\n"
                    "2011-12-24_15-40-46, 2011-12-24 15.40.46, 2011-12-24, 20111224."
                )
            return

        # Switch to Cambiar mode so the controls are enabled
        self._radio_mode_change.setChecked(True)

        # Fill date spinboxes
        self._spin_year.setValue(dt.year)
        self._spin_month.setValue(dt.month)
        self._spin_day.setValue(dt.day)

        # Fill time if the pattern included one; otherwise leave at 00:00:00
        if dt.hour or dt.minute or dt.second:
            self._radio_custom.setChecked(True)
            self._custom_time_widget.setVisible(True)
            self._spin_hour.setValue(dt.hour)
            self._spin_minute.setValue(dt.minute)
            self._spin_second.setValue(dt.second)

        self._lbl_filename_date.setText(
            f"Fecha extraída: {dt.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._lbl_filename_date.setVisible(True)

        # Preview needs to be regenerated
        self._preview_populated = False
        self._update_apply_state()

    # ── Slots ──────────────────────────────────────────────────────────────

    def _on_exif_mode_changed(self, btn_id: int, checked: bool) -> None:
        if not checked:
            return
        self._apply_exif_mode_state()
        # Re-run preview if it was already showing so it refreshes columns
        if self._table.isVisible():
            self._on_preview()

    def _apply_exif_mode_state(self) -> None:
        """Enable/disable groups and columns to match current Conservar/Cambiar mode."""
        keep = self._radio_mode_keep.isChecked()
        self._date_grp.setEnabled(not keep)
        self._time_grp.setEnabled(not keep)
        self._fields_grp.setEnabled(not keep)
        # _COL_NEW is only meaningful in Cambiar mode
        self._table.setColumnHidden(_COL_NEW, keep)
        self._update_apply_state()

    def _on_time_option_changed(self, btn_id: int, checked: bool) -> None:
        if btn_id == _OPT_CUSTOM:
            self._custom_time_widget.setVisible(checked)
            self.adjustSize()

    def _on_rename_toggled(self, checked: bool) -> None:
        self._rename_format_widget.setVisible(checked)
        self._table.setColumnHidden(_COL_RENAME, not checked)
        self._update_apply_state()
        # Re-run preview if it was already visible
        if self._table.isVisible():
            self._on_preview()

    def _on_rename_fmt_changed(self, btn_id: int, checked: bool) -> None:
        if not checked:
            return
        self._preview_populated = False
        self._update_apply_state()
        if self._table.isVisible():
            self._on_preview()

    def _get_rename_fmt(self) -> int:
        """Return the currently selected rename format ID."""
        return self._rename_fmt_group.checkedId()

    def _update_apply_state(self) -> None:
        """Set Aplicar enabled/disabled and show/hide the hint label."""
        keep_mode  = self._radio_mode_keep.isChecked()
        rename_on  = self._chk_rename.isChecked()
        rename_fmt = self._get_rename_fmt()
        # "Will rename" is True only when checkbox is on AND format is not KEEP_NAME
        will_rename = rename_on and rename_fmt != _RENAME_KEEP_NAME

        if keep_mode and not will_rename:
            # Conservar + nothing effective to do
            self._btn_apply.setEnabled(False)
            self._lbl_hint.setVisible(True)
        elif self._preview_populated:
            self._btn_apply.setEnabled(True)
            self._lbl_hint.setVisible(False)
        else:
            self._btn_apply.setEnabled(False)
            self._lbl_hint.setVisible(False)

    def _on_preview(self) -> None:
        keep_mode   = self._radio_mode_keep.isChecked()
        rename_fmt  = self._get_rename_fmt()

        # Validate date only when we intend to write it
        if not keep_mode and not self._validate_date():
            return

        paths = self._get_target_paths()
        if not paths:
            QMessageBox.warning(self, "Sin archivos", "No hay imágenes para procesar.")
            return

        show_rename = self._chk_rename.isChecked()
        self._table.setRowCount(0)
        used: set = set()

        for path in paths:
            exif = read_exif(path)
            current = get_best_date_str(exif["fields"]) or "Sin fecha"

            new_dt = self._resolve_new_dt(path)
            if new_dt is None:
                new_str      = "Sin fecha EXIF" if keep_mode else "Fecha inválida"
                rename_text  = ""
                rename_gray  = False
            else:
                new_str = new_dt.strftime("%Y:%m:%d %H:%M:%S")

                if show_rename:
                    if rename_fmt == _RENAME_KEEP_NAME:
                        rename_text = "— (sin cambio)"
                        rename_gray = True
                    elif rename_fmt == _RENAME_DATE_PLUS:
                        rename_text = make_dated_filename(
                            new_dt, path.parent, path.suffix, used,
                            original_stem=path.stem
                        )
                        used.add(rename_text)
                        rename_gray = False
                    else:  # _RENAME_DATE_ONLY
                        rename_text = make_dated_filename(
                            new_dt, path.parent, path.suffix, used
                        )
                        used.add(rename_text)
                        rename_gray = False
                else:
                    rename_text = ""
                    rename_gray = False

            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, _COL_FILE,    QTableWidgetItem(path.name))
            self._table.setItem(row, _COL_CURRENT, QTableWidgetItem(current))
            self._table.setItem(row, _COL_NEW,     QTableWidgetItem(new_str))

            rename_item = QTableWidgetItem(rename_text)
            if rename_gray:
                rename_item.setForeground(QBrush(_COLOR_NO_CHANGE))
            self._table.setItem(row, _COL_RENAME, rename_item)

        self._preview_populated = True
        self._table.setVisible(True)
        self._update_apply_state()
        self.adjustSize()

    def _on_apply(self) -> None:
        keep_mode   = self._radio_mode_keep.isChecked()
        rename_fmt  = self._get_rename_fmt()

        if not keep_mode and not self._validate_date():
            return

        paths = self._get_target_paths()
        if not paths:
            return

        # In Cambiar mode: verify at least one EXIF field is selected
        fields: List[str] = []
        if not keep_mode:
            fields = [f for f, chk in self._field_checks.items() if chk.isChecked()]
            if not fields:
                QMessageBox.warning(self, "Sin campos", "Selecciona al menos un campo EXIF.")
                return

        # Backup before writing (folder/selection mode; single-file undo by main_window)
        if not keep_mode and self._mode in ("folder", "selection"):
            try:
                create_backup(self._target)
                self._log.log(str(self._target), "", "create_backup", "", "")
            except Exception as e:
                reply = QMessageBox.question(
                    self, "Error en backup",
                    f"No se pudo crear backup:\n{e}\n\n¿Continuar de todas formas?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return

        year  = self._spin_year.value()
        month = self._spin_month.value()
        day   = self._spin_day.value()

        # Pass None for h/m/s to preserve per-file time; explicit values for custom
        if not keep_mode and self._radio_custom.isChecked():
            hour   = self._spin_hour.value()
            minute = self._spin_minute.value()
            second = self._spin_second.value()
        else:
            hour = minute = second = None

        # Precompute rename mapping — KEEP_NAME means no entries at all
        rename = self._chk_rename.isChecked()
        new_names: Dict[Path, str] = {}
        if rename and rename_fmt != _RENAME_KEEP_NAME:
            used: set = set()
            for path in paths:
                new_dt = self._resolve_new_dt(path)
                if new_dt is not None:
                    stem = path.stem if rename_fmt == _RENAME_DATE_PLUS else None
                    name = make_dated_filename(
                        new_dt, path.parent, path.suffix, used,
                        original_stem=stem
                    )
                    used.add(name)
                    new_names[path] = name

        # Progress dialog
        progress_dlg = QProgressDialog("Aplicando cambios…", "Cancelar", 0, len(paths), self)
        progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        progress_dlg.setMinimumDuration(300)

        self._worker = _ApplyWorker(
            paths, year, month, day, fields,
            hour, minute, second,
            rename, new_names,
            write_exif=not keep_mode,
        )
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)

        def on_progress(current: int, total: int, fname: str) -> None:
            progress_dlg.setValue(current)
            progress_dlg.setLabelText(f"Procesando: {fname}")
            if progress_dlg.wasCanceled():
                self._worker.cancel()

        def on_finished(ok: int, failed: int, errors: list) -> None:
            progress_dlg.setValue(len(paths))
            progress_dlg.close()

            if not keep_mode:
                new_date_str = f"{year:04d}:{month:02d}:{day:02d}"
                for p in paths:
                    self._log.log(str(p.parent), p.name, "write_exif", "", new_date_str)

            if rename and rename_fmt != _RENAME_KEEP_NAME:
                for p, new_name in new_names.items():
                    new_path = p.parent / new_name
                    if new_path.exists():
                        self.applied_renames[p] = new_path
                        self._log.log(str(p.parent), p.name, "rename", p.name, new_name)
                        # Keep backup JSON in sync with the new filename so that
                        # restore_backup() can locate the file after a rename.
                        rename_backup_entry(p.parent, p.name, new_name)

            if errors:
                QMessageBox.warning(
                    self, "Aplicado con errores",
                    f"Correctos: {ok}\nErrores: {failed}\n\n" + "\n".join(errors[:10])
                )
            else:
                QMessageBox.information(self, "Completado", f"Se procesaron {ok} archivos.")
            self.accept()

        self._worker.progress.connect(on_progress)
        self._worker.finished.connect(on_finished)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    # ── Helpers ────────────────────────────────────────────────────────────

    def _resolve_new_dt(self, path: Path) -> Optional[datetime]:
        """Compute the datetime to use for this file (renaming or EXIF writing).

        Conservar mode → each file's own current EXIF date.
        Cambiar mode   → the new date from spinboxes (+ time per-file or custom).
        """
        if self._radio_mode_keep.isChecked():
            exif = read_exif(path)
            return parse_exif_dt(get_best_date_str(exif["fields"]))

        year  = self._spin_year.value()
        month = self._spin_month.value()
        day   = self._spin_day.value()

        if self._radio_custom.isChecked():
            h = self._spin_hour.value()
            m = self._spin_minute.value()
            s = self._spin_second.value()
        else:
            exif = read_exif(path)
            existing = parse_exif_dt(get_best_date_str(exif["fields"]))
            h, m, s = (existing.hour, existing.minute, existing.second) if existing else (12, 0, 0)
        try:
            return datetime(year, month, day, h, m, s)
        except ValueError:
            return None

    def _validate_date(self) -> bool:
        y, m, d = self._spin_year.value(), self._spin_month.value(), self._spin_day.value()
        try:
            datetime(y, m, d)
            return True
        except ValueError as e:
            QMessageBox.warning(self, "Fecha inválida", str(e))
            return False

    def _get_target_paths(self) -> List[Path]:
        if self._mode == "single":
            return [self._target]
        if self._mode == "selection":
            return list(self._explicit_paths)
        return scan_folder(self._target)  # folder mode
