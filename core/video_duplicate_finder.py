"""MD5-based duplicate detection for video files."""
import traceback
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
            print(f"[VideoScan] starting — root: {self.root_path}")

            # ── Collect file list ──────────────────────────────────────────
            try:
                all_files = list(iter_videos_recursive(self.root_path))
            except Exception as e_collect:
                traceback.print_exc()
                self.error.emit(f"Error collecting video list: {e_collect}")
                return

            total = len(all_files)
            print(f"[VideoScan] {total} video files found")

            md5_map: dict[str, List[Path]] = {}
            skipped = 0

            # ── Per-file MD5 loop ──────────────────────────────────────────
            for i, path in enumerate(all_files):
                if self._cancelled:
                    partial = [g for g in md5_map.values() if len(g) > 1]
                    partial.sort(key=lambda g: (-len(g), str(g[0])))
                    self.finished.emit(partial)
                    return

                self.progress.emit(i + 1, total, path.name)

                # Wrap each file — one unreadable video must not abort the scan
                try:
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
            groups = [g for g in md5_map.values() if len(g) > 1]
            groups.sort(key=lambda g: (-len(g), str(g[0])))
            print(
                f"[VideoScan] done — {total} files, {skipped} skipped, "
                f"{len(groups)} duplicate groups"
            )
            for idx, paths in enumerate(groups):
                print(f"  group {idx + 1}: {len(paths)} files — {[p.name for p in paths]}")
            self.finished.emit(groups)

        except Exception as e:
            traceback.print_exc()
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
