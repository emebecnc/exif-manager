"""Duplicate finder dialog: scan, compare side-by-side, move to trash folder."""
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QThread, QSize, pyqtSignal
from PyQt6.QtGui import QPixmap, QColor, QBrush
from PyQt6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QSplitter,
    QLabel, QPushButton, QProgressBar, QListWidget, QListWidgetItem,
    QScrollArea, QWidget, QFrame, QGroupBox, QFormLayout,
    QMessageBox, QAbstractItemView,
)

from core.duplicate_finder import DuplicateScanWorker
from core.exif_handler import read_exif, load_thumbnail
from core.file_scanner import compute_md5, unique_dest
from ui.log_viewer import LogManager
from ui.styles import apply_button_style, mb_warning, mb_info

_TRASH_DIRNAME = "_duplicados_eliminados"
_THUMB_SIZE    = 200


class _DuplicateItemWidget(QWidget):
    """Displays metadata + thumbnail for one file in a duplicate group."""

    delete_requested = pyqtSignal(Path)
    keep_requested   = pyqtSignal(Path)

    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.path = path
        self._deleted = False
        self._build_ui(path)

    def _build_ui(self, path: Path) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # Frame border
        self.setStyleSheet("QWidget { border: 1px solid #44444e; border-radius: 4px; }")

        # Thumbnail
        self._thumb_label = QLabel("…")
        self._thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_label.setFixedSize(_THUMB_SIZE, _THUMB_SIZE)
        self._thumb_label.setStyleSheet("border: none; background: #1e1e23;")
        layout.addWidget(self._thumb_label, alignment=Qt.AlignmentFlag.AlignHCenter)
        self._load_thumb(path)

        # Info section
        info_grp = QGroupBox("")
        info_grp.setStyleSheet("QGroupBox { border: none; }")
        form = QFormLayout(info_grp)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        try:
            stat = path.stat()
            size_str = f"{stat.st_size / 1024:.1f} KB"
        except OSError:
            size_str = "N/D"

        from PIL import Image
        dim_str = "N/D"
        try:
            with Image.open(path) as img:
                dim_str = f"{img.width} × {img.height}"
        except Exception:
            pass

        exif = read_exif(path)
        date_orig = exif["fields"].get("DateTimeOriginal", "—")
        date_sys  = exif["fields"].get("DateTime", "—")

        from datetime import datetime
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
        except Exception:
            mtime = "N/D"

        md5 = compute_md5(path)

        for label, val in [
            ("Nombre",         path.name),
            ("Tamaño",         size_str),
            ("Dimensiones",    dim_str),
            ("Fecha EXIF",     date_orig),
            ("Fecha sistema",  date_sys),
            ("Modificado",     mtime),
            ("MD5",            md5[:16] + "…"),
        ]:
            lbl_key = QLabel(f"{label}:")
            lbl_key.setStyleSheet("border: none; font-size: 10pt;")
            lbl_w = QLabel(val)
            lbl_w.setWordWrap(True)
            lbl_w.setStyleSheet("border: none; font-size: 10pt;")
            form.addRow(lbl_key, lbl_w)

        # Full path
        path_lbl = QLabel(str(path))
        path_lbl.setWordWrap(True)
        path_lbl.setStyleSheet("font-size: 9px; color: #888; border: none;")
        path_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(path_lbl)
        layout.addWidget(info_grp)

        # Action buttons
        btn_row = QHBoxLayout()
        self._btn_keep = QPushButton("Conservar")
        self._btn_keep.setStyleSheet("background: #2a6a2a; color: white;")
        self._btn_keep.setToolTip(
            "Marca esta foto como la que se conservará.\n"
            "No mueve ni elimina ningún archivo por sí solo."
        )
        self._btn_keep.clicked.connect(lambda: self.keep_requested.emit(self.path))
        self._btn_delete = QPushButton("🗑 Eliminar")
        self._btn_delete.setStyleSheet("background: #6a2a2a; color: white;")
        self._btn_delete.setToolTip(
            "Mueve esta foto a _duplicados_eliminados.\n"
            "No se borra permanentemente — podés recuperarla desde esa carpeta."
        )
        self._btn_delete.clicked.connect(self._on_delete)
        btn_row.addWidget(self._btn_keep)
        btn_row.addWidget(self._btn_delete)
        layout.addLayout(btn_row)

        self._status_label = QLabel("")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setStyleSheet("border: none;")
        layout.addWidget(self._status_label)

    def _load_thumb(self, path: Path) -> None:
        data = load_thumbnail(path, _THUMB_SIZE)
        if data:
            pix = QPixmap()
            pix.loadFromData(data)
            if not pix.isNull():
                self._thumb_label.setPixmap(
                    pix.scaled(_THUMB_SIZE, _THUMB_SIZE,
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
                )

    def _on_delete(self) -> None:
        trash_dir = self.path.parent / _TRASH_DIRNAME
        try:
            trash_dir.mkdir(exist_ok=True)
            dest = unique_dest(self.path, trash_dir)
            shutil.move(str(self.path), str(dest))
            self._deleted = True
            self._btn_delete.setEnabled(False)
            self._btn_keep.setEnabled(False)
            self._status_label.setText("Eliminado")
            self._status_label.setStyleSheet("color: #e05050; border: none;")
            self.delete_requested.emit(self.path)
        except Exception as e:
            mb_warning(self, "Error", str(e))

    def mark_kept(self) -> None:
        self._status_label.setText("Conservado")
        self._status_label.setStyleSheet("color: #50e050; border: none;")


class DuplicateViewerDialog(QDialog):
    def __init__(self, root_path: Path, log_manager: LogManager, parent=None):
        super().__init__(parent)
        self.setWindowIcon(QApplication.instance().windowIcon())
        self._root = root_path
        self._log  = log_manager
        self._groups: List[List[Path]] = []
        self._worker: Optional[DuplicateScanWorker] = None
        self._thread: Optional[QThread] = None
        # Track which group is displayed and its photo widgets for in-place updates
        self._current_group_index: int = -1
        self._current_item_widgets: Dict[Path, "_DuplicateItemWidget"] = {}

        self.setWindowTitle("Duplicados")
        self.setMinimumSize(900, 600)
        self.resize(1100, 700)
        self._build_ui()
        self._start_scan()

    # ── UI ─────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Scan progress area
        self._scan_widget = QWidget()
        scan_layout = QVBoxLayout(self._scan_widget)
        self._scan_label = QLabel("Escaneando archivos…")
        self._scan_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 0)  # indeterminate initially
        self._btn_cancel_scan = QPushButton("Cancelar")
        self._btn_cancel_scan.setToolTip("Cancela el escaneo en curso y cierra el diálogo.")
        self._btn_cancel_scan.clicked.connect(self._cancel_scan)
        scan_layout.addStretch()
        scan_layout.addWidget(self._scan_label)
        scan_layout.addWidget(self._progress_bar)
        scan_layout.addWidget(self._btn_cancel_scan, alignment=Qt.AlignmentFlag.AlignHCenter)
        scan_layout.addStretch()
        layout.addWidget(self._scan_widget)

        # Results area (hidden during scan)
        self._results_widget = QWidget()
        self._results_widget.setVisible(False)
        results_layout = QVBoxLayout(self._results_widget)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(6)

        # Compact summary label
        self._result_label = QLabel("")
        self._result_label.setStyleSheet("font-size: 10pt;")
        results_layout.addWidget(self._result_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── Left panel: groups list + "Conservar primero" pinned at bottom ──
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        self._groups_list = QListWidget()
        self._groups_list.currentRowChanged.connect(self._on_group_selected)
        left_layout.addWidget(self._groups_list, 1)   # stretch → takes all available height

        self._btn_keep_first = QPushButton("Conservar primero de cada grupo")
        self._btn_keep_first.setToolTip(
            "Mueve automáticamente todos los duplicados de cada grupo a\n"
            "_duplicados_eliminados. Solo conserva la primera foto de cada grupo.\n"
            "Los archivos no se borran permanentemente."
        )
        self._btn_keep_first.clicked.connect(self._keep_first_all)
        apply_button_style(self._btn_keep_first)
        left_layout.addWidget(self._btn_keep_first)   # pinned at bottom

        splitter.addWidget(left_widget)

        # ── Right panel: side-by-side comparison scroll ───────────────────
        self._comparison_scroll = QScrollArea()
        self._comparison_scroll.setWidgetResizable(True)
        self._comparison_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._comparison_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        splitter.addWidget(self._comparison_scroll)
        splitter.setSizes([240, 820])

        results_layout.addWidget(splitter, 1)

        # Bottom bar: just close
        bottom_bar = QHBoxLayout()
        bottom_bar.addStretch()
        btn_close = QPushButton("Cerrar")
        btn_close.setToolTip("Cierra el visor de duplicados.")
        btn_close.clicked.connect(self.accept)
        apply_button_style(btn_close)
        bottom_bar.addWidget(btn_close)
        results_layout.addLayout(bottom_bar)

        layout.addWidget(self._results_widget)

    # ── Scan ───────────────────────────────────────────────────────────────

    def _start_scan(self) -> None:
        self._worker = DuplicateScanWorker(self._root)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_scan_progress)
        self._worker.finished.connect(self._on_scan_finished)
        self._worker.error.connect(self._on_scan_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _cancel_scan(self) -> None:
        if self._worker:
            self._worker.cancel()
        self.reject()

    def _on_scan_progress(self, current: int, total: int, fname: str) -> None:
        if total > 0:
            self._progress_bar.setRange(0, total)
            self._progress_bar.setValue(current)
        self._scan_label.setText(f"Escaneando… {current}/{total}\n{fname}")

    def _on_scan_finished(self, groups: List[List[Path]]) -> None:
        self._groups = groups
        self._scan_widget.setVisible(False)
        self._results_widget.setVisible(True)

        if not groups:
            self._result_label.setText("No se encontraron duplicados exactos.")
            self._btn_keep_first.setEnabled(False)
            return

        total_files = sum(len(g) for g in groups)
        self._result_label.setText(
            f"{len(groups)} grupos de duplicados ({total_files} archivos)."
        )

        for i, group in enumerate(groups):
            item = QListWidgetItem(
                f"Grupo {i+1}: {len(group)} archivos\n{group[0].name}"
            )
            item.setData(Qt.ItemDataRole.UserRole, i)
            self._groups_list.addItem(item)

        self._groups_list.setCurrentRow(0)

    def _on_scan_error(self, msg: str) -> None:
        self._scan_widget.setVisible(False)
        self._results_widget.setVisible(True)
        self._result_label.setText(f"Error durante el escaneo: {msg}")

    # ── Group display ──────────────────────────────────────────────────────

    def _on_group_selected(self, index: int) -> None:
        if index < 0 or index >= len(self._groups):
            return
        self._current_group_index = index
        self._show_group(self._groups[index])

    def _show_group(self, group: List[Path]) -> None:
        """Build and display comparison widgets for the given group."""
        self._current_item_widgets.clear()

        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setSpacing(8)
        layout.setContentsMargins(4, 4, 4, 4)

        for path in group:
            item_widget = _DuplicateItemWidget(path)
            item_widget.delete_requested.connect(self._on_photo_deleted)
            item_widget.delete_requested.connect(
                lambda p: self._log.log(str(p.parent), p.name, "delete_duplicate")
            )
            self._current_item_widgets[path] = item_widget
            layout.addWidget(item_widget)

        layout.addStretch()
        self._comparison_scroll.setWidget(container)

    # ── In-place delete handling ───────────────────────────────────────────

    def _on_photo_deleted(self, path: Path) -> None:
        """Called when a photo widget's delete button is pressed successfully."""
        idx = self._current_group_index
        if idx < 0 or idx >= len(self._groups):
            return

        group = self._groups[idx]
        if path in group:
            group.remove(path)

        # Hide the widget immediately
        widget = self._current_item_widgets.pop(path, None)
        if widget is not None:
            widget.setVisible(False)

        if len(group) <= 1:
            # This group is resolved — remove it entirely
            self._groups_list.takeItem(idx)
            self._groups.pop(idx)
            self._current_group_index = -1
            self._current_item_widgets.clear()
            self._comparison_scroll.setWidget(QWidget())
            self._relabel_group_items()
            self._update_result_label()

            if self._groups:
                new_idx = min(idx, len(self._groups) - 1)
                self._groups_list.setCurrentRow(new_idx)
            else:
                self._btn_keep_first.setEnabled(False)
        else:
            # Update the group's entry in the list sidebar
            list_item = self._groups_list.item(idx)
            if list_item is not None:
                list_item.setText(
                    f"Grupo {idx+1}: {len(group)} archivos\n{group[0].name}"
                )
            self._update_result_label()

    def _relabel_group_items(self) -> None:
        """Re-number all list sidebar items to stay in sync with self._groups."""
        for i, group in enumerate(self._groups):
            item = self._groups_list.item(i)
            if item is not None:
                item.setText(
                    f"Grupo {i+1}: {len(group)} archivos\n{group[0].name}"
                )

    def _update_result_label(self) -> None:
        if not self._groups:
            self._result_label.setText("No quedan grupos de duplicados.")
        else:
            total_files = sum(len(g) for g in self._groups)
            self._result_label.setText(
                f"{len(self._groups)} grupos de duplicados ({total_files} archivos)."
            )

    # ── Batch actions ──────────────────────────────────────────────────────

    def _keep_first_all(self) -> None:
        """Move all non-first files in every group to the trash folder."""
        total_deleted = 0
        errors = []

        for group in self._groups:
            for path in group[1:]:
                if not path.exists():
                    continue
                trash_dir = path.parent / _TRASH_DIRNAME
                try:
                    trash_dir.mkdir(exist_ok=True)
                    dest = unique_dest(path, trash_dir)
                    shutil.move(str(path), str(dest))
                    self._log.log(str(path.parent), path.name, "delete_duplicate")
                    total_deleted += 1
                except Exception as e:
                    errors.append(f"{path.name}: {e}")

        msg = f"Se eliminaron {total_deleted} archivos duplicados."
        if errors:
            msg += f"\n\nErrores ({len(errors)}):\n" + "\n".join(errors[:10])
        mb_info(self, "Completado", msg)

        # All groups are now resolved — clear everything
        self._groups.clear()
        self._groups_list.clear()
        self._comparison_scroll.setWidget(QWidget())
        self._current_group_index = -1
        self._current_item_widgets.clear()
        self._result_label.setText("Todos los duplicados han sido procesados.")
        self._btn_keep_first.setEnabled(False)
