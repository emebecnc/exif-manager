"""Video metadata reading, writing, and thumbnail extraction via ffmpeg / hachoir."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional


def _subprocess_no_window() -> dict:
    """Return extra kwargs that suppress the console window on Windows.

    When the app runs as a PyInstaller --noconsole (windowed) .exe, every
    subprocess.run() call spawns a visible black CMD window unless suppressed.
    capture_output=True only redirects stdout/stderr — it does NOT prevent the
    window from being created.  CREATE_NO_WINDOW is the correct fix.

    On non-Windows platforms the function returns an empty dict so callers are
    portable without any conditional logic at each call site.
    """
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }

# ── Extension registry ────────────────────────────────────────────────────────

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".3gp", ".m4v", ".wmv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".mts",
}
print(f"[VIDEO INIT] VIDEO_EXTENSIONS={VIDEO_EXTENSIONS} | type={type(VIDEO_EXTENSIONS)}", flush=True)

# Formats that reliably support container-level metadata rewrite via
# `ffmpeg -codec copy`.  .3gp triggers "Operation not permitted" errors on
# some platforms because ffmpeg needs to rewrite the full atom structure.
_SUPPORTED_WRITE_FORMATS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".wmv"}

# Override these to use a bundled ffmpeg/ffprobe binary
FFMPEG_CMD  = "ffmpeg"
FFPROBE_CMD = "ffprobe"

# Set by main.py after the startup binary check so callers skip the
# subprocess entirely when ffmpeg is known to be unavailable.
# None  → not yet checked (first call will attempt normally and may fail)
# True  → binary confirmed reachable
# False → binary confirmed missing; all ffmpeg-dependent calls return early
FFMPEG_AVAILABLE: bool | None = None


def set_ffmpeg_available(ok: bool) -> None:
    """Called once by main.py after the startup _check_ffmpeg() result."""
    global FFMPEG_AVAILABLE
    FFMPEG_AVAILABLE = ok


# Camera factory-reset years (same logic as exif_handler)
_INVALID_YEARS = {2000, 2005}


# ── Public helpers ────────────────────────────────────────────────────────────

def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def format_duration(seconds: float) -> str:
    """Format float seconds → 'H:MM:SS' or 'M:SS'."""
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def format_size(size_bytes: int) -> str:
    """Format byte count as human-readable string."""
    if size_bytes >= 1_073_741_824:
        return f"{size_bytes / 1_073_741_824:.1f} GB"
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes} B"


# ── Metadata reading ──────────────────────────────────────────────────────────

def get_video_metadata(path: Path) -> dict:
    """
    Return a metadata dict for a video file.  Always succeeds — sets
    'error' key on failure.  Fields:
      duration_seconds, width, height, fps, codec_video, codec_audio,
      bitrate, size_bytes, creation_time (datetime|None),
      date_modified (datetime), date_created (datetime),
      make, model, rotation, format_name, all_tags, error.
    """
    result: dict = {
        "duration_seconds": 0.0,
        "width": 0,
        "height": 0,
        "fps": 0.0,
        "codec_video": "",
        "codec_audio": "",
        "bitrate": 0,
        "size_bytes": 0,
        "creation_time": None,
        "date_modified": None,
        "date_created": None,
        "make": "",
        "model": "",
        "rotation": 0,
        "format_name": "",
        "all_tags": {},
        "error": None,
    }

    # File stats (always available)
    try:
        stat = path.stat()
        result["size_bytes"]    = stat.st_size
        result["date_modified"] = datetime.fromtimestamp(stat.st_mtime)
        result["date_created"]  = datetime.fromtimestamp(stat.st_ctime)
    except OSError as e:
        result["error"] = str(e)
        return result

    # Try ffprobe; fall back to hachoir when unavailable
    if not _read_ffprobe(path, result):
        _read_hachoir(path, result)

    return result


def get_best_date(metadata: dict) -> Optional[datetime]:
    """Date priority: metadata creation_time → file modification date."""
    ct = metadata.get("creation_time")
    if ct:
        return ct
    return metadata.get("date_modified")


def is_invalid_date(dt: Optional[datetime]) -> bool:
    """True if the date looks like a camera factory-reset date (or is None)."""
    if dt is None:
        return True
    return dt.year in _INVALID_YEARS and dt.month == 1 and dt.day == 1


# ── Metadata writing ──────────────────────────────────────────────────────────

def write_video_date(
    path: Path,
    new_dt: datetime,
    *,
    sync_mtime: bool = True,
    sync_creation: bool = True,
) -> bool:
    """Write a new creation_time to video container metadata using ffmpeg -codec copy.

    Operation is atomic: writes to a temp file then renames over the original.

    sync_mtime=True    → os.utime() updates the file's mtime/atime after the write.
    sync_creation=True → win32file.SetFileTime() updates the Windows creation date.
    Both default to True so existing callers keep the previous behaviour.

    Returns:
        True  — metadata written successfully.
        False — format not supported (e.g. .3gp) or ffmpeg reported an error.
                The original file is left untouched.

    Unlike the old behaviour this function never raises — callers should check
    the return value instead of catching exceptions.
    """
    # Skip formats that don't support lossless container rewrites.
    # .3gp in particular triggers "Operation not permitted" on many builds of
    # ffmpeg because the 3GPP atom layout cannot be patched without re-muxing.
    if path.suffix.lower() not in _SUPPORTED_WRITE_FORMATS:
        return False

    iso = new_dt.strftime("%Y-%m-%dT%H:%M:%S")
    tmp_fd, tmp_path_str = tempfile.mkstemp(
        dir=str(path.parent), suffix=path.suffix
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_path_str)
    try:
        cmd = [
            FFMPEG_CMD, "-y",
            "-i", str(path),
            "-metadata", f"creation_time={iso}",
            "-codec", "copy",
            "-movflags", "use_metadata_tags",
            str(tmp_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=120,
                              **_subprocess_no_window())
        if proc.returncode != 0:
            # ffmpeg printed an error — clean up and report failure
            try:
                tmp_path.unlink()
            except OSError:
                pass
            return False

        os.replace(str(tmp_path), str(path))

    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return False

    # Best-effort filesystem timestamp sync (caller-controlled)
    ts = new_dt.timestamp()
    if sync_mtime:
        try:
            os.utime(str(path), (ts, ts))
        except OSError:
            pass
    if sync_creation:
        # Windows-only: update creation time via pywin32
        try:
            import win32file   # type: ignore[import]
            import pywintypes  # type: ignore[import]
            handle = win32file.CreateFile(
                str(path),
                win32file.GENERIC_WRITE,
                win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                None,
                win32file.OPEN_EXISTING,
                0,
                None,
            )
            win_time = pywintypes.Time(int(ts))
            win32file.SetFileTime(handle, win_time, win_time, win_time)
            win32file.CloseHandle(handle)
        except Exception:
            pass  # pywin32 not available or non-Windows — skip creation-time update

    return True


# ── Thumbnail extraction ──────────────────────────────────────────────────────

def get_video_thumbnail(path: Path, size: int = 150) -> Optional[bytes]:
    """
    Extract a frame at 00:00:01 as JPEG bytes using ffmpeg.
    Retries at 00:00:00 for clips shorter than 1 second.
    Returns None on failure or when ffmpeg is unavailable.
    """
    if FFMPEG_AVAILABLE is False:
        return None
    try:
        cmd = [
            FFMPEG_CMD, "-y",
            "-ss", "00:00:01",
            "-i", str(path),
            "-vframes", "1",
            "-vf", f"scale={size}:{size}:force_original_aspect_ratio=decrease",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "-",
        ]
        _nownd = _subprocess_no_window()
        proc = subprocess.run(cmd, capture_output=True, timeout=30, **_nownd)
        if proc.returncode != 0 or not proc.stdout:
            # Retry at t=0 (very short clips)
            cmd[3] = "00:00:00"
            proc = subprocess.run(cmd, capture_output=True, timeout=30, **_nownd)
        if proc.stdout:
            return bytes(proc.stdout)
        return None
    except Exception:
        return None


# ── File system helpers ───────────────────────────────────────────────────────

def compute_md5(path: Path, chunk_size: int = 65536) -> str:
    """MD5 of file contents. Returns '' on error."""
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def scan_video_folder(path: Path) -> List[Path]:
    """Return sorted list of video files in path (non-recursive)."""
    # Use a local inline set to guarantee no shadowing of the module-level constant.
    # Common video formats: containers (mp4/mov/mkv/avi), MPEG transport streams
    # (ts/m2ts/mts), MPEG program streams (mpg/mpeg), and mobile formats (3gp/m4v/wmv).
    ALLOWED_EXTS = {
        ".mp4", ".mov", ".avi", ".mkv", ".3gp", ".m4v", ".wmv",
        ".mpg", ".mpeg", ".ts", ".m2ts", ".mts",
    }
    video_files: List[Path] = []
    print(f"[SCAN START] folder={path}  ALLOWED_EXTS={ALLOWED_EXTS}", flush=True)
    try:
        all_files = list(path.glob("*"))
        print(f"[SCAN START] total entries={len(all_files)}", flush=True)
        for f in all_files:
            if not f.is_file():
                continue
            suffix_lower = f.suffix.lower()
            if suffix_lower in ALLOWED_EXTS:
                video_files.append(f)
                print(f"[VIDEO ADDED] {f.name}", flush=True)
            else:
                print(f"[VIDEO SKIP] {f.name}  suffix={suffix_lower!r}  not in ALLOWED_EXTS", flush=True)
    except (OSError, PermissionError) as e:
        print(f"[VIDEO SCAN] exception: {e}", flush=True)
    video_files = sorted(video_files, key=lambda p: p.name.lower())
    print(f"[VIDEO SCAN] Found {len(video_files)} videos in {path}: {[v.name for v in video_files]}", flush=True)
    return video_files


def iter_videos_recursive(root_path: Path) -> Iterator[Path]:
    """Yield all video files under root_path, skipping excluded folders."""
    from core.file_scanner import EXCLUDED_FOLDERS
    try:
        for dirpath, dirnames, filenames in os.walk(
            root_path, onerror=lambda _: None
        ):
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d not in EXCLUDED_FOLDERS
            ]
            for fname in sorted(filenames):
                if Path(fname).suffix.lower() in VIDEO_EXTENSIONS:
                    yield Path(dirpath) / fname
    except (OSError, PermissionError):
        return


def make_dated_filename(
    dt: datetime,
    folder: Path,
    suffix: str,
    used: Optional[set] = None,
    original_stem: Optional[str] = None,
    exclude: Optional[str] = None,
) -> str:
    """Collision-free filename like 2007-09-29-02h47m07s.mp4.

    exclude: the current on-disk filename of the file being renamed.  When the
    candidate equals this name the disk-existence check is skipped so that a
    file whose new name equals its current name does not receive a spurious
    _1 suffix.
    """
    base = dt.strftime("%Y-%m-%d-%Hh%Mm%Ss")
    if original_stem:
        base = f"{base}_{original_stem}"
    ext  = suffix.lower()
    used = used if used is not None else set()

    candidate = f"{base}{ext}"
    on_disk = candidate != exclude and (folder / candidate).exists()
    if not on_disk and candidate not in used:
        return candidate
    i = 1
    while True:
        candidate = f"{base}_{i}{ext}"
        on_disk = candidate != exclude and (folder / candidate).exists()
        if not on_disk and candidate not in used:
            return candidate
        i += 1


# ── Backup / restore ──────────────────────────────────────────────────────────

BACKUP_FILENAME = ".video_backup.json"


def has_video_backup(folder: Path) -> bool:
    return (folder / BACKUP_FILENAME).exists()


def backup_video_metadata(folder: Path, filename: str, metadata: dict) -> None:
    """Append one entry to folder/.video_backup.json (atomic write).

    Saves all recoverable fields so that restoring actually brings back the
    original date.  Raises on I/O errors so callers can decide whether to
    abort or proceed without a backup.
    """
    backup_path = folder / BACKUP_FILENAME
    if backup_path.exists():
        with open(backup_path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {
            "_meta": {
                "created_at": datetime.now().isoformat(timespec="seconds")
            }
        }

    # Serialise all useful fields; convert datetime → ISO string
    entry: dict = {}
    for key in ("creation_time", "date_modified", "date_created"):
        val = metadata.get(key)
        if isinstance(val, datetime):
            entry[key] = val.isoformat()
        elif val:
            entry[key] = str(val)
    for key in (
        "duration_seconds", "width", "height", "fps",
        "codec_video", "codec_audio", "bitrate", "size_bytes",
        "make", "model", "rotation", "format_name",
    ):
        val = metadata.get(key)
        if val is not None and val != "" and val != 0:
            entry[key] = val

    data[filename] = entry
    _atomic_write_json(backup_path, data)


def restore_video_backup(folder: Path) -> dict:
    """Restore video metadata from .video_backup.json."""
    backup_path = folder / BACKUP_FILENAME
    if not backup_path.exists():
        return {"ok": 0, "failed": 0, "errors": ["No hay backup de video"]}
    try:
        with open(backup_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": 0, "failed": 0, "errors": [f"Error leyendo backup: {e}"]}

    ok = failed = 0
    errors: List[str] = []
    for filename, entry in data.items():
        if filename.startswith("_"):
            continue
        path = folder / filename
        if not path.exists():
            errors.append(f"Archivo no encontrado: {filename}")
            failed += 1
            continue
        ct_str = entry.get("creation_time", "")
        if not ct_str:
            ok += 1
            continue
        try:
            dt = datetime.fromisoformat(ct_str)
            write_video_date(path, dt)
            ok += 1
        except Exception as e:
            errors.append(f"{filename}: {e}")
            failed += 1
    return {"ok": ok, "failed": failed, "errors": errors}


# ── Private helpers ───────────────────────────────────────────────────────────

def _read_ffprobe(path: Path, result: dict) -> bool:
    """Fill result dict from ffprobe JSON output.  Returns True on success."""
    if FFMPEG_AVAILABLE is False:
        result["error"] = "ffprobe no disponible — instalá ffmpeg desde https://ffmpeg.org"
        return False
    try:
        cmd = [
            FFPROBE_CMD, "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=15, text=True,
                              **_subprocess_no_window())
        if proc.returncode != 0:
            return False
        data = json.loads(proc.stdout)
    except Exception:
        return False

    fmt     = data.get("format", {})
    streams = data.get("streams", [])

    result["format_name"] = fmt.get("format_name", "").split(",")[0]
    try:
        result["duration_seconds"] = float(fmt.get("duration", 0))
    except (ValueError, TypeError):
        pass
    try:
        result["bitrate"] = int(fmt.get("bit_rate", 0))
    except (ValueError, TypeError):
        pass

    fmt_tags = fmt.get("tags", {})
    result["all_tags"] = fmt_tags
    _extract_creation_time(fmt_tags, result)
    result["make"]  = fmt_tags.get("make",  fmt_tags.get("Make",  ""))
    result["model"] = fmt_tags.get("model", fmt_tags.get("Model", ""))

    for stream in streams:
        codec_type = stream.get("codec_type", "")
        if codec_type == "video" and not result["codec_video"]:
            result["codec_video"] = stream.get("codec_name", "")
            result["width"]       = stream.get("width",  0)
            result["height"]      = stream.get("height", 0)
            fps_str = stream.get("avg_frame_rate", "")
            if "/" in fps_str:
                try:
                    n, d = fps_str.split("/")
                    result["fps"] = round(float(n) / float(d), 2) if float(d) else 0.0
                except (ValueError, ZeroDivisionError):
                    pass
            # Rotation from side_data_list
            for sd in stream.get("side_data_list", []):
                if sd.get("side_data_type") == "Display Matrix":
                    try:
                        result["rotation"] = abs(int(sd.get("rotation", 0)))
                    except (ValueError, TypeError):
                        pass
            # Rotation from stream tags
            rotate_str = stream.get("tags", {}).get("rotate", "")
            if rotate_str:
                try:
                    result["rotation"] = abs(int(rotate_str))
                except (ValueError, TypeError):
                    pass
            if not result["creation_time"]:
                _extract_creation_time(stream.get("tags", {}), result)
        elif codec_type == "audio" and not result["codec_audio"]:
            result["codec_audio"] = stream.get("codec_name", "")

    return True


def _read_hachoir(path: Path, result: dict) -> None:
    """Fill result dict using hachoir (fallback when ffprobe unavailable)."""
    try:
        from hachoir.parser import createParser
        from hachoir.metadata import extractMetadata

        parser = createParser(str(path))
        if not parser:
            return
        with parser:
            metadata = extractMetadata(parser)
        if not metadata:
            return

        if metadata.has("creation_date"):
            dt = metadata.get("creation_date")
            if isinstance(dt, datetime):
                result["creation_time"] = dt
        if metadata.has("duration"):
            dur = metadata.get("duration")
            secs = getattr(dur, "seconds", 0) + getattr(dur, "microseconds", 0) / 1e6
            result["duration_seconds"] = secs
        if metadata.has("width"):
            result["width"] = int(metadata.get("width"))
        if metadata.has("height"):
            result["height"] = int(metadata.get("height"))
        if metadata.has("bit_rate"):
            result["bitrate"] = int(metadata.get("bit_rate"))
    except Exception:
        pass


def _extract_creation_time(tags: dict, result: dict) -> None:
    """Parse creation_time ISO-8601 string from a tags dict."""
    raw = (tags.get("creation_time") or "").strip()
    if not raw:
        return
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            result["creation_time"] = datetime.strptime(raw[:26], fmt)
            return
        except ValueError:
            continue


def _atomic_write_json(dest: Path, data: dict) -> None:
    """Write JSON to dest atomically via a temp file in the same directory."""
    fd, tmp_path = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, str(dest))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
