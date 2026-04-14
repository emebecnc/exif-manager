"""Cleanup dialog — scan and permanently delete temp folders/files under a root."""
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QObject, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QCheckBox, QPushButton, QGroupBox,
    QTreeWidget, QTreeWidgetItem, QProgressBar,
    QMessageBox, QProgressDialog, QWidget,
    QRadioButton, QButtonGroup,
)

from ui.log_viewer import LogManager
from ui.styles import apply_button_style, mb_info, mb_question


def _fmt_bytes(n: int) -> str:
    """Format a byte count as a compact human-readable string."""
    if n < 1_024:
        return f"{n} B"
    if n < 1_048_576:
        return f"{n / 1_024:.1f} KB"
    return f"{n / 1_048_576:.1f} MB"


# ── Type registry ─────────────────────────────────────────────────────────────
# Each tuple: (key, display, description, default_checked, is_folder, note, note_color)
_TYPES: List[Tuple] = [
    (
        "_thumbcache",
        "_thumbcache",
        "Miniaturas en caché (se regeneran automáticamente)",
        True, True,
        "✓ Seguro — se regeneran automáticamente al abrir cada carpeta",
        "#60c060",
    ),
    (
        "_duplicados_eliminados",
        "_duplicados_eliminados",
        "Duplicados movidos al revisar",
        True, True,
        "⚠ Estas fotos no se pueden recuperar después de eliminar",
        "#e08030",
    ),
    (
        "_eliminados",
        "_eliminados",
        "Fotos eliminadas manualmente",
        True, True,
        "⚠ Estas fotos no se pueden recuperar después de eliminar",
        "#e08030",
    ),
    (
        "_historial_original.txt",
        "_historial_original.txt",
        "Historial de cambios (recomendado conservar)",
        False, False, None, None,
    ),
    (
        ".exif_backup.json",
        ".exif_backup.json",
        "Backups de EXIF (recomendado conservar)",
        False, False, None, None,
    ),
    (
        ".video_backup.json",
        ".video_backup.json",
        "Backups de metadata de video (recomendado conservar)",
        False, False, None, None,
    ),
]

# Derived quick-lookup tables
_IS_FOLDER:   Dict[str, bool]          = {t[0]: t[4]        for t in _TYPES}
_NOTE_COLOR:  Dict[str, Optional[str]] = {t[0]: t[6]        for t in _TYPES}
_FOLDER_KEYS: set                      = {t[0] for t in _TYPES if     t[4]}
_FILE_KEYS:   set                      = {t[0] for t in _TYPES if not t[4]}


# ── Background scan worker ────────────────────────────────────────────────────

class _CleanupScanWorker(QObject):
    """Walk a root path and emit every temp item found."""
    item_found = pyqtSignal(str, int, str)   # abs_path, size_bytes, type_key
    finished   = pyqtSignal()

    def __init__(self, root: Path) -> None:
        super().__init__()
        self._root = root

    @staticmethod
    def _folder_size(path: Path) -> int:
        total = 0
        try:
            for f in path.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    def run(self) -> None:
        try:
            for dirpath_str, dirnames, filenames in os.walk(str(self._root)):
                dp = Path(dirpath_str)

                # Check folder names; remove matched ones so os.walk never descends
                pruned: List[str] = []
                for d in list(dirnames):
                    if d in _FOLDER_KEYS:
                        folder = dp / d
                        size   = self._folder_size(folder)
                        self.item_found.emit(str(folder), size, d)
                        pruned.append(d)
                for d in pruned:
                    dirnames.remove(d)

                # Check file names
                for fname in filenames:
                    if fname in _FILE_KEYS:
                        fpath = dp / fname
                        try:
                            size = fpath.stat().st_size
                        except OSError:
                            size = 0
                        self.item_found.emit(str(fpath), size, fname)
        except Exception:
            pass
        self.finished.emit()


# ── Background delete worker ──────────────────────────────────────────────────

class _DeleteWorker(QObject):
    """Delete a list of paths (files or directories) in a background thread.

    Each item is ``(abs_path_str, size_bytes)``.  The worker emits
    ``progress(current, total, filename)`` before each deletion and
    ``finished(items_deleted, bytes_freed, errors)`` when done.
    """

    progress = pyqtSignal(int, int, str)        # current (0-based), total, filename
    finished = pyqtSignal(int, float, list)     # items_deleted, bytes_freed, errors

    def __init__(self, items: List[Tuple[str, int]]) -> None:
        super().__init__()
        self._items = items   # [(abs_path_str, size_bytes), ...]

    def run(self) -> None:
        total         = len(self._items)
        deleted_count = 0
        bytes_freed   = 0.0
        errors: List[str] = []

        for i, (path_str, size) in enumerate(self._items):
            path = Path(path_str)
            self.progress.emit(i, total, path.name)

            try:
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.is_file():
                    path.unlink()
                else:
                    continue   # already gone — don't count as deleted or error
                deleted_count += 1
                bytes_freed   += size
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")

        self.finished.emit(deleted_count, bytes_freed, errors)


# ── Dialog ────────────────────────────────────────────────────────────────────

class CleanupDialog(QDialog):
    """Scan and permanently delete temp folders/files under a root collection.

    Parameters
    ----------
    root:
        Collection root to scan (always available).
    current_folder:
        The folder currently open in the grid — enables a "scope" radio button
        so the user can choose to scan only this subtree instead of the full root.

    Usage::

        dlg = CleanupDialog(root, log_manager, parent, current_folder=folder)
        dlg.exec()
        if dlg.cleaned:
            folder_tree.load_root(root)
    """

    cleanup_done = pyqtSignal()   # emitted after a successful delete pass

    def __init__(
        self,
        root: Path,
        log_manager: LogManager,
        parent=None,
        *,
        current_folder: Optional[Path] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowIcon(QApplication.instance().windowIcon())
        self._root           = root
        self._log            = log_manager
        self._current_folder = current_folder   # folder open in grid (may be None)

        # Worker / thread (stored on self to prevent GC while running)
        self._scan_worker:   Optional[_CleanupScanWorker] = None
        self._scan_thread:   Optional[QThread]            = None
        self._delete_worker: Optional[_DeleteWorker]      = None
        self._delete_thread: Optional[QThread]            = None

        self._delete_progress_dlg: Optional[QProgressDialog] = None
        self._delete_total: int = 0

        # Scan results: type_key → [(abs_path, size_bytes), ...]
        self._found: Dict[str, List[Tuple[str, int]]] = {t[0]: [] for t in _TYPES}

        # Tree grouping: group_key (rel parent str) → top-level QTreeWidgetItem
        self._parent_items: Dict[str, QTreeWidgetItem] = {}

        # The root that the *current* scan was started from (snapshot at scan start)
        self._current_scan_root: Path = root

        # Set to True after at least one successful delete run
        self.cleaned = False

        self.setWindowTitle("Limpiar carpetas temporales")
        self.setMinimumWidth(760)
        self._build_ui()
        # Auto-scan the default scope as soon as the event loop starts (first exec() tick)
        QTimer.singleShot(0, self._on_scan)

    # ── Scan-scope property ───────────────────────────────────────────────

    @property
    def _scan_root(self) -> Path:
        """Return the path that will be scanned, based on the scope radio buttons."""
        if (
            self._current_folder is not None
            and self._current_folder != self._root
            and getattr(self, "_radio_current", None) is not None
            and self._radio_current.isChecked()
        ):
            return self._current_folder
        return self._root

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # ── Scope widget (label + optional radio buttons) ─────────────────
        self._scope_widget = QWidget()
        scope_vbox = QVBoxLayout(self._scope_widget)
        scope_vbox.setSpacing(5)
        scope_vbox.setContentsMargins(0, 0, 0, 2)

        self._lbl_scope = QLabel()
        self._lbl_scope.setStyleSheet("color: #aaaaaa; font-size: 10px;")
        self._lbl_scope.setWordWrap(True)
        scope_vbox.addWidget(self._lbl_scope)

        # Initialise radio-button references so _scan_root is always safe to call
        self._radio_root:    Optional[QRadioButton] = None
        self._radio_current: Optional[QRadioButton] = None
        self._btn_group:     Optional[QButtonGroup] = None

        show_radios = (
            self._current_folder is not None
            and self._current_folder != self._root
        )
        if show_radios:
            try:
                rel_cf = str(self._current_folder.relative_to(self._root))
            except ValueError:
                rel_cf = str(self._current_folder)

            self._radio_current = QRadioButton(
                f"📁 Carpeta actual: {rel_cf}"
            )
            self._radio_root = QRadioButton(
                f"📁 Carpeta raíz: {self._root}"
            )
            # Default to the current folder (faster, less surprising)
            self._radio_current.setChecked(True)

            self._btn_group = QButtonGroup(self)
            self._btn_group.addButton(self._radio_current)
            self._btn_group.addButton(self._radio_root)

            radio_row = QHBoxLayout()
            radio_row.addWidget(self._radio_current)
            radio_row.addWidget(self._radio_root)
            radio_row.addStretch()
            scope_vbox.addLayout(radio_row)

            # Connect AFTER setting initial state to avoid a spurious _on_scope_changed
            self._btn_group.buttonClicked.connect(lambda _: self._on_scope_changed())

        layout.addWidget(self._scope_widget)
        self._update_scope_label()

        # ── Type checkboxes ───────────────────────────────────────────────
        self._grp = QGroupBox("🗑  Elementos a eliminar")
        grp_layout = QVBoxLayout(self._grp)
        grp_layout.setSpacing(6)
        grp_layout.setContentsMargins(10, 8, 10, 8)

        self._chk:      Dict[str, QCheckBox] = {}
        self._lbl_size: Dict[str, QLabel]    = {}

        for key, display, desc, default, is_folder, note, note_color in _TYPES:
            container = QWidget()
            vbox = QVBoxLayout(container)
            vbox.setSpacing(1)
            vbox.setContentsMargins(0, 2, 0, 2)

            hdr = QHBoxLayout()

            chk = QCheckBox(display)
            chk.setChecked(default)
            chk.toggled.connect(self._update_total_label)
            self._chk[key] = chk

            sz_lbl = QLabel("—")
            sz_lbl.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            sz_lbl.setStyleSheet("color: #999999; font-size: 10px; min-width: 200px;")
            self._lbl_size[key] = sz_lbl

            hdr.addWidget(chk)
            hdr.addStretch()
            hdr.addWidget(sz_lbl)
            vbox.addLayout(hdr)

            dl = QLabel(desc)
            dl.setStyleSheet("color: #888888; font-size: 10px; padding-left: 22px;")
            vbox.addWidget(dl)

            if note and note_color:
                nl = QLabel(note)
                nl.setStyleSheet(
                    f"color: {note_color}; font-size: 10px; padding-left: 22px;"
                )
                vbox.addWidget(nl)

            grp_layout.addWidget(container)

        layout.addWidget(self._grp)

        # ── Total size label ──────────────────────────────────────────────
        total_row = QHBoxLayout()
        total_row.addStretch()
        self._lbl_total = QLabel("Espacio total a liberar: —")
        self._lbl_total.setStyleSheet("font-weight: bold;")
        total_row.addWidget(self._lbl_total)
        layout.addLayout(total_row)

        # ── Found-items tree (grouped by parent folder) ───────────────────
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setMinimumHeight(200)
        self._tree.setIndentation(18)
        self._tree.setStyleSheet("font-size: 10px;")
        layout.addWidget(self._tree)

        # ── Indeterminate scan progress bar (hidden while idle) ───────────
        self._scan_bar = QProgressBar()
        self._scan_bar.setRange(0, 0)
        self._scan_bar.setFixedHeight(14)
        self._scan_bar.setTextVisible(False)
        self._scan_bar.setVisible(False)
        layout.addWidget(self._scan_bar)

        # ── Result screen (hidden until a delete completes) ───────────────
        self._result_widget = QWidget()
        result_layout = QVBoxLayout(self._result_widget)
        result_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        result_layout.setSpacing(12)

        self._lbl_result = QLabel()
        self._lbl_result.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_result.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #60c060; padding: 30px 20px;"
        )
        self._lbl_result.setWordWrap(True)
        result_layout.addWidget(self._lbl_result)

        self._result_widget.setVisible(False)
        layout.addWidget(self._result_widget)

        # ── Buttons ───────────────────────────────────────────────────────
        btn_row = QHBoxLayout()

        self._btn_scan   = QPushButton("🔍 Escanear")
        self._btn_delete = QPushButton("🗑 Eliminar seleccionados")
        self._btn_close  = QPushButton("Cerrar")

        self._btn_scan.setToolTip(
            "Escanea la carpeta seleccionada y lista todos los elementos "
            "que coincidan con los tipos marcados arriba."
        )
        self._btn_delete.setToolTip(
            "Elimina permanentemente todos los elementos encontrados que "
            "pertenezcan a tipos actualmente marcados.\n"
            "⚠ No se puede deshacer."
        )

        self._btn_scan.clicked.connect(self._on_scan)
        self._btn_delete.clicked.connect(self._on_delete)
        self._btn_close.clicked.connect(self.reject)

        self._btn_delete.setEnabled(False)

        apply_button_style(self._btn_scan)
        apply_button_style(self._btn_delete)
        apply_button_style(self._btn_close)

        btn_row.addWidget(self._btn_scan)
        btn_row.addWidget(self._btn_delete)
        btn_row.addStretch()
        btn_row.addWidget(self._btn_close)
        layout.addLayout(btn_row)

    # ── Scope helpers ─────────────────────────────────────────────────────

    def _update_scope_label(self) -> None:
        """Refresh the top label to confirm the active scan scope."""
        root = self._scan_root
        if root == self._root:
            self._lbl_scope.setText(
                "Se buscarán carpetas y archivos temporales en toda la colección."
            )
        else:
            self._lbl_scope.setText(
                "Se buscarán carpetas y archivos temporales solo en esta carpeta "
                "y sus subcarpetas."
            )

    def _on_scope_changed(self) -> None:
        """Called when the user clicks a scope radio button."""
        self._update_scope_label()
        # Clear previous scan results — they belong to a different scope
        self._reset_scan_results()

    def _reset_scan_results(self) -> None:
        """Clear all scan state: found dict, tree widget, size labels."""
        self._found = {t[0]: [] for t in _TYPES}
        self._tree.clear()
        self._parent_items.clear()
        for key in self._lbl_size:
            self._lbl_size[key].setText("—")
        self._lbl_total.setText("Espacio total a liberar: —")
        self._btn_delete.setEnabled(False)

    # ── Scan slots ────────────────────────────────────────────────────────

    def _on_scan(self) -> None:
        """Start a background scan for all matching temp items."""
        self._reset_scan_results()
        self._lbl_total.setText("Escaneando…")
        # Disable all action buttons for the duration of the scan
        self._btn_scan.setEnabled(False)
        self._btn_delete.setEnabled(False)
        self._btn_close.setEnabled(False)
        self._scan_bar.setVisible(True)

        # Disable scope radios while scanning
        if self._radio_root:
            self._radio_root.setEnabled(False)
        if self._radio_current:
            self._radio_current.setEnabled(False)

        # Snapshot the effective root so grouping stays consistent
        self._current_scan_root = self._scan_root

        self._scan_worker = _CleanupScanWorker(self._current_scan_root)
        self._scan_thread = QThread(self)
        self._scan_worker.moveToThread(self._scan_thread)

        # Thread lifetime pattern (CLAUDE.md): do NOT connect finished→thread.quit here;
        # _on_scan_finished calls quit()+wait() directly to avoid double-quit.
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.item_found.connect(self._on_item_found)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_thread.finished.connect(self._cleanup_scan_thread)

        QApplication.processEvents()   # force scan-bar to paint before thread starts
        self._scan_thread.start()

    def _on_item_found(self, path: str, size: int, type_key: str) -> None:
        """Add one found item to the results; group it by parent folder in the tree."""
        self._found[type_key].append((path, size))

        p      = Path(path)
        parent = p.parent

        # ── Compute relative parent path (used as group key + header text) ──
        try:
            rel_parent = str(parent.relative_to(self._current_scan_root))
        except ValueError:
            rel_parent = str(parent)

        # "." means the item is a direct child of the scan root
        if rel_parent == ".":
            header_text = f"{self._current_scan_root.name}\\"
        else:
            header_text = rel_parent + "\\"

        # ── Find or create the parent folder top-level item ───────────────
        if rel_parent not in self._parent_items:
            folder_item = QTreeWidgetItem(self._tree)
            folder_item.setExpanded(True)
            folder_item.setForeground(0, QColor("#cccccc"))
            self._parent_items[rel_parent] = folder_item
        else:
            folder_item = self._parent_items[rel_parent]

        # ── Add child item for this temp entry ────────────────────────────
        child = QTreeWidgetItem(folder_item)
        child.setText(0, f"{p.name}   ({_fmt_bytes(size)})")
        child.setData(0, Qt.ItemDataRole.UserRole, (path, type_key))
        color = _NOTE_COLOR.get(type_key)
        if color:
            child.setForeground(0, QColor(color))

        # ── Update folder item header: show relative path + total size ────
        folder_total = sum(
            s
            for t in _TYPES
            for pstr, s in self._found[t[0]]
            if Path(pstr).parent == parent
        )
        folder_item.setText(0, f"{header_text}   ({_fmt_bytes(folder_total)})")

        # ── Update per-type count + size label in the checkboxes group ────
        items     = self._found[type_key]
        n         = len(items)
        total_sz  = sum(s for _, s in items)
        is_folder = _IS_FOLDER[type_key]
        unit_sg   = "carpeta" if is_folder else "archivo"
        unit_pl   = "carpetas" if is_folder else "archivos"
        self._lbl_size[type_key].setText(
            f"{n} {unit_sg if n == 1 else unit_pl} · {_fmt_bytes(total_sz)}"
        )

        self._update_total_label()

    def _on_scan_finished(self) -> None:
        """Called when the scan worker signals it is done.

        Calls quit()+wait() before any UI changes so the OS thread is fully
        stopped before Qt objects it may still reference are touched.
        """
        if self._scan_thread and self._scan_thread.isRunning():
            self._scan_thread.quit()
            self._scan_thread.wait()

        self._scan_bar.setVisible(False)
        self._btn_scan.setEnabled(True)
        self._btn_close.setEnabled(True)

        # Re-enable scope radios
        if self._radio_root:
            self._radio_root.setEnabled(True)
        if self._radio_current:
            self._radio_current.setEnabled(True)

        has_items = any(self._found[t[0]] for t in _TYPES)
        self._btn_delete.setEnabled(has_items)

        total = self._selected_total_bytes()
        if has_items:
            self._lbl_total.setText(
                f"Espacio total a liberar: {_fmt_bytes(total)}"
            )
        else:
            self._lbl_total.setText(
                "No se encontraron elementos para limpiar."
            )

    def _cleanup_scan_thread(self) -> None:
        """Stop (if still running) and schedule scan worker/thread for deletion.

        Safe to call from thread.finished signal (thread already stopped) or
        directly (e.g. from reject()) when we need to force-stop an in-progress scan.
        Uses a 5 s wait then terminate() as a last-resort fallback for the
        os.walk loop which cannot be interrupted cooperatively.
        """
        if self._scan_thread:
            if self._scan_thread.isRunning():
                self._scan_thread.quit()
                if not self._scan_thread.wait(5000):
                    self._scan_thread.terminate()
                    self._scan_thread.wait()
            self._scan_thread.deleteLater()
            self._scan_thread = None
        if self._scan_worker:
            self._scan_worker.deleteLater()
            self._scan_worker = None

    def reject(self) -> None:
        """Override to stop any running scan thread before closing the dialog."""
        self._cleanup_scan_thread()
        super().reject()

    # ── Delete ────────────────────────────────────────────────────────────

    def _on_delete(self) -> None:
        """Confirm deletion, then start the _DeleteWorker thread."""
        # Collect (abs_path, size_bytes) for every checked type
        to_delete: List[Tuple[str, int]] = []
        for key, *_ in _TYPES:
            if self._chk[key].isChecked():
                for path_str, size in self._found[key]:
                    to_delete.append((path_str, size))

        if not to_delete:
            mb_info(
                self, "Sin elementos",
                "No hay elementos seleccionados para eliminar.\n"
                "Asegurate de que los tipos que querés limpiar estén marcados."
            )
            return

        n             = len(to_delete)
        bytes_to_free = sum(s for _, s in to_delete)
        reply = mb_question(
            self,
            "Confirmar eliminación permanente",
            f"¿Eliminar permanentemente {n} elemento{'s' if n != 1 else ''} "
            f"({_fmt_bytes(bytes_to_free)})?\n\n"
            "⚠ Esta acción no se puede deshacer.",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Disable all buttons while the worker runs
        self._btn_scan.setEnabled(False)
        self._btn_delete.setEnabled(False)
        self._btn_close.setEnabled(False)

        # ── Progress dialog ───────────────────────────────────────────────
        self._delete_total = n
        self._delete_progress_dlg = QProgressDialog(self)
        self._delete_progress_dlg.setWindowTitle("Eliminando…")
        self._delete_progress_dlg.setLabelText("Iniciando…")
        self._delete_progress_dlg.setRange(0, n)
        self._delete_progress_dlg.setValue(0)
        self._delete_progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self._delete_progress_dlg.setCancelButton(None)
        self._delete_progress_dlg.setMinimumDuration(0)
        self._delete_progress_dlg.canceled.connect(self._on_cancel_delete)
        self._delete_progress_dlg.show()

        # ── Spin up worker thread ─────────────────────────────────────────
        self._delete_worker = _DeleteWorker(to_delete)
        self._delete_thread = QThread()
        self._delete_worker.moveToThread(self._delete_thread)

        self._delete_thread.started.connect(self._delete_worker.run)
        self._delete_worker.progress.connect(self._on_delete_progress)
        self._delete_worker.finished.connect(self._on_delete_finished)
        # Do NOT also connect finished→thread.quit here: _on_delete_finished calls
        # quit()+wait() directly to avoid the double-quit race (CLAUDE.md pattern).
        self._delete_thread.finished.connect(self._cleanup_delete_thread)

        self._delete_thread.start()

    def _on_delete_progress(self, current: int, total: int, filename: str) -> None:
        """Update the progress dialog label and value."""
        if self._delete_progress_dlg:
            self._delete_progress_dlg.setValue(current)
            self._delete_progress_dlg.setLabelText(
                f"Eliminando: {filename}\n{current + 1} de {total}"
            )

    def _on_cancel_delete(self) -> None:
        """Cancel delete operation."""
        if self._delete_worker is not None:
            self._delete_worker.stop_requested = True
        if self._delete_progress_dlg:
            self._delete_progress_dlg.close()
            self._delete_progress_dlg = None
        self._btn_scan.setEnabled(True)
        self._btn_delete.setEnabled(True)
        self._btn_close.setEnabled(True)

    def _on_delete_finished(
        self, deleted_count: int, bytes_freed: float, errors: List[str]
    ) -> None:
        """Called when _DeleteWorker finishes. Must quit+wait before any UI changes."""
        # ── Stop the OS thread synchronously before touching Qt objects ───
        if self._delete_thread and self._delete_thread.isRunning():
            self._delete_thread.quit()
            self._delete_thread.wait()

        # ── Close progress dialog ─────────────────────────────────────────
        if self._delete_progress_dlg:
            self._delete_progress_dlg.setValue(self._delete_total)
            self._delete_progress_dlg.close()
            self._delete_progress_dlg = None

        # ── Log the operation ─────────────────────────────────────────────
        self._log.log(
            "", "", "cleanup", "",
            f"Eliminados: {deleted_count} elementos, "
            f"{_fmt_bytes(int(bytes_freed))} liberados"
        )

        # ── Mark success ──────────────────────────────────────────────────
        self.cleaned = True
        self.cleanup_done.emit()

        # ── Switch to result screen ───────────────────────────────────────
        # Hide all scan-phase widgets; show result label + enable Close
        self._scope_widget.setVisible(False)
        self._grp.setVisible(False)
        self._lbl_total.setVisible(False)
        self._tree.setVisible(False)
        self._scan_bar.setVisible(False)
        self._btn_scan.setVisible(False)
        self._btn_delete.setVisible(False)

        mb_freed = bytes_freed / 1_048_576
        if errors:
            error_lines = "\n".join(errors[:5]) + (" …" if len(errors) > 5 else "")
            result_text = (
                f"⚠ Limpieza completada con errores\n\n"
                f"Eliminados: {deleted_count} elemento{'s' if deleted_count != 1 else ''}\n"
                f"Espacio liberado: {mb_freed:.2f} MB\n\n"
                f"{len(errors)} error{'es' if len(errors) != 1 else ''} al eliminar:\n"
                f"{error_lines}"
            )
            self._lbl_result.setStyleSheet(
                "font-size: 13px; font-weight: bold; color: #e08030; padding: 30px 20px;"
            )
        else:
            result_text = (
                f"✅ Limpieza completada\n\n"
                f"Eliminados: {deleted_count} elemento{'s' if deleted_count != 1 else ''}\n"
                f"Espacio liberado: {mb_freed:.2f} MB"
            )
            self._lbl_result.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #60c060; padding: 30px 20px;"
            )

        self._lbl_result.setText(result_text)
        self._result_widget.setVisible(True)
        self._btn_close.setEnabled(True)

        # Compact the dialog to fit the result screen
        self.adjustSize()

    def _cleanup_delete_thread(self) -> None:
        """Schedule delete worker/thread for safe deferred deletion."""
        if self._delete_worker:
            self._delete_worker.deleteLater()
            self._delete_worker = None
        if self._delete_thread:
            self._delete_thread.deleteLater()
            self._delete_thread = None

    # ── Helpers ───────────────────────────────────────────────────────────

    def _selected_total_bytes(self) -> int:
        """Sum of sizes for all found items whose type checkbox is checked."""
        return sum(
            size
            for t in _TYPES
            if self._chk[t[0]].isChecked()
            for _, size in self._found[t[0]]
        )

    def _update_total_label(self) -> None:
        """Refresh the 'Espacio total a liberar' label."""
        has_scan = any(self._found[t[0]] for t in _TYPES)
        if not has_scan:
            self._lbl_total.setText("Espacio total a liberar: —")
            return
        total = self._selected_total_bytes()
        self._lbl_total.setText(f"Espacio total a liberar: {_fmt_bytes(total)}")
