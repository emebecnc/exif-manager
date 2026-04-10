"""Folder scanning, MD5 hashing, and filesystem helpers with UNC path support."""
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Iterator, List

IMAGE_EXTENSIONS    = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}
WRITABLE_EXTENSIONS = {".jpg", ".jpeg", ".tiff", ".tif"}

# Directories that hold app-managed trash / temp / cache files — never scanned
EXCLUDED_FOLDERS = {
    "_duplicados_eliminados",
    "_eliminados",
    "_thumbcache",
    "__pycache__",
}


def scan_folder(path: Path) -> list:
    """Return sorted list of writable image files (JPG/JPEG/TIFF) in path."""
    results = []
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                suffix = Path(entry.name).suffix.lower()
                if suffix in WRITABLE_EXTENSIONS:
                    results.append(Path(entry.path))
    except (OSError, PermissionError):
        pass
    return sorted(results, key=lambda p: p.name.lower())


def scan_folder_all_images(path: Path) -> list:
    """Return sorted list of ALL supported image files in path (for display)."""
    results = []
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                suffix = Path(entry.name).suffix.lower()
                if suffix in IMAGE_EXTENSIONS:
                    results.append(Path(entry.path))
    except (OSError, PermissionError):
        pass
    return sorted(results, key=lambda p: p.name.lower())


def count_images(path: Path) -> int:
    """Count writable image files in a single folder (non-recursive)."""
    count = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                if Path(entry.name).suffix.lower() in WRITABLE_EXTENSIONS:
                    count += 1
    except (OSError, PermissionError):
        pass
    return count


def list_subdirs(path: Path) -> list:
    """Return sorted list of immediate subdirectory Paths.

    Excludes hidden directories and app-managed folders listed in
    EXCLUDED_FOLDERS so they never appear in the folder tree or lazy-load.
    """
    results = []
    try:
        for entry in os.scandir(path):
            if (entry.is_dir(follow_symlinks=False)
                    and not entry.name.startswith(".")
                    and entry.name not in EXCLUDED_FOLDERS):
                results.append(Path(entry.path))
    except (OSError, PermissionError):
        pass
    return sorted(results, key=lambda p: p.name.lower())


def iter_images_recursive(root_path: Path) -> Iterator[Path]:
    """Yield all writable image files recursively under root_path.

    Skips hidden directories (dot-prefixed) and the app-managed trash/temp
    folders listed in EXCLUDED_FOLDERS so that previously eliminated files
    are never included in duplicate or date-edit scans.
    """
    try:
        for dirpath, dirnames, filenames in os.walk(root_path, onerror=lambda e: None):
            # Skip hidden and app-managed trash/temp directories in-place
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d not in EXCLUDED_FOLDERS
            ]
            for fname in sorted(filenames):
                if Path(fname).suffix.lower() in WRITABLE_EXTENSIONS:
                    yield Path(dirpath) / fname
    except (OSError, PermissionError):
        return


def compute_md5(path: Path) -> str:
    """Compute MD5 hash of file contents. Returns empty string on error."""
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def unique_dest(path: Path, dest_dir: Path) -> Path:
    """Return a collision-free destination path for path inside dest_dir.

    If dest_dir/name already exists, appends _1, _2, … until a free slot
    is found.  Does not create the file.
    """
    dest = dest_dir / path.name
    if not dest.exists():
        return dest
    stem, suffix = path.stem, path.suffix
    i = 1
    while dest.exists():
        dest = dest_dir / f"{stem}_{i}{suffix}"
        i += 1
    return dest


def root_is_available(path: Path) -> bool:
    """Check if a path (including UNC) is accessible."""
    try:
        return path.exists()
    except OSError:
        return False


def read_exif_dates_batch(
    paths: List[Path],
    max_workers: int = 8,
) -> Dict[Path, str]:
    """Read EXIF dates for multiple files concurrently.

    Returns ``{path: date_str}`` where date_str is the best available EXIF
    date (DateTimeOriginal → DateTimeDigitized → DateTime) or "" on failure.
    Uses a ThreadPoolExecutor so I/O-bound reads happen in parallel, which
    dramatically reduces latency for large folders on both local and UNC paths.
    """
    # Imports are here (not top-level) to break the potential circular-import
    # chain: file_scanner ← thumbnail_grid → exif_handler ← file_scanner.
    from core.exif_handler import get_best_date_str, read_exif  # noqa: PLC0415

    def _read_one(path: Path):
        try:
            fields = read_exif(path).get("fields", {})
            return path, get_best_date_str(fields)
        except Exception:
            return path, ""

    results: Dict[Path, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for path, date_str in executor.map(_read_one, paths):
            results[path] = date_str
    return results
