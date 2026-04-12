# CLAUDE.md — exif_manager project guide

## Architecture Summary

### core/ files
| File | Purpose |
|------|---------|
| `backup_manager.py` | Backup and restore EXIF data to/from `.exif_backup.json` |
| `duplicate_finder.py` | MD5-based exact duplicate detection; `DuplicateScanWorker` (QObject) for background use |
| `exif_handler.py` | EXIF read/write via piexif + Pillow; exposes `read_exif()`, `write_exif_date()` |
| `file_scanner.py` | Folder scanning, MD5 hashing, `scan_subfolders()`, `walk_images()`, `unique_dest()`; defines `EXCLUDED_FOLDERS` |
| `video_handler.py` | Video metadata read/write via ffprobe+ffmpeg (hachoir fallback); `get_video_metadata()`, `write_video_date()`, `get_video_thumbnail()`, `scan_video_folder()`, backup helpers |
| `video_duplicate_finder.py` | MD5-based duplicate detection for videos; `VideoDuplicateScanWorker` + `video_quality_score()` |

### ui/ files
| File | Purpose |
|------|---------|
| `cleanup_dialog.py` | Modal dialog (Herramientas menu) to scan and delete temp/trash folders under a root |
| `date_editor.py` | Date editing dialog: folder-mode, single-photo, or explicit-selection; **reference thread pattern** |
| `duplicate_panel.py` | Permanent "Duplicados" tab: scan, side-by-side card comparison, batch dedup |
| `duplicate_viewer.py` | Legacy duplicate dialog — superseded by `duplicate_panel.py`, kept for reference |
| `folder_tree.py` | Left panel: lazy-loading folder tree with backup indicators |
| `log_viewer.py` | `LogManager` (shared singleton) + `LogViewerDialog` |
| `main_window.py` | App shell: `QTabWidget` with **Fotos** + **Duplicados** + **Videos** tabs, menu bar, signal wiring, undo stack |
| `photo_detail.py` | Right panel: full EXIF metadata table + image preview + edit button |
| `styles.py` | Dark-theme QSS constants shared across all widgets |
| `thumbnail_grid.py` | Center panel: two-phase background thumbnail loader with disk cache, sort controls, two-row button bar, progress bar for large folders |
| `video_date_editor.py` | Date editing dialog for videos (mirrors `date_editor.py`); modes: folder / single / selection; `_ApplyWorker` writes via ffmpeg |
| `video_detail.py` | Right panel for Videos tab: metadata groups + async first-frame preview + edit button |
| `video_grid.py` | `VideoGrid` (thumbnail list + sort + context menu) + `VideoPanel` (self-contained tab with its own `FolderTreePanel`) |

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

### Video support (new in this session)

**ffmpeg dependency:**
- `FFMPEG_AVAILABLE = _check_ffmpeg()` computed in `main.py` before `QApplication`; passed to `MainWindow(ffmpeg_available=...)` → `VideoPanel(ffmpeg_available=...)`
- If False: yellow warning banner at top of Videos tab; thumbnails show placeholder; write-date button disabled
- Fallback metadata reading uses `hachoir` (no ffmpeg required)
- Detection also checks for a bundled `ffmpeg.exe` in the project folder

**Thread patterns for video workers:**
- `_VideoThumbnailWorker` follows exact same thread lifetime as `_ThumbnailWorker` (see date_editor.py reference)
- `_ApplyWorker` in `video_date_editor.py`: same finished→quit+wait pattern, never connects `finished→thread.quit`
- `_ThumbWorker` in `video_detail.py`: lightweight single-file thumbnail loader

**Video thumbnail cache:**
- Uses same `_thumbcache` subfolder as photos (per folder)
- Cache key = MD5(path+mtime), same as `_thumb_cache_key()` in `thumbnail_grid.py`
- Skipped when `ffmpeg_available=False`

**Backup for videos:**
- `backup_video_metadata()` writes/appends to `.video_backup.json` (same atomic-write pattern as `.exif_backup.json`)
- `restore_video_backup()` reads it back and calls `write_video_date()`

**VideoPanel architecture:**
- Fully self-contained `QWidget` added as third tab — does NOT share the main window's shared `FolderTreePanel`
- Has its own `FolderTreePanel(main_window=None, ...)` — passing `None` prevents `set_root()` calls on the shared state
- All signal wiring is inside `VideoPanel._wire_signals()`

**VIDEO_EXTENSIONS** = `.mp4 .mov .avi .mkv .3gp .m4v .wmv`

**Excluded folders** for video scans: reuses `EXCLUDED_FOLDERS` from `file_scanner.py`

### Current known bugs
None open. Last fixed:
- **duplicate_panel.py** — lambda capture bug in `_show_group` loop (used bare closure; fixed with `gi=group_idx` default-arg pattern).
- **duplicate_panel.py** — double `quit()` in scan and dedup workers: `finished → thread.quit` signal connection removed; only the in-handler `quit()+wait()` remains.
