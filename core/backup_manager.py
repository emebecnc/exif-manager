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


def create_backup(folder: Path, files_data: dict) -> int:
    """Create or update .exif_backup.json by merging new entries with existing ones.

    Args:
        folder:     Directory that contains (or will contain) .exif_backup.json.
        files_data: Mapping of ``{filename: exif_fields_dict}`` for the files
                    about to be edited, e.g.::

                        {
                            "photo.jpg": {
                                "DateTimeOriginal": "2020:01:01 12:00:00",
                                ...
                            }
                        }

    Merge rules:
      * If ``.exif_backup.json`` already exists its contents are loaded first so
        entries for *other* files are preserved.
      * Entries for the same filename are updated (new values replace old).
      * ``_meta`` is created on first write and its ``last_updated`` stamp is
        refreshed on every subsequent write.

    Returns the number of file entries written (``len(files_data)``).
    Raises on I/O error — callers should catch and ask the user whether to
    proceed without a backup.
    """
    backup_path = folder / BACKUP_FILENAME
    now_iso     = datetime.now().isoformat(timespec="seconds")

    # Load existing backup so we can merge without losing other entries
    if backup_path.exists():
        try:
            with open(backup_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}   # corrupt / unreadable — start fresh
    else:
        data = {}

    # Ensure _meta block exists and is up-to-date
    if "_meta" not in data:
        data["_meta"] = {
            "created_at": now_iso,
            "folder":     str(folder),
        }
    data["_meta"]["last_updated"] = now_iso
    data["_meta"]["folder"]       = str(folder)

    # Merge: each entry uses v2 format {"original_exif_dict": {...}, "timestamp": "ISO"}
    for filename, fields in files_data.items():
        data[filename] = {
            "original_exif_dict": fields,
            "timestamp":          now_iso,
        }

    print(f"[BACKUP] Writing {len(files_data)} entries → {backup_path}")
    _atomic_write_json(backup_path, data)
    exists_after = backup_path.exists()
    print(f"[BACKUP] File exists after write: {exists_after}  ({backup_path})")
    return len(files_data)


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
    filename: str,
    operation: str,             # "fecha_editada" | "renombrado" | "movido" | "eliminado"
    exif_before: dict,          # EXIF fields dict captured before any changes
    exif_after: Optional[dict] = None,  # EXIF fields dict after changes; None → not applicable
    new_name: Optional[str] = None,     # renamed-to filename; None → no rename
) -> None:
    """Append one multi-line record to _historial_original.txt in folder.

    Format::

        [2026-04-13 10:05:22]
        Archivo: foto.jpg → nueva.jpg
        Operación: fecha_editada
        EXIF ANTERIOR:
          DateTimeOriginal: 2010:10:19 23:35:24
          DateTimeDigitized: 2010:10:19 23:35:24
        EXIF NUEVO:
          DateTimeOriginal: 2026:04:13 08:58:56
          DateTimeDigitized: 2026:04:13 08:58:56
        ---

    When ``exif_after`` is ``None`` (rename / move / delete) the "EXIF NUEVO"
    section is omitted.  The file is created with a header on first write;
    subsequent calls append.  Failures are silently swallowed — the historial
    must never abort the main operation that called it.
    """
    hist_path = folder / HISTORIAL_FILENAME
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Archivo line (with optional rename arrow) ─────────────────────────────
    archivo_line = filename
    if new_name is not None:
        archivo_line = f"{filename} → {new_name}"

    # ── Build record lines ────────────────────────────────────────────────────
    lines: list[str] = [
        f"[{now}]",
        f"Archivo: {archivo_line}",
        f"Operación: {operation}",
        "EXIF ANTERIOR:",
    ]
    has_before = False
    for field, val in sorted(exif_before.items()):
        if val:
            lines.append(f"  {field}: {val}")
            has_before = True
    if not has_before:
        lines.append("  (sin datos EXIF)")

    if exif_after is not None:
        lines.append("EXIF NUEVO:")
        has_after = False
        for field, val in sorted(exif_after.items()):
            if val:
                lines.append(f"  {field}: {val}")
                has_after = True
        if not has_after:
            lines.append("  (sin datos EXIF)")

    lines.append("---")
    lines.append("")   # blank separator between records

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
