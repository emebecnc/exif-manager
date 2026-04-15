"""Video tab: grid + detail panel driven by MainWindow's shared folder tree."""
import hashlib
import os
import re
import shutil
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import (
    Qt, pyqtSignal, QObject, QThread, QSize, QMimeData, QUrl,
    QRect,
)
from PyQt6.QtGui import (
    QPixmap, QColor, QPen, QBrush, QAction, QDrag,
    QFont, QPainter, QIcon,
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QPushButton,
    QLabel, QComboBox, QCheckBox, QListWidget, QListWidgetItem, QAbstractItemView,
    QStyledItemDelegate, QStyleOptionViewItem, QApplication,
    QFileDialog, QMenu, QMessageBox, QInputDialog, QProgressBar,
    QLayout, QFrame,
)
from PyQt6.QtCore import QModelIndex

from core.video_handler import (
    get_video_metadata, get_best_date, is_invalid_date,
    get_video_thumbnail, scan_video_folder, format_duration, format_size,
    make_dated_filename, compute_md5, VIDEO_EXTENSIONS,
    has_video_backup, restore_video_backup,
)
from core.file_scanner import unique_dest, EXCLUDED_FOLDERS
from core.backup_manager import append_historial
from ui.log_viewer import LogManager
from ui.styles import apply_button_style, apply_primary_button_style, mb_warning, mb_info, mb_question
from ui.video_detail import VideoDetailPanel

# ── Item data roles ───────────────────────────────────────────────────────────
_ROLE_PATH     = Qt.ItemDataRole.UserRole
_ROLE_DATE     = Qt.ItemDataRole.UserRole + 1
_ROLE_INVALID  = Qt.ItemDataRole.UserRole + 2
_ROLE_DURATION = Qt.ItemDataRole.UserRole + 3
_ROLE_SIZE     = Qt.ItemDataRole.UserRole + 4
_ROLE_STD_NAME = Qt.ItemDataRole.UserRole + 5  # True=standard name, False=non-standard

# Standard filename pattern: YYYY-MM-DD-HHhMMmSSs.ext  (e.g. 2007-09-29-02h47m07s.mp4)
_STANDARD_NAME_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}-\d{2}h\d{2}m\d{2}s(_\d+)?\..+$',
    re.IGNORECASE,
)

# Sort modes
_SORT_DATE     = 0
_SORT_NAME     = 1
_SORT_DURATION = 2
_SORT_SIZE     = 3

_THUMB_SIZE    = 150
_ITEM_W        = 185
_ITEM_H        = 240
_TRASH_DIRNAME = "_eliminados"
_PROGRESS_THRESHOLD = 50

_ILLEGAL_NAME_CHARS = frozenset('\\ / : * ? " < > |'.split())


class ThumbnailCache:
    """LRU in-memory cache for decoded QPixmap thumbnails (max_size entries)."""

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
            self._cache.popitem(last=False)

    def pop(self, key: str, default=None):
        return self._cache.pop(key, default)

    def clear(self) -> None:
        self._cache.clear()


def _thumb_cache_key(path_str: str, mtime: float) -> str:
    data = f"{path_str}|{mtime:.6f}".encode("utf-8")
    return hashlib.md5(data).hexdigest()


# ── Background thumbnail worker ───────────────────────────────────────────────

class _VideoThumbnailWorker(QObject):
    """
    Two-phase background worker for videos.

    Phase 1 (main thread): skeleton items are added with mtime-based date seeds
                           and basic file stats. Very fast — no subprocess calls.
    Phase 2 (background):  reads full video metadata + extracts first frame via
                           ffmpeg. Emits per-item updates.
    """
    # Batch signal: list of (path_str, thumb_bytes|None, dt|None, duration, size)
    # Emitted every 20 items to reduce cross-thread round-trips.
    items_batch_ready = pyqtSignal(list)
    progress          = pyqtSignal(int, int)
    finished          = pyqtSignal()

    def __init__(self, paths: List[Path], cache_dir: Optional[Path],
                 ffmpeg_available: bool):
        super().__init__()
        self._paths           = paths
        self._cache_dir       = cache_dir
        self._ffmpeg_available = ffmpeg_available
        self._cancelled       = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        total = len(self._paths)
        batch: list = []
        for i, path in enumerate(self._paths):
            if self._cancelled:
                break
            self.progress.emit(i + 1, total)

            meta        = get_video_metadata(path)
            best_dt     = get_best_date(meta)
            duration    = meta.get("duration_seconds", 0.0) or 0.0
            size        = meta.get("size_bytes", 0) or 0
            thumb_bytes = self._get_thumb(path)

            batch.append((str(path), thumb_bytes, best_dt, duration, size))
            if len(batch) == 20:
                self.items_batch_ready.emit(batch)
                batch = []

        if batch:
            self.items_batch_ready.emit(batch)

        self.finished.emit()

    def _get_thumb(self, path: Path) -> Optional[bytes]:
        if not self._ffmpeg_available:
            return None
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return get_video_thumbnail(path, _THUMB_SIZE)

        cache_file: Optional[Path] = None
        if self._cache_dir is not None:
            key        = _thumb_cache_key(str(path), mtime)
            cache_file = self._cache_dir / f"{key}.jpg"
            if cache_file.exists():
                try:
                    return cache_file.read_bytes()
                except OSError:
                    pass

        thumb_bytes = get_video_thumbnail(path, _THUMB_SIZE)

        if thumb_bytes and cache_file is not None:
            try:
                self._cache_dir.mkdir(exist_ok=True)
                cache_file.write_bytes(thumb_bytes)
            except OSError:
                pass

        return thumb_bytes


# ── Item delegate ─────────────────────────────────────────────────────────────

class _VideoDelegate(QStyledItemDelegate):
    """Draws coloured borders on flagged items + 🎬 badge + duration badge.

    Priority (highest wins):
      RED    — invalid / missing metadata date
      ORANGE — filename does not match YYYY-MM-DD-HHhMMmSSs.ext
    """

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex) -> None:
        super().paint(painter, option, index)
        rect = option.rect

        # Coloured border: red (invalid date) takes priority over orange (non-standard name)
        is_inv = index.data(_ROLE_INVALID)
        is_std = index.data(_ROLE_STD_NAME)  # True=standard, False=non-standard
        if is_inv:
            painter.save()
            painter.setPen(QPen(QColor(220, 60, 60), 3))
            painter.drawRect(rect.adjusted(2, 2, -2, -2))
            painter.restore()
        elif is_std is False:
            painter.save()
            painter.setPen(QPen(QColor(255, 165, 0), 3))
            painter.drawRect(rect.adjusted(2, 2, -2, -2))
            painter.restore()

        # 🎬 badge — top-right corner
        painter.save()
        badge_r = QRect(rect.right() - 26, rect.top() + 4, 22, 18)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 160))
        painter.drawRoundedRect(badge_r, 3, 3)
        painter.setPen(QColor(255, 255, 255))
        f = QFont()
        f.setPointSize(9)
        painter.setFont(f)
        painter.drawText(badge_r, Qt.AlignmentFlag.AlignCenter, "🎬")
        painter.restore()

        # Duration badge — bottom-left
        dur_secs = index.data(_ROLE_DURATION) or 0.0
        if dur_secs:
            dur_str = format_duration(dur_secs)
            painter.save()
            dur_r = QRect(rect.left() + 4, rect.bottom() - 24, 68, 18)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 170))
            painter.drawRoundedRect(dur_r, 3, 3)
            painter.setPen(QColor(220, 220, 220))
            f2 = QFont()
            f2.setPointSize(8)
            painter.setFont(f2)
            painter.drawText(dur_r, Qt.AlignmentFlag.AlignCenter, dur_str)
            painter.restore()


# ── Draggable list ────────────────────────────────────────────────────────────

class _VideoDraggableList(QListWidget):
    """QListWidget that produces file-URL drag payloads for video items."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.viewport().setAcceptDrops(False)

    def startDrag(self, supported_actions) -> None:
        selected = self.selectedItems()
        if not selected:
            return
        mime = QMimeData()
        mime.setUrls([
            QUrl.fromLocalFile(item.data(_ROLE_PATH))
            for item in selected
            if item.data(_ROLE_PATH)
        ])
        pixmap = selected[0].icon().pixmap(64, 64)
        if pixmap.isNull():
            pixmap = QPixmap(64, 64)
            pixmap.fill(QColor(60, 40, 80))

        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(pixmap)
        drag.setHotSpot(pixmap.rect().center())
        drag.exec(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction)


# ── Video Grid ────────────────────────────────────────────────────────────────

class VideoGrid(QWidget):
    """Center panel: video thumbnail grid with background loading."""

    video_selected               = pyqtSignal(Path)
    edit_video_date              = pyqtSignal(Path)
    edit_selection_date          = pyqtSignal(list)   # list[Path]
    multi_selection              = pyqtSignal(list)   # list[(Path, dt_str)]
    videos_deleted               = pyqtSignal(list)   # list[Path] moved to trash
    folder_created               = pyqtSignal(Path)
    folder_loaded                = pyqtSignal(int)
    read_filename_date_requested = pyqtSignal(Path)
    restore_backup_requested     = pyqtSignal(Path)   # folder to restore video backup for

    def __init__(self, log_manager: LogManager, ffmpeg_available: bool = True,
                 parent=None):
        super().__init__(parent)
        self._log              = log_manager
        self._ffmpeg_available = ffmpeg_available
        self._current_folder: Optional[Path] = None
        self._pixmap_cache: ThumbnailCache = ThumbnailCache(max_size=200)
        self._path_to_item: Dict[str, QListWidgetItem] = {}
        self._worker: Optional[_VideoThumbnailWorker] = None
        self._thread: Optional[QThread] = None
        self._pending_folder: Optional[Path] = None
        self._pending_select: Optional[str] = None
        self._sort_mode: int = _SORT_DATE
        self._sort_ascending: bool = True
        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self._list = _VideoDraggableList()
        self._list.setViewMode(QListWidget.ViewMode.IconMode)
        self._list.setIconSize(QSize(_THUMB_SIZE, _THUMB_SIZE))
        self._list.setGridSize(QSize(_ITEM_W, _ITEM_H))
        self._list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._list.setMovement(QListWidget.Movement.Static)
        self._list.setUniformItemSizes(True)
        self._list.setLayoutMode(QListWidget.LayoutMode.Batched)
        self._list.setBatchSize(30)
        self._list.setItemDelegate(_VideoDelegate(self._list))
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.itemActivated.connect(self._on_item_clicked)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        self._list.itemDoubleClicked.connect(self._on_double_click)
        self._list.installEventFilter(self)
        layout.addWidget(self._list)

        # ── Bottom bar ────────────────────────────────────────────────────
        bottom = QVBoxLayout()
        bottom.setSpacing(2)
        bottom.setContentsMargins(0, 0, 0, 0)

        self._lbl_count = QLabel("0 videos")

        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedHeight(14)
        self._progress_bar.setMinimumWidth(100)
        self._progress_bar.setMaximumWidth(200)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setVisible(False)

        # "Solo sin fecha" filter — default OFF so all videos are visible
        self._chk_sin_fecha = QCheckBox("Solo sin fecha")
        self._chk_sin_fecha.setChecked(False)
        self._chk_sin_fecha.setToolTip(
            "Cuando está marcado, muestra solo los videos sin fecha de metadata válida.\n"
            "Desmarcá para ver todos los videos."
        )
        self._chk_sin_fecha.toggled.connect(self._apply_filter)

        self._sort_combo = QComboBox()
        self._sort_combo.addItems(["Fecha EXIF", "Nombre", "Duración", "Tamaño"])
        self._sort_combo.setToolTip("Criterio de ordenamiento de los videos")
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)

        self._btn_sort_dir = QPushButton("↑ Más viejo primero")
        self._btn_sort_dir.clicked.connect(self._on_sort_dir_toggled)
        apply_button_style(self._btn_sort_dir)

        self._lbl_invalid_legend = QLabel("🔴 = fecha inválida   🟠 = nombre no estándar")
        self._lbl_invalid_legend.setStyleSheet(
            "font-size: 10px; color: #aaaaaa; padding: 0 4px;"
        )
        self._lbl_invalid_legend.setToolTip(
            "🔴 Borde rojo: fecha de metadata ausente o incorrecta.\n\n"
            "🟠 Borde naranja: el nombre no sigue el formato estándar\n"
            "   YYYY-MM-DD-HHhMMmSSs.ext (ej: 2007-09-29-02h47m07s.mp4).\n"
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

        # Row 2: action buttons — identical order to photo grid
        self._btn_edit_selection = QPushButton("Editar selección")
        self._btn_edit_selection.setVisible(False)
        self._btn_edit_selection.clicked.connect(self._on_edit_selection)
        apply_button_style(self._btn_edit_selection)

        self._btn_new_folder = QPushButton("📁 Nueva carpeta")
        self._btn_new_folder.setEnabled(False)
        self._btn_new_folder.setToolTip(
            "Crea una nueva subcarpeta dentro de la carpeta actual."
        )
        self._btn_new_folder.clicked.connect(self._on_new_folder)
        apply_button_style(self._btn_new_folder)

        self._btn_restore = QPushButton("Restaurar EXIF")
        self._btn_restore.setVisible(False)
        self._btn_restore.setToolTip(
            "Revierte todos los cambios de fecha realizados en esta carpeta\n"
            "usando el backup automático creado antes de la última edición."
        )
        self._btn_restore.clicked.connect(self._on_restore_backup)
        apply_button_style(self._btn_restore)

        self._btn_edit = QPushButton("Editar carpeta")
        self._btn_edit.setEnabled(False)
        self._btn_edit.setToolTip(
            "Cambia la fecha de metadata de todos los videos de esta carpeta."
        )
        self._btn_edit.clicked.connect(self._on_edit_folder)
        apply_button_style(self._btn_edit)

        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        row2.addWidget(self._btn_new_folder)
        row2.addWidget(self._btn_restore)
        row2.addWidget(self._btn_edit)
        row2.addWidget(self._btn_edit_selection)
        row2.addStretch()

        bottom.addLayout(row1)
        bottom.addLayout(row2)
        layout.addLayout(bottom)

    # ── Public API ─────────────────────────────────────────────────────────────

    def select_after_load(self, path: Path) -> None:
        self._pending_select = str(path)

    def on_folder_changed(self, folder: Path) -> None:
        """Slot connected to MainWindow.folder_changed (via VideoPanel)."""
        if not folder or folder == self._current_folder:
            return
        self.load_folder(folder)

    def load_folder(self, folder_path: Path) -> None:
        if self._thread and self._thread.isRunning():
            if self._worker:
                self._worker.cancel()
            self._pending_folder = folder_path
            return
        self._start_load(folder_path)

    def refresh_item(self, video_path: Path) -> None:
        """Re-read metadata for a single item."""
        path_str = str(video_path)
        item = self._path_to_item.get(path_str)
        if item is None:
            return
        meta    = get_video_metadata(video_path)
        best_dt = get_best_date(meta)
        invalid = is_invalid_date(best_dt)
        dur     = meta.get("duration_seconds", 0.0) or 0.0
        dt_str  = best_dt.strftime("%Y:%m:%d %H:%M:%S") if best_dt else ""
        display = self._format_date(dt_str)
        item.setText(f"{video_path.name}\n{format_duration(dur)}  •  {display}")
        item.setData(_ROLE_DATE,     dt_str)
        item.setData(_ROLE_INVALID,  invalid)
        item.setData(_ROLE_DURATION, dur)

    # ── Sort ───────────────────────────────────────────────────────────────────

    def _on_sort_changed(self, index: int) -> None:
        self._sort_mode = index
        self._update_sort_button()
        self._apply_sort()

    def _on_sort_dir_toggled(self) -> None:
        self._sort_ascending = not self._sort_ascending
        self._update_sort_button()
        self._apply_sort()

    def _update_sort_button(self) -> None:
        labels = {
            _SORT_DATE:     ("↑ Más viejo primero", "↓ Más reciente primero"),
            _SORT_NAME:     ("↑ A → Z",             "↓ Z → A"),
            _SORT_DURATION: ("↑ Más corto primero",  "↓ Más largo primero"),
            _SORT_SIZE:     ("↑ Más pequeño primero","↓ Más grande primero"),
        }
        asc_lbl, desc_lbl = labels.get(self._sort_mode, ("↑", "↓"))
        self._btn_sort_dir.setText(asc_lbl if self._sort_ascending else desc_lbl)

    def _apply_sort(self) -> None:
        n = self._list.count()
        if n == 0:
            return
        current = self._list.currentItem()
        pairs: List[tuple] = []
        for i in range(n):
            item     = self._list.item(i)
            path_str = item.data(_ROLE_PATH) or ""
            key      = self._sort_key(path_str, item)
            pairs.append((key, item))
        pairs.sort(key=lambda x: x[0], reverse=not self._sort_ascending)
        while self._list.count():
            self._list.takeItem(0)
        for _, item in pairs:
            self._list.addItem(item)
        if current:
            self._list.setCurrentItem(current)
            self._list.scrollToItem(current)

    def _sort_key(self, path_str: str, item: QListWidgetItem):
        if self._sort_mode == _SORT_NAME:
            return path_str.lower() if path_str else "\xff"
        if self._sort_mode == _SORT_DURATION:
            return item.data(_ROLE_DURATION) or 0.0
        if self._sort_mode == _SORT_SIZE:
            return item.data(_ROLE_SIZE) or 0
        # _SORT_DATE default
        dt_str = item.data(_ROLE_DATE)
        if dt_str:
            return dt_str
        if path_str:
            try:
                mtime = Path(path_str).stat().st_mtime
                return datetime.fromtimestamp(mtime).strftime("%Y:%m:%d %H:%M:%S")
            except OSError:
                pass
        return "\xff"

    # ── Internal loading ───────────────────────────────────────────────────────

    def _start_load(self, folder_path: Path) -> None:
        self._current_folder = folder_path
        self._list.clear()
        self._pixmap_cache.clear()
        self._path_to_item.clear()

        videos = scan_video_folder(folder_path)
        count  = len(videos)

        self._lbl_count.setText(f"{count} video{'s' if count != 1 else ''}")
        self.folder_loaded.emit(count)
        self._btn_new_folder.setEnabled(True)
        self._btn_edit.setEnabled(count > 0)
        self._btn_restore.setVisible(has_video_backup(folder_path))

        if not videos:
            self._pending_select = None
            self._progress_bar.setVisible(False)
            return

        # Phase 1 — skeleton items (very fast, no subprocess)
        videos.sort(key=lambda p: p.name.lower())

        self._list.setUpdatesEnabled(False)
        for path in videos:
            item = self._make_skeleton_item(path)
            try:
                mtime     = path.stat().st_mtime
                mtime_str = datetime.fromtimestamp(mtime).strftime("%Y:%m:%d %H:%M:%S")
            except OSError:
                mtime_str = ""
            item.setData(_ROLE_DATE, mtime_str)
            self._list.addItem(item)
            self._path_to_item[str(path)] = item
        self._list.setUpdatesEnabled(True)

        self._apply_sort()

        if self._pending_select:
            sel_item = self._path_to_item.get(self._pending_select)
            if sel_item:
                self._list.setCurrentItem(sel_item)
                self._list.scrollToItem(sel_item)
            self._pending_select = None

        if count >= _PROGRESS_THRESHOLD:
            self._progress_bar.setRange(0, count)
            self._progress_bar.setValue(0)
            self._progress_bar.setVisible(True)

        # Phase 2 — full metadata + thumbnails in background
        cache_dir = folder_path / "_thumbcache"
        self._worker = _VideoThumbnailWorker(videos, cache_dir, self._ffmpeg_available)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.items_batch_ready.connect(self._on_items_batch_ready)
        self._worker.progress.connect(self._on_load_progress)
        self._worker.finished.connect(self._on_worker_finished)
        self._thread.start()

    def _make_skeleton_item(self, path: Path) -> QListWidgetItem:
        item = QListWidgetItem(path.name)
        item.setData(_ROLE_PATH,     str(path))
        item.setData(_ROLE_DATE,     "")
        item.setData(_ROLE_INVALID,  False)
        item.setData(_ROLE_DURATION, 0.0)
        item.setData(_ROLE_SIZE,     0)
        item.setData(_ROLE_STD_NAME, bool(_STANDARD_NAME_RE.match(path.name)))
        item.setSizeHint(QSize(_ITEM_W, _ITEM_H))
        return item

    def _on_items_batch_ready(self, batch: list) -> None:
        """Handle a batch of up to 20 video item updates from the background worker.

        Wrapping in setUpdatesEnabled(False/True) reduces repaint calls to
        1-per-batch instead of 1-per-item, keeping the UI smooth at 5000+ videos.
        """
        self._list.setUpdatesEnabled(False)
        try:
            for path_str, thumb_bytes, best_dt, duration, size in batch:
                item = self._path_to_item.get(path_str)
                if item is None:
                    continue

                if thumb_bytes:
                    src = QPixmap()
                    src.loadFromData(thumb_bytes)
                    if not src.isNull():
                        icon_pixmap = QPixmap(_THUMB_SIZE, _THUMB_SIZE)
                        icon_pixmap.fill(QColor(30, 20, 40))
                        painter = QPainter(icon_pixmap)
                        x = (_THUMB_SIZE - src.width())  // 2
                        y = (_THUMB_SIZE - src.height()) // 2
                        painter.drawPixmap(x, y, src)
                        painter.end()
                        item.setIcon(QIcon(icon_pixmap))
                        self._pixmap_cache.put(path_str, icon_pixmap)

                dt_str  = best_dt.strftime("%Y:%m:%d %H:%M:%S") if best_dt else ""
                invalid = is_invalid_date(best_dt)
                display = self._format_date(dt_str)
                dur_str = format_duration(duration) if duration else ""
                item.setText(f"{Path(path_str).name}\n{dur_str}  •  {display}")
                item.setData(_ROLE_DATE,     dt_str)
                item.setData(_ROLE_INVALID,  invalid)
                item.setData(_ROLE_DURATION, duration)
                item.setData(_ROLE_SIZE,     size)

                if not dt_str or invalid:
                    item.setForeground(QBrush(QColor(220, 80, 80)))
                else:
                    item.setForeground(QBrush(QColor(220, 220, 225)))
        finally:
            self._list.setUpdatesEnabled(True)
            self._list.update()
        self._apply_filter()

    def _on_load_progress(self, current: int, total: int) -> None:
        if self._progress_bar.isVisible():
            self._progress_bar.setValue(current)
        self._lbl_count.setText(f"Cargando… {current}/{total}")

    def _on_worker_finished(self) -> None:
        worker = self._worker
        thread = self._thread
        self._worker = None
        self._thread = None
        if thread:
            thread.quit()
            thread.wait()
            if worker:
                worker.deleteLater()
            thread.deleteLater()

        if self._sort_mode == _SORT_DATE:
            self._apply_sort()

        self._progress_bar.setVisible(False)
        self._apply_filter()   # sets count label to visible items

        if self._pending_folder:
            pending = self._pending_folder
            self._pending_folder = None
            self._start_load(pending)

    # ── Filter ────────────────────────────────────────────────────────────────

    def _apply_filter(self) -> None:
        """Show/hide items based on the 'Solo sin fecha' checkbox.

        "Sin fecha" = date_str is empty OR is_invalid (e.g. 2000-01-01).
        Items still loading (date_str="") stay visible until real data arrives.
        """
        sin_fecha_only = self._chk_sin_fecha.isChecked()
        visible = 0
        for i in range(self._list.count()):
            item = self._list.item(i)
            if sin_fecha_only:
                date_str = item.data(_ROLE_DATE) or ""
                is_inv   = bool(item.data(_ROLE_INVALID))
                show     = not date_str or is_inv
            else:
                show = True
            item.setHidden(not show)
            if show:
                visible += 1
        n_total = self._list.count()
        if sin_fecha_only:
            self._lbl_count.setText(f"{visible} / {n_total} video{'s' if n_total != 1 else ''}")
        else:
            self._lbl_count.setText(f"{n_total} video{'s' if n_total != 1 else ''}")

    def _on_restore_backup(self) -> None:
        """Emit restore_backup_requested so VideoPanel can handle the restore dialog."""
        if self._current_folder:
            self.restore_backup_requested.emit(self._current_folder)

    # ── Selection ──────────────────────────────────────────────────────────────

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        path_str = item.data(_ROLE_PATH)
        if path_str:
            self.video_selected.emit(Path(path_str))

    def _on_double_click(self, item: QListWidgetItem) -> None:
        path_str = item.data(_ROLE_PATH)
        if path_str:
            path = Path(path_str)
            if path.exists():
                os.startfile(str(path))

    def _on_selection_changed(self) -> None:
        selected = self._list.selectedItems()
        count    = len(selected)
        self._btn_edit_selection.setVisible(count >= 2)
        if count >= 2:
            self._btn_edit_selection.setText(f"Editar selección ({count})")
            pairs = []
            for item in selected:
                path_str = item.data(_ROLE_PATH) or ""
                dt_str   = item.data(_ROLE_DATE)  or ""
                if path_str:
                    pairs.append((Path(path_str), dt_str))
            self.multi_selection.emit(pairs)

    def _on_edit_selection(self) -> None:
        paths = self._get_selected_paths()
        if len(paths) >= 2:
            self.edit_selection_date.emit(paths)

    def _on_edit_folder(self) -> None:
        if self._current_folder:
            self.edit_video_date.emit(self._current_folder)

    def _on_refresh_folder(self) -> None:
        if self._current_folder:
            self.load_folder(self._current_folder)

    def _get_selected_paths(self) -> List[Path]:
        paths = []
        for item in self._list.selectedItems():
            path_str = item.data(_ROLE_PATH)
            if path_str:
                paths.append(Path(path_str))
        return paths

    # ── Context menu ───────────────────────────────────────────────────────────

    def _on_context_menu(self, pos) -> None:
        selected = self._get_selected_paths()
        if not selected:
            return
        n    = len(selected)
        menu = QMenu(self)

        if n == 1:
            act_edit = QAction("📅 Editar fecha de este video", self)
            act_edit.triggered.connect(
                lambda: self.edit_selection_date.emit([selected[0]])
            )
            menu.addAction(act_edit)

            act_fn = QAction("📋 Leer fecha del nombre", self)
            act_fn.setToolTip(
                "Abre el editor pre-rellenado con la fecha del nombre del archivo."
            )
            act_fn.triggered.connect(
                lambda: self.read_filename_date_requested.emit(selected[0])
            )
            menu.addAction(act_fn)

            menu.addSeparator()

            act_open = QAction("🖼 Abrir en Windows", self)
            act_open.setToolTip("Abre el video con el reproductor predeterminado.")
            act_open.triggered.connect(
                lambda checked, p=selected[0]: os.startfile(str(p))
            )
            menu.addAction(act_open)

        else:
            act_edit = QAction(f"📅 Editar fecha de seleccionados ({n} videos)", self)
            act_edit.triggered.connect(
                lambda: self.edit_selection_date.emit(list(selected))
            )
            menu.addAction(act_edit)

        menu.addSeparator()

        act_move = QAction("📁 Mover a carpeta…", self)
        act_move.triggered.connect(lambda: self._prompt_move(list(selected)))
        menu.addAction(act_move)

        act_copy = QAction("📋 Copiar a carpeta…", self)
        act_copy.triggered.connect(lambda: self._prompt_copy(list(selected)))
        menu.addAction(act_copy)

        menu.addSeparator()

        act_del = QAction(f"🗑 Eliminar ({n} video{'s' if n != 1 else ''})", self)
        act_del.setToolTip(f"Mueve los videos a la carpeta _{_TRASH_DIRNAME}.")
        act_del.triggered.connect(lambda: self._confirm_and_delete(list(selected)))
        menu.addAction(act_del)

        menu.addSeparator()

        act_refresh = QAction("🔄 Actualizar carpeta", self)
        act_refresh.triggered.connect(self._on_refresh_folder)
        menu.addAction(act_refresh)

        menu.exec(self._list.viewport().mapToGlobal(pos))

    # ── Move / Copy / Delete ───────────────────────────────────────────────────

    def _prompt_move(self, paths: List[Path]) -> None:
        dest_str = QFileDialog.getExistingDirectory(
            self, "Mover a carpeta…",
            str(self._current_folder or ""),
            QFileDialog.Option.DontUseNativeDialog,
        )
        if dest_str:
            self._move_files(paths, Path(dest_str))

    def _prompt_copy(self, paths: List[Path]) -> None:
        dest_str = QFileDialog.getExistingDirectory(
            self, "Copiar a carpeta…",
            str(self._current_folder or ""),
            QFileDialog.Option.DontUseNativeDialog,
        )
        if dest_str:
            self._copy_files(paths, Path(dest_str))

    def _move_files(self, paths: List[Path], dest: Path) -> None:
        moved: List[Path] = []
        errors: List[str] = []
        for path in paths:
            if path.parent == dest:
                continue
            try:
                dst_file = unique_dest(path, dest)
                shutil.move(str(path), str(dst_file))
                self._log.log(str(path.parent), path.name, "move",
                               str(path), str(dst_file))
                moved.append(path)
            except Exception as e:
                errors.append(f"{path.name}: {e}")
        if errors:
            mb_warning(self, "Errores al mover", "\n".join(errors[:10]))
        for path in moved:
            ps = str(path)
            item = self._path_to_item.pop(ps, None)
            if item:
                row = self._list.row(item)
                if row >= 0:
                    self._list.takeItem(row)
            self._pixmap_cache.pop(ps, None)
        remaining = self._list.count()
        self._lbl_count.setText(f"{remaining} video{'s' if remaining != 1 else ''}")
        self._btn_edit.setEnabled(remaining > 0)
        if moved:
            self.videos_deleted.emit(moved)

    def _copy_files(self, paths: List[Path], dest: Path) -> None:
        errors: List[str] = []
        for path in paths:
            try:
                dst_file = unique_dest(path, dest)
                shutil.copy2(str(path), str(dst_file))
                self._log.log(str(path.parent), path.name, "copy",
                               str(path), str(dst_file))
            except Exception as e:
                errors.append(f"{path.name}: {e}")
        if errors:
            mb_warning(self, "Errores al copiar", "\n".join(errors[:10]))

    def _confirm_and_delete(self, paths: List[Path]) -> None:
        if not paths or not self._current_folder:
            return
        names     = [p.name for p in paths]
        shown     = names[:5]
        name_list = "\n".join(f"  • {n}" for n in shown)
        extra     = len(names) - 5
        if extra > 0:
            name_list += f"\n  … y {extra} más"

        n   = len(paths)
        msg = QMessageBox(self)
        msg.setWindowTitle("Confirmar eliminación")
        msg.setText(f"¿Mover {n} video{'s' if n != 1 else ''} a _{_TRASH_DIRNAME}?")
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
                shutil.move(str(path), str(dest))
                self._log.log(str(path.parent), path.name, "delete",
                               str(path), str(dest))
                moved.append(path)
            except Exception as e:
                errors.append(f"{path.name}: {e}")

        for path in moved:
            ps   = str(path)
            item = self._path_to_item.pop(ps, None)
            if item:
                row = self._list.row(item)
                if row >= 0:
                    self._list.takeItem(row)
            self._pixmap_cache.pop(ps, None)

        remaining = self._list.count()
        self._lbl_count.setText(f"{remaining} video{'s' if remaining != 1 else ''}")
        self._btn_edit.setEnabled(remaining > 0)
        if errors:
            mb_warning(self, "Errores al eliminar", "\n".join(errors[:10]))
        if moved:
            self.videos_deleted.emit(moved)

    # ── New folder ─────────────────────────────────────────────────────────────

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
            mb_warning(self, "Nombre inválido",
                       'El nombre contiene caracteres no permitidos:\n\\ / : * ? " < > |')
            return
        new_path = self._current_folder / name
        try:
            new_path.mkdir(exist_ok=False)
        except FileExistsError:
            mb_warning(self, "Ya existe",
                       f"Ya existe una carpeta con el nombre '{name}'.")
            return
        except OSError as e:
            mb_warning(self, "Error al crear carpeta", str(e))
            return
        self.folder_created.emit(new_path)

    # ── Event filter (Delete key) ──────────────────────────────────────────────

    def eventFilter(self, obj, event) -> bool:
        from PyQt6.QtCore import QEvent
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
        try:
            parts = date_str.split(" ")
            d = parts[0].replace(":", "/")
            t = parts[1][:5] if len(parts) > 1 else ""
            return f"{d}  {t}"
        except Exception:
            return date_str


# ── Self-contained Video Panel ─────────────────────────────────────────────────

class VideoPanel(QWidget):
    """Videos tab: video grid + detail panel driven by the shared folder tree.

    The folder tree lives in MainWindow; VideoPanel receives folder changes via
    its on_folder_changed(Path) slot connected to MainWindow.folder_changed.
    """

    # Forwarded from VideoGrid so MainWindow can reveal new folders in the
    # shared folder tree.
    folder_created = pyqtSignal(Path)

    def __init__(self, log_manager: LogManager, ffmpeg_available: bool = True,
                 parent=None):
        super().__init__(parent)
        self._log              = log_manager
        self._ffmpeg_available = ffmpeg_available
        self._current_folder: Optional[Path] = None
        self._current_video:  Optional[Path] = None
        self._build_ui()
        self._wire_signals()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Warning banner shown when ffmpeg is not available
        if not self._ffmpeg_available:
            banner = QLabel(
                "⚠  FFmpeg no encontrado — miniaturas y edición de fecha no disponibles.  "
                "Instalá ffmpeg desde https://ffmpeg.org y agregalo al PATH."
            )
            banner.setStyleSheet(
                "background-color: #5a4a00; color: #ffdd88; "
                "padding: 6px 10px; font-size: 11px;"
            )
            banner.setWordWrap(True)
            outer.addWidget(banner)

        # Horizontal splitter: grid | detail
        # The folder tree is now the SHARED one in MainWindow (left of QTabWidget).
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(6)
        splitter.setChildrenCollapsible(False)

        # Left side of this tab: video grid
        self._grid = VideoGrid(self._log, self._ffmpeg_available)
        self._grid.setMinimumWidth(350)
        splitter.addWidget(self._grid)

        # Right side: detail panel
        self._detail = VideoDetailPanel(self._log, self._ffmpeg_available)
        self._detail.setMinimumWidth(280)
        splitter.addWidget(self._detail)

        splitter.setStretchFactor(0, 1)   # grid stretches
        splitter.setStretchFactor(1, 0)   # detail keeps its width

        outer.addWidget(splitter)

    def _wire_signals(self) -> None:
        # Grid → detail
        self._grid.video_selected.connect(self._on_video_selected)
        self._grid.multi_selection.connect(self._on_multi_selection)

        # Edit requests → open date editor
        self._grid.edit_video_date.connect(self._open_editor_folder_or_single)
        self._grid.edit_selection_date.connect(self._open_editor_selection)
        self._grid.read_filename_date_requested.connect(
            self._open_editor_prefill_filename
        )

        # Videos deleted from grid → clear detail if needed
        self._grid.videos_deleted.connect(self._on_videos_deleted)

        # Restore video backup from grid's "Restaurar EXIF" button
        self._grid.restore_backup_requested.connect(self._on_restore_video_backup)

        # Forward new-folder events to MainWindow so it can reveal the folder
        # in the shared folder tree (MainWindow connects this in _wire_signals).
        self._grid.folder_created.connect(self.folder_created)

        # Detail panel edit button
        self._detail.edit_video_date.connect(self._open_editor_single)

    # ── Public slots ───────────────────────────────────────────────────────────

    def on_folder_changed(self, folder: Path) -> None:
        """Slot connected to MainWindow.folder_changed signal.

        Drives the video grid when the user picks a folder in the shared tree.
        Ignores no-ops (same folder or None) to avoid redundant reloads when
        switching tabs.
        """
        if not folder or folder == self._current_folder:
            return
        self._current_folder = folder
        self._grid.load_folder(folder)

    def on_files_moved(self, src_folder: Path, moved: list) -> None:
        """Called by MainWindow when the shared folder tree moves files via drag-drop.

        Reloads the video grid when the currently-displayed folder is affected,
        and clears the detail panel if the current video was moved.
        """
        if self._current_folder == src_folder:
            self._grid.load_folder(src_folder)
        src_names = {url.name for url in moved}
        if (self._current_video
                and self._current_video.parent == src_folder
                and self._current_video.name in src_names):
            self._current_video = None
            self._detail.clear()

    def _on_video_selected(self, path: Path) -> None:
        self._current_video = path
        self._detail.load_video(path)

    def _on_multi_selection(self, pairs: list) -> None:
        self._current_video = None
        self._detail.show_selection([p for p, _ in pairs])

    def _on_videos_deleted(self, moved: list) -> None:
        if self._current_video and self._current_video in moved:
            self._current_video = None
            self._detail.clear()

    def _on_restore_video_backup(self, folder_path: Path) -> None:
        """Restore video metadata from .video_backup.json for folder_path."""
        if not has_video_backup(folder_path):
            mb_info(
                self, "Sin backup",
                f"No existe backup de video en:\n{folder_path}"
            )
            return

        reply = mb_question(
            self, "Restaurar backup",
            f"¿Restaurar metadatos originales de todos los videos en:\n{folder_path.name}?",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        result = restore_video_backup(folder_path)
        self._log.log(
            str(folder_path), "", "restore_video_backup",
            "", f"ok={result['ok']} errores={result['failed']}"
        )

        if result["errors"]:
            mb_warning(
                self, "Restauración con errores",
                f"Restaurados: {result['ok']}\nErrores: {result['failed']}\n\n"
                + "\n".join(result["errors"][:10])
            )
        else:
            mb_info(self, "Backup restaurado",
                    f"Se restauraron {result['ok']} video(s).")

        self._grid.load_folder(folder_path)
        if self._current_video and self._current_video.parent == folder_path:
            self._detail.load_video(self._current_video)

    def _open_editor_folder_or_single(self, target: Path) -> None:
        """Called by grid's 'Editar carpeta' button — target is a folder."""
        from ui.video_date_editor import VideoDateEditorDialog
        dlg = VideoDateEditorDialog("folder", target, self._log, self)
        if dlg.exec() and self._current_folder == target:
            self._grid.load_folder(target)

    def _open_editor_single(self, path: Path) -> None:
        from ui.video_date_editor import VideoDateEditorDialog
        dlg = VideoDateEditorDialog("single", path, self._log, self)
        if dlg.exec():
            new_path = dlg.applied_renames.get(path, path)
            self._current_video = new_path
            if self._current_folder:
                self._grid.select_after_load(new_path)
                self._grid.load_folder(self._current_folder)
            self._detail.load_video(new_path)

    def _open_editor_selection(self, paths: list) -> None:
        if not paths:
            return
        from ui.video_date_editor import VideoDateEditorDialog
        folder = paths[0].parent
        dlg = VideoDateEditorDialog(
            "selection", folder, self._log, self, paths=paths
        )
        if dlg.exec():
            new_path = dlg.applied_renames.get(
                self._current_video, self._current_video
            ) if self._current_video else None
            if self._current_folder:
                if new_path:
                    self._grid.select_after_load(new_path)
                self._grid.load_folder(self._current_folder)
            if new_path:
                self._current_video = new_path
                self._detail.load_video(new_path)

    def _open_editor_prefill_filename(self, path: Path) -> None:
        from ui.video_date_editor import VideoDateEditorDialog
        dlg = VideoDateEditorDialog(
            "single", path, self._log, self, prefill_from_filename=True
        )
        if dlg.exec():
            new_path = dlg.applied_renames.get(path, path)
            self._current_video = new_path
            if self._current_folder:
                self._grid.select_after_load(new_path)
                self._grid.load_folder(self._current_folder)
            self._detail.load_video(new_path)
