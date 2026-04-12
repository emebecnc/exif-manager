# EXIF Manager — CLAUDE.md

**Last updated:** 2026-04-12 (session 25)
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
- Move via "Mover a..." context menu
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
✅ Drag & drop removed from thumbnail grid (use "Mover a..." menu)
✅ Conservar button → immediate deletion + auto-advance
✅ Right panel hidden in Duplicados and Videos tabs
✅ Duplicate cards show complete EXIF / video metadata
✅ Duplicate card metadata font increased to 11px (readable)
✅ Duplicate card fonts upgraded to pt units (11pt metadata, 10pt path)
✅ Toggle buttons renamed: FOTOS / VIDEOS / DUPLICADOS (3-way)
✅ DUPLICADOS mode auto-detects dominant media type from folder
✅ on_folder_changed auto-selects FOTOS or VIDEOS based on file count
✅ Toggle buttons: normal case (Fotos/Videos/Duplicados), pill-style rounded
✅ FPS row removed from video duplicate cards
✅ Cards: rounder corners (10px), pill badges, better button colors
✅ Global APP_STYLE applied: refined scrollbars, lists, inputs, tooltips, checkboxes
✅ CRASH FIX: _on_scan_error now calls thread.quit()+wait() → no more -805306369
✅ Workers: traceback.print_exc() for full crash debugging
✅ Folder tree: SP_DirIcon folder icons on all tree items
✅ Workers: per-file try/except → one bad file never aborts scan
✅ Workers: 100-file checkpoints + zero-byte + stat-fail guards
✅ Workers: separate inner try/except for file-collection phase
✅ README.md updated: video support, duplicados features, v1.0 changelog

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

## Session changes: Issues 16–18 — ffmpeg detection, duplicate UX (session 6)

### Files modified

- **`core/video_handler.py`** — Issue 16 (ffmpeg graceful fallback):
  - Added `FFMPEG_AVAILABLE: bool | None = None` module-level flag
  - Added `set_ffmpeg_available(ok: bool)` setter — called once by `main.py`
  - `get_video_thumbnail()`: returns `None` immediately when `FFMPEG_AVAILABLE is False`
    (skips subprocess call entirely — avoids `FileNotFoundError` noise in logs)
  - `_read_ffprobe()`: returns `False` immediately when `FFMPEG_AVAILABLE is False`,
    sets `result["error"]` to a human-readable explanation + install URL

- **`main.py`** — Issue 16 (ffmpeg check):
  - After `_check_ffmpeg()`, calls `set_ffmpeg_available(ffmpeg_ok)` so the module
    flag is set before any UI code can trigger video operations
  - Warning dialog shown only when ffmpeg is missing (unchanged)

- **`ui/main_window.py`** — Issue 16 (status bar feedback):
  - After `showMaximized()`, shows ffmpeg status in status bar:
    - Found: `"✓ ffmpeg detectado — todas las funciones de video disponibles"` (6 s timeout)
    - Missing: `"⚠ ffmpeg no encontrado — miniaturas y edición de video deshabilitadas | Instalá ffmpeg desde https://ffmpeg.org"` (permanent until next action)

- **`ui/duplicate_panel.py`** — Issues 17, 18:
  - Added `QTimer` to PyQt6 imports
  - **Issue 17 (Conservar toast + auto-advance)**:
    - `_on_card_keep()` now shows an immediate toast in `_lbl_header`:
      `"✓ Marcado para CONSERVAR  —  Grupo N / M  →  avanzando al siguiente…"`
    - After 600 ms, calls `_groups_list.setCurrentRow(next_idx)` to advance
    - After 800 ms, restores the summary header via `_update_header_label()`
    - On last group: shows `"(último grupo)"` suffix and restores header after 2.5 s
  - **Issue 18 (Detailed dedup confirmation dialog)**:
    - `_on_dedup_all()` imports `VIDEO_EXTENSIONS` to classify each file
    - Counts `del_photos`, `del_videos`, `keep_photos`, `keep_videos` and their sizes
    - Confirmation dialog now reads:
      ```
      Se moverán N archivos a _duplicados_eliminados (X MB):

      Se eliminará:
        • X fotos  (Y MB)
        • Z videos  (W MB)

      Se conservará:
        • A fotos
        • B videos

      ¿Continuar?
      ```
    - Sections are omitted when count is zero (pure-photo or pure-video session)
    - Fixed `n_del` → `total_del` variable rename in progress dialog setup
  - **Issue 18 (result message)**:
    - `_on_dedup_finished()` header reads:
      `"✓ N archivos movidos a _duplicados_eliminados/\nX MB liberados"`

## Session changes: Issues 10–15 (session 5)

### Files modified

- **`ui/duplicate_panel.py`** — Issues 10, 11, 12:
  - Added `from core.video_handler import get_video_metadata, get_video_thumbnail`
  - Added `_best_video_in_group()` module-level function (uses `video_quality_score`)
  - Added `_get_best(group)` instance method — dispatches to photo or video scorer
  - **Issue 10 (Conservar logic)**: `_PhotoCard._on_keep()` now ONLY emits signal; state is set exclusively by `DuplicatePanel._on_card_keep()` via `set_action()`. Fixed `_on_card_keep` to use `str()` comparison for all path equality checks (avoids Path object identity issues). Same fix in `_on_card_delete_now`.
  - **Issue 11 (Video metadata/thumbnails)**: Added `_VideoCard` class — calls `get_video_metadata()` for resolution, duration, FPS, codec, bitrate, date; calls `get_video_thumbnail()` for first-frame preview via ffmpeg; same Conservar/Eliminar buttons as `_PhotoCard`.
  - **Issue 12 (Best always on left)**: `_show_group()` now sorts `sorted_group = [best] + [others...]` so the highest-quality card is always leftmost. Uses `_VideoCard` for video mode, `_PhotoCard` for photo mode.
  - All callers of `_best_in_group(group)` in instance methods updated to `self._get_best(group)`: `_on_scan_finished`, `_restore_groups_display`, `_remove_group`, `_refresh_list_item`.
  - Initial selection uses `str(p) == str(best)` for robustness.

- **`ui/video_date_editor.py`** — Issues 13, 14:
  - **Issue 13 (Año checkbox)**: `_update_state()` now: in Conservar mode → disables `_grp_date` group AND unchecks all date checkboxes; in Cambiar mode → auto-checks all three if all were off. Added `self._grp_date` instance variable (was local `grp_date`).
  - **Issue 14 (Hora layout)**: Replaced 3-row `QVBoxLayout` + `time_row` with a single compact `QHBoxLayout`: `[◯ Conservar original] [◯ Personalizada:] [HH] h [MM] m [SS] s`. Saves ~2 rows of vertical space.

- **`ui/date_editor.py`** — Issues 14, 15:
  - **Issue 14 (Hora layout)**: Replaced `_time_grp` VBox + `_custom_time_widget` show/hide pattern with a single compact HBox row. Spinboxes start disabled; `_on_time_option_changed` enables/disables them (no widget show/hide). `_apply_exif_mode_state` re-syncs spinbox enabled state when switching Conservar/Cambiar. Removed `_custom_time_widget` reference from `_try_apply_filename_date`.
  - **Issue 15 (Nombre nuevo column)**: `_COL_RENAME` is now always visible (removed `setColumnHidden(_COL_RENAME, True)` from init and `setColumnHidden` from `_on_rename_toggled`). When rename is OFF, preview shows `"— (conservar nombre)"` in grey. Fixed in both `_PreviewWorker.run()` and the sync path in `_on_preview()`.

## Session changes: Conservar immediate deletion + hide right panel (session 7)

### Files modified

- **`core/duplicate_finder.py`** — debug logging added:
  - Prints scan path, file count, group count + filenames at start/end of `run()`

- **`core/video_duplicate_finder.py`** — debug logging added:
  - Same pattern as above for video scans

- **`ui/duplicate_panel.py`** — `_on_card_keep()` rewritten (Workflow 1):
  - Was: mark cards green/red, show toast, auto-advance (no file changes)
  - Now: immediately move every non-kept file to `_duplicados_eliminados/`, log each,
    call `_remove_group()` (which auto-advances), then set toast
    `"✓ N archivos eliminados, X MB liberados"` overriding `_remove_group`'s header
  - QTimer 2500 ms restores `_update_header_label()` if groups remain
  - `"— ✓ Todos procesados"` appended to toast when last group is resolved
  - Debug `print()` statements added to `_on_scan_finished()` and `_show_group()`

- **`ui/main_window.py`** — right panel hidden in non-Photos tabs:
  - Added `self._detail_panel_width: int = detail_w` in `_build_ui()` to persist width
  - `_on_center_tab_changed()` rewritten:
    - index 0 (Fotos): calls `_photo_detail.show()` + restores splitter sizes from saved width
    - index 1 (Duplicados): saves current detail width, calls `_photo_detail.hide()`
    - index 2 (Videos): saves width + hides detail panel (same as Duplicados) + clears detail

## Session changes: Complete EXIF/metadata in duplicate cards (session 8)

### Files modified

- **`ui/duplicate_panel.py`**:
  - Added `get_all_metadata` to `core.exif_handler` import
  - Added `format_duration`, `format_size` to `core.video_handler` import
  - **`_PhotoCard`**: replaced 4 sparse info rows with full `get_all_metadata()` output:
    - File: Nombre, Tamaño, Dims
    - EXIF dates: Fecha orig, Fecha digit, Fecha sist (only if present)
    - EXIF display tags: Make, Model, Orientación, Resolución X/Y, ISO, Exposición, Apertura, Flash (only if present)
    - GPS (if present)
    - File timestamps: Modificado, Creado
    - Selectable path label
  - **`_VideoCard`**: replaced 7 sparse rows with full `get_video_metadata()` output (matches `video_detail.py`):
    - Nombre, Tamaño, Resolución, Duración, FPS, Video codec, Audio codec, Bitrate, Rotación, Formato, Cámara
    - Dates: Fecha meta (creation_time), Modificado, Creado
    - Selectable path label
  - **`_comparison_scroll`**: changed `ScrollBarAlwaysOff` → `ScrollBarAsNeeded` for vertical axis
    (allows scrolling to action buttons when cards grow tall with full metadata)

## Session changes: Robust scan logging + README update (session 13)

### What was already done (sessions 10–12, no changes needed)
- Buttons: `📷 Fotos`, `🎬 Videos`, `🔀 Duplicados` — already normal case ✅
- FPS removed from `_VideoCard` — already done ✅
- Visual polish (rounded cards 10px, pill badges, gradient buttons, APP_STYLE) — done ✅
- Folder icons (SP_DirIcon) — done ✅

### Files modified

- **`core/duplicate_finder.py`** — hardened `DuplicateScanWorker.run()`:
  - File-collection phase wrapped in its own `try/except` with `error.emit()`
  - Per-file `try/except` inside MD5 loop — one bad file skips with `[skip]` log, scan continues
  - `path.stat()` guard before MD5: catches inaccessible files, skips zero-byte files
  - 100-file checkpoint: prints processed/total/unique-hashes/skipped to console
  - Summary log on completion: total, skipped, groups found
  - Clean `[PhotoScan]` prefix on all log lines

- **`core/video_duplicate_finder.py`** — same hardening for `VideoDuplicateScanWorker.run()`:
  - Identical structure: collection try/except, per-file guard, stat check, 100-file checkpoint
  - Clean `[VideoScan]` prefix on all log lines

- **`README.md`** — complete rewrite to reflect current state:
  - Added video grid, video date editor, video duplicates sections
  - Updated duplicados section: 3-mode toggle, auto-detect, per-file robustness
  - ffmpeg listed as prerequisite
  - Stack table updated (ffmpeg, hachoir)
  - Roadmap replaced with v1.0 changelog (all features shipped)
  - `.video_backup.json` added to folder table

---

## Session changes: Crash fix on scan error + folder icons (session 12)

### Root cause of crash -805306369

`_on_scan_error()` in `duplicate_panel.py` was not calling `thread.quit()+wait()`.
When the worker emitted `error`, the main thread handler updated UI state but left
the QThread running. The next scan or app close destroyed the still-running QThread
object → Windows exception code -805306369 (`QThread: Destroyed while thread is still running`).

### Files modified

- **`core/duplicate_finder.py`**:
  - Added `import traceback`
  - `DuplicateScanWorker.run()`: added `traceback.print_exc()` in `except` block
    (prints full stack trace to console for debugging)

- **`core/video_duplicate_finder.py`**:
  - Added `import traceback`
  - `VideoDuplicateScanWorker.run()`: same `traceback.print_exc()` in `except` block

- **`ui/duplicate_panel.py`**:
  - `_on_scan_error()`: added `thread.quit()+wait(5000)` before UI updates,
    with `terminate()+wait(1000)` fallback if thread doesn't stop in time.
    Same pattern as `_on_scan_finished()`. Also prints error to console.
    Label text updated to `"⚠ Error al escanear:\n{msg}"`.

- **`ui/folder_tree.py`**:
  - Added `QStyle` to PyQt6 imports
  - `_make_item()`: sets `SP_DirIcon` folder icon on every tree item
    (`self._tree.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)`)

---

## Session changes: Visual polish, button rename, remove FPS (session 11)

### Files modified

- **`ui/duplicate_panel.py`**:
  - **FIX 1 (normal case buttons)**: Toggle buttons renamed from ALL-CAPS to normal case:
    `"📷 FOTOS"` → `"📷 Fotos"`, `"🎬 VIDEOS"` → `"🎬 Videos"`, `"🔀 DUPLICADOS"` → `"🔀 Duplicados"`.
  - **FIX 2 (remove FPS)**: Removed FPS row from `_VideoCard` metadata display.
    Card now shows: Nombre, Tamaño, Resolución, Duración, Video codec, Audio codec, Bitrate, Rotación, Formato, Cámara, Fecha meta, Modificado, Creado.
  - **FIX 3 (visual polish)**:
    - Card buttons (`_BTN_KEEP_ON/OFF`, `_BTN_DEL_ON/OFF`, `_BTN_NEUTRAL`, `_BTN_CANCEL`):
      border-radius 4px → 8px; larger padding (5px 10px); font-size 10pt; better hover contrast.
    - Card frames (`_apply_visual` in both `_PhotoCard` and `_VideoCard`):
      border-radius 4px → 10px; subtler background tint (18 alpha vs 20).
    - Badges (`_apply_badge_style` in both cards): pill shape (border-radius: 10px);
      border added; text changed to normal case ("★ Conservar" / "Duplicado"); font-size 10pt.
    - Toggle buttons (`_update_toggle_style`): teal ON state (#0d7377), pill shape (border-radius: 10px),
      font-size 10pt, better hover for OFF state.

- **`ui/styles.py`**:
  - `BUTTON_STYLE`: border-radius 6px → 8px; font-size 10pt; better hover brightness.
  - `BUTTON_PRIMARY`: border-radius 6px → 8px; font-size 10pt; brighter hover.
  - `BUTTON_DANGER`: border-radius 6px → 8px; font-size 10pt.
  - `TAB_STYLE`: height 32px → 34px; padding 18px → 22px; font-size 10pt; pane border-top 2px.
  - Added `APP_STYLE`: comprehensive global stylesheet — scrollbars (10px, rounded handles),
    lists (border-radius: 6px, teal selection), tree, inputs, status bar, group boxes,
    checkboxes, radio buttons, tooltips. All font-size: 10pt baseline.
  - Added `apply_app_style(app)` helper function.

- **`main.py`**:
  - Replaced inline `app.setStyleSheet(...)` block in `apply_dark_theme()` with
    `from ui.styles import APP_STYLE; app.setStyleSheet(APP_STYLE)`.
    QPalette (Fusion dark) is retained; only the stylesheet part is replaced.

---

## Session changes: Font sizes, button rename, auto-detect media type (session 10)

### Files modified

- **`ui/duplicate_panel.py`**:
  - **FIX 1 (font sizes)**: `_PhotoCard._info_row` and `_VideoCard._info_row` key+value labels
    changed from `font-size: 11px` → `font-size: 11pt` (proper point sizing, more readable).
    Path labels at card bottom changed from `font-size: 8px` → `font-size: 10pt`.
  - **FIX 2 (button rename)**: Toggle buttons renamed `"📷 Fotos"` → `"📷 FOTOS"`,
    `"🎬 Videos"` → `"🎬 VIDEOS"`. Added third toggle `"🔀 DUPLICADOS"` (`media_type="both"`).
    `_update_toggle_style()` updated to handle 3-way state.
    `set_media_type()` updated to handle `"both"` mode with `_all_groups/_all_selections` cache.
    `_begin_scan()` in "both" mode auto-detects dominant type from folder before scanning.
    `_get_best()` and `_show_group()` in "both" mode infer type from file extension.
  - **FIX 3 (auto-detect)**: `on_folder_changed()` now counts photos vs videos in the folder
    (using new `_count_files_with_extensions()` helper) and auto-selects FOTOS or VIDEOS toggle.
    Videos > photos → auto-select VIDEOS; photos >= videos → auto-select FOTOS.
    User can still manually click any toggle button to override.
  - Added `import os` and top-level imports: `EXCLUDED_FOLDERS` from `file_scanner`,
    `VIDEO_EXTENSIONS` from `video_handler` (removed redundant local import in `_on_dedup_all`).

---

## Session changes: Increase metadata font size in duplicate cards (session 9)

### Files modified

- **`ui/duplicate_panel.py`**:
  - `_PhotoCard._info_row`: key + value label `font-size: 9px` → `11px`; key `setMinimumWidth` 44 → 72
  - `_VideoCard._info_row`: key + value label `font-size: 9px` → `11px`; key `setMinimumWidth` 60 → 80
  - Path labels (selectable full path at card bottom) kept at `8px` — reference-only, smaller is correct

---

## Session changes: Folder counters, remove drag & drop, README (session 14 — FINAL)

### Files modified

- **`ui/folder_tree.py`** — FIX 1 (folder counters always show V):
  - `_update_item_label()`: removed `if videos:` branch — label is always `f"{name}  ({photos}) V({videos})"`
  - `_make_item()`: same — always shows V(n) even when 0
  - Counting already used `IMAGE_EXTENSIONS` and `VIDEO_EXTENSIONS` correctly; only label format changed

- **`ui/thumbnail_grid.py`** — FIX 2 (remove drag & drop from grid):
  - Removed entire `_DraggableList` subclass (34 lines) — no more `setDragEnabled`, `startDrag`, `QDrag`
  - Replaced `self._list = _DraggableList()` → `self._list = QListWidget()` + explicit `setSelectionMode(ExtendedSelection)`
  - Removed unused imports: `QMimeData`, `QUrl` from `PyQt6.QtCore`; `QDrag` from `PyQt6.QtGui`
  - Users move photos via "Mover a..." context menu (in `folder_tree.py` / `_DropTree`) — fully functional
  - `QEvent` kept (used by `eventFilter` for Delete key handling)

- **`README.md`** — already present from session 13, no changes needed

### APP STATUS: DONE ✅

---

## Session changes: Remove emojis from duplicate panel toggle buttons (session 15)

### Files modified

- **`ui/duplicate_panel.py`** — toggle button labels:
  - `"📷 Fotos"` → `"Fotos"`
  - `"🎬 Videos"` → `"Videos"`
  - `"🔀 Duplicados"` → `"Duplicados"`

- **`ui/folder_tree.py`** — no change needed (already correct from session 14):
  - Counting uses `_IMAGE_EXTENSIONS` and `_VIDEO_EXTENSIONS` ✅
  - Label always shows `({photos}) V({videos})` ✅

---

## Session changes: Tab order, folder counters fix (session 16)

### Files modified

- **`ui/main_window.py`** — tab reorder + index updates:
  - Tab order changed: `Fotos(0) → Duplicados(1) → Videos(2)` → `Fotos(0) → Videos(1) → Duplicados(2)`
  - Removed emojis from tab labels: `"📷  Fotos"` → `"Fotos"`, `"🎬  Videos"` → `"Videos"`, `"🔍  Duplicados"` → `"Duplicados"`
  - `scan_started` connection: `setCurrentIndex(1)` → `setCurrentIndex(2)` (Duplicados now at index 2)
  - `_on_center_tab_changed`: updated index logic — Videos now index 1 (was 2), Duplicados now index 2 (was 1)

- **`ui/folder_tree.py`** — rewrite counting to use `path.glob("*")`:
  - `_count_photos` and `_count_videos` now use `path.glob("*")` + `f.is_file()` instead of `os.scandir` + `entry.is_file(follow_symlinks=False)`
  - Root cause of (0) bug: `follow_symlinks=False` on Windows can incorrectly classify regular files on certain path types (UNC, junctions)
  - `_IMAGE_EXTENSIONS` and `_VIDEO_EXTENSIONS` constants unchanged

- **`ui/duplicate_panel.py`** — no change (already correct from session 15)

---

## Session changes: Fix run_exif_manager.bat (session 17)

- **`run_exif_manager.bat`** — replaced broken venv-detection script with simple launcher:
  ```batch
  @echo off
  cd /d D:\homelab\exif_manager
  python main.py
  pause
  ```
  Old version tried to activate `venv\Scripts\activate.bat` (venv doesn't exist) and had no `pause` on success, so errors were invisible.

---

## Session changes: Fix run_exif_manager.bat — auto-detect python.exe (session 18)

- **`run_exif_manager.bat`** — replaced hardcoded `python` call with `where python` auto-detection:
  - Uses `for /f` loop over `where python` output to resolve full path to `python.exe`
  - Exits with clear error message if Python not found in PATH
  - Runs `main.py` via full resolved path — avoids PATH lookup failures on double-click

---

## Session changes: Fix run_exif_manager.bat — hardcoded Python310 path (session 19)

- **`run_exif_manager.bat`** — simplified to hardcoded full path:
  - Detected installed version: `Python310` (not 311 as initially assumed)
  - Path: `C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python310\python.exe`
  - Uses `%USERNAME%` so it works for any user on this machine

---

## Session changes: Fix launchers — hardcoded Python310 path for user MB (session 20)

- Verified: `python` is in PATH (Python 3.10.10) and `C:\Users\MB\AppData\Local\Programs\Python\Python310\python.exe` exists
- **`run_exif_manager.bat`** — uses hardcoded full path to python.exe (avoids cmd.exe PATH lookup failures on double-click):
  `C:\Users\MB\AppData\Local\Programs\Python\Python310\python.exe main.py`
- **`start_app.vbs`** (NEW) — alternative VBScript launcher using same full path, `Run(..., 1)` keeps window visible

---

## Session changes: Three new launchers (session 21)

- **`run.cmd`** — simplest: `start python main.py` opens app in new process, cmd exits immediately
- **`launch_exif_manager.ps1`** — PowerShell script with execution policy bypass
- **`launch_app.cmd`** — CMD wrapper that calls the .ps1 via `powershell -ExecutionPolicy Bypass`
- Try in order: `run.cmd` first (simplest), then `launch_app.cmd` if that fails

---

## Session changes: Fix startup slowness — lazy folder tree (session 22)

- **`ui/folder_tree.py`** — `_make_item()` rewritten:
  - **Before**: counted photos + videos (`path.glob("*")` × 2) + checked backup (`has_backup()`) for every tree item created
  - **After**: creates item with just `path.name`, no disk I/O at creation time
  - Root cause of 2-min startup: `load_root` expands root → `_on_item_expanded` calls `_make_item` for every child → N subfolders × (2 globs + 1 backup check) = hundreds of disk scans before window appears
- **`_on_item_clicked`**: added `_apply_backup_indicator()` call so green marker appears on first click
- **Net effect**: file counts and backup indicators still show — just load on first click instead of at startup
- `refresh_item()` unchanged — still does full update after edits

---

## Session changes: Cleanup old/debug files (session 23 — PRODUCTION)

### Deleted
- `run_exif_manager.bat` — superseded by `run.cmd`
- `launch_exif_manager.ps1` — unused PowerShell launcher
- `launch_app.cmd` — unused CMD wrapper
- `start_app.vbs` — unused VBScript launcher
- `debug_app.py` — debug-only script
- `error_log.txt` — was already absent

### Kept
- `run.cmd` — working launcher (double-click to start app)
- All source code, README.md, CLAUDE.md

### Project state: PRODUCTION READY ✅
Launcher: `run.cmd`
Entry point: `main.py`

---

## Session changes: Fix Conservar button (session 24)

### Root cause identified

`on_folder_changed()` auto-detected media type on every folder click. If the new folder
had a different dominant media type, it called `set_media_type()` → `_restore_groups_display([], {})`
→ **`self._groups` cleared**. Old cards remained in `_comparison_scroll` (hidden by `_right_stack`
switching to index 0). When user clicked Conservar on a card, `_on_card_keep` hit:
`if group_idx >= len(self._groups): return` → **silent return, nothing happened**.

### Fix — `ui/duplicate_panel.py`

- **`on_folder_changed()`**: added early `return` when `self._groups` is non-empty.
  Auto media-type detection now only runs when there are no active scan results.
  Once user finishes reviewing (all groups processed), auto-detect resumes normally.

- **`_show_group()`**: removed stale `print(f"DEBUG: _show_group...")` left from prior session.

- **`_on_card_keep()`**: added debug prints:
  - On entry: `[Conservar] clicked: group_idx=N, groups=M, path=...`
  - On guard fire: `[Conservar] GUARD FIRED — bug!`
  - After move loop: `[Conservar] to_trash=N, deleted=M, errors=[...]`
  → Run app, click Conservar, check console output to confirm fix works.

---

## Session changes: Move buttons below badge in duplicate cards (session 25)

- **`ui/duplicate_panel.py`** — `_PhotoCard` and `_VideoCard` layout reordered:
  - **Before**: thumb → badge → metadata → path → stretch → buttons (buttons hidden at bottom)
  - **After**: thumb → badge → **buttons** → stretch → metadata → path
  - Buttons are now directly visible below the ★ Conservar / Duplicado label
  - `addStretch()` pushes metadata below buttons — always need to scroll to read metadata,
    but action buttons are always immediately reachable without scrolling
