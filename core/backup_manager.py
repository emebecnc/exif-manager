"""Backup and restore EXIF data to/from .exif_backup.json."""
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.exif_handler import read_exif, write_exif_timestamps
from core.file_scanner import scan_folder

BACKUP_FILENAME       = ".exif_backup.json"
VIDEO_BACKUP_FILENAME = ".video_backup.json"   # mirrors video_handler.BACKUP_FILENAME
HISTORIAL_FILENAME    = "_historial_original.txt"
_HISTORIAL_HEADER     = "# Historial de cambios — generado por ExifManager\n"


def create_backup(folder_path: Path) -> int:
    """Backup current EXIF fields for all JPGs in folder_path.

    Returns the number of files backed up.
    Writes .exif_backup.json atomically using tempfile + os.replace.

    Entry format (v2):
        {"filename.jpg": {"original_exif_dict": {fields}, "timestamp": "ISO"}, ...}
    restore_backup() handles both v1 (flat fields dict) and v2 transparently.
    """
    images = scan_folder(folder_path)
    now_iso = datetime.now().isoformat(timespec="seconds")
    backup = {
        "_meta": {
            "created_at": now_iso,
            "file_count": len(images),
            "folder": str(folder_path),
        }
    }

    for img_path in images:
        exif = read_exif(img_path)
        backup[img_path.name] = {
            "original_exif_dict": exif["fields"],   # full fields dict
            "timestamp": now_iso,
        }

    _atomic_write_json(folder_path / BACKUP_FILENAME, backup)
    return len(images)


def restore_backup(folder_path: Path) -> dict:
    """Restore EXIF timestamps from .exif_backup.json.

    For each backup entry the lookup order is:
      1. Exact original filename — fast path, normal case.
      2. EXIF-date scan — fallback when the file was renamed without an EXIF
         edit (Conservar mode), so its current dates still match the backup.
         Only used when exactly one candidate matches to avoid ambiguity.
      3. Logged as "not found" if both lookups fail.

    Returns {"ok": int, "failed": int, "errors": list[str]}.
    """
    backup_path = folder_path / BACKUP_FILENAME
    if not backup_path.exists():
        return {"ok": 0, "failed": 0, "errors": ["No existe archivo de backup"]}

    try:
        with open(backup_path, encoding="utf-8") as f:
            backup = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": 0, "failed": 0, "errors": [f"Error leyendo backup: {e}"]}

    ok = 0
    failed = 0
    errors = []

    for filename, entry in backup.items():
        if filename.startswith("_"):
            continue  # skip _meta

        # Handle both v1 (flat fields dict) and v2 ({"original_exif_dict": ...})
        actual_fields = _extract_fields(entry)

        img_path = folder_path / filename

        # Fallback: file may have been renamed (Conservar+rename) — scan by dates
        if not img_path.exists():
            img_path = _find_by_exif_dates(folder_path, actual_fields)

        if img_path is None or not img_path.exists():
            errors.append(f"Archivo no encontrado: {filename}")
            failed += 1
            continue

        if not actual_fields:
            ok += 1  # nothing to restore, counts as success
            continue
        try:
            write_exif_timestamps(img_path, actual_fields)
            ok += 1
        except Exception as e:
            errors.append(f"{filename}: {e}")
            failed += 1

    return {"ok": ok, "failed": failed, "errors": errors}


def rename_backup_entry(folder_path: Path, old_name: str, new_name: str) -> None:
    """Update the backup JSON so the entry key matches the file's new name.

    Must be called immediately after every file rename so that a subsequent
    restore_backup() can locate the file by its current (renamed) filename.
    No-ops silently when no backup exists or the old name is not present.
    """
    backup_path = folder_path / BACKUP_FILENAME
    if not backup_path.exists():
        return
    try:
        with open(backup_path, encoding="utf-8") as f:
            backup = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    if old_name not in backup:
        return
    backup[new_name] = backup.pop(old_name)
    try:
        _atomic_write_json(backup_path, backup)
    except Exception:
        pass  # best-effort; a stale key is recoverable via EXIF-date fallback


def has_backup(folder_path: Path) -> bool:
    """Return True if folder has a photo or video backup file."""
    return (
        (folder_path / BACKUP_FILENAME).exists()
        or (folder_path / VIDEO_BACKUP_FILENAME).exists()
    )


def get_backup_info(folder_path: Path) -> dict:
    """Return metadata from backup file, or None if not found."""
    backup_path = folder_path / BACKUP_FILENAME
    if not backup_path.exists():
        return {}
    try:
        with open(backup_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("_meta", {})
    except Exception:
        return {}


def append_historial(
    folder: Path,
    original_name: str,
    new_name: Optional[str],    # None → no rename happened
    original_exif: dict,        # fields dict before any changes
    operation: str,             # "fecha_editada" | "renombrado" | "movido" | "eliminado"
) -> None:
    """Append one human-readable record to _historial_original.txt in folder.

    The file is created with a header on first write; subsequent calls append.
    Failures are silently swallowed — the historial must never abort the main
    operation that called it.
    """
    hist_path = folder / HISTORIAL_FILENAME
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    nombre_nuevo = new_name if new_name is not None else "sin cambio"

    lines: list = [
        f"[{now}]",
        f"Archivo original: {original_name}",
        f"Nombre nuevo:     {nombre_nuevo}",
        "EXIF original:",
    ]
    has_any = False
    # Show all fields present in the dict, sorted for consistent output
    for field, val in sorted(original_exif.items()):
        if val:
            lines.append(f"  {field}:  {val}")
            has_any = True
    if not has_any:
        lines.append("  (sin datos EXIF)")
    lines.append(f"Operación: {operation}")
    lines.append("---")
    lines.append("")          # blank line between records

    try:
        write_header = not hist_path.exists()
        with open(hist_path, "a", encoding="utf-8") as f:
            if write_header:
                f.write(_HISTORIAL_HEADER + "\n")
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def _extract_fields(entry) -> dict:
    """Return the actual EXIF fields dict from a backup entry.

    Handles both the v1 format (flat fields dict stored directly) and the v2
    format ({"original_exif_dict": {...fields...}, "timestamp": "..."}).
    Returns an empty dict for any unexpected value.
    """
    if isinstance(entry, dict):
        if "original_exif_dict" in entry:
            result = entry["original_exif_dict"]
            return result if isinstance(result, dict) else {}
        return entry   # v1: entry IS the fields dict
    return {}


def _find_by_exif_dates(folder_path: Path, fields: dict) -> Optional[Path]:
    """Return the unique file in folder_path whose current EXIF dates exactly
    match every field stored in the backup entry.

    Returns None when fields is empty, when no file matches, or when more than
    one file matches (ambiguous — safer to give up than to restore the wrong file).
    """
    if not fields:
        return None
    matches = []
    for candidate in scan_folder(folder_path):
        exif = read_exif(candidate)
        if all(exif["fields"].get(k) == v for k, v in fields.items()):
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


def _atomic_write_json(dest: Path, data: dict) -> None:
    """Write JSON to dest atomically using a temp file in the same directory."""
    dir_path = dest.parent
    fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp")
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
