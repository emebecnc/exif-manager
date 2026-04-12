"""MD5-based exact duplicate detection with a QObject worker for background use."""
import traceback
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
            print(f"[PhotoScan] starting — root: {self.root_path}")

            # ── Collect file list ──────────────────────────────────────────
            try:
                all_files = list(iter_images_recursive(self.root_path))
            except Exception as e_collect:
                traceback.print_exc()
                self.error.emit(f"Error collecting file list: {e_collect}")
                return

            total = len(all_files)
            print(f"[PhotoScan] {total} image files found")

            md5_map: dict[str, List[Path]] = {}
            skipped = 0

            # ── Per-file MD5 loop ──────────────────────────────────────────
            for i, path in enumerate(all_files):
                if self._cancelled:
                    partial = [p for p in md5_map.values() if len(p) > 1]
                    partial.sort(key=lambda g: (-len(g), str(g[0])))
                    self.finished.emit(partial)
                    return

                self.progress.emit(i + 1, total, path.name)

                # Wrap each file individually — one bad file must not abort
                try:
                    # Guard: skip missing or zero-byte files
                    try:
                        size = path.stat().st_size
                    except OSError as e_stat:
                        print(f"  [skip] stat failed: {path.name} — {e_stat}")
                        skipped += 1
                        continue
                    if size == 0:
                        print(f"  [skip] zero-byte file: {path.name}")
                        skipped += 1
                        continue

                    digest = compute_md5(path)
                    if digest:
                        md5_map.setdefault(digest, []).append(path)
                    else:
                        print(f"  [skip] MD5 failed (empty digest): {path.name}")
                        skipped += 1

                except Exception as e_file:
                    print(f"  [skip] unexpected error on {path.name}: {e_file}")
                    skipped += 1
                    continue

                # 100-file progress checkpoint
                if (i + 1) % 100 == 0:
                    print(
                        f"  [checkpoint] {i + 1}/{total} processed, "
                        f"{len(md5_map)} unique hashes, {skipped} skipped"
                    )

            # ── Emit results ───────────────────────────────────────────────
            groups = [p for p in md5_map.values() if len(p) > 1]
            groups.sort(key=lambda g: (-len(g), str(g[0])))
            print(
                f"[PhotoScan] done — {total} files, {skipped} skipped, "
                f"{len(groups)} duplicate groups"
            )
            for idx, paths in enumerate(groups):
                print(f"  group {idx + 1}: {len(paths)} files — {[p.name for p in paths]}")
            self.finished.emit(groups)

        except Exception as e:
            traceback.print_exc()
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
