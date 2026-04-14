"""MD5-based duplicate detection for video files."""
import time
import traceback
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from core.video_handler import (
    compute_md5, get_video_metadata, iter_videos_recursive,
)

# Maximum timestamp difference (seconds) to annotate with ⏱️ in the comparison UI.
# Change this single constant to tune the tolerance globally.
TIMESTAMP_TOLERANCE: int = 4


def _file_timestamp(path: Path) -> Optional[float]:
    """Return file timestamp as POSIX seconds.

    Priority: video container creation_time → filesystem mtime.
    Returns None only if both reads fail.
    """
    try:
        meta = get_video_metadata(path)
        ct = meta.get("creation_time")
        if ct is not None:
            return ct.timestamp()
    except Exception:
        pass
    # Fallback: filesystem mtime
    try:
        return path.stat().st_mtime
    except OSError:
        return None


class VideoDuplicateScanWorker(QObject):
    """
    Worker that scans root_path for duplicate videos (by MD5) in a background
    thread.  Same pattern as DuplicateScanWorker for photos.

    Quality scoring for keep / discard (higher score = better quality):
        score = (width * height) * 0.5 + bitrate * 0.3 + duration_seconds * 0.2
    """

    progress        = pyqtSignal(int, int, str)   # current, total, filename
    partial_results = pyqtSignal(list)            # groups found so far (intermediate)
    finished        = pyqtSignal(list)            # final complete list of groups
    error           = pyqtSignal(str)

    def __init__(self, root_path: Path, parent=None):
        super().__init__(parent)
        self.root_path      = root_path
        self._cancelled     = False
        self.error_details: str = ""   # full traceback, readable via _on_scan_error
        # Populated in run() — mtime diff (seconds) per group, parallel to finished groups
        self.group_ts_diffs: list[float] = []

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            print(f"[VideoScan] starting — root: {self.root_path}")

            # ── Collect file list ──────────────────────────────────────────
            try:
                all_files = list(iter_videos_recursive(self.root_path))
            except Exception as e_collect:
                self.error_details = traceback.format_exc()
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

                # 50-file progress checkpoint — yield to event loop so the UI
                # progress dialog updates smoothly on large folders (1600+ files).
                if (i + 1) % 50 == 0:
                    time.sleep(0)   # release GIL → let Qt main thread repaint
                    print(
                        f"  [checkpoint] {i + 1}/{total} processed, "
                        f"{len(md5_map)} unique hashes, {skipped} skipped"
                    )
                    partial = [g for g in md5_map.values() if len(g) > 1]
                    partial.sort(key=lambda g: (-len(g), str(g[0])))
                    self.partial_results.emit(partial)

            # ── Emit results ───────────────────────────────────────────────
            groups = [g for g in md5_map.values() if len(g) > 1]
            groups.sort(key=lambda g: (-len(g), str(g[0])))
            print(
                f"[VideoScan] done — {total} files, {skipped} skipped, "
                f"{len(groups)} duplicate groups"
            )
            for idx, paths in enumerate(groups):
                print(f"  group {idx + 1}: {len(paths)} files — {[p.name for p in paths]}")

            # ── Compute timestamp diffs per group (for ⏱️ UI annotation) ──
            # Uses video container creation_time when available; falls back to mtime.
            self.group_ts_diffs = []
            for grp in groups:
                ts    = [_file_timestamp(p) for p in grp]
                valid = [t for t in ts if t is not None]
                diff  = (max(valid) - min(valid)) if len(valid) >= 2 else 0.0
                self.group_ts_diffs.append(diff)

            self.finished.emit(groups)

        except Exception as e:
            self.error_details = traceback.format_exc()
            traceback.print_exc()
            try:
                log_path = Path(__file__).parent.parent / "scan_error.log"
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(f"VideoDuplicateScanWorker error\n{self.error_details}")
                print(f"[VideoScan] error log written to: {log_path}")
            except Exception:
                pass
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
