# EXIF Manager — CLAUDE.md

**Last updated:** 2026-04-13 (session 4)
**Repo:** github.com/emebecnc/exif-manager
**Local:** D:\homelab\exif_manager\

---

## PROJECT

EXIF Manager — Desktop Windows app (PyQt6 + Python 3.11) to manage photo/video EXIF dates.

**Stack:** Python 3.11, PyQt6 6.4+, Pillow, piexif, ffmpeg-python, hachoir, pywin32

---

## ARCHITECTURE (v2.0)

QMainWindow
└─ QSplitter (horizontal)
   ├─ FolderTreePanel (220px) ← SINGLE TREE
   └─ QTabWidget
      ├─ 📷 Photos (grid + metadata)
      ├─ 🎬 Videos (grid + metadata)
      └─ 🔀 Duplicates (detection + trash)

Signal: FolderTree folder_changed(Path) → all tabs' on_folder_changed() slots

---

## FEATURES

### ✅ Photos
- Tree with photo+video counts
- 2-phase thumbnail grid (placeholder → EXIF+cache)
- LRU cache (200 items max)
- Multi-select (Ctrl/Shift)
- EXIF editor: independent year/month/day
- Auto-rename with date format
- Drag & drop to tree
- Backup (.exif_backup.json) + history
- Duplicates by MD5 + trash folder
- Cleanup tool
- Log viewer

### ✅ Videos
- Grid with first-frame thumbnails
- Metadata: duration, resolution, FPS, codec, bitrate
- Date editing (same as photos)
- Backup (.video_backup.json) + history
- Duplicates by MD5 + trash folder
- Supported: MP4, MOV, M4V, MKV, AVI, WMV
- .3GP: skip gracefully

### ⚠️ Optimizations
- Batch updates every 20 items
- LRU cache
- setUpdatesEnabled(False) bulk load
- Virtual scrolling: TODO

---

## CRITICAL PATTERNS

Threading (MANDATORY):
- Create: self._thread = QThread() + self._worker = MyWorker()
- Move worker: self._worker.moveToThread(self._thread)
- Connect: started → worker.run, finished → _on_finished, finished → quit, thread.finished → _cleanup_thread
- Cleanup: wait(5000), if still running → terminate() + wait(1000), then deleteLater()

Lambda (always default arg): lambda checked, p=path: self._on_delete(p)

Progress (BEFORE thread.start()): setMinimumDuration(0), show(), processEvents(), THEN start()

Excluded folders (EVERY os.walk): filter dirs[:] to exclude _thumbcache, _eliminados, _duplicados_eliminados, __pycache__

piexif (EXACT order): read_bytes → piexif.load() → pop MakerNote → _clean_exif_for_dump() → modify date only → piexif.dump() → piexif.insert()

---

## FILES

Core: exif_handler, video_handler, file_scanner, backup_manager, duplicate_finder, video_duplicate_finder

UI: main_window, folder_tree, thumbnail_grid, photo_detail, video_grid, video_detail, duplicate_panel, date_editor, video_date_editor, cleanup_dialog, log_viewer, styles

Config: main.py, build.spec, requirements.txt, run_exif_manager.bat

---

## BUGS FIXED (Latest)

✅ Tree duplication → single tree
✅ Video counting → V(X) displays
✅ FFmpeg codec=copy → no recompression
✅ Video error handling → no freeze
✅ Video history → logging works
✅ Video backup → _video_backup.json created
✅ Video duplicates → _duplicados_eliminados works
✅ Cleanup threading → no double-quit race
✅ Drag & drop → verified OK
✅ Conservar button → verified OK

---

## CRITICAL NOTES

- EXCLUDED_FOLDERS in EVERY os.walk()
- piexif.load() ALWAYS first
- QThread + worker MANDATORY
- Progress dialog BEFORE thread.start()
- Batch updates every 20 items
- Cleanup threads ALWAYS
- Safety check _current_folder (can be None)
- NO double-quit() on workers
- Lambda: lambda checked, p=path: func(p)

---

## Session changes: Cleanup - removed unnecessary scripts and prompts

- Deleted: `update_claude_md.bat`
- Deleted: `claude_code_prompt.md`

## Session changes: Fix video historial operation label

- `ui/video_date_editor.py` — fixed operation label in `append_historial()` call:
  was always `"fecha_editada"`; now correctly uses `"renombrado"` when `keep_mode=True`
  (rename-only, no date change), matching the `date_editor.py` photo behaviour.

## Session changes: 4 improvements (persist green marker, sin-fecha filter, full backup, dynamic dupe button)

- `core/backup_manager.py`:
  - `has_backup()` now also checks `.video_backup.json` → green marker shows for video-processed folders
  - `create_backup()` v2 format: `{"original_exif_dict": {...}, "timestamp": "..."}` per entry
  - `restore_backup()` handles both v1 (flat dict) and v2 via new `_extract_fields()` helper
  - `append_historial()` now iterates ALL keys in `original_exif` (not just 3 hardcoded fields)
  - Added `VIDEO_BACKUP_FILENAME = ".video_backup.json"` public constant
- `ui/thumbnail_grid.py`:
  - Added `QCheckBox("Solo sin fecha")` (default CHECKED) to the sort/filter bar
  - Added `_apply_filter()` — hides items with valid dates when checked; updates count label
  - Filter applied after each thumbnail batch and after worker finishes
- `ui/duplicate_panel.py`:
  - Added `_media_type` state (`"photo"` default)
  - Added `set_media_type("photo"|"video")` — updates button labels and clears stale results
  - Buttons now read "Buscar duplicados de foto/video" instead of generic "Buscar en carpeta actual"
- `ui/main_window.py`:
  - Connected `_center_tabs.currentChanged` to new `_on_center_tab_changed()` slot
  - Photos tab → `duplicate_panel.set_media_type("photo")`
  - Videos tab → `duplicate_panel.set_media_type("video")` + `photo_detail.clear()` (fixes stuck image)

## Session changes: Full audit — 9 critical fixes (session 4)

### Batch 1 — thumbnail_grid.py, date_editor.py, video_date_editor.py

- `ui/thumbnail_grid.py`:
  - **Issue 8 (photos disappear)**: `_chk_sin_fecha` default changed CHECKED → UNCHECKED.
    Root cause: filter was hiding valid-date photos immediately as they loaded.
  - **Issue 2 (freeze on large folders)**: Fixed O(n²) `_apply_sort()` by removing items from
    the END of the list (O(1) each) instead of from index 0 (O(n) each).
    Added `setUpdatesEnabled(False/True)` + `update()` around the rebuild loop.

- `ui/date_editor.py`:
  - **Issue 3**: Added `setMinimumHeight(600)`; raised `setMaximumHeight` to 0.90 × screen;
    table `setMinimumHeight` 150 → 250, `setMaximumHeight` 200 → 400.
  - **Issue 4**: Already correct — `_on_rename_toggled` shows/hides `_COL_RENAME` when
    rename checkbox is toggled. No change needed.
  - **Issue 5**: `_apply_exif_mode_state()` now unchecks all date checkboxes when switching
    to Conservar mode; auto-checks all three when switching to Cambiar if all were off.
  - **Issue 6**: Already correct — `_PreviewWorker` emits all 5 data fields.

- `ui/video_date_editor.py`:
  - **Issue 3**: Added `setMinimumWidth(700)`, `setMinimumHeight(600)`, raised
    `setMaximumHeight` to 0.90 × screen; table `setMinimumHeight` added at 250, `setMaximumHeight` 200 → 400.

### Batch 2 — duplicate_panel.py, video_duplicate_finder.py

- `core/video_duplicate_finder.py`:
  - **Issue 7**: Already correct — `compute_md5` reads entire file in 64 KB chunks
    via `iter(lambda: f.read(chunk_size), b"")`. No change needed.

- `ui/duplicate_panel.py`:
  - **Issue 9**: Full implementation of [📷 Fotos] / [🎬 Videos] toggle + separate result sets:
    - Added `from core.video_duplicate_finder import VideoDuplicateScanWorker`
    - Added `_photo_groups`, `_photo_selections`, `_video_groups`, `_video_selections` caches
    - `_build_ui()`: added toggle button row at top of left panel
    - `set_media_type()`: saves current results → switches type → restores cached results
    - `_update_toggle_style()`: new helper — applies ON/OFF stylesheet to toggle buttons
    - `_restore_groups_display()`: new helper — repopulates groups list from cache
    - `_begin_scan()`: uses `VideoDuplicateScanWorker` when `_media_type == "video"`,
      `DuplicateScanWorker` when `"photo"`
    - `_on_scan_finished()`: caches completed results into the appropriate photo/video store
    - `_PhotoCard` handles video files gracefully (PIL failures → "Sin vista previa" / "N/D")

### Issues verified already-correct (no code change needed)
- **Issue 1** (green marker): `folder_tree._apply_backup_indicator()` delegates to
  `has_backup()` which already checks both `.exif_backup.json` and `.video_backup.json`
- **Issue 4** (`_COL_RENAME` show/hide): `_on_rename_toggled` already correct
- **Issue 6** (preview worker data): `_PreviewWorker` already emits all 5 fields
- **Issue 7** (MD5 full file): `compute_md5` already reads entire file in chunks
