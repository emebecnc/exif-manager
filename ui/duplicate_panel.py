"""DuplicatePanel — permanent tab for scanning and resolving duplicate images."""
import gc
import os
import shutil
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from PIL import Image, ImageOps
from PyQt6.QtCore import Qt, QObject, QSize, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QProgressDialog, QPushButton, QScrollArea, QSplitter,
    QStackedWidget, QVBoxLayout, QWidget,
)

from core.duplicate_finder import (
    DuplicateScanWorker, SimilarImageScanWorker, IMAGEHASH_AVAILABLE,
    dates_match, extract_date_from_filename,
)
from core.exif_handler import (
    get_all_metadata, get_best_date_str, parse_exif_dt, read_exif,
)
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
_TS_TOLERANCE_S  = 4.0   # timestamp diff (s) threshold for ⏱️ copy-time annotation
_BATCH_SIZE      = 20    # max groups added to list per QTimer tick (prevents UI freeze)

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
    """Higher = better quality.

    Base score  : (width × height) × 0.6 + file_size × 0.4
    Name bonus  : +1000 when the filename encodes a date that matches the
                  EXIF DateTimeOriginal (year-month-day).  This biases
                  selection toward files whose name is directly derived from
                  the capture date, which are typically the camera originals.
    """
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
    base = (w * h) * 0.6 + size * 0.4

    # Filename-EXIF date matching bonus
    bonus = 0.0
    try:
        fn_date = extract_date_from_filename(path.stem)
        if fn_date is not None:
            info = read_exif(path)
            exif_str = get_best_date_str(info.get("fields", {}))
            if exif_str:
                exif_dt = parse_exif_dt(exif_str)
                if exif_dt is not None and dates_match(fn_date, exif_dt):
                    bonus = 1000.0
    except Exception:
        pass

    return base + bonus


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
        self.stop_requested = False

    def run(self) -> None:
        total         = len(self._items)
        deleted_count = 0
        bytes_freed   = 0
        errors: List[str] = []

        for i, (path_str, file_size) in enumerate(self._items):
            if self.stop_requested:
                self.finished.emit(deleted_count, bytes_freed, errors)
                return
            
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
    keep_clicked       = pyqtSignal(object)        # user clicked "✓ Conservar"
    delete_now         = pyqtSignal(object)        # user clicked "🗑 Eliminar"
    force_keep_toggled = pyqtSignal(object, bool)  # path, checked — "Conservar también"

    def __init__(self, path: Path, is_best: bool, action: str,
                 ts_diff: float = 0.0, parent=None):
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

        # 3. Action buttons — directly below badge
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

        # "Conservar también" checkbox — visible only on non-best (delete) cards
        self._chk_force_keep = QCheckBox("Conservar también")
        self._chk_force_keep.setStyleSheet("color: #aaaaaa; font-size: 9pt; border: none;")
        self._chk_force_keep.setVisible(action == "delete")
        self._chk_force_keep.stateChanged.connect(
            lambda state: self.force_keep_toggled.emit(
                self._path, state == Qt.CheckState.Checked.value
            )
        )
        layout.addWidget(self._chk_force_keep)

        layout.addStretch()

        # ⏱️ Copy-time annotation — shown when mtime of copies differs within tolerance
        if 0 < ts_diff <= _TS_TOLERANCE_S:
            secs = int(round(ts_diff))
            ts_row = QHBoxLayout()
            ts_icon  = QLabel("⏱️")
            ts_icon.setStyleSheet("font-size: 13pt; border: none;")
            ts_lbl   = QLabel(f"+{secs}s diferencia de copia")
            ts_lbl.setStyleSheet("color: #f0b060; font-size: 10pt; border: none;")
            ts_lbl.setToolTip(
                f"Mismo archivo — las copias tienen {secs}s de diferencia en modificación"
            )
            ts_row.addWidget(ts_icon)
            ts_row.addWidget(ts_lbl, 1)
            layout.addLayout(ts_row)

        # 4. Info rows
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
        self._chk_force_keep.setVisible(action == "delete")

    def get_action(self) -> str:
        return self._action

    def is_force_kept(self) -> bool:
        """True when the user checked 'Conservar también' on this card."""
        return self._chk_force_keep.isChecked()


# ── _VideoCard ──────────────────────────────────────────────────────────────────

class _VideoCard(QFrame):
    """Displays one video in the side-by-side duplicate comparison.

    Shows a first-frame thumbnail (via ffmpeg), video metadata
    (resolution, duration, FPS, codec, date) and the same Conservar /
    Eliminar buttons as _PhotoCard.
    """

    keep_clicked       = pyqtSignal(object)
    delete_now         = pyqtSignal(object)
    force_keep_toggled = pyqtSignal(object, bool)  # path, checked — "Conservar también"

    def __init__(self, path: Path, is_best: bool, action: str,
                 ts_diff: float = 0.0, parent=None):
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

        # Action buttons — directly below badge
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

        # "Conservar también" checkbox — visible only on non-best (delete) cards
        self._chk_force_keep = QCheckBox("Conservar también")
        self._chk_force_keep.setStyleSheet("color: #aaaaaa; font-size: 9pt; border: none;")
        self._chk_force_keep.setVisible(action == "delete")
        self._chk_force_keep.stateChanged.connect(
            lambda state: self.force_keep_toggled.emit(
                self._path, state == Qt.CheckState.Checked.value
            )
        )
        layout.addWidget(self._chk_force_keep)

        layout.addStretch()

        # ⏱️ Copy-time annotation — shown when mtime of copies differs within tolerance
        if 0 < ts_diff <= _TS_TOLERANCE_S:
            secs = int(round(ts_diff))
            ts_row = QHBoxLayout()
            ts_icon  = QLabel("⏱️")
            ts_icon.setStyleSheet("font-size: 13pt; border: none;")
            ts_lbl   = QLabel(f"+{secs}s diferencia de copia")
            ts_lbl.setStyleSheet("color: #f0b060; font-size: 10pt; border: none;")
            ts_lbl.setToolTip(
                f"Mismo archivo — las copias tienen {secs}s de diferencia en modificación"
            )
            ts_row.addWidget(ts_icon)
            ts_row.addWidget(ts_lbl, 1)
            layout.addLayout(ts_row)

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
        self._chk_force_keep.setVisible(action == "delete")

    def get_action(self) -> str:
        return self._action

    def is_force_kept(self) -> bool:
        """True when the user checked 'Conservar también' on this card."""
        return self._chk_force_keep.isChecked()


# ── DuplicatePanel ─────────────────────────────────────────────────────────────

class DuplicatePanel(QWidget):
    """Permanent panel for scanning and resolving duplicate photos."""

    scan_started      = pyqtSignal()        # emitted when a scan begins → main window switches tab
    scan_busy_changed = pyqtSignal(bool)   # True = scan running, False = idle → lock folder tree

    def __init__(self, log_manager: LogManager, parent=None) -> None:
        super().__init__(parent)
        self._log = log_manager

        # Paths set by main window
        self._root:           Optional[Path] = None
        self._current_folder: Optional[Path] = None

        # "photo" or "video" — controls button labels; default to photo
        self._media_type: str = "photo"

        # "exact" (MD5) or "similar" (perceptual hash)
        self._scan_mode: str = "exact"

        # Path that was passed to the most recent _begin_scan() call
        self._scanned_path: Optional[Path] = None

        # Scan worker / thread (type is DuplicateScanWorker or VideoDuplicateScanWorker)
        self._scan_worker = None
        self._scan_thread: Optional[QThread] = None
        self._scanning:    bool = False
        self._scan_progress_dlg:  Optional[QProgressDialog] = None
        self._group_progress_dlg: Optional[QProgressDialog] = None  # phase-2 loading
        self._groups_loaded:      int                       = 0     # counter for _load_next_batch
        self._thumb_progress_dlg: Optional[QProgressDialog] = None  # phase-3 thumbnail loading
        self._thumbs_loaded:      int                       = 0     # counter for _load_next_thumbnail
        self._group_ts_diffs:     list[float]               = []    # mtime diff (s) per group index
        self._force_keeps:        Dict[int, Set[str]]       = {}    # group_idx → forced-keep paths

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
        Does NOT switch media type while there are active scan results — that
        would clear self._groups and silently break the Conservar button.
        """
        self.set_current_folder(folder)

        # Don't auto-switch while the user has active scan results to review.
        # set_media_type() calls _restore_groups_display() which clears _groups;
        # any Conservar click after that hits the guard and silently does nothing.
        if self._groups:
            return

        # Auto-detect dominant media type
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
        if folder is not None:
            self._lbl_folder.setText(str(folder))
            self._lbl_folder.setToolTip(str(folder))
        else:
            self._lbl_folder.setText("Sin carpeta seleccionada")
            self._lbl_folder.setToolTip("")

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

    def _set_scan_mode(self, mode: str) -> None:
        """Switch between 'exact' (MD5) and 'similar' (pHash) scan modes."""
        if mode == self._scan_mode:
            return
        self._scan_mode = mode
        self._update_mode_style()

    def _update_mode_style(self) -> None:
        """Apply ON/OFF stylesheet to the scan-mode toggle buttons."""
        _ON  = ("QPushButton { background-color: #5a3d99; border: 1px solid #7c59c9;"
                " border-radius: 10px; color: white; padding: 4px 10px;"
                " font-weight: bold; font-size: 10pt; }"
                "QPushButton:hover { background-color: #7c59c9; }"
                "QPushButton:pressed { background-color: #3d2a6e; }")
        _OFF = ("QPushButton { background-color: #252525; border: 1px solid #404040;"
                " border-radius: 10px; color: #777777; padding: 4px 10px;"
                " font-size: 10pt; }"
                "QPushButton:hover { background-color: #303030; border-color: #5a5a5a;"
                " color: #aaaaaa; }")
        _DIS = ("QPushButton { background-color: #1e1e1e; border: 1px solid #333333;"
                " border-radius: 10px; color: #444444; padding: 4px 10px;"
                " font-size: 10pt; }")
        exact_on   = self._scan_mode == "exact"
        similar_on = self._scan_mode == "similar"
        self._btn_mode_exact.setStyleSheet(_ON if exact_on else _OFF)
        self._btn_mode_exact.setChecked(exact_on)
        if IMAGEHASH_AVAILABLE:
            self._btn_mode_similar.setStyleSheet(_ON if similar_on else _OFF)
            self._btn_mode_similar.setChecked(similar_on)
        else:
            self._btn_mode_similar.setStyleSheet(_DIS)
            self._btn_mode_similar.setChecked(False)

    def _restore_groups_display(
        self,
        groups: List[List[Path]],
        selections: Dict[int, Dict[Path, str]],
    ) -> None:
        """Repopulate the groups list and right panel from cached result data.

        Uses the same _batch_add_groups mechanism as _on_scan_finished so that
        restoring a large cached result set (e.g. after switching tabs) also
        never freezes the UI.
        """
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

        self._btn_dedup_all.setEnabled(True)
        self._lbl_header.setText(f"Cargando {len(self._groups)} grupos…")
        self._batch_add_groups(0)

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

        # ── Current folder path ───────────────────────────────────────────
        self._lbl_folder = QLabel("Sin carpeta seleccionada")
        self._lbl_folder.setWordWrap(True)
        self._lbl_folder.setStyleSheet(
            "font-size: 9pt; color: #888888;"
            " padding: 3px 2px; border-bottom: 1px solid #333333;"
        )
        left_layout.addWidget(self._lbl_folder)

        # ── Media type toggle ─────────────────────────────────────────────
        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(2)
        self._btn_toggle_photo = QPushButton("Fotos")
        self._btn_toggle_photo.setCheckable(True)
        self._btn_toggle_photo.setChecked(True)
        self._btn_toggle_video = QPushButton("Videos")
        self._btn_toggle_video.setCheckable(True)
        self._btn_toggle_all   = QPushButton("Duplicados")
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

        # ── Scan-mode row (Exactos / Similares) ───────────────────────────
        mode_row = QHBoxLayout()
        mode_row.setSpacing(2)
        self._btn_mode_exact   = QPushButton("Exactos")
        self._btn_mode_similar = QPushButton("Similares")
        self._btn_mode_exact.setCheckable(True)
        self._btn_mode_exact.setChecked(True)
        self._btn_mode_similar.setCheckable(True)
        self._btn_mode_exact.setToolTip(
            "Detecta duplicados byte a byte (MD5).\n"
            "Rápido y sin falsos positivos."
        )
        _similar_tip = (
            "Detecta copias redimensionadas o re-comprimidas\n"
            "usando hash perceptual (pHash).\n"
            "Requiere:  pip install imagehash"
        )
        if not IMAGEHASH_AVAILABLE:
            _similar_tip = "imagehash no instalado.\n" + _similar_tip
        self._btn_mode_similar.setToolTip(_similar_tip)
        if not IMAGEHASH_AVAILABLE:
            self._btn_mode_similar.setEnabled(False)
        self._btn_mode_exact.clicked.connect(lambda: self._set_scan_mode("exact"))
        self._btn_mode_similar.clicked.connect(lambda: self._set_scan_mode("similar"))
        mode_row.addWidget(self._btn_mode_exact)
        mode_row.addWidget(self._btn_mode_similar)
        left_layout.addLayout(mode_row)
        self._update_mode_style()

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
        self._btn_dedup_all.setEnabled(
            not busy and bool(self._groups)
        )

    # ── Scan ───────────────────────────────────────────────────────────────

    def _on_scan_folder_clicked(self) -> None:
        if self._current_folder:
            self.start_scan(self._current_folder)

    def _begin_scan(self, path: Path) -> None:
        # Reset all previous results
        self._groups.clear()
        self._selections.clear()
        self._groups_list.clear()
        self._current_group_idx = -1
        self._current_cards.clear()
        self._right_stack.setCurrentIndex(0)
        self._btn_dedup_all.setEnabled(False)
        self._force_keeps.clear()
        if self._thumb_progress_dlg is not None:
            self._thumb_progress_dlg.close()
            self._thumb_progress_dlg = None

        self._scanning = True
        self.scan_busy_changed.emit(True)
        self._scanned_path = path
        mode_label = "similares" if self._scan_mode == "similar" else "exactos"
        self._lbl_header.setText(
            f"Buscando {mode_label} en:\n{path}"
        )
        self._btn_cancel.setVisible(True)
        self._btn_cancel.setEnabled(True)
        self._btn_scan_folder.setEnabled(False)

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
        elif self._scan_mode == "similar":
            self._scan_worker = SimilarImageScanWorker(path)
        else:
            self._scan_worker = DuplicateScanWorker(path)
        self._scan_thread = QThread(self)
        self._scan_worker.moveToThread(self._scan_thread)

        # Thread lifetime pattern (see CLAUDE.md): do NOT connect finished→thread.quit
        # here — _on_scan_finished calls quit()+wait() directly to avoid double-quit.
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.progress.connect(self._on_scan_progress)
        # partial_results: SimilarImageScanWorker doesn't have this signal
        if hasattr(self._scan_worker, 'partial_results'):
            self._scan_worker.partial_results.connect(self._on_partial_results)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.error.connect(self._on_scan_error)
        self._scan_thread.finished.connect(self._cleanup_scan_thread)

        # ── Modal progress dialog (BEFORE thread.start() per CLAUDE.md) ───────
        # None = no Cancel button → the UI's _btn_cancel handles cancellation.
        # Without a Cancel button the QProgressDialog never emits canceled(), which
        # eliminates the re-entrancy crash: setValue() can call processEvents()
        # internally; if canceled() were connected it could null _scan_progress_dlg
        # mid-execution of _on_scan_progress (after the is-not-None guard, before
        # setLabelText), causing AttributeError on NoneType.
        if effective_type == "video":
            _scan_label = "Escaneando videos…"
            _scan_title = "Buscando duplicados de video"
        elif self._scan_mode == "similar":
            _scan_label = "Escaneando similares (pHash)…"
            _scan_title = "Buscando duplicados similares"
        else:
            _scan_label = "Escaneando exactos (MD5)…"
            _scan_title = "Buscando duplicados exactos"

        self._scan_progress_dlg = QProgressDialog(
            _scan_label, None, 0, 0, self
        )
        self._scan_progress_dlg.setWindowTitle(_scan_title)
        self._scan_progress_dlg.setModal(True)           # ApplicationModal
        self._scan_progress_dlg.setMinimumDuration(0)   # show immediately
        self._scan_progress_dlg.setValue(0)
        self._scan_progress_dlg.show()
        QApplication.processEvents()

        self._scan_thread.start()

    def _on_cancel_scan(self) -> None:
        """Cancel a running scan: signal worker, reset UI, clean up thread.

        Must perform a full quit → wait → terminate sequence here because
        the worker may be in a long-running Python computation (e.g. pHash
        comparison) that doesn't process Qt events, so quit() alone won't
        interrupt it.  After this method returns the thread is gone; any
        late ``finished`` or ``error`` signal is ignored by the early-return
        guards in ``_on_scan_finished`` / ``_on_scan_error``.
        """
        # Tell the worker to exit its run-loop at the next cancellation check
        if self._scan_worker is not None:
            self._scan_worker.cancel()

        # Reset scanning state immediately so the UI reflects the cancellation
        self._scanning = False
        self.scan_busy_changed.emit(False)
        self._btn_cancel.setVisible(False)
        self._btn_cancel.setEnabled(False)
        self._lbl_header.setText("⏹ Escaneo cancelado.")
        self._update_button_states()
        if self._scan_progress_dlg is not None:
            self._scan_progress_dlg.close()
            self._scan_progress_dlg = None

        # Stop and wait for the OS thread.
        # quit()  → asks the thread's Qt event loop to stop (helpful when the
        #           worker is waiting for Qt events; no-op for pure Python loops)
        # wait()  → blocks until the OS thread exits (worker returns from run())
        # terminate() → last resort: force-kills the OS thread
        if self._scan_thread is not None and self._scan_thread.isRunning():
            self._scan_thread.quit()
            if not self._scan_thread.wait(5000):          # 5 s cooperative wait
                print("[Cancel] scan thread did not stop — terminating")
                self._scan_thread.terminate()
                self._scan_thread.wait(1000)

        # Schedule deferred deletion; _cleanup_scan_thread may also queue
        # deleteLater but that's safe — Qt ignores duplicate deferred deletes.
        if self._scan_worker:
            self._scan_worker.deleteLater()
            self._scan_worker = None
        if self._scan_thread:
            self._scan_thread.deleteLater()
            self._scan_thread = None

    def _on_cancel_group_loading(self) -> None:
        """Cancel group loading operation."""
        if self._group_progress_dlg is not None:
            self._group_progress_dlg.close()
            self._group_progress_dlg = None
        self._groups_loading = False

    def _on_cancel_thumb_loading(self) -> None:
        """Cancel thumbnail loading operation."""
        if self._thumb_progress_dlg is not None:
            self._thumb_progress_dlg.close()
            self._thumb_progress_dlg = None
        self._thumbs_loaded = 0

    def _on_scan_progress(self, current: int, total: int, fname: str) -> None:
        self._lbl_header.setText(f"Escaneando… {current}/{total}\n{fname}")
        # Capture a local reference BEFORE calling any Qt methods.
        # setValue() can trigger internal processEvents(); a local variable keeps
        # the object alive even if re-entrant code nulls self._scan_progress_dlg.
        dlg = self._scan_progress_dlg
        if dlg is not None:
            dlg.setMaximum(total)
            dlg.setValue(current)
            dlg.setLabelText(f"Escaneando… {current}/{total}\n{fname}")
            # Force repaint even if window is not active (e.g. user switched windows)
            dlg.repaint()
            QApplication.processEvents()

    def _on_partial_results(self, groups: list) -> None:
        """Called during scanning with all groups found so far.

        Appends any newly discovered groups (beyond what's already in the list)
        so the user can start reviewing duplicates before the scan completes.
        Groups are added one at a time — no batching needed here because
        partial_results fires at most every 100 files, so increments are small.
        """
        if not self._scanning:
            return

        norm_groups = [
            [p if isinstance(p, Path) else Path(p) for p in g]
            for g in groups
        ]

        already = len(self._groups)
        new_groups = norm_groups[already:]
        if not new_groups:
            return

        for offset, g in enumerate(new_groups):
            idx = already + offset
            self._groups.append(g)
            best = self._get_best(g)
            best_str = str(best)
            self._selections[idx] = {
                p: ("keep" if str(p) == best_str else "delete") for p in g
            }
            group_size = sum(_safe_size(p) for p in g)
            item = QListWidgetItem(
                f"Grupo {idx + 1} — {len(g)} archivos · {_fmt_bytes(group_size)}\n"
                f"{best.name}"
            )
            item.setSizeHint(QSize(250, _LIST_THUMB_SIZE + 14))
            pix = _load_pixmap(best, _LIST_THUMB_SIZE)
            if pix is not None:
                item.setIcon(QIcon(pix))
            self._groups_list.addItem(item)

        # On first groups found: select row 0 and enable the dedup button
        if already == 0 and new_groups:
            self._groups_list.setCurrentRow(0)
            self._btn_dedup_all.setEnabled(True)

    def _on_scan_finished(self, groups: list) -> None:
        try:
            self._on_scan_finished_inner(groups)
        except Exception:
            import traceback as _tb
            msg = _tb.format_exc()
            print(f"[on_scan_finished] CRASH:\n{msg}")
            try:
                from pathlib import Path as _Path
                log = _Path(__file__).parent.parent / "scan_error.log"
                log.write_text(f"_on_scan_finished crash\n{msg}", encoding="utf-8")
            except Exception:
                pass
            # Reset UI so app stays usable
            self._scanning = False
            self.scan_busy_changed.emit(False)
            self._btn_cancel.setVisible(False)
            self._btn_cancel.setEnabled(False)
            if self._scan_progress_dlg is not None:
                self._scan_progress_dlg.close()
                self._scan_progress_dlg = None
            if self._group_progress_dlg is not None:
                self._group_progress_dlg.close()
                self._group_progress_dlg = None
            self._update_button_states()
            self._lbl_header.setText(f"⚠ Error al cargar grupos.\nVer scan_error.log")

    def _on_scan_finished_inner(self, groups: list) -> None:
        # Cancel was already handled by _on_cancel_scan — ignore this late signal
        if not self._scanning:
            return

        # ── 1. Close scan-phase progress dialog ───────────────────────────────
        if self._scan_progress_dlg is not None:
            self._scan_progress_dlg.close()
            self._scan_progress_dlg = None

        # ── 2. Normalise paths (needed for count before thread cleanup) ────────
        norm_groups = [
            [p if isinstance(p, Path) else Path(p) for p in g]
            for g in groups
        ]

        # ── 3. Show group-loading dialog immediately — gives visual feedback
        #       while quit()+wait() runs in the cleanup below.
        if norm_groups:
            self._group_progress_dlg = QProgressDialog(
                "Cargando grupos…", "Cancelar", 0, len(norm_groups), self
            )
            self._group_progress_dlg.setWindowTitle("Procesando resultados")
            self._group_progress_dlg.setModal(False)
            self._group_progress_dlg.setMinimumDuration(0)
            self._group_progress_dlg.setValue(0)
            self._group_progress_dlg.canceled.connect(self._on_cancel_group_loading)
            self._group_progress_dlg.show()
            QApplication.processEvents()

        # ── 4. Grab per-group mtime diffs BEFORE worker is deleted ────────────
        raw_ts_diffs: list[float] = list(
            getattr(self._scan_worker, 'group_ts_diffs', []) or []
        )

        # ── 5. Full thread cleanup (disconnect signals, quit+wait, deleteLater) ─
        self._cleanup_scan_thread()

        self._scanning = False
        self.scan_busy_changed.emit(False)
        self._btn_cancel.setVisible(False)
        self._btn_cancel.setEnabled(False)
        self._update_button_states()

        if not norm_groups:
            no_dup_msg = "✓ No se encontraron duplicados."
            if self._scanned_path is not None:
                no_dup_msg += f"\nen: {self._scanned_path}"
            self._lbl_header.setText(no_dup_msg)
            self._group_ts_diffs = []
            if self._media_type == "photo":
                self._photo_groups = [];  self._photo_selections = {}
            elif self._media_type == "video":
                self._video_groups = [];  self._video_selections = {}
            else:
                self._all_groups = [];    self._all_selections = {}
            return

        # ── 6. Reset display state ─────────────────────────────────────────────
        self._groups = norm_groups
        # Align timestamp-diff list with groups; pad/trim defensively
        self._group_ts_diffs = raw_ts_diffs[:len(self._groups)]
        while len(self._group_ts_diffs) < len(self._groups):
            self._group_ts_diffs.append(0.0)
        self._selections = {}
        self._current_group_idx = -1
        self._current_cards.clear()
        self._groups_list.clear()
        self._right_stack.setCurrentIndex(0)

        # ── 7. Initialise per-group selections: best → keep, rest → delete ─────
        for i, group in enumerate(self._groups):
            best = self._get_best(group)
            best_str = str(best)
            self._selections[i] = {
                p: ("keep" if str(p) == best_str else "delete") for p in group
            }

        self._btn_dedup_all.setEnabled(True)
        n = len(self._groups)
        self._lbl_header.setText(f"Cargando {n} grupos…")

        # ── 7. Cache results for tab switching ────────────────────────────────
        if self._media_type == "photo":
            self._photo_groups     = list(self._groups)
            self._photo_selections = {k: dict(v) for k, v in self._selections.items()}
        elif self._media_type == "video":
            self._video_groups     = list(self._groups)
            self._video_selections = {k: dict(v) for k, v in self._selections.items()}
        else:
            self._all_groups     = list(self._groups)
            self._all_selections = {k: dict(v) for k, v in self._selections.items()}

        # ── 8. Free worker memory + flush any remaining queued events ──────────
        gc.collect()
        QApplication.processEvents()

        # ── 9. Load groups one per timer tick — dialog stays open and updates live.
        # _group_progress_dlg is non-modal (session 51) so QTimer.singleShot fires
        # freely; no need to close it first.  _load_next_batch closes it when done.
        self._groups_list.clear()
        self._groups_loaded = 0
        self._lbl_header.setText(f"Cargando {len(self._groups)} grupos…")
        QTimer.singleShot(0, self._load_next_batch)

    def _load_next_batch(self) -> None:
        """Load exactly one group item per QTimer tick.

        Called by QTimer.singleShot(0) from _on_scan_finished_inner and
        re-schedules itself until all groups are loaded.  Processing one item
        per tick gives Qt a full event-loop cycle between each addition, so
        the UI stays responsive and _group_progress_dlg updates smoothly.
        _batch_add_groups (used by _restore_groups_display) is unchanged.
        """
        total = len(self._groups)
        if self._groups_loaded < total:
            try:
                self._add_group_item(self._groups_loaded)
            except Exception as e:
                print(f"  [skip] _load_next_batch group {self._groups_loaded}: {e}")

            self._groups_loaded += 1

            # Update progress dialog (non-modal — safe to repaint here)
            dlg = self._group_progress_dlg
            if dlg is not None:
                dlg.setValue(self._groups_loaded)
                dlg.setLabelText(f"Cargando grupos… {self._groups_loaded}/{total}")
                dlg.repaint()

            # Select the first row as soon as it appears
            if self._groups_loaded == 1 and self._groups_list.count() > 0:
                self._groups_list.setCurrentRow(0)

            QTimer.singleShot(0, self._load_next_batch)   # yield, then continue
        else:
            # All groups loaded — close dialog, finalise header, start thumbnail phase
            if self._group_progress_dlg is not None:
                self._group_progress_dlg.close()
                self._group_progress_dlg = None
            self._update_header_label()
            QTimer.singleShot(0, self._load_thumbnails_batched)

    def _load_thumbnails_batched(self) -> None:
        """Start the thumbnail phase — one list-item icon per timer tick.

        Called automatically by _load_next_batch when all text rows are in place.
        Shows a non-modal progress dialog so the user can already start clicking
        groups while thumbnails load in the background.
        """
        n = self._groups_list.count()
        if n == 0:
            return
        self._thumbs_loaded = 0
        self._thumb_progress_dlg = QProgressDialog(
            f"Cargando miniaturas… 0/{n}", "Cancelar", 0, n, self
        )
        self._thumb_progress_dlg.setWindowTitle("Cargando miniaturas")
        self._thumb_progress_dlg.setModal(False)
        self._thumb_progress_dlg.setMinimumDuration(0)
        self._thumb_progress_dlg.setValue(0)
        self._thumb_progress_dlg.canceled.connect(self._on_cancel_thumb_loading)
        self._thumb_progress_dlg.show()
        QTimer.singleShot(0, self._load_next_thumbnail)

    def _load_next_thumbnail(self) -> None:
        """Load one group-list icon per timer tick."""
        n = self._groups_list.count()
        if self._thumbs_loaded < n and self._thumbs_loaded < len(self._groups):
            idx = self._thumbs_loaded
            try:
                item = self._groups_list.item(idx)
                if item is not None:
                    best = self._get_best(self._groups[idx])
                    pix  = _load_pixmap(best, _LIST_THUMB_SIZE)
                    if pix is not None:
                        item.setIcon(QIcon(pix))
            except Exception as e:
                print(f"  [thumb] group {idx}: {e}")

            self._thumbs_loaded += 1
            dlg = self._thumb_progress_dlg
            if dlg is not None:
                dlg.setValue(self._thumbs_loaded)
                dlg.setLabelText(f"Cargando miniaturas… {self._thumbs_loaded}/{n}")
                dlg.repaint()

            QTimer.singleShot(0, self._load_next_thumbnail)
        else:
            if self._thumb_progress_dlg is not None:
                self._thumb_progress_dlg.close()
                self._thumb_progress_dlg = None

    def _add_group_item(self, idx: int) -> None:
        """Build and append one QListWidgetItem for groups[idx].

        Intentionally text-only (no thumbnail icon) so list population is
        near-instant even for 600+ groups.  The full-size thumbnail is loaded
        on demand inside _show_group() when the user clicks a row.
        """
        if idx >= len(self._groups):
            return
        try:
            group      = self._groups[idx]
            if not group:
                print(f"  [skip] group {idx} is empty")
                return
            best       = self._get_best(group)
            group_size = sum(_safe_size(p) for p in group)
            item = QListWidgetItem(
                f"Grupo {idx + 1} — {len(group)} archivos · {_fmt_bytes(group_size)}\n"
                f"{best.name}"
            )
            item.setSizeHint(QSize(250, _LIST_THUMB_SIZE + 14))  # reserves icon row height
            self._groups_list.addItem(item)
        except Exception as e:
            print(f"  [skip] _add_group_item({idx}) failed: {e}")

    # ── Non-blocking group loader — used by _on_scan_finished_inner + _restore_groups_display ──

    def _batch_add_groups(self, start: int) -> None:
        """Add up to _BATCH_SIZE group items to the list starting at *start*.

        After each batch the method re-schedules itself via QTimer.singleShot(0)
        so Qt can process pending events (repaints, user input) between chunks.
        This prevents the main thread from blocking when there are hundreds of
        groups to display.  Must NOT be called while a modal QProgressDialog is
        open — the modal blocks the event loop, preventing the timer from firing.
        """
        if start >= len(self._groups):
            # All groups are in the list — finalise.
            self._update_header_label()
            if self._groups_list.count() > 0 and self._groups_list.currentRow() < 0:
                self._groups_list.setCurrentRow(0)
            return

        end = min(start + _BATCH_SIZE, len(self._groups))

        self._groups_list.setUpdatesEnabled(False)
        for i in range(start, end):
            group      = self._groups[i]
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
        self._groups_list.setUpdatesEnabled(True)

        # Select first row once it exists
        if start == 0 and self._groups_list.count() > 0:
            self._groups_list.setCurrentRow(0)

        remaining = len(self._groups) - end
        if remaining > 0:
            self._lbl_header.setText(
                f"Cargando grupos… {end}/{len(self._groups)}"
            )
            QTimer.singleShot(0, lambda: self._batch_add_groups(end))
        else:
            self._update_header_label()

    def _on_scan_error(self, msg: str) -> None:
        # Cancel was already handled by _on_cancel_scan — ignore this late signal
        if not self._scanning:
            return

        # Quit+wait the thread FIRST — same pattern as _on_scan_finished.
        # Without this, the QThread is destroyed while still running → crash -805306369.
        if self._scan_thread and self._scan_thread.isRunning():
            self._scan_thread.quit()
            self._scan_thread.wait(5000)
            if self._scan_thread and self._scan_thread.isRunning():
                self._scan_thread.terminate()
                self._scan_thread.wait(1000)

        # Grab full traceback BEFORE deleteLater wipes the worker object
        details = getattr(self._scan_worker, "error_details", "") or msg

        # Explicit cleanup (same as _on_scan_finished)
        if self._scan_worker:
            self._scan_worker.deleteLater()
            self._scan_worker = None
        if self._scan_thread:
            self._scan_thread.deleteLater()
            self._scan_thread = None

        self._scanning = False
        self.scan_busy_changed.emit(False)
        self._btn_cancel.setVisible(False)
        self._btn_cancel.setEnabled(False)
        self._update_button_states()
        if self._scan_progress_dlg is not None:
            self._scan_progress_dlg.close()
            self._scan_progress_dlg = None

        self._lbl_header.setText(f"⚠ Error al escanear:\n{msg}")
        print(f"ERROR in scan: {msg}")

        # Show a dialog with the full traceback accessible via "Show Details"
        dlg = QMessageBox(self)
        dlg.setIcon(QMessageBox.Icon.Critical)
        dlg.setWindowTitle("Error al escanear")
        dlg.setText("Se produjo un error inesperado durante el escaneo.")
        dlg.setInformativeText(str(msg))
        dlg.setDetailedText(details)   # "Show Details" button reveals full traceback
        dlg.exec()

    def _cleanup_scan_thread(self) -> None:
        """Stop scan thread and free all objects.  Safe to call from
        _on_scan_finished (thread running) AND as the thread.finished slot
        (thread already stopped).  The None-guards prevent double-deleteLater.
        """
        if self._scan_thread is None and self._scan_worker is None:
            return

        # Disconnect worker signals so no stale callbacks fire after cleanup
        for obj, sig_name in [
            (self._scan_thread, "started"),
            (self._scan_worker, "progress"),
            (self._scan_worker, "finished"),
            (self._scan_worker, "error"),
        ]:
            if obj is None:
                continue
            try:
                getattr(obj, sig_name).disconnect()
            except Exception:
                pass

        # Stop thread — no-op when already stopped (isRunning() == False)
        if self._scan_thread is not None and self._scan_thread.isRunning():
            self._scan_thread.quit()
            if not self._scan_thread.wait(5000):
                print("[cleanup] scan thread still running — terminating")
                self._scan_thread.terminate()
                self._scan_thread.wait(1000)

        if self._scan_worker is not None:
            self._scan_worker.deleteLater()
            self._scan_worker = None
        if self._scan_thread is not None:
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
        if group_idx < 0 or group_idx >= len(self._groups):
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

        force_keeps = self._force_keeps.get(group_idx, set())
        ts_diff = (
            self._group_ts_diffs[group_idx]
            if group_idx < len(self._group_ts_diffs) else 0.0
        )

        for p in sorted_group:
            action = sels.get(p, "keep" if str(p) == str(best) else "delete")
            card   = CardClass(p, is_best=(str(p) == str(best)), action=action,
                               ts_diff=ts_diff)

            # Default-argument capture avoids the classic loop-closure bug:
            # each lambda captures the value of gi and emitted at definition time.
            card.keep_clicked.connect(
                lambda emitted, gi=group_idx: self._on_card_keep(emitted, gi)
            )
            card.delete_now.connect(
                lambda emitted, gi=group_idx: self._on_card_delete_now(emitted, gi)
            )
            card.force_keep_toggled.connect(
                lambda emitted, checked, gi=group_idx: self._on_force_keep_toggled(emitted, checked, gi)
            )

            # Restore "Conservar también" state if user already checked it
            if str(p) in force_keeps:
                card._chk_force_keep.setChecked(True)

            self._current_cards[p] = card
            layout.addWidget(card)

        layout.addStretch()
        self._comparison_scroll.setWidget(container)
        self._right_stack.setCurrentIndex(1)

    # ── Card signal handlers ───────────────────────────────────────────────

    def _on_card_keep(self, path_obj: object, group_idx: int) -> None:
        """User clicked 'Conservar' — immediately move every other file in the group
        to _duplicados_eliminados/, remove the group from the list, and advance."""
        print(f"[Conservar] clicked: group_idx={group_idx}, groups={len(self._groups)}, path={path_obj}")
        path = path_obj if isinstance(path_obj, Path) else Path(path_obj)
        if group_idx >= len(self._groups):
            print(f"[Conservar] GUARD FIRED — group_idx {group_idx} >= {len(self._groups)} groups. Bug!")
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

        print(f"[Conservar] to_trash={len(to_trash)}, deleted={deleted}, errors={errors}")
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

    def _on_force_keep_toggled(self, path_obj: object, checked: bool, group_idx: int) -> None:
        """User toggled 'Conservar también' on a card — persist in _force_keeps."""
        path = path_obj if isinstance(path_obj, Path) else Path(path_obj)
        if group_idx not in self._force_keeps:
            self._force_keeps[group_idx] = set()
        if checked:
            self._force_keeps[group_idx].add(str(path))
        else:
            self._force_keeps[group_idx].discard(str(path))

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
        summary = (
            f"{n_groups} grupo{'s' if n_groups != 1 else ''}"
            f" · {n_files} archivos · {_fmt_bytes(dup_bytes)} duplicados"
        )
        if self._scanned_path is not None:
            summary += f"\nen: {self._scanned_path}"
        self._lbl_header.setText(summary)

    # ── Batch deduplication ────────────────────────────────────────────────

    def _on_dedup_all(self) -> None:
        """Collect all delete-marked paths, confirm, then run _DeduplicateWorker."""
        to_delete: List[Tuple[str, int]] = []   # (abs_path_str, file_size_bytes)
        # Breakdown counters
        del_photos = del_photos_bytes = 0
        del_videos = del_videos_bytes = 0
        keep_photos = keep_videos = 0

        for i, group in enumerate(self._groups):
            sels        = self._selections.get(i, {})
            force_keeps = self._force_keeps.get(i, set())
            for p in group:
                is_video = p.suffix.lower() in VIDEO_EXTENSIONS
                if sels.get(p) == "delete" and str(p) not in force_keeps:
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

        # Progress dialog with cancel button
        self._dedup_total = total_del
        self._dedup_progress_dlg = QProgressDialog(self)
        self._dedup_progress_dlg.setWindowTitle("Deduplicando…")
        self._dedup_progress_dlg.setLabelText("Iniciando…")
        self._dedup_progress_dlg.setRange(0, total_del)
        self._dedup_progress_dlg.setValue(0)
        self._dedup_progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self._dedup_progress_dlg.setCancelButton(None)
        self._dedup_progress_dlg.setMinimumDuration(0)
        self._dedup_progress_dlg.canceled.connect(self._on_cancel_dedup)
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

    def _on_cancel_dedup(self) -> None:
        """Cancel dedup operation."""
        if self._dedup_worker is not None:
            self._dedup_worker.stop_requested = True
        if self._dedup_progress_dlg:
            self._dedup_progress_dlg.close()
            self._dedup_progress_dlg = None
        self.setEnabled(True)
        self._deduplicating = False
        self._update_button_states()

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
