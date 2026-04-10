"""EXIF reading, writing, and metadata extraction."""
import io
import re
import struct
from datetime import datetime
from pathlib import Path
from typing import Optional

import piexif
from PIL import Image, ImageOps, UnidentifiedImageError

from core.file_scanner import compute_md5

EXIF_DT_FORMAT = "%Y:%m:%d %H:%M:%S"
EXIF_DT_PATTERN = re.compile(r"^\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}$")

_TIMESTAMP_TAGS = {
    "DateTime":          ("0th",  piexif.ImageIFD.DateTime),
    "DateTimeOriginal":  ("Exif", piexif.ExifIFD.DateTimeOriginal),
    "DateTimeDigitized": ("Exif", piexif.ExifIFD.DateTimeDigitized),
}

# Human-readable EXIF tag names for display
_DISPLAY_TAGS = {
    piexif.ImageIFD.Make:             ("0th",  "Make"),
    piexif.ImageIFD.Model:            ("0th",  "Model"),
    piexif.ImageIFD.Orientation:      ("0th",  "Orientación"),
    piexif.ImageIFD.XResolution:      ("0th",  "Resolución X"),
    piexif.ImageIFD.YResolution:      ("0th",  "Resolución Y"),
    piexif.ExifIFD.ISOSpeedRatings:   ("Exif", "ISO"),
    piexif.ExifIFD.ExposureTime:      ("Exif", "Exposición"),
    piexif.ExifIFD.FNumber:           ("Exif", "Apertura"),
    piexif.ExifIFD.Flash:             ("Exif", "Flash"),
}

_ORIENTATION_LABELS = {
    1: "Normal", 2: "Espejo H", 3: "180°", 4: "Espejo V",
    5: "Espejo H + 270°", 6: "90° CW", 7: "Espejo H + 90°", 8: "270° CW",
}

# Years considered "invalid/reset" camera date when month=1 day=1
_INVALID_YEARS = {2000, 2005}


def _load_exif_bytes(path: Path) -> Optional[dict]:
    try:
        data = path.read_bytes()
        return piexif.load(data)
    except Exception:
        return None


def parse_exif_dt(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value.strip("\x00"), EXIF_DT_FORMAT)
    except (ValueError, AttributeError):
        return None


def format_exif_dt(dt: datetime) -> str:
    return dt.strftime(EXIF_DT_FORMAT)


def is_invalid_date(dt_str: str) -> bool:
    """Return True if the date looks like a camera factory-reset date."""
    if not dt_str:
        return True
    dt = parse_exif_dt(dt_str)
    if dt is None:
        return True
    return dt.year in _INVALID_YEARS and dt.month == 1 and dt.day == 1


def read_exif(path: Path) -> dict:
    """Return EXIF info dict. Always succeeds; sets 'error' key on failure."""
    suffix = path.suffix.lower()
    result = {
        "path": str(path),
        "format": suffix,
        "writable": suffix in (".jpg", ".jpeg", ".tiff", ".tif"),
        "fields": {},
        "display": {},
        "gps": None,
        "error": None,
    }

    exif_dict = _load_exif_bytes(path)
    if exif_dict is None:
        result["error"] = "no_exif"
        return result

    # Timestamp fields
    for name, (ifd, tag) in _TIMESTAMP_TAGS.items():
        val = exif_dict.get(ifd, {}).get(tag)
        if val:
            decoded = val.decode("ascii", errors="replace") if isinstance(val, bytes) else str(val)
            result["fields"][name] = decoded.strip("\x00")

    # Human-readable display fields
    for tag_id, (ifd, label) in _DISPLAY_TAGS.items():
        val = exif_dict.get(ifd, {}).get(tag_id)
        if val is None:
            continue
        if label == "Orientación":
            result["display"][label] = _ORIENTATION_LABELS.get(val, str(val))
        elif label in ("Exposición",):
            result["display"][label] = _format_rational(val)
        elif label in ("Apertura",):
            result["display"][label] = f"f/{_rational_to_float(val):.1f}" if val else ""
        elif label in ("Resolución X", "Resolución Y"):
            result["display"][label] = str(_rational_to_float(val))
        elif label == "Flash":
            result["display"][label] = "Sí" if (val & 1) else "No"
        elif isinstance(val, bytes):
            result["display"][label] = val.decode("ascii", errors="replace").strip("\x00")
        else:
            result["display"][label] = str(val)

    # GPS
    gps_ifd = exif_dict.get("GPS", {})
    if gps_ifd:
        result["gps"] = _parse_gps(gps_ifd)

    return result


def get_best_date_str(fields: dict) -> str:
    """Return the most authoritative EXIF date string from a fields dict, or ''."""
    return (
        fields.get("DateTimeOriginal")
        or fields.get("DateTimeDigitized")
        or fields.get("DateTime")
        or ""
    )


def write_exif_date(
    path: Path,
    year: int,
    month: int,
    day: int,
    fields: list,
    hour: Optional[int] = None,
    minute: Optional[int] = None,
    second: Optional[int] = None,
) -> None:
    """Write a new date to the specified EXIF timestamp fields.

    When hour/minute/second are None, the existing HH:MM:SS is read from the
    file's EXIF and preserved.  When all three are provided, that exact time
    is written regardless of what the file currently contains.
    """
    suffix = path.suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".tiff", ".tif"):
        raise ValueError(f"EXIF writing not supported for {suffix}")

    if hour is None or minute is None or second is None:
        # Preserve the original time from the file
        existing = read_exif(path)
        existing_dt = None
        for fname in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
            raw = existing["fields"].get(fname, "")
            if raw:
                existing_dt = parse_exif_dt(raw)
                if existing_dt:
                    break
        if existing_dt:
            hour = existing_dt.hour if hour is None else hour
            minute = existing_dt.minute if minute is None else minute
            second = existing_dt.second if second is None else second
        else:
            hour = hour if hour is not None else 12
            minute = minute if minute is not None else 0
            second = second if second is not None else 0

    try:
        new_dt = datetime(year, month, day, hour, minute, second)
    except ValueError as e:
        raise ValueError(f"Fecha inválida: {e}") from e

    new_str = format_exif_dt(new_dt)
    new_fields = {f: new_str for f in fields}
    write_exif_timestamps(path, new_fields)


def write_exif_timestamps(path: Path, fields: dict) -> None:
    """Low-level: write EXIF timestamp dict to a JPEG/TIFF file.

    Reads the file exactly once, loads the existing EXIF from those same bytes
    (never builds the dict from scratch), modifies only the requested timestamp
    fields, then writes back using piexif.insert() so all other metadata is
    preserved intact.
    """
    image_data = path.read_bytes()
    try:
        exif_dict = piexif.load(image_data)
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}

    # Remove MakerNote to avoid serialisation errors on Sony/Canon files
    exif_dict.get("Exif", {}).pop(piexif.ExifIFD.MakerNote, None)

    for name, val in fields.items():
        if name not in _TIMESTAMP_TAGS:
            continue
        ifd, tag = _TIMESTAMP_TAGS[name]
        exif_dict.setdefault(ifd, {})[tag] = val.encode("ascii")

    exif_bytes = piexif.dump(exif_dict)

    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        piexif.insert(exif_bytes, image_data, str(path))
    else:
        with Image.open(io.BytesIO(image_data)) as img:
            img.save(path, exif=exif_bytes)


def make_dated_filename(
    dt: datetime,
    folder: Path,
    suffix: str,
    used: Optional[set] = None,
    original_stem: Optional[str] = None,
) -> str:
    """Return a collision-free filename like 2011-12-24-15h40m46s.jpg.

    If original_stem is provided the result is like
    2011-12-24-15h40m46s_IMG_2045.jpg (date + original stem, no extension).
    Checks both files already on disk AND the `used` set so that batch
    operations assigning the same timestamp to multiple files get unique names.
    """
    base = dt.strftime("%Y-%m-%d-%Hh%Mm%Ss")
    if original_stem:
        base = f"{base}_{original_stem}"
    ext = suffix.lower()
    used = used if used is not None else set()

    candidate = f"{base}{ext}"
    if not (folder / candidate).exists() and candidate not in used:
        return candidate

    i = 1
    while True:
        candidate = f"{base}_{i}{ext}"
        if not (folder / candidate).exists() and candidate not in used:
            return candidate
        i += 1


def parse_date_from_filename(stem: str) -> Optional[datetime]:
    """Try to extract a datetime from a filename stem using common patterns.

    Patterns tried in order (most specific first):
      2011-12-24-15h40m46s
      2011-12-24 15.40.46  or  2011-12-24_15.40.46
      2011-12-24_15-40-46  or  2011-12-24 15-40-46
      20111224_154046
      2011-12-24
      20111224

    Date-only patterns default to 00:00:00.
    Returns None when no pattern matches or the extracted values are invalid.
    """
    _PATTERNS = [
        # 2011-12-24-15h40m46s
        (r"(\d{4})-(\d{2})-(\d{2})-(\d{2})h(\d{2})m(\d{2})s", True),
        # 2011-12-24 15.40.46  /  2011-12-24_15.40.46
        (r"(\d{4})-(\d{2})-(\d{2})[ _](\d{2})\.(\d{2})\.(\d{2})", True),
        # 2011-12-24_15-40-46  /  2011-12-24 15-40-46
        (r"(\d{4})-(\d{2})-(\d{2})[ _](\d{2})-(\d{2})-(\d{2})", True),
        # 20111224_154046
        (r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", True),
        # 2011-12-24
        (r"(\d{4})-(\d{2})-(\d{2})", False),
        # 20111224
        (r"(\d{4})(\d{2})(\d{2})", False),
    ]
    for pattern, has_time in _PATTERNS:
        m = re.search(pattern, stem)
        if m:
            g = m.groups()
            try:
                y, mo, d = int(g[0]), int(g[1]), int(g[2])
                h, mi, s = (int(g[3]), int(g[4]), int(g[5])) if has_time else (0, 0, 0)
                return datetime(y, mo, d, h, mi, s)
            except ValueError:
                continue
    return None


def get_all_metadata(path: Path) -> dict:
    """Return comprehensive metadata dict: EXIF + file stats + dimensions + MD5."""
    result = {
        "exif": {},
        "file": {},
        "error": None,
    }

    # File stats
    try:
        stat = path.stat()
        result["file"]["nombre"] = path.name
        result["file"]["ruta"] = str(path)
        size_bytes = stat.st_size
        if size_bytes >= 1_048_576:
            result["file"]["tamaño"] = f"{size_bytes / 1_048_576:.2f} MB"
        else:
            result["file"]["tamaño"] = f"{size_bytes / 1024:.1f} KB"
        result["file"]["modificado"] = datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m/%Y %H:%M:%S")
        result["file"]["creado"] = datetime.fromtimestamp(stat.st_ctime).strftime("%d/%m/%Y %H:%M:%S")
    except OSError as e:
        result["error"] = str(e)
        return result

    # MD5
    result["file"]["md5"] = compute_md5(path)

    # Image dimensions
    try:
        with Image.open(path) as img:
            w, h = img.size
            result["file"]["dimensiones"] = f"{w} × {h} px"
    except Exception:
        result["file"]["dimensiones"] = "N/D"

    # EXIF
    exif_info = read_exif(path)
    result["exif"]["writable"] = exif_info["writable"]
    result["exif"]["error"] = exif_info["error"]
    result["exif"]["fields"] = exif_info["fields"]
    result["exif"]["display"] = exif_info["display"]
    result["exif"]["gps"] = exif_info["gps"]

    return result


def load_thumbnail(path: Path, size: int = 150) -> Optional[bytes]:
    """Load image, apply EXIF rotation, scale to size×size, return JPEG bytes."""
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((size, size), Image.LANCZOS)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75)
            return buf.getvalue()
    except Exception:
        return None


def load_preview(path: Path, max_width: int = 800, max_height: int = 600) -> Optional[bytes]:
    """Load image scaled for preview panel. Returns JPEG bytes."""
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((max_width, max_height), Image.LANCZOS)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()
    except Exception:
        return None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _rational_to_float(val) -> float:
    if isinstance(val, tuple) and len(val) == 2:
        num, den = val
        return num / den if den else 0.0
    return float(val) if val else 0.0


def _format_rational(val) -> str:
    f = _rational_to_float(val)
    if f == 0:
        return "N/D"
    if f < 1:
        denom = round(1 / f)
        return f"1/{denom}s"
    return f"{f:.1f}s"


def _parse_gps(gps_ifd: dict) -> Optional[str]:
    """Convert piexif GPS IFD to a human-readable string."""
    try:
        lat_ref = gps_ifd.get(piexif.GPSIFD.GPSLatitudeRef, b"N").decode()
        lon_ref = gps_ifd.get(piexif.GPSIFD.GPSLongitudeRef, b"E").decode()
        lat_raw = gps_ifd.get(piexif.GPSIFD.GPSLatitude)
        lon_raw = gps_ifd.get(piexif.GPSIFD.GPSLongitude)
        if not lat_raw or not lon_raw:
            return None
        lat = _dms_to_decimal(lat_raw, lat_ref)
        lon = _dms_to_decimal(lon_raw, lon_ref)
        return f"{lat:.6f}, {lon:.6f}"
    except Exception:
        return None


def _dms_to_decimal(dms, ref: str) -> float:
    d = _rational_to_float(dms[0])
    m = _rational_to_float(dms[1])
    s = _rational_to_float(dms[2])
    val = d + m / 60 + s / 3600
    if ref in ("S", "W"):
        val = -val
    return val
