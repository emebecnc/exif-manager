"""DuplicatePanel — permanent tab for scanning and resolving duplicate images."""
import os
import shutil
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageOps
from PyQt6.QtCore import Qt, QObject, QSize, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QProgressDialog, QPushButton, QScrollArea, QSplitter,
    QStackedWidget, QVBoxLayout, QWidget,
)

from core.duplicate_finder import DuplicateScanWorker
from core.exif_handler import get_all_metadata, read_exif
from core.file_scanner import unique_dest, EXCLUDED_FOLDERS
from core.video_duplicate_finder import VideoDuplicateScanWorker
from core.video_handler import (
    VIDEO_EXTENSIONS,
    format_duration, format_size, get_video_metadata, get_video_thumbnail,
)
from ui.log_viewer import LogManager
from ui.styles import apply_button_style, apply_primary_button_style, mb_warning, mb_info, mb_question

_TRASH_DIRNAME   = "_duplicados_eliminados"
_THUMB_SIZE      = 200   # photo card thumbnail (px)
_LIST_THUMB_SIZE = 60    # group list thumbnail (px)
_CARD_WIDTH      = 280   # fixed width of each photo card (px)

# ── Button stylesheets with hover/pressed states ──────────────────────────────

_BTN_KEEP_ON = (
    "QPushButton {"
    " background-color: #236b23; border: 1px solid #3fa83f;"
    " border-radius: 8px; color: #e8ffe8; padding: 5px 10px; font-size: 10pt;"
    " font-weight: bold; }"
    "QPushButton:hover { background-color: #2e8f2e; border-color: #5ecb5e; }"
    "QPushButton:pressed { background-color: #174f17; }"
)
_BTN_KEEP_OFF = (
    "QPushButton {"
    " background-color: #2a2a2a; border: 1px solid #484848;"
    " border-radius: 8px; color: #777777; padding: 5px 10px; font-size: 10pt; }"
    "QPushButton:hover { background-color: #363636; border-color: #5a5a5a; color: #aaaaaa; }"
    "QPushButton:pressed { background-color: #1e1e1e; }"
)
_BTN_DEL_ON = (
    "QPushButton {"
    " background-color: #6b2323; border: 1px solid #a83f3f;"
    " border-radius: 8px; color: #ffe8e8; padding: 5px 10px; font-size: 10pt;"
    " font-weight: bold; }"
    "QPushButton:hover { background-color: #8f2e2e; border-color: #cb5e5e; }"
    "QPushButton:pressed { background-color: #4f1717; }"
)
_BTN_DEL_OFF = (
    "QPushButton {"
    " background-color: #2a2a2a; border: 1px solid #484848;"
    " border-radius: 8px; color: #777777; padding: 5px 10px; font-size: 10pt; }"
    "QPushButton:hover { background-color: #363636; border-color: #5a5a5a; color: #aaaaaa; }"
    "QPushButton:pressed { background-color: #1e1e1e; }"
)
_BTN_NEUTRAL = (
    "QPushButton {"
    " background-color: #383838; border: 1px solid #555555;"
    " border-radius: 8px; color: white; padding: 6px 14px; }"
    "QPushButton:hover { background-color: #484848; border-color: #888888; }"
    "QPushButton:pressed { background-color: #282828; }"
)
_BTN_CANCEL = (
    "QPushButton {"
    " background-color: #3e2424; border: 1px solid #6b3838;"
    " border-radius: 8px; color: #ffbbbb; padding: 6px 14px; }"
    "QPushButton:hover { background-color: #4e2e2e; border-color: #8a5050; }"
    "QPushButton:pressed { background-color: #2e1818; }"
)


# ── Module-level helpers ───────────────────────────────────────────────────────

def _fmt_bytes(n: int) -> str:
    if n < 1_024:      return f"{n} B"
    if n < 1_048_576:  return f"{n / 1_024:.1f} KB"
    return f"{n / 1_048_576:.1f} MB"


def _safe_size(path: Path) -> int:
    """Return file size in bytes, 0 if the file cannot be stat'd."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _load_pixmap(path: Path, size: int) -> Optional[QPixmap]:
    """Load a PIL image as QPixmap, applying EXIF rotation. Returns None on any error.

    Uses Pillow directly (with exif_transpose) rather than the EXIF-embedded thumbnail
    so that *every* photo in a duplicate group gets a correct preview regardless of
    whether the EXIF thumbnail byte-range is intact.
    """
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((size, size), Image.LANCZOS)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=85)
        pix = QPixmap()
        pix.loadFromData(buf.getvalue())
        return pix if not pix.isNull() else None
    except Exception:
        return None


def _quality_score(path: Path) -> float:
    """Higher = better quality. Pixel count × 0.6 + file size × 0.4."""
    w = h = 0
    try:
        with Image.open(path) as img:
            w, h = img.width, img.height
    except Exception:
        pass
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return (w * h) * 0.6 + size * 0.4


def _best_in_group(group: List[Path]) -> Path:
    """Return highest-quality photo path. Tiebreak: earlier ctime wins."""
    def _key(p: Path):
        try:   ctime = p.stat().st_ctime
        except OSError: ctime = float("inf")
        return (-_quality_score(p), ctime)
    return min(group, key=_key)


def _best_video_in_group(group: List[Path]) -> Path:
    """Return highest-quality video path using resolution/bitrate/duration.
    Tiebreak: earlier ctime wins."""
    from core.video_duplicate_finder import video_quality_score
    def _key(p: Path):
        try:   ctime = p.stat().st_ctime
        except OSError: ctime = float("inf")
        return (-video_quality_score(p), ctime)
    return min(group, key=_key)


def _count_files_with_extensions(folder: Path, extensions: set) -> int:
    """Recursively count files whose suffix (lowercase) is in *extensions*.
    Skips EXCLUDED_FOLDERS so trash/cache dirs are not counted."""
    count = 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_FOLDERS]
        for file in files:
            if Path(file).suffix.lower() in extensions:
                count += 1
    return count


# ── Deduplication worker ───────────────────────────────────────────────────────

class _DeduplicateWorker(QObject):
    """Move a list of duplicate files to _duplicados_eliminados in a background thread."""

    progress = pyqtSignal(int, int, str)   # current (1-based), total, filename
    finished = pyqtSignal(int, int, list)  # deleted_count, bytes_freed, errors

    def __init__(self, items: List[Tuple[str, int]]) -> None:
        """``items`` is a list of (absolute_path_str, file_size_bytes)."""
        super().__init__()
        self._items = items

    def run(self) -> None:
        total         = len(self._items)
        deleted_count = 0
        bytes_freed   = 0
        errors: List[str] = []

        for i, (path_str, file_size) in enumerate(self._items):
            path = Path(path_str)
            self.progress.emit(i + 1, total, path.name)   # 1-based for display

            if not path.exists():
                continue   # already gone — silently skip

            trash_dir = path.parent / _TRASH_DIRNAME
            try:
                trash_dir.mkdir(exist_ok=True)
                dest = unique_dest(path, trash_dir)
                shutil.move(str(path), str(dest))
                deleted_count += 1
                bytes_freed   += file_size
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")

        self.finished.emit(deleted_count, bytes_freed, errors)


# ── _PhotoCard ─────────────────────────────────────────────────────────────────

class _PhotoCard(QFrame):
    """Displays one photo in the side-by-side group comparison.

    Signals use ``object`` (not ``Path``) for the emitted value to avoid any
    PyQt6 meta-type registration issues with ``pathlib.Path``.
    """

    # pyqtSignal(object) — emitted value is always a pathlib.Path
    keep_clicked = pyqtSignal(object)   # user clicked "✓ Conservar"
    delete_now   = pyqtSignal(object)   # user clicked "🗑 Eliminar"

    def __init__(self, path: Path, is_best: bool, action: str, parent=None):
        super().__init__(parent)
        self._path    = path
        self._is_best = is_best
        self._action  = action   # "keep" or "delete"

        self.setFixedWidth(_CARD_WIDTH)
        self.setFrameShape(QFrame.Shape.StyledPanel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # 1. Thumbnail — loaded via PIL + EXIF transpose so every photo renders
        self._thumb = QLabel()
        self._thumb.setFixedSize(_THUMB_SIZE, _THUMB_SIZE)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setStyleSheet(
            "background-color: #1a1a1f; color: #666666; font-size: 9px;"
        )
        self._thumb.setText("…")
        layout.addWidget(self._thumb)
        self._load_thumb()

        # 2. Quality badge
        self._badge = QLabel("★ MEJOR" if is_best else "DUPLICADO")
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._apply_badge_style()
        layout.addWidget(self._badge)

        # 3. Info rows
        def _info_row(key: str, value: str) -> QHBoxLayout:
            row = QHBoxLayout()
            row.setSpacing(4)
            k = QLabel(key)
            k.setMinimumWidth(72)
            k.setStyleSheet("color: #888888; font-size: 11pt; border: none;")
            v = QLabel(value)
            v.setStyleSheet("color: #cccccc; font-size: 11pt; border: none;")
            v.setWordWrap(True)
            row.addWidget(k)
            row.addWidget(v, 1)
            return row

        # 4. Full metadata
        _meta      = get_all_metadata(path)
        _exif_sec  = _meta.get("exif", {})
        _fields    = _exif_sec.get("fields", {})
        _display   = _exif_sec.get("display", {})
        _gps       = _exif_sec.get("gps")
        _file_info = _meta.get("file", {})

        layout.addLayout(_info_row("Nombre:", path.name))
        layout.addLayout(_info_row("Tamaño:", _file_info.get("tamaño", "N/D")))
        if _file_info.get("dimensiones"):
            layout.addLayout(_info_row("Dims:", _file_info["dimensiones"]))

        for _lbl, _key in [
            ("Fecha orig:", "DateTimeOriginal"),
            ("Fecha digit:", "DateTimeDigitized"),
            ("Fecha sist:", "DateTime"),
        ]:
            _val = _fields.get(_key)
            if _val:
                layout.addLayout(_info_row(_lbl, _val))

        for _lbl, _val in _display.items():
            if _val:
                layout.addLayout(_info_row(f"{_lbl}:", _val))

        if _gps:
            layout.addLayout(_info_row("GPS:", _gps))

        if _file_info.get("modificado"):
            layout.addLayout(_info_row("Modificado:", _file_info["modificado"]))
        if _file_info.get("creado"):
            layout.addLayout(_info_row("Creado:", _file_info["creado"]))

        # Selectable full path
        path_lbl = QLabel(str(path))
        path_lbl.setStyleSheet("color: #666666; font-size: 10pt; border: none;")
        path_lbl.setWordWrap(True)
        path_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(path_lbl)

        layout.addStretch()

        # 5. Action buttons — connected to instance methods, no loop-closure risk
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self._btn_keep   = QPushButton("✓ Conservar")
        self._btn_delete = QPushButton("🗑 Eliminar")
        self._btn_keep.setFixedHeight(28)
        self._btn_delete.setFixedHeight(28)
        btn_row.addWidget(self._btn_keep)
        btn_row.addWidget(self._btn_delete)
        layout.addLayout(btn_row)

        self._btn_keep.clicked.connect(self._on_keep)
        self._btn_delete.clicked.connect(self._on_delete)

        self._apply_visual()

    # ── private ────────────────────────────────────────────────────────────

    def _load_thumb(self) -> None:
        """Load thumbnail using PIL with EXIF transpose — works for all photos."""
        pix = _load_pixmap(self._path, _THUMB_SIZE)
        if pix is not None:
            self._thumb.setPixmap(pix)
            self._thumb.setText("")
        else:
            self._thumb.setText("Sin vista previa")

    def _apply_badge_style(self) -> None:
        """Style and text of badge are driven by the current action, not is_best."""
        if self._action == "keep":
            self._badge.setText("★ Conservar")
            self._badge.setStyleSheet(
                "background-color: #1a4d1a; color: #7fff7f;"
                " font-weight: bold; font-size: 10pt; padding: 3px 10px;"
                " border-radius: 10px; border: 1px solid #3a8a3a;"
            )
        else:
            self._badge.setText("Duplicado")
            self._badge.setStyleSheet(
                "background-color: #4a2800; color: #ffaa55;"
                " font-weight: bold; font-size: 10pt; padding: 3px 10px;"
                " border-radius: 10px; border: 1px solid #7a4800;"
            )

    def _apply_visual(self) -> None:
        """Update card border/background and button highlight to reflect current action."""
        if self._action == "keep":
            self.setStyleSheet(
                "QFrame {"
                " border: 2px solid #3ea83e; border-radius: 10px;"
                " background-color: rgba(62,168,62,18); }"
                "QLabel { border: none; background-color: transparent; }"
            )
            self._btn_keep.setStyleSheet(_BTN_KEEP_ON)
            self._btn_delete.setStyleSheet(_BTN_DEL_OFF)
        else:
            self.setStyleSheet(
                "QFrame {"
                " border: 2px solid #a83e3e; border-radius: 10px;"
                " background-color: rgba(168,62,62,18); }"
                "QLabel { border: none; background-color: transparent; }"
            )
            self._btn_keep.setStyleSheet(_BTN_KEEP_OFF)
            self._btn_delete.setStyleSheet(_BTN_DEL_ON)

        # Re-apply badge (text + style) after frame stylesheet resets all QLabel borders
        self._apply_badge_style()

    def _on_keep(self) -> None:
        # Emit only — DuplicatePanel._on_card_keep() is the single source of truth
        # for selection state.  It will call set_action() for ALL cards in the group
        # (including this one) so there is no visual flash or state divergence.
        self.keep_clicked.emit(self._path)

    def _on_delete(self) -> None:
        self.delete_now.emit(self._path)

    # ── public ─────────────────────────────────────────────────────────────

    def set_action(self, action: str) -> None:
        self._action = action
        self._apply_visual()

    def get_action(self) -> str:
        return self._action


# ── _VideoCard ──────────────────────────────────────────────────────────────────

class _VideoCard(QFrame):
    """Displays one video in the side-by-side duplicate comparison.

    Shows a first-frame thumbnail (via ffmpeg), video metadata
    (resolution, duration, FPS, codec, date) and the same Conservar /
    Eliminar buttons as _PhotoCard.
    """

    keep_clicked = pyqtSignal(object)
    delete_now   = pyqtSignal(object)

    def __init__(self, path: Path, is_best: bool, action: str, parent=None):
        super().__init__(parent)
        self._path   = path
        self._action = action

        self.setFixedWidth(_CARD_WIDTH)
        self.setFrameShape(QFrame.Shape.StyledPanel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # Thumbnail — first frame via ffmpeg
        self._thumb = QLabel()
        self._thumb.setFixedSize(_THUMB_SIZE, _THUMB_SIZE)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setStyleSheet(
            "background-color: #1a1a1f; color: #888888; font-size: 22px;"
        )
        self._thumb.setText("🎬")
        layout.addWidget(self._thumb)
        self._load_thumb()

        # Quality badge
        self._badge = QLabel()
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._badge)

        # Info rows helper (same style as _PhotoCard)
        def _info_row(key: str, value: str) -> QHBoxLayout:
            row = QHBoxLayout()
            row.setSpacing(4)
            k = QLabel(key)
            k.setMinimumWidth(80)
            k.setStyleSheet("color: #888888; font-size: 11pt; border: none;")
            v = QLabel(value)
            v.setStyleSheet("color: #cccccc; font-size: 11pt; border: none;")
            v.setWordWrap(True)
            row.addWidget(k)
            row.addWidget(v, 1)
            return row

        # Full metadata
        meta = get_video_metadata(path)

        layout.addLayout(_info_row("Nombre:", path.name))
        layout.addLayout(_info_row("Tamaño:", format_size(meta.get("size_bytes", 0) or 0)))

        _w = meta.get("width", 0) or 0
        _h = meta.get("height", 0) or 0
        if _w and _h:
            layout.addLayout(_info_row("Resolución:", f"{_w} × {_h}"))

        _dur = meta.get("duration_seconds", 0) or 0
        layout.addLayout(_info_row("Duración:", format_duration(_dur) if _dur else "N/D"))

        if meta.get("codec_video"):
            layout.addLayout(_info_row("Video codec:", meta["codec_video"]))
        if meta.get("codec_audio"):
            layout.addLayout(_info_row("Audio codec:", meta["codec_audio"]))
        if meta.get("bitrate"):
            layout.addLayout(_info_row("Bitrate:", f"{meta['bitrate'] / 1_000_000:.1f} Mbps"))
        if meta.get("rotation"):
            layout.addLayout(_info_row("Rotación:", f"{meta['rotation']}°"))
        if meta.get("format_name"):
            layout.addLayout(_info_row("Formato:", meta["format_name"]))
        _cam = f"{meta.get('make', '')} {meta.get('model', '')}".strip()
        if _cam:
            layout.addLayout(_info_row("Cámara:", _cam))

        _ct = meta.get("creation_time")
        if _ct:
            layout.addLayout(_info_row("Fecha meta:", _ct.strftime("%Y:%m:%d %H:%M:%S")))
        _dm = meta.get("date_modified")
        if _dm:
            layout.addLayout(_info_row("Modificado:", _dm.strftime("%d/%m/%Y %H:%M")))
        _dc = meta.get("date_created")
        if _dc:
            layout.addLayout(_info_row("Creado:", _dc.strftime("%d/%m/%Y")))

        # Selectable full path
        path_lbl = QLabel(str(path))
        path_lbl.setStyleSheet("color: #666666; font-size: 10pt; border: none;")
        path_lbl.setWordWrap(True)
        path_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(path_lbl)

        layout.addStretch()

        # Action buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self._btn_keep   = QPushButton("✓ Conservar")
        self._btn_delete = QPushButton("🗑 Eliminar")
        self._btn_keep.setFixedHeight(28)
        self._btn_delete.setFixedHeight(28)
        btn_row.addWidget(self._btn_keep)
        btn_row.addWidget(self._btn_delete)
        layout.addLayout(btn_row)

        self._btn_keep.clicked.connect(self._on_keep)
        self._btn_delete.clicked.connect(self._on_delete)

        self._apply_visual()

    # ── private ────────────────────────────────────────────────────────────

    def _load_thumb(self) -> None:
        """Extract first frame with ffmpeg; show emoji placeholder on failure."""
        thumb_bytes = get_video_thumbnail(self._path, _THUMB_SIZE)
        if thumb_bytes:
            pix = QPixmap()
            if pix.loadFromData(thumb_bytes) and not pix.isNull():
                self._thumb.setPixmap(pix)
                self._thumb.setText("")

    def _apply_badge_style(self) -> None:
        if self._action == "keep":
            self._badge.setText("★ Conservar")
            self._badge.setStyleSheet(
                "background-color: #1a4d1a; color: #7fff7f;"
                " font-weight: bold; font-size: 10pt; padding: 3px 10px;"
                " border-radius: 10px; border: 1px solid #3a8a3a;"
            )
        else:
            self._badge.setText("Duplicado")
            self._badge.setStyleSheet(
                "background-color: #4a2800; color: #ffaa55;"
                " font-weight: bold; font-size: 10pt; padding: 3px 10px;"
                " border-radius: 10px; border: 1px solid #7a4800;"
            )

    def _apply_visual(self) -> None:
        if self._action == "keep":
            self.setStyleSheet(
                "QFrame { border: 2px solid #3ea83e; border-radius: 10px;"
                " background-color: rgba(62,168,62,18); }"
                "QLabel { border: none; background-color: transparent; }"
            )
            self._btn_keep.setStyleSheet(_BTN_KEEP_ON)
            self._btn_delete.setStyleSheet(_BTN_DEL_OFF)
        else:
            self.setStyleSheet(
                "QFrame { border: 2px solid #a83e3e; border-radius: 10px;"
                " background-color: rgba(168,62,62,18); }"
                "QLabel { border: none; background-color: transparent; }"
            )
            self._btn_keep.setStyleSheet(_BTN_KEEP_OFF)
            self._btn_delete.setStyleSheet(_BTN_DEL_ON)
        self._apply_badge_style()

    def _on_keep(self) -> None:
        self.keep_clicked.emit(self._path)

    def _on_delete(self) -> None:
        self.delete_now.emit(self._path)

    # ── public ─────────────────────────────────────────────────────────────

    def set_action(self, action: str) -> None:
        self._action = action
        self._apply_visual()

    def get_action(self) -> str:
        return self._action


# ── DuplicatePanel ─────────────────────────────────────────────────────────────

class DuplicatePanel(QWidget):
    """Permanent panel for scanning and resolving duplicate photos."""

    scan_started = pyqtSignal()   # emitted when a scan begins → main window switches tab

    def __init__(self, log_manager: LogManager, parent=None) -> None:
        super().__init__(parent)
        self._log = log_manager

        # Paths set by main window
        self._root:           Optional[Path] = None
        self._current_folder: Optional[Path] = None

        # "photo" or "video" — controls button labels; default to photo
        self._media_type: str = "photo"

        # Scan worker / thread (type is DuplicateScanWorker or VideoDuplicateScanWorker)
        self._scan_worker = None
        self._scan_thread: Optional[QThread] = None
        self._scanning:    bool = False

        # Dedup worker / thread
        self._dedup_worker:       Optional[_DeduplicateWorker] = None
        self._dedup_thread:       Optional[QThread]            = None
        self._dedup_progress_dlg: Optional[QProgressDialog]   = None
        self._dedup_total:        int = 0
        self._dedup_items:        List[Tuple[str, int]]        = []
        self._deduplicating:      bool = False

        # Results — separate caches so switching tabs preserves each type's scan
        self._photo_groups:      List[List[Path]]            = []
        self._photo_selections:  Dict[int, Dict[Path, str]]  = {}
        self._video_groups:      List[List[Path]]            = []
        self._video_selections:  Dict[int, Dict[Path, str]]  = {}
        self._all_groups:        List[List[Path]]            = []
        self._all_selections:    Dict[int, Dict[Path, str]]  = {}
        # Active display (always points to the current type's cache)
        self._groups:            List[List[Path]]            = []
        self._selections:        Dict[int, Dict[Path, str]]  = {}
        self._current_group_idx: int                         = -1
        self._current_cards:     Dict[Path, _PhotoCard]      = {}

        self._build_ui()

    # ── Public API ─────────────────────────────────────────────────────────

    def on_folder_changed(self, folder: Path) -> None:
        """Slot connected to MainWindow.folder_changed signal.

        Updates the current folder scope and auto-selects the FOTOS or VIDEOS
        toggle based on which media type dominates in the folder.
        Does NOT start a scan automatically — the user must press the button.
        """
        self.set_current_folder(folder)

        # Auto-detect dominant media type (FIX 3)
        _PHOTO_EXT = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}
        photo_count = _count_files_with_extensions(folder, _PHOTO_EXT)
        video_count = _count_files_with_extensions(folder, VIDEO_EXTENSIONS)

        if video_count > photo_count:
            if self._media_type != "video":
                self.set_media_type("video")
        else:
            # photos >= videos → default to FOTOS
            if self._media_type != "photo":
                self.set_media_type("photo")

    def set_root(self, root: Optional[Path]) -> None:
        self._root = root
        self._update_button_states()

    def set_current_folder(self, folder: Optional[Path]) -> None:
        self._current_folder = folder
        self._update_button_states()

    def set_media_type(self, media_type: str) -> None:
        """Switch between 'photo', 'video', or 'both' duplicate search mode.

        Saves the current type's results, restores the new type's cached
        results (if any), and updates button labels and toggle buttons.
        """
        if media_type == self._media_type:
            return

        # Save current active results to the current type's cache
        if self._media_type == "photo":
            self._photo_groups     = list(self._groups)
            self._photo_selections = {k: dict(v) for k, v in self._selections.items()}
        elif self._media_type == "video":
            self._video_groups     = list(self._groups)
            self._video_selections = {k: dict(v) for k, v in self._selections.items()}
        else:  # "both"
            self._all_groups     = list(self._groups)
            self._all_selections = {k: dict(v) for k, v in self._selections.items()}

        self._media_type = media_type

        # Update scan button labels
        if media_type == "photo":
            kind = "foto"
        elif media_type == "video":
            kind = "video"
        else:
            kind = "auto"
        self._btn_scan_folder.setText(f"🔍 Buscar duplicados de {kind}")
        self._btn_scan_root.setText(f"🔍 Buscar duplicados de {kind} (raíz)")

        # Update toggle button visual state
        self._update_toggle_style()

        # Restore the new type's cached results (or clear if none)
        if media_type == "photo":
            self._restore_groups_display(self._photo_groups, self._photo_selections)
        elif media_type == "video":
            self._restore_groups_display(self._video_groups, self._video_selections)
        else:
            self._restore_groups_display(self._all_groups, self._all_selections)

    def _update_toggle_style(self) -> None:
        """Apply checked/unchecked stylesheet to the media-type toggle buttons."""
        _ON  = ("QPushButton { background-color: #0d7377; border: 1px solid #14a0a6;"
                " border-radius: 10px; color: white; padding: 5px 10px;"
                " font-weight: bold; font-size: 10pt; }"
                "QPushButton:hover { background-color: #14a0a6; border-color: #1dc0c8; }"
                "QPushButton:pressed { background-color: #0a5558; }")
        _OFF = ("QPushButton { background-color: #252525; border: 1px solid #404040;"
                " border-radius: 10px; color: #777777; padding: 5px 10px; font-size: 10pt; }"
                "QPushButton:hover { background-color: #303030; border-color: #5a5a5a;"
                " color: #aaaaaa; }")
        self._btn_toggle_photo.setStyleSheet(_ON if self._media_type == "photo" else _OFF)
        self._btn_toggle_video.setStyleSheet(_ON if self._media_type == "video" else _OFF)
        self._btn_toggle_all.setStyleSheet(_ON if self._media_type == "both" else _OFF)
        self._btn_toggle_photo.setChecked(self._media_type == "photo")
        self._btn_toggle_video.setChecked(self._media_type == "video")
        self._btn_toggle_all.setChecked(self._media_type == "both")

    def _restore_groups_display(
        self,
        groups: List[List[Path]],
        selections: Dict[int, Dict[Path, str]],
    ) -> None:
        """Repopulate the groups list and right panel from cached result data."""
        self._groups     = list(groups)
        self._selections = {k: dict(v) for k, v in selections.items()}
        self._current_group_idx = -1
        self._current_cards.clear()
        self._groups_list.clear()
        self._right_stack.setCurrentIndex(0)

        if not self._groups:
            self._lbl_header.setText("No hay duplicados escaneados aún.")
            self._btn_dedup_all.setEnabled(False)
            return

        for i, group in enumerate(self._groups):
            best       = self._get_best(group)
            group_size = sum(_safe_size(p) for p in group)
            item = QListWidgetItem(
                f"Grupo {i + 1} — {len(group)} archivos · {_fmt_bytes(group_size)}\n"
                f"{best.name}"
            )
            item.setSizeHint(QSize(250, _LIST_THUMB_SIZE + 14))
            pix = _load_pixmap(best, _LIST_THUMB_SIZE)
            if pix is not None:
                item.setIcon(QIcon(pix))
            self._groups_list.addItem(item)

        self._groups_list.setCurrentRow(0)
        self._btn_dedup_all.setEnabled(True)
        self._update_header_label()

    def start_scan(self, path: Path) -> None:
        """Begin a duplicate scan of ``path``. Emits ``scan_started`` first."""
        if self._scanning or self._deduplicating:
            return
        self.scan_started.emit()
        self._begin_scan(path)

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── Left panel ────────────────────────────────────────────────────
        left = QWidget()
        left.setMinimumWidth(260)
        left.setMaximumWidth(340)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(6, 6, 6, 6)
        left_layout.setSpacing(4)

        # ── Media type toggle ─────────────────────────────────────────────
        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(2)
        self._btn_toggle_photo = QPushButton("📷 Fotos")
        self._btn_toggle_photo.setCheckable(True)
        self._btn_toggle_photo.setChecked(True)
        self._btn_toggle_video = QPushButton("🎬 Videos")
        self._btn_toggle_video.setCheckable(True)
        self._btn_toggle_all   = QPushButton("🔀 Duplicados")
        self._btn_toggle_all.setCheckable(True)
        self._btn_toggle_photo.setToolTip("Buscar duplicados de fotos")
        self._btn_toggle_video.setToolTip("Buscar duplicados de videos")
        self._btn_toggle_all.setToolTip("Buscar duplicados de todos los archivos (auto-detecta tipo)")
        self._btn_toggle_photo.clicked.connect(lambda: self.set_media_type("photo"))
        self._btn_toggle_video.clicked.connect(lambda: self.set_media_type("video"))
        self._btn_toggle_all.clicked.connect(lambda: self.set_media_type("both"))
        self._update_toggle_style()
        toggle_row.addWidget(self._btn_toggle_photo)
        toggle_row.addWidget(self._btn_toggle_video)
        toggle_row.addWidget(self._btn_toggle_all)
        left_layout.addLayout(toggle_row)

        self._lbl_header = QLabel("No hay duplicados escaneados aún.")
        self._lbl_header.setStyleSheet("font-size: 10px; color: #aaaaaa;")
        self._lbl_header.setWordWrap(True)
        left_layout.addWidget(self._lbl_header)

        self._groups_list = QListWidget()
        self._groups_list.setIconSize(QSize(_LIST_THUMB_SIZE, _LIST_THUMB_SIZE))
        self._groups_list.setSpacing(2)
        self._groups_list.currentRowChanged.connect(self._on_group_selected)
        left_layout.addWidget(self._groups_list, 1)

        self._btn_cancel = QPushButton("⏹ Cancelar escaneo")
        self._btn_cancel.setVisible(False)
        self._btn_cancel.setStyleSheet(_BTN_CANCEL)
        self._btn_cancel.clicked.connect(self._on_cancel_scan)
        left_layout.addWidget(self._btn_cancel)

        self._btn_scan_folder = QPushButton("🔍 Buscar duplicados de foto")
        self._btn_scan_folder.setToolTip(
            "Escanea solo la carpeta actualmente abierta en busca de duplicados."
        )
        apply_button_style(self._btn_scan_folder)
        self._btn_scan_folder.clicked.connect(self._on_scan_folder_clicked)
        left_layout.addWidget(self._btn_scan_folder)

        self._btn_scan_root = QPushButton("🔍 Buscar duplicados de foto (raíz)")
        self._btn_scan_root.setToolTip(
            "Escanea toda la colección desde la carpeta raíz.\n"
            "Puede tardar varios minutos en colecciones grandes."
        )
        apply_button_style(self._btn_scan_root)
        self._btn_scan_root.clicked.connect(self._on_scan_root_clicked)
        left_layout.addWidget(self._btn_scan_root)

        self._btn_dedup_all = QPushButton("🗑 Deduplicar todo")
        self._btn_dedup_all.setToolTip(
            "Mueve automáticamente todos los duplicados a _duplicados_eliminados,\n"
            "conservando la foto de mayor calidad en cada grupo."
        )
        apply_primary_button_style(self._btn_dedup_all)
        self._btn_dedup_all.setEnabled(False)
        self._btn_dedup_all.clicked.connect(self._on_dedup_all)
        left_layout.addWidget(self._btn_dedup_all)

        splitter.addWidget(left)

        # ── Right stack ───────────────────────────────────────────────────
        self._right_stack = QStackedWidget()

        empty_lbl = QLabel(
            "No hay duplicados escaneados aún.\n\nUsá 🔍 Buscar para comenzar."
        )
        empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_lbl.setStyleSheet("color: #666666; font-size: 12px;")
        self._right_stack.addWidget(empty_lbl)   # index 0

        self._comparison_scroll = QScrollArea()
        self._comparison_scroll.setWidgetResizable(True)
        self._comparison_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._comparison_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._comparison_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._right_stack.addWidget(self._comparison_scroll)   # index 1

        splitter.addWidget(self._right_stack)
        splitter.setSizes([300, 700])

        outer.addWidget(splitter)
        self._update_button_states()

    # ── Button state ───────────────────────────────────────────────────────

    def _update_button_states(self) -> None:
        busy = self._scanning or self._deduplicating
        self._btn_scan_folder.setEnabled(
            not busy and self._current_folder is not None
        )
        self._btn_scan_root.setEnabled(
            not busy and self._root is not None
        )
        self._btn_dedup_all.setEnabled(
            not busy and bool(self._groups)
        )

    # ── Scan ───────────────────────────────────────────────────────────────

    def _on_scan_folder_clicked(self) -> None:
        if self._current_folder:
            self.start_scan(self._current_folder)

    def _on_scan_root_clicked(self) -> None:
        if self._root:
            self.start_scan(self._root)

    def _begin_scan(self, path: Path) -> None:
        # Reset all previous results
        self._groups.clear()
        self._selections.clear()
        self._groups_list.clear()
        self._current_group_idx = -1
        self._current_cards.clear()
        self._right_stack.setCurrentIndex(0)
        self._btn_dedup_all.setEnabled(False)

        self._scanning = True
        self._lbl_header.setText(f"Escaneando {path.name}…")
        self._btn_cancel.setVisible(True)
        self._btn_scan_folder.setEnabled(False)
        self._btn_scan_root.setEnabled(False)

        # Use the appropriate worker based on the active media type.
        # In "both" mode, auto-detect the dominant type from the folder.
        _PHOTO_EXT = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}
        effective_type = self._media_type
        if effective_type == "both":
            photo_count = _count_files_with_extensions(path, _PHOTO_EXT)
            video_count = _count_files_with_extensions(path, VIDEO_EXTENSIONS)
            effective_type = "video" if video_count > photo_count else "photo"

        if effective_type == "video":
            self._scan_worker = VideoDuplicateScanWorker(path)
        else:
            self._scan_worker = DuplicateScanWorker(path)
        self._scan_thread = QThread(self)
        self._scan_worker.moveToThread(self._scan_thread)

        # Thread lifetime pattern (see CLAUDE.md): do NOT connect finished→thread.quit
        # here — _on_scan_finished calls quit()+wait() directly to avoid double-quit.
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.progress.connect(self._on_scan_progress)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.error.connect(self._on_scan_error)
        self._scan_thread.finished.connect(self._cleanup_scan_thread)

        self._scan_thread.start()

    def _on_cancel_scan(self) -> None:
        if self._scan_worker is not None:
            self._scan_worker.cancel()

    def _on_scan_progress(self, current: int, total: int, fname: str) -> None:
        self._lbl_header.setText(f"Escaneando… {current}/{total}\n{fname}")

    def _on_scan_finished(self, groups: list) -> None:
        print(f"DEBUG: _on_scan_finished called with {len(groups)} groups")
        # Quit+wait before any UI changes to prevent 'QThread destroyed while running'
        if self._scan_thread and self._scan_thread.isRunning():
            self._scan_thread.quit()
            self._scan_thread.wait()

        self._scanning = False
        self._btn_cancel.setVisible(False)
        self._update_button_states()

        # Normalise to Path objects — worker may emit str or Path depending on version
        self._groups = [
            [p if isinstance(p, Path) else Path(p) for p in g]
            for g in groups
        ]

        if not self._groups:
            self._lbl_header.setText("✓ No se encontraron duplicados.")
            # Persist empty result to cache for this media type
            if self._media_type == "photo":
                self._photo_groups = []
                self._photo_selections = {}
            elif self._media_type == "video":
                self._video_groups = []
                self._video_selections = {}
            else:
                self._all_groups = []
                self._all_selections = {}
            return

        # Initialise per-group selections: best → keep, rest → delete
        for i, group in enumerate(self._groups):
            best = self._get_best(group)
            best_str = str(best)
            self._selections[i] = {
                p: ("keep" if str(p) == best_str else "delete") for p in group
            }

        self._btn_dedup_all.setEnabled(True)
        self._update_header_label()

        # Populate group list with thumbnails
        for i, group in enumerate(self._groups):
            best       = self._get_best(group)
            group_size = sum(_safe_size(p) for p in group)
            item = QListWidgetItem(
                f"Grupo {i + 1} — {len(group)} archivos · {_fmt_bytes(group_size)}\n"
                f"{best.name}"
            )
            item.setSizeHint(QSize(250, _LIST_THUMB_SIZE + 14))

            pix = _load_pixmap(best, _LIST_THUMB_SIZE)
            if pix is not None:
                item.setIcon(QIcon(pix))

            self._groups_list.addItem(item)

        self._groups_list.setCurrentRow(0)

        # Cache scan results so switching tabs doesn't lose them
        if self._media_type == "photo":
            self._photo_groups     = list(self._groups)
            self._photo_selections = {k: dict(v) for k, v in self._selections.items()}
        elif self._media_type == "video":
            self._video_groups     = list(self._groups)
            self._video_selections = {k: dict(v) for k, v in self._selections.items()}
        else:
            self._all_groups     = list(self._groups)
            self._all_selections = {k: dict(v) for k, v in self._selections.items()}

    def _on_scan_error(self, msg: str) -> None:
        # Quit+wait the thread FIRST — same pattern as _on_scan_finished.
        # Without this, the QThread is destroyed while still running → crash -805306369.
        if self._scan_thread and self._scan_thread.isRunning():
            self._scan_thread.quit()
            self._scan_thread.wait(5000)
            if self._scan_thread and self._scan_thread.isRunning():
                self._scan_thread.terminate()
                self._scan_thread.wait(1000)

        self._scanning = False
        self._btn_cancel.setVisible(False)
        self._update_button_states()
        self._lbl_header.setText(f"⚠ Error al escanear:\n{msg}")
        print(f"ERROR in scan: {msg}")

    def _cleanup_scan_thread(self) -> None:
        if self._scan_worker:
            self._scan_worker.deleteLater()
            self._scan_worker = None
        if self._scan_thread:
            self._scan_thread.deleteLater()
            self._scan_thread = None

    # ── Best-in-group helper ───────────────────────────────────────────────

    def _get_best(self, group: List[Path]) -> Path:
        """Return the highest-quality path using the correct scorer for the
        current media type (photo → pixel quality; video → resolution/bitrate).
        In 'both' mode, infer type from the first file's extension."""
        if self._media_type == "video":
            return _best_video_in_group(group)
        if self._media_type == "both":
            # Infer from file extension
            if group and group[0].suffix.lower() in VIDEO_EXTENSIONS:
                return _best_video_in_group(group)
        return _best_in_group(group)

    # ── Group display ──────────────────────────────────────────────────────

    def _on_group_selected(self, row: int) -> None:
        if 0 <= row < len(self._groups):
            self._current_group_idx = row
            self._show_group(row)

    def _show_group(self, group_idx: int) -> None:
        print(f"DEBUG: _show_group({group_idx})")
        if group_idx < 0 or group_idx >= len(self._groups):
            print(f"  ERROR: group_idx {group_idx} out of range (len={len(self._groups)})")
            return

        group = self._groups[group_idx]
        sels  = self._selections.get(group_idx, {})
        print(f"  Group has {len(group)} files: {[p.name for p in group]}")
        best  = self._get_best(group)

        self._current_cards.clear()

        container = QWidget()
        layout    = QHBoxLayout(container)
        layout.setSpacing(10)
        layout.setContentsMargins(8, 8, 8, 8)

        # Sort so the best card is always the LEFTMOST (Issue 12).
        # Remaining cards keep their original order for stability.
        sorted_group = [best] + [p for p in group if str(p) != str(best)]

        if self._media_type == "video":
            CardClass = _VideoCard
        elif self._media_type == "both":
            # Infer from first file's extension
            CardClass = _VideoCard if (group and group[0].suffix.lower() in VIDEO_EXTENSIONS) else _PhotoCard
        else:
            CardClass = _PhotoCard

        for p in sorted_group:
            action = sels.get(p, "keep" if str(p) == str(best) else "delete")
            card   = CardClass(p, is_best=(str(p) == str(best)), action=action)

            # Default-argument capture avoids the classic loop-closure bug:
            # each lambda captures the value of gi and emitted at definition time.
            card.keep_clicked.connect(
                lambda emitted, gi=group_idx: self._on_card_keep(emitted, gi)
            )
            card.delete_now.connect(
                lambda emitted, gi=group_idx: self._on_card_delete_now(emitted, gi)
            )

            self._current_cards[p] = card
            layout.addWidget(card)

        layout.addStretch()
        self._comparison_scroll.setWidget(container)
        self._right_stack.setCurrentIndex(1)

    # ── Card signal handlers ───────────────────────────────────────────────

    def _on_card_keep(self, path_obj: object, group_idx: int) -> None:
        """User clicked 'Conservar' — immediately move every other file in the group
        to _duplicados_eliminados/, remove the group from the list, and advance."""
        path = path_obj if isinstance(path_obj, Path) else Path(path_obj)
        if group_idx >= len(self._groups):
            return

        group    = self._groups[group_idx]
        path_str = str(path)

        # Trash every file except the kept one
        to_trash = [p for p in group if str(p) != path_str]

        deleted     = 0
        bytes_freed = 0
        errors: List[str] = []

        for p in to_trash:
            sz = _safe_size(p)
            if not p.exists():
                continue
            trash_dir = p.parent / _TRASH_DIRNAME
            try:
                trash_dir.mkdir(exist_ok=True)
                dest = unique_dest(p, trash_dir)
                shutil.move(str(p), str(dest))
                deleted     += 1
                bytes_freed += sz
                self._log.log(str(p.parent), p.name,
                              "delete_duplicate", p.name, "conservar")
            except Exception as exc:
                errors.append(f"{p.name}: {exc}")

        # Remove group from list + state; auto-advances to next group (or shows empty)
        self._remove_group(group_idx)

        # Build toast — override whatever _remove_group wrote into the header
        toast = (
            f"✓ {deleted} archivo{'s' if deleted != 1 else ''}"
            f" eliminado{'s' if deleted != 1 else ''}"
            f", {_fmt_bytes(bytes_freed)} liberados"
            if deleted else "✓ Grupo procesado"
        )
        if errors:
            toast += f"  ⚠ {len(errors)} error{'es' if len(errors) != 1 else ''}"
        if not self._groups:
            toast += " — ✓ Todos procesados"

        self._lbl_header.setText(toast)

        # Restore summary header after a pause so the user can read the toast
        if self._groups:
            QTimer.singleShot(2500, self._update_header_label)

    def _on_card_delete_now(self, path_obj: object, group_idx: int) -> None:
        """User clicked '🗑 Eliminar' — guard, then move the file immediately."""
        path = path_obj if isinstance(path_obj, Path) else Path(path_obj)

        # Stale index guard (can happen if groups were removed while cards were open)
        if group_idx >= len(self._groups):
            return
        group = self._groups[group_idx]
        path_str = str(path)
        if not any(str(p) == path_str for p in group):
            return

        # At least one other file must be marked keep before deletion
        sels        = self._selections.get(group_idx, {})
        other_keeps = sum(1 for p in group if str(p) != path_str and sels.get(p) == "keep")
        if other_keeps < 1:
            mb_warning(
                self, "No se puede eliminar",
                "Debe conservar al menos una foto del grupo.\n"
                "Hacé clic en '✓ Conservar' en otra foto primero."
            )
            return

        # File-existence check before attempting the move
        if not path.exists():
            mb_warning(
                self, "Archivo no encontrado",
                f"El archivo ya no existe en el disco:\n{path}\n\n"
                "Se eliminará del grupo."
            )
            self._remove_path_from_group(group_idx, path)
            return

        # Move the file to _duplicados_eliminados
        trash_dir = path.parent / _TRASH_DIRNAME
        try:
            trash_dir.mkdir(exist_ok=True)
            dest = unique_dest(path, trash_dir)
            shutil.move(str(path), str(dest))
        except Exception as exc:
            mb_warning(self, "Error al eliminar", str(exc))
            return

        self._log.log(
            str(path.parent), path.name,
            "delete_duplicate", path.name, ""
        )

        self._remove_path_from_group(group_idx, path)

    def _remove_path_from_group(self, group_idx: int, path: Path) -> None:
        """Remove ``path`` from group and selections, update cards and list."""
        group = self._groups[group_idx]
        group.remove(path)
        if group_idx in self._selections:
            self._selections[group_idx].pop(path, None)

        card = self._current_cards.pop(path, None)
        if card:
            card.setVisible(False)
            card.deleteLater()

        if len(group) <= 1:
            self._remove_group(group_idx)
        else:
            self._refresh_list_item(group_idx)

    # ── Group list management ──────────────────────────────────────────────

    def _remove_group(self, group_idx: int) -> None:
        """Remove a fully-resolved group from both the list and internal state."""
        self._groups_list.takeItem(group_idx)
        if 0 <= group_idx < len(self._groups):
            self._groups.pop(group_idx)
        self._selections.pop(group_idx, None)

        # Re-key selections: all indices above group_idx shift down by 1
        new_sel: Dict[int, Dict[Path, str]] = {}
        for k, v in self._selections.items():
            new_sel[k if k < group_idx else k - 1] = v
        self._selections = new_sel

        # Re-label remaining list items to keep "Grupo N" numbering contiguous
        for i in range(self._groups_list.count()):
            item  = self._groups_list.item(i)
            group = self._groups[i]
            best  = self._get_best(group)
            item.setText(
                f"Grupo {i + 1} — {len(group)} archivos"
                f" · {_fmt_bytes(sum(_safe_size(p) for p in group))}\n"
                f"{best.name}"
            )

        self._current_group_idx = -1
        self._current_cards.clear()

        if not self._groups:
            self._comparison_scroll.setWidget(QWidget())
            self._right_stack.setCurrentIndex(0)
            self._btn_dedup_all.setEnabled(False)
            self._lbl_header.setText("✓ Todos los duplicados han sido procesados.")
        else:
            new_row = min(group_idx, len(self._groups) - 1)
            self._groups_list.setCurrentRow(new_row)
            self._update_header_label()

    def _refresh_list_item(self, group_idx: int) -> None:
        """Update the text of a single list item after a card was removed from it."""
        item = self._groups_list.item(group_idx)
        if item is None or group_idx >= len(self._groups):
            return
        group = self._groups[group_idx]
        best  = self._get_best(group)
        item.setText(
            f"Grupo {group_idx + 1} — {len(group)} archivos"
            f" · {_fmt_bytes(sum(_safe_size(p) for p in group))}\n"
            f"{best.name}"
        )
        self._update_header_label()

    def _update_header_label(self) -> None:
        """Recompute and set the summary label from current group state."""
        n_groups  = len(self._groups)
        n_files   = sum(len(g) for g in self._groups)
        dup_bytes = sum(
            _safe_size(p)
            for i, g in enumerate(self._groups)
            for p, action in self._selections.get(i, {}).items()
            if action == "delete"
        )
        self._lbl_header.setText(
            f"{n_groups} grupo{'s' if n_groups != 1 else ''}"
            f" · {n_files} archivos · {_fmt_bytes(dup_bytes)} duplicados"
        )

    # ── Batch deduplication ────────────────────────────────────────────────

    def _on_dedup_all(self) -> None:
        """Collect all delete-marked paths, confirm, then run _DeduplicateWorker."""
        to_delete: List[Tuple[str, int]] = []   # (abs_path_str, file_size_bytes)
        # Breakdown counters
        del_photos = del_photos_bytes = 0
        del_videos = del_videos_bytes = 0
        keep_photos = keep_videos = 0

        for i, group in enumerate(self._groups):
            sels = self._selections.get(i, {})
            for p in group:
                is_video = p.suffix.lower() in VIDEO_EXTENSIONS
                if sels.get(p) == "delete":
                    sz = _safe_size(p)
                    to_delete.append((str(p), sz))
                    if is_video:
                        del_videos       += 1
                        del_videos_bytes += sz
                    else:
                        del_photos       += 1
                        del_photos_bytes += sz
                else:
                    if is_video:
                        keep_videos += 1
                    else:
                        keep_photos += 1

        if not to_delete:
            mb_info(
                self, "Sin elementos",
                "No hay archivos marcados para eliminar."
            )
            return

        # ── Build detailed confirmation message ─────────────────────────────
        del_lines: List[str] = []
        if del_photos:
            del_lines.append(
                f"  • {del_photos} foto{'s' if del_photos != 1 else ''}"
                f"  ({_fmt_bytes(del_photos_bytes)})"
            )
        if del_videos:
            del_lines.append(
                f"  • {del_videos} video{'s' if del_videos != 1 else ''}"
                f"  ({_fmt_bytes(del_videos_bytes)})"
            )

        keep_lines: List[str] = []
        if keep_photos:
            keep_lines.append(
                f"  • {keep_photos} foto{'s' if keep_photos != 1 else ''}"
            )
        if keep_videos:
            keep_lines.append(
                f"  • {keep_videos} video{'s' if keep_videos != 1 else ''}"
            )

        total_del  = len(to_delete)
        total_size = sum(s for _, s in to_delete)

        msg = (
            f"Se moverán {total_del} archivo{'s' if total_del != 1 else ''}"
            f" a  _duplicados_eliminados  ({_fmt_bytes(total_size)}):\n\n"
            "Se eliminará:\n"
            + "\n".join(del_lines)
            + "\n\nSe conservará:\n"
            + "\n".join(keep_lines)
            + "\n\n¿Continuar?"
        )

        reply = mb_question(self, "Confirmar deduplicación", msg)
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Store item list so _on_dedup_finished can log each file
        self._dedup_items = to_delete

        # Disable all action buttons for the duration
        self._deduplicating = True
        self._update_button_states()

        # Progress dialog (no cancel button — operation is not interruptible)
        self._dedup_total = total_del
        self._dedup_progress_dlg = QProgressDialog(self)
        self._dedup_progress_dlg.setWindowTitle("Deduplicando…")
        self._dedup_progress_dlg.setLabelText("Iniciando…")
        self._dedup_progress_dlg.setRange(0, total_del)
        self._dedup_progress_dlg.setValue(0)
        self._dedup_progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self._dedup_progress_dlg.setCancelButton(None)
        self._dedup_progress_dlg.setMinimumDuration(0)
        self._dedup_progress_dlg.show()
        self.setEnabled(False)
        QApplication.processEvents()   # force paint before thread starts

        # Spin up worker thread
        # Thread lifetime pattern: do NOT connect finished→thread.quit here;
        # _on_dedup_finished calls quit()+wait() directly.
        self._dedup_worker = _DeduplicateWorker(to_delete)
        self._dedup_thread = QThread(self)
        self._dedup_worker.moveToThread(self._dedup_thread)

        self._dedup_thread.started.connect(self._dedup_worker.run)
        self._dedup_worker.progress.connect(self._on_dedup_progress)
        self._dedup_worker.finished.connect(self._on_dedup_finished)
        self._dedup_thread.finished.connect(self._cleanup_dedup_thread)

        self._dedup_thread.start()

    def _on_dedup_progress(self, current: int, total: int, filename: str) -> None:
        if self._dedup_progress_dlg:
            self._dedup_progress_dlg.setValue(current)
            self._dedup_progress_dlg.setLabelText(
                f"Moviendo: {filename}\n{current} de {total}"
            )

    def _on_dedup_finished(
        self, deleted_count: int, bytes_freed: int, errors: List[str]
    ) -> None:
        """Called when _DeduplicateWorker finishes. Quit+wait before UI changes."""
        if self._dedup_thread and self._dedup_thread.isRunning():
            self._dedup_thread.quit()
            self._dedup_thread.wait()

        if self._dedup_progress_dlg:
            self._dedup_progress_dlg.setValue(self._dedup_total)
            self._dedup_progress_dlg.close()
            self._dedup_progress_dlg = None
        self.setEnabled(True)

        # Log each moved file individually
        error_names = {e.split(":")[0] for e in errors}
        for path_str, _ in self._dedup_items:
            p = Path(path_str)
            if p.name not in error_names:
                self._log.log(
                    str(p.parent), p.name,
                    "delete_duplicate", p.name, "deduplicar_todo"
                )
        self._dedup_items = []

        # Clear all group state
        self._deduplicating = False
        self._groups.clear()
        self._selections.clear()
        self._groups_list.clear()
        self._current_group_idx = -1
        self._current_cards.clear()
        self._comparison_scroll.setWidget(QWidget())
        self._right_stack.setCurrentIndex(0)
        self._update_button_states()

        summary = (
            f"✓ {deleted_count} archivo{'s' if deleted_count != 1 else ''}"
            f" movido{'s' if deleted_count != 1 else ''} a _duplicados_eliminados/"
            f"\n{_fmt_bytes(bytes_freed)} liberados"
        )
        if errors:
            summary += (
                f"\n\n⚠ {len(errors)} error{'es' if len(errors) != 1 else ''}:\n"
                + "\n".join(errors[:10])
                + (f"\n… y {len(errors) - 10} más" if len(errors) > 10 else "")
            )
        self._lbl_header.setText(summary)

    def _cleanup_dedup_thread(self) -> None:
        if self._dedup_worker:
            self._dedup_worker.deleteLater()
            self._dedup_worker = None
        if self._dedup_thread:
            self._dedup_thread.deleteLater()
            self._dedup_thread = None
