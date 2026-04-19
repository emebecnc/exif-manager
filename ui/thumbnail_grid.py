"""Center panel: thumbnail grid with two-phase background loading and disk cache."""
import hashlib
import json
import os
import re
import shutil
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

from PyQt6.QtCore import (
    Qt, pyqtSignal, QObject, QThread, QSize, QEvent,
)
from PyQt6.QtGui import QPixmap, QColor, QPen, QBrush, QAction
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QComboBox,
    QListWidget, QListWidgetItem, QAbstractItemView, QStyledItemDelegate,
    QStyleOptionViewItem, QApplication, QFileDialog, QMenu, QMessageBox, QInputDialog,
    QProgressBar, QLayout, QCheckBox,
)
from PyQt6.QtCore import QModelIndex

from core.backup_manager import has_backup, append_historial
from core.exif_handler import load_thumbnail, read_exif, is_invalid_date, get_best_date_str
from core.file_scanner import scan_folder, unique_dest, read_exif_dates_batch
from ui.log_viewer import LogManager
from ui.styles import apply_button_style, mb_warning

# UserRole slots
_ROLE_PATH    = Qt.ItemDataRole.UserRole
_ROLE_DATE    = Qt.ItemDataRole.UserRole + 1
_ROLE_INVALID = Qt.ItemDataRole.UserRole + 2
_ROLE_STD_NAME = Qt.ItemDataRole.UserRole + 3  # True=standard name, False=non-standard

# Standard filename pattern: YYYY-MM-DD-HHhMMmSSs.ext  (e.g. 2011-12-24-15h40m46s.jpg)
_STANDARD_NAME_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}-\d{2}h\d{2}m\d{2}s(_\d+)?\..+$',
    re.IGNORECASE,
)

# Sort mode constants
_SORT_DATE = 0   # by EXIF date (DateTimeOriginal → Digitized → DateTime → mtime)
_SORT_NAME = 1   # by filename

_THUMB_SIZE = 150
_ITEM_W = 175
_ITEM_H = 220

_TRASH_DIRNAME = "_eliminados"

# Characters forbidden in folder names on Windows
_ILLEGAL_NAME_CHARS = frozenset('\\ / : * ? " < > |'.split())

# Minimum photo count before the progress bar is shown
_PROGRESS_THRESHOLD = 100


class ThumbnailCache:
    """LRU in-memory cache for decoded QPixmap thumbnails.

    Keeps only the *max_size* most-recently-used entries so RAM usage stays
    bounded even in folders with thousands of images.
    """

    def __init__(self, max_size: int = 200) -> None:
        self._cache: OrderedDict = OrderedDict()
        self._max_size = max_size

    def get(self, key: str):
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def put(self, key: str, value) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)  # evict oldest

    def pop(self, key: str, default=None):
        return self._cache.pop(key, default)

    def clear(self) -> None:
        self._cache.clear()


def _thumb_cache_key(path_str: str, file_size: int) -> str:
    """MD5 key for a thumbnail cache entry: encodes path + file size.

    File size is used instead of mtime because NAS drives often report
    unreliable or reset modification times across remounts, causing a cache
    miss on every open even when the file has not changed.  File size is
    stable across remounts and sufficient to detect genuine file changes.
    """
    data = f"{path_str}|{file_size}".encode("utf-8")
    return hashlib.md5(data).hexdigest()


class _ThumbnailWorker(QObject):
    """Two-phase background worker.

    The worker skips EXIF-date-sort blocking:
      Phase 1 (batch EXIF):  reads all EXIF dates in parallel using
                             ThreadPoolExecutor so the sort after loading
                             is nearly instant.
      Phase 2 (thumbnails):  loads each thumbnail from the disk cache
                             (fast re-open) or generates it with Pillow
                             (slow first-open) and emits per-item updates.
    """
    # Batch update: list of (path_str, bytes|None, date_str, is_invalid)
    # Emitted every 20 items so the main thread does fewer round-trips.
    thumbnails_batch_ready = pyqtSignal(list)
    progress  = pyqtSignal(int, int)   # current, total  (Phase 2)
    finished  = pyqtSignal()

    def __init__(self, paths: List[Path], cache_dir: Optional[Path]):
        super().__init__()
        self._paths     = paths
        self._cache_dir = cache_dir
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        print(f"[WORKER] _ThumbnailWorker started, _cancelled={self._cancelled}, paths={len(self._paths)}", flush=True)
        if not self._paths:
            self.finished.emit()
            return

        # ── Phase 1: EXIF dates (disk-cache-aware) ────────────────────────
        # On first open: reads EXIF from every original file (slow on NAS).
        # On subsequent opens: loads a single _exif_cache.json — near-instant.
        # Only re-reads files whose size has changed since the cache was written.
        if self._cancelled:
            self.finished.emit()
            return
        exif_dates = self._load_exif_dates_cached()

        # ── Phase 2: thumbnail loading (disk-cache-aware, batched) ────────
        # Items are accumulated in batches of 20 before being sent to the main
        # thread.  This dramatically reduces the number of cross-thread signal
        # deliveries for large folders (5000 items → 250 signals instead of 5000).
        total = len(self._paths)
        batch: list = []
        for i, path in enumerate(self._paths):
            if self._cancelled:
                break
            self.progress.emit(i + 1, total)

            thumb_bytes = self._get_thumb(path)
            date_str    = exif_dates.get(path, "")
            invalid     = is_invalid_date(date_str)
            batch.append((str(path), thumb_bytes, date_str, invalid))

            if len(batch) == 20:
                self.thumbnails_batch_ready.emit(batch)
                batch = []

        # Flush any remainder
        if batch:
            self.thumbnails_batch_ready.emit(batch)

        self.finished.emit()

    # ── EXIF date disk cache ───────────────────────────────────────────────

    _EXIF_CACHE_FILE = "_exif_cache.json"

    def _load_exif_dates_cached(self) -> Dict[Path, str]:
        """Return EXIF date strings for all paths, using a disk cache.

        Cache format (stored in ``_thumbcache/_exif_cache.json``):
            { "filename.jpg": {"size": 123456, "date": "2020:01:01 12:00:00"}, … }

        A cache entry is valid when the file's current size matches the stored
        size (same logic as the thumbnail cache key).  Changed or new files are
        read via ``read_exif_dates_batch`` and the cache is updated on disk.
        """
        cached   = self._read_exif_cache()       # {filename → {size, date}}
        to_read: List[Path] = []
        results: Dict[Path, str] = {}

        for path in self._paths:
            try:
                size = path.stat().st_size
            except OSError:
                size = -1
            fname = path.name
            entry = cached.get(fname)
            if entry and entry.get("size") == size:
                # Cache hit — no disk EXIF read needed
                results[path] = entry.get("date", "")
            else:
                # Cache miss or file changed
                to_read.append(path)

        if to_read:
            fresh = read_exif_dates_batch(to_read)
            for path, date_str in fresh.items():
                results[path] = date_str
                try:
                    size = path.stat().st_size
                except OSError:
                    size = -1
                cached[path.name] = {"size": size, "date": date_str}
            self._write_exif_cache(cached)

        return results

    def _read_exif_cache(self) -> dict:
        """Load _exif_cache.json; return empty dict on any error."""
        if self._cache_dir is None:
            return {}
        cache_path = self._cache_dir / self._EXIF_CACHE_FILE
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_exif_cache(self, data: dict) -> None:
        """Persist EXIF cache dict to disk; silently ignores I/O errors."""
        if self._cache_dir is None:
            return
        try:
            self._cache_dir.mkdir(exist_ok=True)
            cache_path = self._cache_dir / self._EXIF_CACHE_FILE
            cache_path.write_text(
                json.dumps(data, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ── Thumbnail disk cache ───────────────────────────────────────────────

    def _get_thumb(self, path: Path) -> Optional[bytes]:
        """Return thumbnail bytes; hit disk cache when available."""
        try:
            file_size = path.stat().st_size
        except OSError:
            return load_thumbnail(path, _THUMB_SIZE)

        cache_file: Optional[Path] = None
        if self._cache_dir is not None:
            key        = _thumb_cache_key(str(path), file_size)
            cache_file = self._cache_dir / f"{key}.jpg"
            if cache_file.exists():
                try:
                    return cache_file.read_bytes()
                except OSError:
                    pass  # fall through to Pillow

        # Cache miss — generate with Pillow
        thumb_bytes = load_thumbnail(path, _THUMB_SIZE)

        # Persist to cache (best-effort — ignore any I/O error)
        if thumb_bytes and cache_file is not None:
            try:
                self._cache_dir.mkdir(exist_ok=True)   # type: ignore[union-attr]
                cache_file.write_bytes(thumb_bytes)
            except OSError:
                pass

        return thumb_bytes


class _ThumbnailDelegate(QStyledItemDelegate):
    """Custom delegate that draws coloured borders on flagged items.

    Priority (highest wins):
      RED    — invalid / missing EXIF date
      ORANGE — filename does not match YYYY-MM-DD-HHhMMmSSs.ext
    """

    def paint(self, painter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        super().paint(painter, option, index)
        is_inv = index.data(_ROLE_INVALID)
        is_std = index.data(_ROLE_STD_NAME)  # True=standard, False=non-standard
        if is_inv:
            painter.save()
            painter.setPen(QPen(QColor(220, 60, 60), 3))
            painter.drawRect(option.rect.adjusted(2, 2, -2, -2))
            painter.restore()
        elif is_std is False:
            painter.save()
            painter.setPen(QPen(QColor(255, 165, 0), 3))
            painter.drawRect(option.rect.adjusted(2, 2, -2, -2))
            painter.restore()


class ThumbnailGrid(QWidget):
    photo_selected = pyqtSignal(Path)
    edit_folder_date = pyqtSignal(Path)
    edit_selection_date = pyqtSignal(list)       # list[Path] — selected photos to edit
    # Emitted whenever 2+ items are selected: list of (Path, date_str) tuples
    # where date_str is the cached EXIF date already held in item data.
    multi_selection = pyqtSignal(list)
    restore_backup_requested = pyqtSignal(Path)
    photos_deleted = pyqtSignal(list)            # list[Path] — original paths of moved files
    folder_created = pyqtSignal(Path)            # new subfolder path
    read_filename_date_requested = pyqtSignal(Path)  # single photo — open editor pre-filled from filename
    folder_loaded = pyqtSignal(int)              # emitted with photo count after each folder scan

    def __init__(self, log_manager: LogManager, parent=None):
        super().__init__(parent)
        self._log = log_manager
        self._current_folder: Optional[Path] = None
        self._pixmap_cache: ThumbnailCache = ThumbnailCache(max_size=200)
        self._path_to_item: Dict[str, QListWidgetItem] = {}
        self._worker: Optional[_ThumbnailWorker] = None
        self._thread: Optional[QThread] = None
        self._pending_folder: Optional[Path] = None
        self._pending_select: Optional[str] = None   # path to select after next load
        self._sort_mode: int = _SORT_DATE            # default: by EXIF date
        self._sort_ascending: bool = True            # default: oldest first
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # List widget in icon mode
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setViewMode(QListWidget.ViewMode.IconMode)
        self._list.setIconSize(QSize(_THUMB_SIZE, _THUMB_SIZE))
        self._list.setGridSize(QSize(_ITEM_W, _ITEM_H))
        self._list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._list.setMovement(QListWidget.Movement.Static)
        self._list.setUniformItemSizes(True)
        # Batched layout: Qt lays out items in batches → smoother large-folder rendering
        self._list.setLayoutMode(QListWidget.LayoutMode.Batched)
        self._list.setBatchSize(50)
        self._list.setItemDelegate(_ThumbnailDelegate(self._list))
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.itemActivated.connect(self._on_item_clicked)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        self._list.itemDoubleClicked.connect(self._on_double_click)
        self._list.installEventFilter(self)
        layout.addWidget(self._list)

        # ── Bottom bar — two rows ──────────────────────────────────────────
        bottom = QVBoxLayout()
        bottom.setSpacing(2)
        bottom.setContentsMargins(0, 0, 0, 0)

        # Row 1: sort controls + legend + count + loading progress bar
        self._lbl_count = QLabel("0 fotos")

        # Compact progress bar (visible only during loading of large folders)
        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedHeight(14)
        self._progress_bar.setMinimumWidth(100)
        self._progress_bar.setMaximumWidth(200)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setVisible(False)

        # Sort controls
        self._sort_combo = QComboBox()
        self._sort_combo.addItems(["Fecha EXIF", "Nombre de archivo"])
        self._sort_combo.setToolTip("Criterio de ordenamiento de las fotos en el grid")
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)

        self._btn_sort_dir = QPushButton("↑ Más viejo primero")
        self._btn_sort_dir.setToolTip(
            "Mostrando del más antiguo al más reciente. Clic para invertir."
        )
        self._btn_sort_dir.clicked.connect(self._on_sort_dir_toggled)
        apply_button_style(self._btn_sort_dir)

        # "Solo sin fecha" filter checkbox — default OFF so all photos are visible.
        # User can enable to focus on photos that need date correction.
        self._chk_sin_fecha = QCheckBox("Solo sin fecha")
        self._chk_sin_fecha.setChecked(False)
        self._chk_sin_fecha.setToolTip(
            "Cuando está marcado, muestra solo las fotos sin fecha EXIF válida\n"
            "(fecha ausente o con valor incorrecto como 01/01/2000).\n"
            "Desmarcá para ver todas las fotos."
        )
        self._chk_sin_fecha.toggled.connect(self._apply_filter)

        # Border legend
        self._lbl_invalid_legend = QLabel("🔴 = fecha inválida   🟠 = nombre no estándar")
        self._lbl_invalid_legend.setStyleSheet("font-size: 10px; color: #aaaaaa; padding: 0 4px;")
        self._lbl_invalid_legend.setToolTip(
            "🔴 Borde rojo: fecha EXIF ausente o incorrecta\n"
            "   (ej: 01/01/2000). La foto aparecerá mal ordenada en Immich.\n\n"
            "🟠 Borde naranja: el nombre no sigue el formato estándar\n"
            "   YYYY-MM-DD-HHhMMmSSs.ext (ej: 2011-12-24-15h40m46s.jpg).\n"
            "   Usá 'Renombrar archivos' en el editor de fecha para corregirlo."
        )

        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.addWidget(self._chk_sin_fecha)
        row1.addWidget(self._sort_combo)
        row1.addWidget(self._btn_sort_dir)
        row1.addStretch()
        row1.addWidget(self._lbl_invalid_legend)
        row1.addWidget(self._progress_bar)
        row1.addWidget(self._lbl_count)

        # Row 2: action buttons (never clipped — use SetMinimumSize constraint)
        # Selection-edit button — only visible when 2+ items are selected
        self._btn_edit_selection = QPushButton("Editar selección")
        self._btn_edit_selection.setVisible(False)
        self._btn_edit_selection.setToolTip(
            "Abre el editor de fecha para las fotos seleccionadas.\n"
            "Podés cambiar la fecha EXIF o solo renombrar con la fecha actual."
        )
        self._btn_edit_selection.clicked.connect(self._on_edit_selection)
        apply_button_style(self._btn_edit_selection)

        self._btn_new_folder = QPushButton("📁 Nueva carpeta")
        self._btn_new_folder.setEnabled(False)
        self._btn_new_folder.setToolTip(
            "Crea una nueva subcarpeta dentro de la carpeta actual.\n"
            "También se guarda un registro legible en _historial_original.txt dentro de cada carpeta."
        )
        self._btn_new_folder.clicked.connect(self._on_new_folder)
        apply_button_style(self._btn_new_folder)

        self._btn_edit = QPushButton("Editar carpeta")
        self._btn_edit.setEnabled(False)
        self._btn_edit.setToolTip(
            "Cambia la fecha EXIF de todas las fotos de esta carpeta.\n"
            "Podés conservar la hora original o ingresar una nueva.\n"
            "También se guarda un registro legible en _historial_original.txt dentro de cada carpeta."
        )
        self._btn_edit.clicked.connect(self._on_edit_folder)
        apply_button_style(self._btn_edit)

        self._btn_restore = QPushButton("Restaurar EXIF")
        self._btn_restore.setVisible(False)
        self._btn_restore.setToolTip(
            "Revierte todos los cambios de fecha realizados en esta carpeta\n"
            "usando el backup automático creado antes de la última edición.\n"
            "También se guarda un registro legible en _historial_original.txt dentro de cada carpeta."
        )
        self._btn_restore.clicked.connect(self._on_restore_backup)
        apply_button_style(self._btn_restore)

        self._btn_refresh = QPushButton("🔄 Actualizar")
        self._btn_refresh.setEnabled(False)
        self._btn_refresh.setToolTip(
            "Vuelve a escanear la carpeta actual y recarga el grid.\n"
            "Útil si se agregaron o eliminaron fotos desde el Explorador."
        )
        self._btn_refresh.clicked.connect(self._on_refresh_folder)
        apply_button_style(self._btn_refresh)

        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        row2.addWidget(self._btn_new_folder)
        row2.addWidget(self._btn_restore)
        row2.addWidget(self._btn_edit)
        row2.addWidget(self._btn_edit_selection)
        row2.addWidget(self._btn_refresh)
        row2.addStretch()

        bottom.addLayout(row1)
        bottom.addLayout(row2)
        layout.addLayout(bottom)

    # ── Public API ─────────────────────────────────────────────────────────

    def select_after_load(self, path: Path) -> None:
        """Request that path be selected once the next folder load populates
        the skeleton items.  Works for both immediately-starting and queued loads."""
        self._pending_select = str(path)

    def on_folder_changed(self, folder: Path) -> None:
        """Slot connected to MainWindow.folder_changed signal.

        Ignores no-ops (same folder or None) so switching tabs doesn't
        trigger a redundant reload.
        """
        if not folder or folder == self._current_folder:
            return
        self.load_folder(folder)

    def load_folder(self, folder_path: Path) -> None:
        """Load thumbnails for all images in folder_path (background thread)."""
        if self._thread and self._thread.isRunning():
            # Cancel current worker and queue the new load
            if self._worker:
                self._worker.cancel()
            self._pending_folder = folder_path
            return

        self._start_load(folder_path)

    def refresh_item(self, photo_path: Path) -> None:
        """Re-read EXIF date for a single item and update its label."""
        path_str = str(photo_path)
        item = self._path_to_item.get(path_str)
        if item is None:
            return
        exif = read_exif(photo_path)
        date_str = get_best_date_str(exif["fields"])
        invalid = is_invalid_date(date_str)
        display_date = self._format_date(date_str)
        item.setText(f"{photo_path.name}\n{display_date}")
        item.setData(_ROLE_DATE, date_str)
        item.setData(_ROLE_INVALID, invalid)

    # ── Filter ────────────────────────────────────────────────────────────

    def _apply_filter(self) -> None:
        """Show/hide items based on the 'Solo sin fecha' checkbox.

        "Sin fecha" = date_str is empty OR is_invalid (e.g. 2000-01-01).
        Items whose EXIF data hasn't loaded yet have date_str="" which reads
        as sin fecha — they stay visible until real data arrives.
        """
        sin_fecha_only = self._chk_sin_fecha.isChecked()
        visible = 0
        for i in range(self._list.count()):
            item = self._list.item(i)
            if sin_fecha_only:
                date_str = item.data(_ROLE_DATE) or ""
                is_inv   = bool(item.data(_ROLE_INVALID))
                show     = not date_str or is_inv   # show only invalid/missing dates
            else:
                show = True
            item.setHidden(not show)
            if show:
                visible += 1
        self._lbl_count.setText(f"{visible} fotos")

    # ── Sort controls ──────────────────────────────────────────────────────

    def _on_sort_changed(self, index: int) -> None:
        self._sort_mode = index
        self._update_sort_dir_button()
        self._apply_sort()

    def _on_sort_dir_toggled(self) -> None:
        self._sort_ascending = not self._sort_ascending
        self._update_sort_dir_button()
        self._apply_sort()

    def _update_sort_dir_button(self) -> None:
        """Sync button label and tooltip to current sort mode + direction."""
        if self._sort_mode == _SORT_DATE:
            if self._sort_ascending:
                self._btn_sort_dir.setText("↑ Más viejo primero")
                self._btn_sort_dir.setToolTip(
                    "Mostrando del más antiguo al más reciente. Clic para invertir."
                )
            else:
                self._btn_sort_dir.setText("↓ Más reciente primero")
                self._btn_sort_dir.setToolTip(
                    "Mostrando del más reciente al más antiguo. Clic para invertir."
                )
        else:  # _SORT_NAME
            if self._sort_ascending:
                self._btn_sort_dir.setText("↑ A → Z")
                self._btn_sort_dir.setToolTip(
                    "Mostrando del más antiguo al más reciente. Clic para invertir."
                )
            else:
                self._btn_sort_dir.setText("↓ Z → A")
                self._btn_sort_dir.setToolTip(
                    "Mostrando del más reciente al más antiguo. Clic para invertir."
                )

    def _apply_sort(self) -> None:
        """Re-sort QListWidget items in-place using cached item data (no disk I/O)."""
        n = self._list.count()
        if n == 0:
            return

        current_item = self._list.currentItem()

        pairs: List[tuple] = []
        for i in range(n):
            item = self._list.item(i)
            path_str = item.data(_ROLE_PATH) or ""
            key = self._sort_key_for_item(path_str, item)
            pairs.append((key, item))

        pairs.sort(key=lambda x: x[0], reverse=not self._sort_ascending)

        # Rebuild list: remove from the END (O(1) each) to avoid O(n²) shifting,
        # then re-add in sorted order under setUpdatesEnabled(False) for one repaint.
        self._list.setUpdatesEnabled(False)
        count = self._list.count()
        for i in range(count - 1, -1, -1):
            self._list.takeItem(i)
        for _, item in pairs:
            self._list.addItem(item)
        self._list.setUpdatesEnabled(True)
        self._list.update()

        if current_item is not None:
            self._list.setCurrentItem(current_item)
            self._list.scrollToItem(current_item)

    def _sort_key_for_item(self, path_str: str, item: QListWidgetItem) -> str:
        """Sort key derived from cached item data — no filesystem access."""
        if self._sort_mode == _SORT_NAME:
            return path_str.lower() if path_str else "\xff"
        # Date mode: use the cached EXIF/mtime date already stored in the item
        date_str = item.data(_ROLE_DATE)
        if date_str:
            return date_str
        # Fallback to mtime for items still loading
        if path_str:
            try:
                mtime = Path(path_str).stat().st_mtime
                return datetime.fromtimestamp(mtime).strftime("%Y:%m:%d %H:%M:%S")
            except OSError:
                pass
        return "\xff"

    def _sort_paths(self, images: List[Path]) -> List[Path]:
        """Sort a Path list by EXIF date with mtime fallback (reads disk — only for
        on-demand use; normal load path uses _apply_sort from cached item data)."""
        if self._sort_mode == _SORT_NAME:
            return sorted(images, key=lambda p: p.name.lower(),
                          reverse=not self._sort_ascending)

        def _date_key(p: Path) -> str:
            date_str = get_best_date_str(read_exif(p)["fields"])
            if date_str:
                return date_str
            try:
                return datetime.fromtimestamp(
                    p.stat().st_mtime
                ).strftime("%Y:%m:%d %H:%M:%S")
            except OSError:
                return "\xff"

        return sorted(images, key=_date_key, reverse=not self._sort_ascending)

    # ── Internal ───────────────────────────────────────────────────────────

    def _start_load(self, folder_path: Path) -> None:
        self._current_folder = folder_path
        self._list.clear()
        self._pixmap_cache.clear()
        self._path_to_item.clear()

        images = scan_folder(folder_path)
        count  = len(images)

        self._lbl_count.setText(f"{count} fotos")
        self.folder_loaded.emit(count)
        self._btn_new_folder.setEnabled(True)
        self._btn_refresh.setEnabled(True)
        self._btn_edit.setEnabled(count > 0)
        self._btn_restore.setVisible(has_backup(folder_path))

        if not images:
            self._pending_select = None
            self._progress_bar.setVisible(False)
            return

        # ── Phase 1 (main thread, fast): add skeleton items sorted by filename.
        # No EXIF reads, no Pillow — this is nearly instant even for 2000 files.
        images.sort(key=lambda p: p.name.lower())  # stable filename order for now

        self._list.setUpdatesEnabled(False)
        for path in images:
            item = self._make_skeleton_item(path)
            # Seed _ROLE_DATE with mtime so the grid can sort before EXIF arrives
            try:
                mtime = path.stat().st_mtime
                mtime_str = datetime.fromtimestamp(mtime).strftime("%Y:%m:%d %H:%M:%S")
            except OSError:
                mtime_str = ""
            item.setData(_ROLE_DATE, mtime_str)
            self._list.addItem(item)
            self._path_to_item[str(path)] = item
        self._list.setUpdatesEnabled(True)

        # Apply initial sort using seeded mtime values (fast, no I/O)
        self._apply_sort()

        # Apply any pending re-selection (set by select_after_load before load_folder)
        if self._pending_select:
            sel_item = self._path_to_item.get(self._pending_select)
            if sel_item is not None:
                self._list.setCurrentItem(sel_item)
                self._list.scrollToItem(sel_item)
            self._pending_select = None

        # Show progress bar for large folders
        if count >= _PROGRESS_THRESHOLD:
            self._progress_bar.setRange(0, count)
            self._progress_bar.setValue(0)
            self._progress_bar.setVisible(True)

        # ── Phase 2 (background): batch EXIF dates + thumbnail loading ──────
        cache_dir = folder_path / "_thumbcache"

        self._worker = _ThumbnailWorker(images, cache_dir)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.thumbnails_batch_ready.connect(self._on_thumbnails_batch_ready)
        self._worker.progress.connect(self._on_load_progress)
        # Capture this specific worker/thread pair in the closure so that a
        # new load starting before the queued finished-signal fires cannot
        # accidentally kill the new load (cross-thread queued-signal race).
        _w, _t = self._worker, self._thread
        self._worker.finished.connect(lambda: self._on_worker_finished_for(_w, _t))
        self._thread.start()

    def _make_skeleton_item(self, path: Path) -> QListWidgetItem:
        item = QListWidgetItem(path.name)
        item.setData(_ROLE_PATH, str(path))
        item.setData(_ROLE_DATE, "")
        item.setData(_ROLE_INVALID, False)
        item.setData(_ROLE_STD_NAME, bool(_STANDARD_NAME_RE.match(path.name)))
        item.setSizeHint(QSize(_ITEM_W, _ITEM_H))
        return item

    def _on_thumbnails_batch_ready(self, batch: list) -> None:
        """Handle a batch of up to 20 thumbnail updates from the background worker.

        Running on the MAIN thread (queued connection) — safe to create QPixmap.
        Wrapping the batch in setUpdatesEnabled(False/True) cuts repaint calls
        from N → 1 per batch, keeping the UI responsive at 5000+ photos.
        """
        from PyQt6.QtGui import QPainter, QIcon

        self._list.setUpdatesEnabled(False)
        try:
            for path_str, thumb_bytes, date_str, is_inv in batch:
                item = self._path_to_item.get(path_str)
                if item is None:
                    continue

                if thumb_bytes:
                    src = QPixmap()
                    src.loadFromData(thumb_bytes)
                    if not src.isNull():
                        icon_pixmap = QPixmap(_THUMB_SIZE, _THUMB_SIZE)
                        icon_pixmap.fill(QColor(45, 45, 50))
                        painter = QPainter(icon_pixmap)
                        x = (_THUMB_SIZE - src.width()) // 2
                        y = (_THUMB_SIZE - src.height()) // 2
                        painter.drawPixmap(x, y, src)
                        painter.end()
                        item.setIcon(QIcon(icon_pixmap))
                        self._pixmap_cache.put(path_str, icon_pixmap)

                display_date = self._format_date(date_str)
                item.setText(f"{Path(path_str).name}\n{display_date}")
                item.setData(_ROLE_DATE, date_str)
                item.setData(_ROLE_INVALID, is_inv)

                if not date_str or is_inv:
                    item.setForeground(QBrush(QColor(220, 80, 80)))
                else:
                    item.setForeground(QBrush(QColor(220, 220, 225)))
        finally:
            self._list.setUpdatesEnabled(True)
            self._list.update()
        # Apply filter after each batch so items with valid dates hide immediately
        self._apply_filter()

    def _on_load_progress(self, current: int, total: int) -> None:
        """Update the progress bar during Phase 2 loading.

        The count label is NOT overwritten here — it already shows the correct
        "N fotos" text set in _start_load, and keeping it stable avoids the
        misleading "Cargando… X/Y" text appearing even when all thumbnails are
        loaded instantly from the disk cache.
        """
        if self._progress_bar.isVisible():
            self._progress_bar.setValue(current)

    def _on_worker_finished_for(self, my_worker, my_thread) -> None:
        """Slot called when a specific worker/thread pair finishes.

        Using a closure-captured pair (rather than reading self._worker /
        self._thread at call time) prevents the cross-thread queued-signal
        race where a new load starts between the worker emitting finished and
        this slot firing on the main thread — which would otherwise cause
        this slot to quit() the NEW thread instead of the old one.
        """
        # Only clear the instance vars if they still point to OUR objects.
        # If a new load has already started they will point to new objects;
        # leave them alone so the new load is not disrupted.
        if self._worker is my_worker:
            self._worker = None
        if self._thread is my_thread:
            self._thread = None

        # Clean up the finished thread (safe even if the slot fires late).
        if my_thread is not None:
            my_thread.quit()
            my_thread.wait()       # brief — run() already returned
            if my_worker is not None:
                my_worker.deleteLater()
            my_thread.deleteLater()

        # Only update UI / trigger pending loads when OUR thread was the
        # active one (i.e. no new load has superseded us).
        if self._worker is not None or self._thread is not None:
            # A newer load is already running — skip UI finalisation here;
            # _on_worker_finished_for will be called again for that load.
            return

        # All EXIF dates are now cached in item data → re-sort by EXIF date if needed
        if self._sort_mode == _SORT_DATE:
            self._apply_sort()

        # Group problem items at the top: red (invalid EXIF) → orange (non-standard
        # name) → normal.  Applied once after load so problem photos are always
        # immediately visible without scrolling.
        self._group_problem_items()

        # Finalise UI: apply filter (sets count label to visible items)
        self._progress_bar.setVisible(False)
        self._apply_filter()

        # Process any folder load that arrived while we were busy
        if self._pending_folder:
            pending = self._pending_folder
            self._pending_folder = None
            self._start_load(pending)

    def _group_problem_items(self) -> None:
        """Reorder list items so problem photos are always at the top.

        Groups (in order):
          1. RED   — invalid / missing EXIF date  (_ROLE_INVALID is True)
          2. ORANGE — non-standard filename         (_ROLE_STD_NAME is False)
          3. NORMAL — both date valid and name OK

        Within each group the current sort order is preserved.
        No-op when there are no red or orange items.
        """
        n = self._list.count()
        if n == 0:
            return

        red_items: list = []
        orange_items: list = []
        normal_items: list = []

        for i in range(n):
            item = self._list.item(i)
            is_inv = bool(item.data(_ROLE_INVALID))
            is_std = item.data(_ROLE_STD_NAME)   # True=standard, False=non-standard
            if is_inv:
                red_items.append(item)
            elif is_std is False:
                orange_items.append(item)
            else:
                normal_items.append(item)

        if not red_items and not orange_items:
            return  # nothing to reorder — avoid a pointless full rebuild

        sorted_items = red_items + orange_items + normal_items

        self._list.setUpdatesEnabled(False)
        # Remove from end to avoid O(n²) index shifting
        for i in range(n - 1, -1, -1):
            self._list.takeItem(i)
        for item in sorted_items:
            self._list.addItem(item)
        self._list.setUpdatesEnabled(True)
        self._list.update()

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        path_str = item.data(_ROLE_PATH)
        if path_str:
            self.photo_selected.emit(Path(path_str))

    def _on_double_click(self, item: QListWidgetItem) -> None:
        """Open photo in the system's default viewer on double-click."""
        import os
        path_str = item.data(_ROLE_PATH)
        if path_str:
            path = Path(path_str)
            if path.exists():
                os.startfile(str(path))

    def _on_selection_changed(self) -> None:
        """Show/hide the selection-edit button and notify the detail panel."""
        selected = self._list.selectedItems()
        count = len(selected)
        self._btn_edit_selection.setVisible(count >= 2)
        if count >= 2:
            self._btn_edit_selection.setText(
                f"Editar selección ({count})"
            )
            # Build (Path, date_str) pairs from cached item data — no disk access
            pairs = []
            for item in selected:
                path_str = item.data(_ROLE_PATH) or ""
                date_str = item.data(_ROLE_DATE) or ""
                if path_str:
                    pairs.append((Path(path_str), date_str))
            self.multi_selection.emit(pairs)

    def _on_edit_selection(self) -> None:
        """Emit edit_selection_date with the current multi-selection."""
        paths = self._get_selected_paths()
        if len(paths) >= 2:
            self.edit_selection_date.emit(paths)

    def _on_edit_folder(self) -> None:
        if self._current_folder:
            self.edit_folder_date.emit(self._current_folder)

    def _on_restore_backup(self) -> None:
        if self._current_folder:
            self.restore_backup_requested.emit(self._current_folder)

    def _on_refresh_folder(self) -> None:
        """Reload the current folder from disk (useful after external changes)."""
        if self._current_folder:
            self.load_folder(self._current_folder)

    # ── Deletion ───────────────────────────────────────────────────────────

    def _get_selected_paths(self) -> List[Path]:
        paths = []
        for item in self._list.selectedItems():
            path_str = item.data(_ROLE_PATH)
            if path_str:
                paths.append(Path(path_str))
        return paths

    def _on_context_menu(self, pos) -> None:
        selected = self._get_selected_paths()
        if not selected:
            return
        n = len(selected)
        menu = QMenu(self)

        if n == 1:
            # Single photo: date edit, filename-date prefill, open in Windows
            act_edit_single = QAction("📅 Editar fecha de esta foto", self)
            act_edit_single.setToolTip(
                "Abre el editor de fecha para modificar el EXIF de esta foto."
            )
            act_edit_single.triggered.connect(
                lambda: self.edit_selection_date.emit([selected[0]])
            )
            menu.addAction(act_edit_single)

            act_fn = QAction("📋 Leer fecha del nombre", self)
            act_fn.setToolTip(
                "Abre el editor de fecha con los controles pre-rellenados\n"
                "a partir de la fecha detectada en el nombre del archivo."
            )
            act_fn.triggered.connect(
                lambda: self.read_filename_date_requested.emit(selected[0])
            )
            menu.addAction(act_fn)

            menu.addSeparator()

            act_open = QAction("🖼 Abrir en Windows", self)
            act_open.setToolTip("Abre la foto con el visor predeterminado de Windows.")
            act_open.triggered.connect(
                lambda checked, p=selected[0]: os.startfile(str(p))
            )
            menu.addAction(act_open)

        else:
            # 2+ photos: batch date edit
            lbl_edit = f"📅 Editar fecha de seleccionadas ({n} fotos)"
            act_edit = QAction(lbl_edit, self)
            act_edit.setToolTip(
                "Abre el editor de fecha para las fotos seleccionadas.\n"
                "Podés cambiar la fecha EXIF o solo renombrar con la fecha actual."
            )
            act_edit.triggered.connect(lambda: self.edit_selection_date.emit(list(selected)))
            menu.addAction(act_edit)

        menu.addSeparator()

        # Move / Copy — always available
        act_move = QAction("📁 Mover a carpeta…", self)
        act_move.setToolTip("Mueve las fotos seleccionadas a otra carpeta.")
        act_move.triggered.connect(lambda: self._prompt_move(list(selected)))
        menu.addAction(act_move)

        act_copy = QAction("📋 Copiar a carpeta…", self)
        act_copy.setToolTip("Copia las fotos seleccionadas a otra carpeta.")
        act_copy.triggered.connect(lambda: self._prompt_copy(list(selected)))
        menu.addAction(act_copy)

        menu.addSeparator()

        lbl_del = f"🗑 Eliminar ({n} foto{'s' if n != 1 else ''})"
        act_del = QAction(lbl_del, self)
        act_del.setToolTip(f"Mueve las fotos seleccionadas a la carpeta _{_TRASH_DIRNAME}.")
        act_del.triggered.connect(lambda: self._confirm_and_delete(list(selected)))
        menu.addAction(act_del)

        menu.addSeparator()

        act_refresh = QAction("🔄 Actualizar carpeta", self)
        act_refresh.setToolTip(
            "Recarga las fotos de la carpeta actual desde el disco. "
            "Útil después de cambios externos."
        )
        act_refresh.triggered.connect(self._on_refresh_folder)
        menu.addAction(act_refresh)

        menu.exec(self._list.viewport().mapToGlobal(pos))

    def _prompt_move(self, paths: List[Path]) -> None:
        """Ask for a destination folder then move all paths there."""
        if not paths:
            return
        dest_str = QFileDialog.getExistingDirectory(
            self, "Mover a carpeta…",
            str(self._current_folder or ""),
            QFileDialog.Option.DontUseNativeDialog,
        )
        if dest_str:
            self._move_files(paths, Path(dest_str))

    def _prompt_copy(self, paths: List[Path]) -> None:
        """Ask for a destination folder then copy all paths there."""
        if not paths:
            return
        dest_str = QFileDialog.getExistingDirectory(
            self, "Copiar a carpeta…",
            str(self._current_folder or ""),
            QFileDialog.Option.DontUseNativeDialog,
        )
        if dest_str:
            self._copy_files(paths, Path(dest_str))

    def _move_files(self, paths: List[Path], dest: Path) -> None:
        """Move files to dest, log each operation, remove items from grid."""
        moved: List[Path] = []
        errors: List[str] = []
        for path in paths:
            if path.parent == dest:
                continue
            try:
                dst_file = unique_dest(path, dest)
                original_exif = read_exif(path)["fields"]
                append_historial(path.parent, path.name, "movido", original_exif)
                shutil.move(str(path), str(dst_file))
                self._log.log(str(path.parent), path.name, "move", str(path), str(dst_file))
                moved.append(path)
            except Exception as e:
                errors.append(f"{path.name}: {e}")

        if errors:
            mb_warning(self, "Errores al mover", "\n".join(errors[:10]))

        for path in moved:
            path_str = str(path)
            item = self._path_to_item.pop(path_str, None)
            if item is not None:
                row = self._list.row(item)
                if row >= 0:
                    self._list.takeItem(row)
            self._pixmap_cache.pop(path_str, None)

        remaining = self._list.count()
        self._btn_edit.setEnabled(remaining > 0)
        self._apply_filter()
        if moved:
            self.photos_deleted.emit(moved)

    def _copy_files(self, paths: List[Path], dest: Path) -> None:
        """Copy files to dest and log each operation (grid items unchanged)."""
        errors: List[str] = []
        for path in paths:
            try:
                dst_file = unique_dest(path, dest)
                shutil.copy2(str(path), str(dst_file))
                self._log.log(str(path.parent), path.name, "copy", str(path), str(dst_file))
            except Exception as e:
                errors.append(f"{path.name}: {e}")

        if errors:
            mb_warning(self, "Errores al copiar", "\n".join(errors[:10]))

    def _confirm_and_delete(self, paths: List[Path]) -> None:
        if not paths or not self._current_folder:
            return

        # Build confirmation message — show up to 5 names
        names = [p.name for p in paths]
        shown = names[:5]
        name_list = "\n".join(f"  • {n}" for n in shown)
        extra = len(names) - 5
        if extra > 0:
            name_list += f"\n  … y {extra} más"

        n = len(paths)
        msg = QMessageBox(self)
        msg.setWindowTitle("Confirmar eliminación")
        msg.setText(f"¿Mover {n} foto{'s' if n != 1 else ''} a la carpeta _{_TRASH_DIRNAME}?")
        msg.setInformativeText(name_list)
        btn_move = msg.addButton("Mover", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        msg.exec()
        if msg.clickedButton() is not btn_move:
            return

        trash_dir = self._current_folder / _TRASH_DIRNAME
        moved: List[Path] = []
        errors: List[str] = []

        for path in paths:
            try:
                trash_dir.mkdir(exist_ok=True)
                dest = unique_dest(path, trash_dir)
                # Log before moving — captures current state
                original_exif = read_exif(path)["fields"]
                append_historial(path.parent, path.name, "eliminado", original_exif)
                shutil.move(str(path), str(dest))
                self._log.log(str(path.parent), path.name, "delete", str(path), str(dest))
                moved.append(path)
            except Exception as e:
                errors.append(f"{path.name}: {e}")

        # Remove moved items from grid
        for path in moved:
            path_str = str(path)
            item = self._path_to_item.pop(path_str, None)
            if item is not None:
                row = self._list.row(item)
                if row >= 0:
                    self._list.takeItem(row)
            self._pixmap_cache.pop(path_str, None)

        # Refresh count (respects active filter)
        remaining = self._list.count()
        self._btn_edit.setEnabled(remaining > 0)
        self._apply_filter()

        if errors:
            mb_warning(
                self, "Errores al mover",
                "\n".join(errors[:10]),
            )

        if moved:
            self.photos_deleted.emit(moved)

    # ── New folder ────────────────────────────────────────────────────────

    def _on_new_folder(self) -> None:
        if not self._current_folder:
            return
        name, ok = QInputDialog.getText(
            self, "Nueva carpeta", "Nombre de la nueva carpeta:"
        )
        if not ok:
            return
        name = name.strip()
        if not name:
            mb_warning(self, "Nombre vacío", "El nombre no puede estar vacío.")
            return
        if any(c in _ILLEGAL_NAME_CHARS for c in name):
            mb_warning(
                self, "Nombre inválido",
                'El nombre contiene caracteres no permitidos:\n\\ / : * ? " < > |',
            )
            return
        new_path = self._current_folder / name
        try:
            new_path.mkdir(exist_ok=False)
        except FileExistsError:
            mb_warning(
                self, "Ya existe", f"Ya existe una carpeta con el nombre '{name}'."
            )
            return
        except OSError as e:
            mb_warning(self, "Error al crear carpeta", str(e))
            return
        self.folder_created.emit(new_path)

    # ── Event filter (Delete key on list widget) ───────────────────────────

    def eventFilter(self, obj, event) -> bool:
        if obj is self._list and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Delete:
                selected = self._get_selected_paths()
                if selected:
                    self._confirm_and_delete(selected)
                return True
        return super().eventFilter(obj, event)

    @staticmethod
    def _format_date(date_str: str) -> str:
        if not date_str:
            return "Sin fecha"
        # date_str is "YYYY:MM:DD HH:MM:SS"
        try:
            parts = date_str.split(" ")
            d = parts[0].replace(":", "/")
            t = parts[1][:5] if len(parts) > 1 else ""
            return f"{d}  {t}"
        except Exception:
            return date_str

    # ── Keyboard navigation ────────────────────────────────────────────────

    def keyPressEvent(self, event) -> None:
        key = event.key()
        current = self._list.currentRow()
        count = self._list.count()
        if key == Qt.Key.Key_Right and current < count - 1:
            self._list.setCurrentRow(current + 1)
            self._on_item_clicked(self._list.currentItem())
        elif key == Qt.Key.Key_Left and current > 0:
            self._list.setCurrentRow(current - 1)
            self._on_item_clicked(self._list.currentItem())
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            cols = max(1, self._list.viewport().width() // _ITEM_W)
            if key == Qt.Key.Key_Down:
                new_row = min(current + cols, count - 1)
            else:
                new_row = max(current - cols, 0)
            self._list.setCurrentRow(new_row)
            self._on_item_clicked(self._list.currentItem())
        else:
            super().keyPressEvent(event)
