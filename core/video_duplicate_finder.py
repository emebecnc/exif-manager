"""MD5-based duplicate detection for video files."""
from pathlib import Path
from typing import List

from PyQt6.QtCore import QObject, pyqtSignal

from core.video_handler import (
    compute_md5, get_video_metadata, iter_videos_recursive,
)


class VideoDuplicateScanWorker(QObject):
    """
    Worker that scans root_path for duplicate videos (by MD5) in a background
    thread.  Same pattern as DuplicateScanWorker for photos.

    Quality scoring for keep / discard (higher score = better quality):
        score = (width * height) * 0.5 + bitrate * 0.3 + duration_seconds * 0.2
    """

    progress = pyqtSignal(int, int, str)   # current, total, filename
    finished = pyqtSignal(list)            # list of groups; each group = list[Path]
    error    = pyqtSignal(str)

    def __init__(self, root_path: Path, parent=None):
        super().__init__(parent)
        self.root_path  = root_path
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            all_files = list(iter_videos_recursive(self.root_path))
            total = len(all_files)
            md5_map: dict[str, List[Path]] = {}

            for i, path in enumerate(all_files):
                if self._cancelled:
                    partial = [g for g in md5_map.values() if len(g) > 1]
                    partial.sort(key=lambda g: (-len(g), str(g[0])))
                    self.finished.emit(partial)
                    return

                self.progress.emit(i + 1, total, path.name)
                digest = compute_md5(path)
                if digest:
                    md5_map.setdefault(digest, []).append(path)

            groups = [g for g in md5_map.values() if len(g) > 1]
            groups.sort(key=lambda g: (-len(g), str(g[0])))
            self.finished.emit(groups)

        except Exception as e:
            self.error.emit(str(e))


def video_quality_score(path: Path) -> float:
    """Return a quality score for a video file.  Higher = better (prefer to keep)."""
    try:
        meta = get_video_metadata(path)
        w   = meta.get("width",            0) or 0
        h   = meta.get("height",           0) or 0
        br  = meta.get("bitrate",          0) or 0
        dur = meta.get("duration_seconds", 0) or 0
        return w * h * 0.5 + br * 0.3 + dur * 0.2
    except Exception:
        return 0.0
