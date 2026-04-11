# CLAUDE.md — exif_manager project guide

## Architecture Summary

### core/ files
| File | Purpose |
|------|---------|
| `backup_manager.py` | Backup and restore EXIF data to/from `.exif_backup.json` |
| `duplicate_finder.py` | MD5-based exact duplicate detection; `DuplicateScanWorker` (QObject) for background use |
| `exif_handler.py` | EXIF read/write via piexif + Pillow; exposes `read_exif()`, `write_exif_date()` |
| `file_scanner.py` | Folder scanning, MD5 hashing, `scan_subfolders()`, `walk_images()`, `unique_dest()`; defines `EXCLUDED_FOLDERS` |

### ui/ files
| File | Purpose |
|------|---------|
| `cleanup_dialog.py` | Modal dialog to scan and delete temp/trash folders under a root |
| `date_editor.py` | Date editing dialog: folder-mode, single-photo, or explicit-selection; reference thread pattern |
| `duplicate_panel.py` | Permanent tab for duplicate scanning and resolution (side-by-side cards) |
| `duplicate_viewer.py` | Legacy duplicate dialog (scan → compare → move to trash) |
| `folder_tree.py` | Left panel: lazy-loading folder tree with backup indicators |
| `log_viewer.py` | `LogManager` (shared singleton) + `LogViewerDialog` |
| `main_window.py` | App shell: layout, menu bar, tab management, signal wiring, undo stack |
| `photo_detail.py` | Right panel: full EXIF metadata table + image preview + edit button |
| `styles.py` | Dark-theme QSS constants shared across all widgets |
| `thumbnail_grid.py` | Center panel: two-phase background thumbnail loader with disk cache |

### Key patterns

**Thread lifetime (use date_editor.py as reference):**
```python
self._worker = Worker(...)
self._thread = QThread(self)
self._worker.moveToThread(self._thread)
self._thread.started.connect(self._worker.run)
self._worker.finished.connect(self._on_finished)   # NOT connected to thread.quit()
self._thread.finished.connect(self._cleanup_thread)
self._thread.start()

def _on_finished(self, ...):
    if self._thread and self._thread.isRunning():
        self._thread.quit()
        self._thread.wait()   # no terminate fallback needed; workers are cooperative
    # now safe to touch UI
```
Do NOT also connect `worker.finished → thread.quit` — causes double `quit()`.

**Path handling:** Always store and pass `Path` objects. Use `str(path)` only for `shutil.move()` / `os` calls. Convert incoming strings with `Path(s)`, never `eval()`.

**Lambda capture in loops:** `lambda checked, p=path: func(p)` — default-arg capture, never bare closure.

**Excluded folders:** `EXCLUDED_FOLDERS` in `file_scanner.py` = `{"_duplicados_eliminados", "_eliminados", ...}`. All scanners (`scan_subfolders`, `walk_images`, `DuplicateScanWorker`) skip these automatically.

**piexif safety rules:**
- Always call `_clean_exif_for_dump(exif_dict)` before `piexif.dump()` — never call dump() directly.
- `_clean_exif_for_dump` strips `MakerNote` (tag `0x927C` / 37500) and all `_EXIF_UNDEFINED_TAGS` that arrived as `int` instead of `bytes` (would crash dump).
- `_EXIF_UNDEFINED_TAGS` is defined at top of `exif_handler.py`; add any new crash-causing tags there.

### Current known bugs (duplicate_panel.py)
- **Bug 7:** `_begin_scan` connects `_scan_worker.finished → _scan_thread.quit` AND `_on_scan_finished` also calls `quit()+wait()` — harmless double-quit, should be removed.
- **Bug 8:** Same double-quit pattern in `_on_dedup_all` / `_on_dedup_finished` for the dedup worker thread.
- Both need the signal-connection line removed so only the in-handler `quit()+wait()` remains.
