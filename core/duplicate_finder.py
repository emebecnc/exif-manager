"""Duplicate detection: exact (MD5) and similar (perceptual hash) modes."""
import gc
import sys
import time
import traceback
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from PyQt6.QtCore import QObject, pyqtSignal
from PIL import Image

from core.exif_handler import (
    get_best_date_str, parse_date_from_filename, parse_exif_dt, read_exif,
)
from core.file_scanner import compute_md5, iter_images_recursive

# Maximum timestamp difference (seconds) to annotate with ⏱️ in the comparison UI.
TIMESTAMP_TOLERANCE: int = 4

# Maximum timestamp spread (seconds) within an MD5 group that is treated as a
# "burst" (same photo shot/copied within 3 minutes) — burst groups are NOT shown
# as duplicates.  Files copied more than BURST_WINDOW seconds apart are true
# duplicates and will appear in the dedup UI.
BURST_WINDOW: int = 180  # 3 minutes


def _file_timestamp(path: Path) -> Optional[float]:
    """Return file timestamp as POSIX seconds.

    Priority: EXIF DateTimeOriginal / DateTimeDigitized / DateTime → filesystem mtime.
    Returns None only if both reads fail.
    """
    try:
        info = read_exif(path)
        date_str = get_best_date_str(info.get("fields", {}))
        if date_str:
            dt = parse_exif_dt(date_str)
            if dt is not None:
                return dt.timestamp()
    except Exception:
        pass
    # Fallback: filesystem mtime
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def is_burst(files_in_group: List[Path], tolerance_seconds: int = BURST_WINDOW) -> bool:
    """Return True if all files in the group have timestamps within *tolerance_seconds*.

    A burst group means the same photo was copied/backed-up within a short time
    window.  When True, the group is excluded from the duplicates list so the
    user doesn't accidentally delete copies made in the same session.

    Returns False (show as duplicate) when timestamps cannot be read for most
    files, since it is safer to show the group than to silently hide it.
    """
    ts = [_file_timestamp(p) for p in files_in_group]
    valid = [t for t in ts if t is not None]
    if len(valid) < 2:
        return False  # can't determine — default to showing as duplicate
    return (max(valid) - min(valid)) <= tolerance_seconds


def extract_date_from_filename(filename: str) -> Optional[date]:
    """Return the calendar date encoded in *filename* (stem only), or None.

    Delegates to :func:`core.exif_handler.parse_date_from_filename` which
    recognises six common patterns (e.g. ``20111224_154046``, ``2011-12-24``).
    """
    stem = Path(filename).stem
    dt = parse_date_from_filename(stem)
    return dt.date() if dt is not None else None


def dates_match(filename_date: date, exif_date) -> bool:  # exif_date: datetime
    """Return True if *filename_date* and *exif_date* share the same year-month-day."""
    return (
        filename_date.year  == exif_date.year
        and filename_date.month == exif_date.month
        and filename_date.day   == exif_date.day
    )


# ── Optional imagehash dependency ─────────────────────────────────────────────
# Loaded lazily so the app works fine without it; only SimilarImageScanWorker
# will refuse to run when the library is absent.
try:
    import imagehash as _imagehash
    from PIL import Image as _PilImage
    IMAGEHASH_AVAILABLE = True
except ImportError:
    IMAGEHASH_AVAILABLE = False


class DuplicateScanWorker(QObject):
    """Worker that scans root_path for exact duplicates (by MD5) in a background thread."""

    progress        = pyqtSignal(int, int, str)   # current, total, current_filename
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
            print(f"[PhotoScan] starting — root: {self.root_path}")

            # ── Collect file list ──────────────────────────────────────────
            try:
                all_files = list(iter_images_recursive(self.root_path))
            except Exception as e_collect:
                self.error_details = traceback.format_exc()
                traceback.print_exc()
                self.error.emit(f"Error collecting file list: {e_collect}")
                return

            total = len(all_files)
            print(f"[PhotoScan] {total} image files found")
            print(f"[INIT] Starting scan, total files: {total}")
            sys.stdout.flush()

            md5_map: dict[str, List[Path]] = {}
            skipped = 0

            # ── Per-file MD5 loop ──────────────────────────────────────────
            for i, path in enumerate(all_files):
                file_num = i + 1
                print(f"[{file_num:04d}] START: {path.name}")
                sys.stdout.flush()

                if self._cancelled:
                    partial = [p for p in md5_map.values() if len(p) > 1]
                    partial.sort(key=lambda g: (-len(g), str(g[0])))
                    self.finished.emit(partial)
                    return

                self.progress.emit(file_num, total, path.name)

                # Wrap each file individually — one bad file must not abort
                try:
                    # Guard: skip missing or zero-byte files
                    try:
                        size = path.stat().st_size
                    except OSError as e_stat:
                        print(f"[{file_num:04d}] SKIP stat failed: {path.name} — {e_stat}")
                        sys.stdout.flush()
                        skipped += 1
                        continue
                    if size == 0:
                        print(f"[{file_num:04d}] SKIP zero-byte: {path.name}")
                        sys.stdout.flush()
                        skipped += 1
                        continue

                    # Validate image with Pillow before MD5
                    try:
                        img = Image.open(str(path))
                        img.verify()
                        img.close()
                    except Exception as e_img:
                        print(f"[{file_num:04d}] BAD IMAGE: {path.name} — {e_img}")
                        sys.stdout.flush()
                        skipped += 1
                        continue

                    digest = compute_md5(path)
                    if digest:
                        md5_map.setdefault(digest, []).append(path)
                    else:
                        print(f"[{file_num:04d}] SKIP MD5 failed (empty digest): {path.name}")
                        sys.stdout.flush()
                        skipped += 1

                except Exception as e_file:
                    print(f"[{file_num:04d}] ERROR unexpected: {path.name}: {e_file}")
                    sys.stdout.flush()
                    skipped += 1
                    continue

                print(f"[{file_num:04d}] SUCCESS: {path.name}")
                sys.stdout.flush()

                # 50-file checkpoint — yield to Qt so the progress dialog repaints.
                # partial_results is NOT emitted mid-scan: rendering groups while
                # the worker thread is still running causes UI/memory conflicts.
                # Groups are shown only once, after finished.emit() completes.
                if file_num % 50 == 0:
                    time.sleep(0)   # release GIL → let Qt main thread repaint
                    print(
                        f"  [checkpoint] {file_num}/{total} processed, "
                        f"{len(md5_map)} unique hashes, {skipped} skipped"
                    )
                    sys.stdout.flush()

            # ── Emit results ───────────────────────────────────────────────
            # Burst filtering: groups where ALL files have timestamps within
            # BURST_WINDOW seconds are NOT treated as duplicates.  They're
            # identical copies made in the same session; keep them all.
            groups = []
            burst_count = 0
            for paths in md5_map.values():
                if len(paths) <= 1:
                    continue
                if is_burst(paths):
                    burst_count += 1
                    print(
                        f"  [burst] {len(paths)} files within {BURST_WINDOW}s "
                        f"— excluded: {[p.name for p in paths]}"
                    )
                else:
                    groups.append(paths)

            groups.sort(key=lambda g: (-len(g), str(g[0])))
            print(
                f"[PhotoScan] done — {total} files, {skipped} skipped, "
                f"{len(groups)} duplicate groups, {burst_count} burst groups excluded"
            )
            for idx, paths in enumerate(groups):
                print(f"  group {idx + 1}: {len(paths)} files — {[p.name for p in paths]}")

            # ── Compute timestamp diffs per group (for ⏱️ UI annotation) ──
            # Uses EXIF datetime when available; falls back to filesystem mtime.
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
                    f.write(f"DuplicateScanWorker error\n{self.error_details}")
                print(f"[PhotoScan] error log written to: {log_path}")
            except Exception:
                pass
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


# ── Perceptual-hash similarity scan ───────────────────────────────────────────

def _phash_groups(
    hashes: List[Tuple[Path, Any]],
    threshold: int,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> List[List[Path]]:
    """Group images whose pHash Hamming distance ≤ threshold using union-find.

    Args:
        hashes:       List of (path, phash_value) tuples.
        threshold:    Maximum Hamming distance to consider two images similar.
                      Typical useful range is 3–15 (default 8 = ~12.5 % of 64 bits).
        is_cancelled: Optional zero-argument callable; when it returns True the
                      comparison loop exits early and an empty list is returned.

    Returns:
        List of groups; each group is a list of Path objects with ≥ 2 members.
        Returns an empty list when cancelled.
    """
    n = len(hashes)
    parent = list(range(n))

    def find(x: int) -> int:
        # Path-compression
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(x: int, y: int) -> None:
        parent[find(x)] = find(y)

    for i in range(n):
        # Check cancellation once per outer-loop iteration (cheap: one bool read)
        if is_cancelled is not None and is_cancelled():
            return []
        for j in range(i + 1, n):
            # imagehash overloads the '-' operator as Hamming distance
            if (hashes[i][1] - hashes[j][1]) <= threshold:
                union(i, j)

    group_map: dict[int, List[Path]] = defaultdict(list)
    for i, (path, _) in enumerate(hashes):
        group_map[find(i)].append(path)

    groups = [g for g in group_map.values() if len(g) > 1]
    groups.sort(key=lambda g: (-len(g), str(g[0])))
    return groups


class SimilarImageScanWorker(QObject):
    """Worker that finds visually similar images using perceptual hashing (pHash).

    Detects duplicates that differ in resolution, compression level, or minor
    edits — cases that MD5 misses.  Requires the ``imagehash`` library::

        pip install imagehash

    Signals are identical to ``DuplicateScanWorker`` so the panel can swap
    workers transparently.
    """

    progress = pyqtSignal(int, int, str)   # current, total, current_filename
    finished = pyqtSignal(list)            # list of groups; each group = list[Path]
    error    = pyqtSignal(str)

    # Default Hamming-distance threshold (0–64 bits).
    # ≤ 2  → extremely strict  (pixel-perfect or near-lossless re-saves only)
    # ≤ 3  → strict            (same image, different resolution/compression) ← default
    # ≤ 5  → moderate          (catches most resizes / light edits)
    #    8  → loose             (was default; caused false positives on different scenes)
    # ≥ 12 → very permissive   (groups visually dissimilar images)
    DEFAULT_THRESHOLD = 3

    def __init__(
        self,
        root_path: Path,
        threshold: int = DEFAULT_THRESHOLD,
        parent=None,
    ):
        super().__init__(parent)
        self.root_path    = root_path
        self.threshold    = threshold
        self._cancelled   = False
        self.error_details: str = ""   # full traceback, readable via _on_scan_error

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:  # noqa: C901
        if not IMAGEHASH_AVAILABLE:
            self.error.emit(
                "La biblioteca 'imagehash' no está instalada.\n\n"
                "Instalá con:\n  pip install imagehash\n\n"
                "Después reiniciá la aplicación."
            )
            return

        try:
            print(
                f"[SimilarScan] starting — root: {self.root_path}, "
                f"threshold: {self.threshold}"
            )

            # ── 1. Collect file list ───────────────────────────────────────
            try:
                all_files = list(iter_images_recursive(self.root_path))
            except Exception as e_collect:
                self.error_details = traceback.format_exc()
                traceback.print_exc()
                self.error.emit(f"Error al listar archivos: {e_collect}")
                return

            total = len(all_files)
            print(f"[SimilarScan] {total} image files found")

            if total == 0:
                self.finished.emit([])
                return

            # ── 2. Compute perceptual hash for every image ─────────────────
            hashes: List[Tuple[Path, Any]] = []
            skipped = 0

            for i, path in enumerate(all_files):
                file_num = i + 1
                print(f"[{file_num:04d}] START: {path.name}")
                sys.stdout.flush()

                if self._cancelled:
                    self.finished.emit([])
                    return

                self.progress.emit(file_num, total, path.name)

                try:
                    st = path.stat()
                    if st.st_size == 0:
                        print(f"[{file_num:04d}] SKIP zero-byte: {path.name}")
                        sys.stdout.flush()
                        skipped += 1
                        continue

                    # `with` ensures the file handle + PIL buffers are always
                    # released even if phash raises; img_rgb is a separate object
                    # so we close it explicitly after hashing.
                    try:
                        with _PilImage.open(path) as img:
                            try:
                                img_rgb = img.convert("RGB")
                            except Exception as e_conv:
                                print(f"[{file_num:04d}] BAD convert RGB: {path.name} — {e_conv}")
                                sys.stdout.flush()
                                skipped += 1
                                continue
                            h = _imagehash.phash(img_rgb)
                            img_rgb.close()
                    except Exception as e_open:
                        print(f"[{file_num:04d}] BAD open: {path.name} — {e_open}")
                        sys.stdout.flush()
                        skipped += 1
                        continue

                    hashes.append((path, h))

                except Exception as e_file:
                    print(f"[{file_num:04d}] ERROR unexpected: {path.name}: {e_file}")
                    sys.stdout.flush()
                    skipped += 1
                    continue

                print(f"[{file_num:04d}] SUCCESS: {path.name}")
                sys.stdout.flush()

                if file_num % 20 == 0:
                    gc.collect()    # free accumulated image buffers every 20 files
                    time.sleep(0)   # release GIL → let Qt main thread repaint
                    print(
                        f"  [checkpoint] {file_num}/{total} hashed, "
                        f"{skipped} skipped"
                    )
                    sys.stdout.flush()

            n = len(hashes)
            print(
                f"[SimilarScan] hashing done — {n} hashed, {skipped} skipped; "
                f"now comparing {n*(n-1)//2} pairs …"
            )

            if self._cancelled:
                self.finished.emit([])
                return

            # ── 3. Pairwise comparison + union-find grouping ───────────────
            # Emit a single "comparing…" progress pulse so the UI doesn't freeze
            self.progress.emit(total, total, "Comparando similares…")

            groups = _phash_groups(hashes, self.threshold,
                                   is_cancelled=lambda: self._cancelled)

            if self._cancelled:
                self.finished.emit([])
                return

            # ── Burst filtering ────────────────────────────────────────────
            # Similar-hash groups where all files are within BURST_WINDOW seconds
            # of each other are burst copies (same moment, different re-saves),
            # not duplicates — exclude them so the user isn't asked to delete them.
            filtered = []
            burst_count = 0
            for grp in groups:
                if is_burst(grp):
                    burst_count += 1
                    print(
                        f"  [burst] {len(grp)} similar files within {BURST_WINDOW}s "
                        f"— excluded: {[p.name for p in grp]}"
                    )
                else:
                    filtered.append(grp)
            groups = filtered

            print(
                f"[SimilarScan] done — {len(groups)} similar groups "
                f"(threshold={self.threshold}), {burst_count} burst groups excluded"
            )
            for idx, grp in enumerate(groups):
                print(f"  group {idx + 1}: {len(grp)} files — {[p.name for p in grp]}")

            self.finished.emit(groups)

        except Exception as e:
            self.error_details = traceback.format_exc()
            traceback.print_exc()
            try:
                log_path = Path(__file__).parent.parent / "scan_error.log"
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(f"SimilarImageScanWorker error\n{self.error_details}")
                print(f"[SimilarScan] error log written to: {log_path}")
            except Exception:
                pass
            self.error.emit(str(e))
