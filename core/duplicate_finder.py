"""MD5-based exact duplicate detection with a QObject worker for background use."""
from pathlib import Path
from typing import Callable, List, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from core.file_scanner import compute_md5, iter_images_recursive


class DuplicateScanWorker(QObject):
    """Worker that scans root_path for exact duplicates (by MD5) in a background thread."""

    progress = pyqtSignal(int, int, str)   # current, total, current_filename
    finished = pyqtSignal(list)            # list of groups; each group = list[Path]
    error = pyqtSignal(str)

    def __init__(self, root_path: Path, parent=None):
        super().__init__(parent)
        self.root_path = root_path
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            # Collect all files first so we know the total
            all_files = list(iter_images_recursive(self.root_path))
            total = len(all_files)

            md5_map: dict[str, List[Path]] = {}

            for i, path in enumerate(all_files):
                if self._cancelled:
                    self.finished.emit([])
                    return
                self.progress.emit(i + 1, total, path.name)
                digest = compute_md5(path)
                if digest:
                    md5_map.setdefault(digest, []).append(path)

            groups = [paths for paths in md5_map.values() if len(paths) > 1]
            # Sort groups by size descending, then by first path name
            groups.sort(key=lambda g: (-len(g), str(g[0])))
            self.finished.emit(groups)

        except Exception as e:
            self.error.emit(str(e))


def find_duplicates(
    root_path: Path,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> List[List[Path]]:
    """Synchronous duplicate finder. Returns list of groups (each group = list of paths)."""
    all_files = list(iter_images_recursive(root_path))
    total = len(all_files)
    md5_map: dict[str, List[Path]] = {}

    for i, path in enumerate(all_files):
        if progress_callback:
            progress_callback(i + 1, total, path.name)
        digest = compute_md5(path)
        if digest:
            md5_map.setdefault(digest, []).append(path)

    groups = [paths for paths in md5_map.values() if len(paths) > 1]
    groups.sort(key=lambda g: (-len(g), str(g[0])))
    return groups
