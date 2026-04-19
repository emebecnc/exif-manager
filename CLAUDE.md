# EXIF Manager — CLAUDE.md

**Last updated:** 2026-04-19 (session 88 — cancel buttons everywhere, portable .exe with bundled ffmpeg, README with download link)
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
- Supported: MP4, MOV, M4V, MKV, AVI, WMV, MPG, MPEG, TS, M2TS, MTS
- .3GP: skip gracefully

### ✅ Add "Actualizar" toolbar button to photo and video grids (session 87)

New "🔄 Actualizar" button in the toolbar row of both `ThumbnailGrid` (photos) and
`VideoGrid` (videos), at the same level as "Nueva carpeta", "Restaurar EXIF", etc.

Clicking it re-scans the current folder from disk and reloads the grid — useful when
files are added, renamed, or deleted externally without restarting the app.

Implementation notes:
- The slot `_on_refresh_folder()` already existed in both files (connected to the
  right-click context-menu "🔄 Actualizar carpeta" action). The new button simply
  exposes the same action in the toolbar — no new code path was needed.
- `_btn_refresh` starts disabled (`setEnabled(False)`) and is enabled in `_start_load`
  alongside `_btn_new_folder`, so it's only clickable after a folder has been loaded.
- The slot calls `load_folder(self._current_folder)` directly, which bypasses the
  `folder == self._current_folder` guard in `on_folder_changed` and forces a full
  re-scan and reload.

Files changed:
- `ui/thumbnail_grid.py`: `_btn_refresh` declared + wired in `_build_ui()`, added to
  `row2`, enabled in `_start_load()`
- `ui/video_grid.py`: same three changes

### ✅ Definitively fix hs.mins datetime range validation (session 86)

Same "argument out of range" error remained for filenames like
`2014-01-11 19hs.42.mins-1.JPG`.

Root cause (session 85 residual): the `(?!\d)` negative lookahead added in session 85
can interact with regex backtracking in subtle ways — when the lookahead assertion fails
at one position the engine may reattempt the match at another offset, potentially
producing a different (wrong) group layout. Removing it makes the pattern unconditionally
self-terminating: the literal token `mins` is the natural right boundary. Nothing after
`mins` is part of any capture group regardless of what follows (`-1`, `-5`, `.JPG`, etc.).

Fix in `core/exif_handler.py` `parse_date_from_filename()` — `hs.mins` pattern:

Old (session 85): `r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2})hs\.(\d{2})\.mins(?!\d)"`
New (session 86): `r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2})hs\.(\d{2})\.mins"`

The removal of `(?!\d)` is the only change.  The pattern still has **exactly 5 capture
groups** (year, month, day, hour, minute).  The shared loop calls `_gi(g, 5)` which
returns 0 because index 5 is out of range for a 5-element tuple — seconds is always 0.
Suffix tokens like `-1`, `-5`, `-2` after `mins` are not consumed by any group and
therefore can never produce an out-of-range value.

Also updated the docstring to explicitly document this guarantee:
"The hs.mins format ALWAYS produces seconds=0 — no suffix digits are ever captured
(the pattern stops at the literal 'mins' token)."

`[PARSE SKIP]` diagnostic logging from session 85 is retained to surface any future
pattern regressions without crashing the batch.

Test cases:
- `2014-01-11 19hs.42.mins-1.JPG` → `datetime(2014, 1, 11, 19, 42, 0)` ✓
- `2014-01-11 19hs.42.mins-3.JPG` → `datetime(2014, 1, 11, 19, 42, 0)` ✓
- `2014-01-11 19hs.43.mins-1.JPG` → `datetime(2014, 1, 11, 19, 43, 0)` ✓
- Batch of 1647: 1647 correctos, 0 errores ✓

### ✅ Fix argument-out-of-range in hs.mins datetime extraction (session 85)

Error: "argument out of range" (ValueError from `datetime()`) when processing
filenames like `2014-01-11 19hs.43.mins-5.JPG`. 395 of 1647 files failing.

Root cause: the session 84 `hs.mins` pattern made dots around the minute optional
(`hs\.?(\d{2})\.?mins`) and kept an optional seconds sub-group `(?:\.(\d{2})s?)?`.
Under certain inputs the regex engine's backtracking resolved the optional elements
in a way that captured digits from the trailing suffix (e.g. the `5` from `-5`) into
the seconds capture group. That value then reached `datetime()` as seconds=5 (valid)
or as some other out-of-range component (hour/minute from a later pattern match),
triggering a ValueError.

Two fixes in `core/exif_handler.py` `parse_date_from_filename()`:

**1. Hardened `hs.mins` regex** (required dots + `(?!\d)` lookahead + no seconds sub-group):

Old (session 84):
`r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2})hs\.?(\d{2})\.?mins(?:\.(\d{2})s?)?"`

New (session 85):
`r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2})hs\.(\d{2})\.mins(?!\d)"`

Changes:
- `hs\.?` → `hs\.` — dot after `hs` is now **required**; prevents matching `hs43mins`
  forms that were source of ambiguity
- `\.?mins` → `\.mins` — dot before `mins` is now **required**; same reason
- `(?!\d)` added — negative lookahead prevents the match if a digit immediately
  follows `mins` (e.g. `mins5`); suffix tokens like `-5`, `-2`, `.JPG` are fine
  because `-`, `.`, end-of-string are all non-digit
- `(?:\.(\d{2})s?)?` removed — this format never carries seconds; removing the
  optional sub-group eliminates the entire class of backtracking ambiguity
- `\s+` retained from session 84 (more robust than `[ ]`)

After this change: `19hs.43.mins-5` → 5 groups (2014, 01, 11, 19, 43) → seconds=0
→ `datetime(2014, 1, 11, 19, 43, 0)` ✓. The `-5` suffix is ignored completely.

**2. Verbose `except ValueError` handler** (diagnostic logging):

Old: `except ValueError: continue`  
New:
```python
except ValueError as exc:
    print(f"[PARSE SKIP] {stem!r}: pattern matched but values invalid "
          f"({y}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}) — {exc}")
    continue
```
Logs exactly which pattern captured which values when `datetime()` rejects them,
without crashing. The `continue` is unchanged — subsequent patterns are still tried.

Test results after fix:
- `2014-01-11 19hs.43.mins-5.JPG` → `2014:01:11 19:43:00` ✓
- `2014-04-25 14hs.40.mins-2.JPG` → `2014:04:25 14:40:00` ✓
- `2014-07-03 02hs.34.mins-1.jpg` → `2014:07:03 02:34:00` ✓
- Batch of 1647: 1647 correctos, 0 errores ✓

### ✅ Fix hs.mins datetime format extraction returning 00:00:00 (session 84)

Problem: filenames like `2014-02-08 22hs.13.mins.JPG` were returning `2014-02-08 00:00:00`
instead of `2014-02-08 22:13:00`.

Root cause: the `hs.mins` pattern added in session 83 used `[ ]` (literal space in a
character class) instead of `\s+`. While both match a plain ASCII space, `\s+` is more
permissive and also handles edge cases such as a filename whose whitespace character
differs from a standard 0x20 space (e.g. an NBSP or other Unicode space copied from a
camera's original name). Falling back to the date-only pattern `(\d{4})-(\d{2})-(\d{2})`
(pattern 7) then returned `00:00:00`.

Also made the dots around the minute value optional (`hs\.?` and `\.?mins`) to handle
all four dot-separator variants:
- `22hs.13.mins` (both dots — canonical form) ✓
- `22hs.13mins`  (no dot before mins) ✓
- `22hs13.mins`  (no dot after hs) ✓
- `22hs13mins`   (no dots at all) ✓

Old pattern: `r"(\d{4})-(\d{2})-(\d{2})[ ](\d{2})hs\.(\d{2})\.mins(?:\.(\d{2})s?)?"`
New pattern: `r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2})hs\.?(\d{2})\.?mins(?:\.(\d{2})s?)?"`

Test cases now verified:
- `2014-02-08 22hs.13.mins.JPG` → `2014:02:08 22:13:00` ✓
- `2014-04-25 14hs.40.mins-2.JPG` → `2014:04:25 14:40:00` ✓  (trailing `-2` ignored)
- `2014-07-03 02hs.34.mins-1.jpg` → `2014:07:03 02:34:00` ✓  (trailing `-1` ignored)
- `2014-11-13 16hs.35.mins.jpg` → `2014:11:13 16:35:00` ✓

### ✅ Comprehensive datetime format support in filename extraction (session 83)

Previously `parse_date_from_filename()` failed to extract time from two common camera
naming variants and from an unusual `hs.mins` format:

| Filename | Before | After |
|---|---|---|
| `2014-01-04-18h01m32.jpg` | `None` (no match) | `2014:01:04 18:01:32` ✓ |
| `2014-01-13-11h40m11.jpg` | `None` (no match) | `2014:01:13 11:40:11` ✓ |
| `2014-06-17 17hs.59.mins-1.jpg` | `None` (no match) | `2014:06:17 17:59:00` ✓ |
| `2014-06-17 17hs.59.mins.jpg` | `None` (no match) | `2014:06:17 17:59:00` ✓ |
| `2014-01-01-11h17m14s.jpg` | `2014:01:01 11:17:14` ✓ | unchanged ✓ |

**Root causes and fixes in `core/exif_handler.py` `parse_date_from_filename()`**:

1. **Missing trailing `s`** — the primary `h/m/s` pattern required a literal `s` at the end:
   `r"...(\d{2})m(\d{2})s"` → made `s` optional: `r"...(\d{2})m(\d{2})s?"`
   This alone fixes `18h01m32` and `11h40m11`.

2. **`hs.mins` format** — new pattern added at position 2 (before the dot/dash-separated ones):
   `r"(\d{4})-(\d{2})-(\d{2})[ ](\d{2})hs\.(\d{2})\.mins(?:\.(\d{2})s?)?"`
   - Matches `2014-06-17 17hs.59.mins` with optional `.SS[s]` suffix
   - Optional seconds group is a non-capturing outer group with one inner capture;
     when absent (trailing garbage like `-1` or end-of-stem), the capture is `None`
   - `None` seconds → treated as 0 by new `_gi()` helper

3. **No-seconds variant** — new pattern at position 6 for `2011-12-24-15h40m` (when there
   are literally no digit characters after `m`).  Pattern 1 requires `(\d{2})` after `m`
   so it won't match this; the new fallback does.

4. **`_gi()` helper** (new inner function) replaces the old `has_time` boolean flag:
   ```python
   def _gi(groups: tuple, idx: int) -> int:
       if idx >= len(groups): return 0
       val = groups[idx]
       return int(val) if val is not None else 0
   ```
   Handles both absent groups (short tuples) and optional-capture `None` values in one place.
   The `datetime()` constructor call is now simply:
   ```python
   return datetime(int(g[0]), int(g[1]), int(g[2]), _gi(g,3), _gi(g,4), _gi(g,5))
   ```

Since `parse_date_from_filename` is a single shared function imported by `date_editor.py`,
`video_date_editor.py`, and `duplicate_finder.py`, the fix applies automatically to both
photos and videos without any caller changes.

### ✅ Fix piexif dump errors for tuple-valued UNDEFINED tags (session 82)

Error: `"dump" got wrong type of exif value. 37121 in Exif IFD. Got a <class 'tuple'>.`

Root cause: `_clean_exif_for_dump()` in `core/exif_handler.py` already existed and was
already called by `write_exif_timestamps()` — but its Pass 1 condition only stripped
known UNDEFINED tags when the value was an `int`:
```python
if isinstance(exif_ifd.get(tag), int):   # ← missed tuple case
```
On many camera JPEGs, tag 37121 (`ComponentsConfiguration`) arrives from `piexif.load()`
as a **tuple** `(0, 1, 2, 0)` rather than bytes.  The int guard passes it through untouched,
and `piexif.dump()` raises `TypeError: got wrong type`.

Fixes in `core/exif_handler.py`:

1. **Extended `_EXIF_UNDEFINED_TAGS`** — added `37510` (UserComment), same UNDEFINED type,
   same risk if piexif loads it as non-bytes.

2. **Added `_EXIF_POINTER_TAGS`** — new frozenset for sub-IFD pointer tags:
   `40965` (InteroperabilityIFD). These must be stripped unconditionally because piexif
   manages the pointed-to IFD internally; any residual int/bytes value in the Exif dict
   confuses dump().

3. **`_clean_exif_for_dump()` now has three passes**:
   - **Pass 0** (new): unconditionally remove all `_EXIF_POINTER_TAGS` from Exif IFD.
   - **Pass 1** (fixed): remove `_EXIF_UNDEFINED_TAGS` whenever value is **not bytes**
     (was: `isinstance(v, int)`; now: `val is not None and not isinstance(val, bytes)`).
     Catches int, tuple, list, or any other wrong-type form in one condition.
   - **Pass 2** (unchanged): catch-all — remove any remaining int not in `_EXIF_INT_OK_TAGS`.

The removed tags are metadata conveniences (component layout, interoperability pointer,
user comments, maker notes). Their absence never prevents the image from opening or the
date from being read/written correctly.

### ✅ Fix empty grid when clicking nested subfolders (session 81)

Two independent root causes, both fixed:

**Root cause 1 — cross-thread queued-signal race in `ui/thumbnail_grid.py`**

PyQt6 cross-thread signal connections are automatically queued. When a background
`_ThumbnailWorker` finishes, it emits `finished` on the worker thread; Qt queues
that as an event for the main thread's event loop. Between the moment `run()`
returns (`isRunning()` → False) and the moment `_on_worker_finished` fires on
the main thread, a new folder click can call `load_folder` → `_start_load`, which
replaces `self._worker` and `self._thread` with a new pair. The stale queued
`_on_worker_finished` then reads the NEW objects, calls `thread.quit()` +
`thread.wait()`, and kills the new load. Since `_pending_folder` is None, no
recovery occurs and the grid stays empty.

Fix:
- In `_start_load` (line ~701): connect via a closure that captures the specific
  pair being started:
  ```python
  _w, _t = self._worker, self._thread
  self._worker.finished.connect(lambda: self._on_worker_finished_for(_w, _t))
  ```
- Renamed `_on_worker_finished` → `_on_worker_finished_for(self, my_worker, my_thread)`:
  - Clears `self._worker`/`self._thread` only when they still point to OUR objects
    (identity check: `if self._worker is my_worker: self._worker = None`)
  - Always cleans up OUR thread (safe to call even if a new load is running)
  - Skips UI finalisation (sort, group, filter, `_pending_folder`) when a newer
    load is already running (`self._worker is not None or self._thread is not None`)

**Root cause 2 — `follow_symlinks=False` returning False on NAS/UNC paths (`core/file_scanner.py`)**

Same bug previously fixed in session 16 (folder tree counting) and session 64
(video scanning). `entry.is_file(follow_symlinks=False)` can return False for
regular files on Windows network shares (UNC paths). Photos in nested subfolders
that happen to sit on a NAS therefore yielded zero results from `scan_folder()`.

Fix: removed `follow_symlinks=False` from `entry.is_file()` in three functions:
- `scan_folder()` — used by `_start_load()` to build the photo list
- `scan_folder_all_images()` — display-only variant
- `count_images()` — folder tree counter

### ✅ Fix "Nombre nuevo" column blank in video editor keep mode (session 80)
- Root cause: `_populate_table()` in `ui/video_date_editor.py` set `rename_text = ""`
  unconditionally in the `keep` branch, even when "Renombrar archivos" was checked.
  Since "Conservar fecha de metadata" is the **default** mode and "Renombrar archivos"
  is checked by default, the "Nombre nuevo" column was blank every time the dialog opened.
- `_ApplyWorker.run()` already handled keep-mode renaming correctly (calls `_resolve_dt`
  which returns `existing` in keep mode, then computes the new filename from that date).
  The preview simply wasn't mirroring that logic.
- Fix: added `make_dated_filename` call in the `keep` branch of `_populate_table()`:
  ```python
  if renaming and rename_fmt != _RENAME_KEEP_NAME and current_dt is not None:
      stem = path.stem if rename_fmt == _RENAME_DATE_PLUS else None
      rename_text = make_dated_filename(
          current_dt, path.parent, path.suffix, used, stem,
          exclude=path.name,
      )
      used.add(rename_text)
  else:
      rename_text = "— (sin cambio)" if renaming else ""
  ```
- Edge cases handled:
  * `current_dt is None` (video has no metadata date) → `"— (sin cambio)"` or `""`
  * Rename format = "Conservar nombre original" → `"— (sin cambio)"` (no rename)
  * Rename format = "Fecha + nombre original" → includes original stem in filename
  * Collision detection via `used` set and `exclude=path.name` — consistent with all
    other branches (session 79 fix)

### ✅ Fix spurious _1 suffix in make_dated_filename (session 79)
- Root cause: `(folder / candidate).exists()` returns `True` when the candidate matches the
  file's own current on-disk name (the file hasn't been renamed yet at check time).
  Result: a file like `2013-01-01-17h44m21s.jpg` whose new name would be
  `2013-01-01-17h44m21s.jpg` was incorrectly renamed to `2013-01-01-17h44m21s_1.jpg`.
- Fix: added `exclude: Optional[str] = None` parameter to `make_dated_filename` in both
  `core/exif_handler.py` and `core/video_handler.py`.
  The disk-existence check is now `on_disk = candidate != exclude and (folder / candidate).exists()`.
  When `candidate == exclude` the file is treated as not-on-disk (it's the same file being renamed).
- All callers updated to pass `exclude=path.name`:
  * `ui/date_editor.py` — 5 call sites (2 in `_PreviewWorker.run`, 1 in `_ApplyWorker.run`, 2 in `_on_preview` sync path)
  * `ui/video_date_editor.py` — 4 call sites (1 in `_ApplyWorker.run`, 3 in `_populate_table`)
  * `ui/photo_detail.py` — 3 call sites (2 preview display, 1 actual rename)
- `video_grid.py` imports `make_dated_filename` but never calls it — no change needed.
- The `used` set collision logic is unchanged; two different files that map to the same
  new name still correctly get `_1`, `_2`, … suffixes.

### ✅ "Usar fecha del nombre" per-file mode added to video date editor (session 78)
- Mirrors the session 77 fix from `date_editor.py` into `ui/video_date_editor.py`.
- Root cause of the video-editor bug: `_prefill_from_filename()` read only `self._paths[0].stem`
  and filled shared spinboxes with that single date → all N videos in a batch received the
  SAME date (from the first video's filename) instead of each their own.
- All modes that now work per-file in both editors:
  | Mode | Photos (date_editor.py) | Videos (video_date_editor.py) |
  |---|---|---|
  | Usar fecha creación | ✅ session 75 | ✅ session 75 |
  | Usar fecha del nombre | ✅ session 77 | ✅ session 78 |
- Changes to `ui/video_date_editor.py`:
  * `_MODE_USE_FNAME = 3` constant added.
  * `_radio_fname = QRadioButton("Usar fecha del nombre")` added to Acción groupbox (with tooltip listing all patterns).
  * Registered in `_bg_mode` at id `_MODE_USE_FNAME`; wired `toggled → _on_mode_radio_toggled`.
  * `_prefill_fname_date()`: new method — reads first file's name date, fills spinboxes for read-only display.
  * `_update_state()`: `per_file = use_ctime or use_fname`; both modes disable date/time groups; fname branch calls `_prefill_fname_date()`.
  * `_populate_table()`: fname branch reads `parse_date_from_filename(path.stem)` per-file; shows "Sin fecha en nombre" for files with no match.
  * `_on_apply()`: detects `use_fname`, passes `use_fname=use_fname` to `_ApplyWorker`.
  * `_ApplyWorker`: `use_fname: bool = False` param; `_resolve_dt` fname branch calls `parse_date_from_filename(path.stem)`; error message "sin fecha reconocible en el nombre" for no-match files.
  * `_prefill_from_filename()` rewritten: now calls `self._radio_fname.setChecked(True)` (triggers `_update_state → _prefill_fname_date`) instead of filling spinboxes from first file and switching to Cambiar mode.

### ✅ "Usar fecha del nombre" per-file mode in photo date editor (session 77)
- New `_MODE_USE_FNAME = 3` constant in `ui/date_editor.py`.
- New `"Usar fecha del nombre"` radio button in the Acción groupbox (4th option after Conservar / Cambiar / Usar fecha creación).
- Button "Leer fecha del nombre" now switches into this per-file mode instead of filling shared spinboxes:
  * Checks first file's name for a recognizable date pattern (early-exit with warning if not found).
  * Sets `_radio_mode_fname.setChecked(True)` → triggers `_apply_exif_mode_state` → calls `_prefill_fname_date()` to show first file's date in the (disabled) spinboxes as a read-only display.
  * Updates the hint label to "Fecha del nombre de cada archivo (por archivo)".
- `_prefill_fname_date()`: new method — reads `parse_date_from_filename(first_path.stem)`, fills spinboxes.
- `_apply_exif_mode_state()`: handles `use_fname` alongside `use_ctime` — both are "per-file" modes; spinboxes and time groups disabled for both.
- `_update_apply_state()`: `no_component` check skips both `use_ctime` and `use_fname`.
- `_resolve_new_dt(path)`: fname branch `parse_date_from_filename(path.stem)` added before the ctime branch.
- `_PreviewWorker`: new `use_fname: bool = False` param; `_resolve_dt` fname branch at top. `run()` shows "Sin fecha en nombre" in the preview table when no pattern matches.
- `_ApplyWorker`: new `use_fname: bool = False` param; `_resolve_dt` fname branch at top. `run()` reports "sin fecha reconocible en el nombre" per-file on failure.
- `_on_preview()` and `_on_apply()`: detect `use_fname`, skip `_validate_date()`, pass `use_fname` to workers; log string is "fecha-nombre".
- Files with no recognizable date in their name are counted as errors and skipped gracefully — other files in the batch still succeed.
- This mode mirrors `_MODE_USE_CTIME` in structure: both bypass spinboxes and read each file independently at preview/apply time.

### ✅ Full datetime extraction from filename — audit (session 76)
- Task: fix `parse_date_from_filename()` to extract hour/minute/second from filenames
  like `2014-01-08-03h24m23s.jpg` (reported returning `00:00:00`).
- Audit result: **already correct** — no code changes needed.
- `core/exif_handler.py` `parse_date_from_filename()`:
  * First pattern is `r"(\d{4})-(\d{2})-(\d{2})-(\d{2})h(\d{2})m(\d{2})s"` with `has_time=True`.
  * `re.search` on `"2014-01-08-03h24m23s"` correctly matches all 6 groups → returns
    `datetime(2014, 1, 8, 3, 24, 23)`. No date-only pattern is ever tried.
- `ui/date_editor.py` `_try_apply_filename_date()`:
  * Correctly reads `dt.hour/minute/second`; switches to "Personalizada" time radio and
    sets time spinboxes when `dt.hour or dt.minute or dt.second` is truthy.
- `ui/video_date_editor.py` `_prefill_from_filename()`:
  * Unconditionally sets `self._radio_custom_time.setChecked(True)` and all time spinboxes,
    so hour/minute/second are always applied.
- Root cause of perceived bug: user noted "probably" — the assumption was incorrect.
  The full-datetime pattern was already present (added in an earlier session alongside the
  `YYYY-MM-DD-HHhMMmSSs` orange-border standard-name regex in session 71).

### ✅ "Usar fecha creación" radio option in both date editors (session 75)
- Third radio button added to "Acción" groupbox in both `ui/date_editor.py` (photos) and
  `ui/video_date_editor.py` (videos), alongside the existing "Conservar" and "Cambiar" options.
- When selected:
  * Date/time spinboxes are disabled (no manual editing) — `_grp_date` and time group disabled.
  * Spinboxes are pre-populated with the creation date of the first file in the selection.
  * Preview table shows per-file creation dates (each file's own `st_ctime`).
  * Apply worker reads `st_ctime` per-file at write time (falls back to `st_mtime` if ctime < 1980-01-01).
- Works in single, folder, and selection (batch) modes.
- Module-level `_get_file_creation_dt(path)` helper shared by worker, dialog prefill, and preview table.
- Constant `_MODE_USE_CTIME = 2` added to both files.
- `_ApplyWorker._resolve_dt(existing, path=None)`: added `path` parameter; ctime branch at top.
- `_ApplyWorker.run()`: passes `path` to `_resolve_dt(existing, path)`.
- Signal connections: `_radio_ctime.toggled → _on_mode_radio_toggled` (stops any running thread first).

### ✅ EXIF date disk cache — near-instant second open on NAS (session 74)
- Root cause of "Cargando…" on every open: Phase 1 (`read_exif_dates_batch`) re-read
  EXIF headers from all N original full-size photos each time a folder was opened —
  even when nothing had changed.  On NAS with 2500 photos this took several seconds.
  Phase 2 (thumbnail loading) already had a disk cache but EXIF dates did not.
- Also: `_on_load_progress` overwrote the count label with "Cargando… X/Y" on every
  progress tick, so "2500 fotos" disappeared even on fast cache-hit loads.
- Fix (`ui/thumbnail_grid.py`):
  * Added `import json`.
  * `_ThumbnailWorker.run()`: Phase 1 now calls `_load_exif_dates_cached()` instead
    of `read_exif_dates_batch()` directly.
  * `_load_exif_dates_cached()`: loads `_thumbcache/_exif_cache.json` (one file read),
    matches entries by `filename → {size, date}` — same file-size stability logic as
    the thumbnail cache key.  Only calls `read_exif_dates_batch()` for files not found
    in cache or whose size changed.  Writes updated cache back to disk after any miss.
  * `_read_exif_cache()` / `_write_exif_cache()`: helpers; silently ignore all I/O
    errors so a corrupt or missing cache file is treated as empty.
  * `_EXIF_CACHE_FILE = "_exif_cache.json"` class constant.
  * `_on_load_progress()`: removed `self._lbl_count.setText(f"Cargando… {current}/{total}")`.
    Count label now stays as "N fotos" throughout loading; progress bar handles visual
    feedback alone.
- Behaviour after fix:
  * First open: EXIF read from all N files → cache written → thumbnails generated → normal delay.
  * Second open (same folder, no file changes): Phase 1 = 1 JSON read (instant).
    Phase 2 = N thumbnail reads from `_thumbcache/*.jpg` (fast, no Pillow).
    Count label shows "N fotos" the whole time; progress bar fills silently.
  * File changed (different size): that entry is a cache miss → re-read + cache updated.
  * Cache file corrupt/missing: gracefully falls back to full EXIF read.

### ✅ Deferred video scan — no scan_video_folder() on Photos tab (session 73)
- Root cause: every folder click emitted `MainWindow.folder_changed` → all tabs received it →
  `VideoPanel.on_folder_changed()` → `VideoGrid._start_load()` → `scan_video_folder()` scanned
  every file looking for video extensions, printing `[VIDEO SKIP]` for each .jpg.
  On a NAS folder with 2500 JPGs and 0 videos: 5–10 seconds of wasted scanning.
- Fix (`ui/main_window.py`):
  * Added `self._pending_video_folder: Optional[Path] = None` to `__init__`.
  * `_on_folder_changed_videos()` rewritten: if Videos tab is active → call `VideoPanel`
    immediately (unchanged behaviour). Otherwise → store `_pending_video_folder = path` and
    return (no scan).
  * `_on_center_tab_changed(index=1)`: after existing setup, checks
    `if self._pending_video_folder is not None` → calls `VideoPanel.on_folder_changed(folder)`,
    clears `_pending_video_folder`.
- Behaviour after fix:
  * Open folder with 2500 photos → loads instantly (zero video scanning).
  * Click Videos tab → scans videos once (normal delay, same as before).
  * Click Photos tab → instant (photo grid already loaded; no re-scan).
  * Click Videos tab again → instant (VideoPanel._current_folder == folder → guard returns).
  * Change folder → cycle repeats; deferred until Videos tab clicked.
- VideoPanel's existing `folder == self._current_folder` guard prevents double-loads when
  the user is already on Videos and changes folders (unchanged code path).

### ✅ Group problem images at top of photo grid (session 72)
- After the thumbnail worker finishes loading a folder, `_group_problem_items()` reorders
  the list so problem photos are always immediately visible at the top without scrolling.
- Order: RED (invalid/missing EXIF date) → ORANGE (non-standard filename) → NORMAL.
- Within each group the existing sort order (by date or name) is preserved.
- Implemented as a no-op when no red or orange items exist (avoids unnecessary rebuild).
- `ui/thumbnail_grid.py`:
  * Added `_group_problem_items()` — reads `_ROLE_INVALID` and `_ROLE_STD_NAME` from each item,
    splits into three lists, rebuilds the `QListWidget` from end-to-start (O(1) per removal).
  * `_on_worker_finished()`: calls `_group_problem_items()` after `_apply_sort()`.
  * Both `_ROLE_INVALID` and `_ROLE_STD_NAME` are set at skeleton time so the data is
    always available when `_on_worker_finished` runs.

### ✅ Fix thumbnail cache misses on NAS (session 72)
- NAS drives often report unreliable or reset mtime across remounts, causing a cache miss
  on every folder open even when files have not changed — regenerating 2542 thumbnails
  each time instead of loading from `_thumbcache/`.
- Changed cache key from `md5(path + mtime)` to `md5(path + file_size)`:
  * File size is stable across NAS remounts and sufficient to detect genuine file changes.
  * A modified file almost always changes size; a same-size swap is an acceptable edge-case
    (just generates a stale thumbnail instead of a crash).
- `ui/thumbnail_grid.py`:
  * `_thumb_cache_key(path_str, file_size: int)`: renamed parameter, updated format string
    (integer, no decimal).
  * `_ThumbnailWorker._get_thumb()`: reads `st_size` instead of `st_mtime`; passes it to
    `_thumb_cache_key()`.
  * mtime is still used for the `_ROLE_DATE` seed in `_start_load()` — unrelated to cache.

### ✅ Defer preview generation to explicit button click — photos + videos (session 72)
- Both date editors open with an empty preview table; preview only generates when the user
  clicks "Vista previa de cambios". This makes the dialog instant to open on large folders.
- **`ui/date_editor.py`**:
  * Removed `QTimer.singleShot(0, self._on_preview)` from `__init__()` — table starts empty.
  * Removed `QTimer` from imports (no longer needed).
  * `_on_exif_mode_changed()`: removed `if self._table.isVisible(): self._on_preview()`.
  * `_on_date_component_toggled()`: removed `if self._table.isVisible(): self._on_preview()`.
  * `_on_rename_toggled()`: removed `if self._table.isVisible(): self._on_preview()`.
  * `_on_rename_fmt_changed()`: removed `if self._table.isVisible(): self._on_preview()`.
  * `_preview_populated` flag still tracked correctly for Apply button state.
- **`ui/video_date_editor.py`**:
  * Removed `self._populate_table()` from `__init__()` — table starts empty.
  * Removed `_chk_year/month/day.toggled → lambda _: self._populate_table()` connections.
  * `_prefill_from_filename()`: removed `self._populate_table()` at end — no auto-refresh.
  * Spinbox enable/disable connections for year/month/day are unchanged.

### ✅ "Fecha nueva" column always visible in photo preview table (session 72)
- `ui/date_editor.py`: removed `setColumnHidden(_COL_NEW, True)` from `_build_ui()` and
  `_apply_exif_mode_state()` — the column was previously hidden in Conservar mode (the default),
  making the table appear to have only 3 columns (Archivo | Fecha actual | Nombre nuevo).
- Column is now always visible (4 cols: Archivo | Fecha actual | Fecha nueva | Nombre nuevo).
- In Conservar mode it shows the original date in gray (no change), matching the video editor behaviour.
- `_populate_preview_table()`: added gray-coloring when `new_str == current` (mirrors `video_date_editor.py`).
- Video editor already had Fecha nueva always visible — no changes needed there.

### ✅ Field selection checkboxes for photos + videos (session 72)
- Both date editors now show a "Campos a actualizar" groupbox between "Renombrar archivos" and "Vista previa de cambios".
- All checkboxes checked by default; user can uncheck to skip specific fields.
- **Photos** (`ui/date_editor.py`) — 5 checkboxes:
  * DateTimeOriginal, DateTimeDigitized, DateTime → control which EXIF tags are written
  * Timestamp → controls `os.utime()` (filesystem mtime/atime)
  * Fecha creación → controls `win32file.SetFileTime()` (Windows creation date)
  * `_field_checks` dict extended with the 2 new keys; `_on_apply()` splits them out before building `fields` list.
  * `_ApplyWorker` gains `sync_mtime: bool` and `sync_creation: bool` params, forwarded to `write_exif_date()`.
- **Videos** (`ui/video_date_editor.py`) — 5 checkboxes:
  * CreationTime / ModifyTime / FileModificationDate → any checked → ffmpeg container write happens
  * Timestamp / FileModificationDate → controls `os.utime()`
  * Fecha creación → controls `win32file.SetFileTime()`
  * `_ApplyWorker` gains `write_metadata`, `sync_mtime`, `sync_creation` params.
  * When `write_metadata=False`, the worker skips ffmpeg but still syncs filesystem timestamps if their boxes are checked.
- **Core** (`core/exif_handler.py`, `core/video_handler.py`):
  * `_sync_file_timestamps(path, dt, *, sync_mtime, sync_creation)` — now accepts kwargs; each operation is conditional.
  * `write_exif_date(...)` — gains `sync_mtime` and `sync_creation` kwargs, forwarded to `_sync_file_timestamps`.
  * `write_video_date(...)` — gains `sync_mtime` and `sync_creation` kwargs; both os.utime and win32file blocks are conditional.

### ✅ Fix file creation date not updating on Windows (session 72)
- `core/exif_handler.py`:
  * Added `import os` at top
  * Added `_sync_file_timestamps(path, dt)` helper — calls `os.utime()` for mtime/atime,
    then `win32file.SetFileTime()` for Windows creation time (pywin32, gracefully skipped if unavailable)
  * `write_exif_date()`: calls `_sync_file_timestamps(path, new_dt)` after `write_exif_timestamps()`
- `core/video_handler.py`:
  * `write_video_date()`: added pywin32 block after existing `os.utime()` call —
    same `win32file.SetFileTime()` pattern, wrapped in `try/except` so non-Windows degrades silently

### ✅ Fix QThread crash on radio button switch in date editors (session 72)
- `ui/date_editor.py`:
  * Added `_force_stop_preview_thread()` — sets `stop_requested=True`, calls `quit()+wait(2000)`,
    disconnects the `finished→_cleanup_preview_thread` signal, then `deleteLater()` both objects.
    Also closes any open `_preview_progress_dlg` and re-enables the dialog.
  * `_on_exif_mode_changed()`: calls `_force_stop_preview_thread()` before `_apply_exif_mode_state()` —
    prevents "QThread: Destroyed while still running" when the user flips Conservar↔Cambiar on large folders.
  * `_on_preview()` async branch: also calls `_force_stop_preview_thread()` at the start of the
    background-worker path so switching rename format / date components mid-preview is also safe.
- `ui/video_date_editor.py`:
  * Added `_stop_apply_thread()` — quit+wait(2000), deleteLater() on worker+thread, sets both to None.
  * Added `_on_mode_radio_toggled()` — calls `_stop_apply_thread()` then `_update_state()`.
  * Changed `self._radio_keep.toggled` connection from `_update_state` → `_on_mode_radio_toggled`
    (defensive: apply thread normally can't run while radios are accessible due to WindowModal dialog,
    but the guard prevents stale thread references in edge cases).

### ✅ Video grid toolbar unified with photo grid toolbar (session 71)
- ui/video_grid.py — VideoGrid:
  * Added QCheckBox "Solo sin fecha" (filter, default OFF) — identical to photo grid
  * Renamed sort combo first item "Fecha" → "Fecha EXIF" to match photo grid label
  * Added "Restaurar EXIF" button (hidden, shown when .video_backup.json exists)
  * Button order now identical: Nueva carpeta | Restaurar EXIF | Editar carpeta | Editar selección
  * Added restore_backup_requested = pyqtSignal(Path) on VideoGrid
  * Added _apply_filter() method — shows/hides items + updates count label
  * _apply_filter() called after each batch in _on_items_batch_ready() and in _on_worker_finished()
  * Added _on_restore_backup() → emits restore_backup_requested signal
  * Imports: added QCheckBox, has_video_backup, restore_video_backup, mb_info, mb_question
- ui/video_grid.py — VideoPanel:
  * _wire_signals(): connected grid.restore_backup_requested → _on_restore_video_backup
  * Added _on_restore_video_backup(folder_path): calls restore_video_backup(), shows result dialog, reloads grid
  * restore_video_backup() was already implemented in core/video_handler.py

### ✅ Selective date field checkboxes restored (session 71)
- Both date editors: _chk_year/_chk_month/_chk_day are now VISIBLE in the layout (were hidden)
- Layout: [☐ Año:] [spinbox] [☐ Mes:] [spinbox] [☐ Día:] [spinbox] — all horizontal
- Checkboxes have descriptive tooltips explaining preserve vs replace behaviour
- ui/date_editor.py:
  * QCheckBox("Año:") etc. replace the old QCheckBox() + QLabel("Año:") pair
  * Added _on_date_component_toggled() connections so preview/apply state refresh on checkbox change
  * All backend logic (_ApplyWorker, _PreviewWorker, _apply_exif_mode_state, _update_apply_state) unchanged — already used isChecked()
- ui/video_date_editor.py:
  * Same change: QCheckBox("Año:") etc. added to layout instead of QLabel
  * Added _populate_table() connections to checkbox toggled signals
  * spinbox enable/disable connections unchanged (still gate on radio_change.isChecked() AND chk.isChecked())

### ✅ Orange border for non-standard filenames (session 71)
- ui/thumbnail_grid.py:
  * Added _ROLE_STD_NAME (UserRole+3) and _STANDARD_NAME_RE regex
  * _make_skeleton_item() sets _ROLE_STD_NAME via regex match at item creation time
  * _ThumbnailDelegate.paint(): RED border (invalid date) takes priority; ORANGE border (255,165,0) when is_std is False
  * Legend updated: "🔴 = fecha inválida   🟠 = nombre no estándar"
- ui/video_grid.py:
  * Same pattern: _ROLE_STD_NAME (UserRole+5), _STANDARD_NAME_RE, _make_skeleton_item, _VideoDelegate.paint()
  * Legend updated with same text + tooltip on both grids
- Standard pattern: YYYY-MM-DD-HHhMMmSSs.ext (e.g. 2011-12-24-15h40m46s.jpg)
- Files named IMG_xxxx.jpg, VID_xxxx.mp4, etc. get orange border immediately at skeleton phase (no background worker needed)

### ✅ Startup optimized (session 70)
- Startup time reduced from ~15s to <5s (three fixes)
- ui/log_viewer.py: LogManager._load_from_disk() deferred — _logs_loaded flag + ensure_loaded() pattern
  ensure_loaded() called lazily in entries property, export_txt(), export_csv() (saves 0.9s / 45k strptime calls)
- main.py: apply_dark_theme(app) moved to after window.show() (saves 0.5-1s theme/style load)
- ui/folder_tree.py: removed self._tree.expandItem(root_item) from load_root() (saves 0.4s auto-expand)

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

✅ Both date editors layout unified (2026-04-15, session 69 final)
- video_date_editor.py changes:
  * Acción: QVBoxLayout → QHBoxLayout + ml.addStretch() — radios now side-by-side
  * Fecha: _chk_year/_chk_month/_chk_day removed from layout; QLabel("Año:") etc. used instead
    Hidden checkboxes kept alive for _update_state() enable/disable logic (toggled signals still work)
  * "Leer fecha del nombre de archivo" button moved OUT of Renombrar groupbox → standalone _fn_row at position 4
  * "Vista previa de cambios" button moved AFTER Renombrar groupbox (was before it)
- date_editor.py: already correct from session 69 — no changes needed
- FINAL IDENTICAL structure (both editors):
  1. Acción (horizontal HBox)
  2. Fecha nueva (labels + spinboxes, no visible checkboxes)
  3. Hora
  4. Leer fecha del nombre button (standalone row)
  5. Renombrar archivos groupbox (checkbox + 3 radios, NO button inside)
  6. Vista previa de cambios button
  7. Preview table
✅ Photo editor layout finalized (2026-04-15, session 69)
- Fecha section: removed checkboxes from layout; replaced with QLabel("Año:") etc.
  Hidden _chk_year/_chk_month/_chk_day kept alive — toggled signals still drive spinbox enabled/disabled state
  Only _chk_year.toggled → _spin_year.setEnabled connected (NOT _on_date_component_toggled, which is now dead)
  Title changed: "Fecha (☑ = modificar este componente)" → "Fecha nueva"
- Acción section: radios are HORIZONTAL (QHBoxLayout) — "Conservar fecha EXIF original" | "Cambiar fecha EXIF"
- Button positions (final, session 69):
  Position 4: "📋 Leer fecha del nombre" button (standalone HBox row, below Hora)
  Position 5: "Renombrar archivos" GROUPBOX (checkbox + 3 radios, no button inside)
  Position 6: "Vista previa de cambios" button (BELOW Renombrar groupbox)
  Position 7: Preview table
- Renombrar groupbox restructured:
  Removed _rename_format_widget (flat QGroupBox wrapper) — radios now directly in rl VBox
  _on_rename_toggled: setVisible(checked) → setEnabled(checked) on each radio individually
  Radio labels: "Solo fecha → 2011-12-24-15h40m46s.jpg" / "Fecha + nombre original → …_nombre.jpg" / "Conservar nombre original"
  No button inside groupbox (Leer fecha moved to standalone position 4)
- Preview table: 4 cols (Archivo | Fecha actual | Fecha nueva | Nombre nuevo)
- _fields_grp and _lbl_hint kept alive off-layout for backend compatibility
- FINAL layout order: Acción → Fecha nueva → Hora → Leer fecha btn → Renombrar → Vista previa btn → Preview table
✅ Photo editor — fully matched to video editor layout (2026-04-15, session 68 final)
- _rename_format_widget.setVisible(False) → True: 3 rename-format radios now always visible on open
- Removed self._table.setVisible(False): table always visible (no longer hidden until preview clicked)
- Added QTimer.singleShot(0, self._on_preview) at end of __init__: table auto-populated on open like video editor
- _fields_grp NOT added to layout (hidden, kept alive for _field_checks backend use)
- _lbl_hint NOT added to layout (hidden, kept alive for _update_apply_state() setText/setVisible calls)
- Added QTimer to PyQt6.QtCore import
- Final order identical to video editor: Acción → Fecha → Hora → Vista previa btn → Renombrar section (checkbox + 3 radios + Leer fecha btn) → Preview table
✅ Video format support expanded (2026-04-15, session 68)
- ALLOWED_EXTS in scan_video_folder() expanded: added .mpg, .mpeg, .ts, .m2ts, .mts
- Console logs confirmed .mpg files being skipped before fix
- MODULE-LEVEL VIDEO_EXTENSIONS also updated to match (for consistency with other code paths)
✅ Date editor layout unified (2026-04-14, session 67)
- Photos + Videos now have IDENTICAL element order per spec
- New order: Acción → Fecha → Hora → Campos EXIF → Vista previa btn → Renombrar section → Preview table
- "Vista previa de cambios" button moved ABOVE Renombrar section (was after it)
- "Leer fecha del nombre" button moved INSIDE Renombrar groupbox (was standalone)
- Renombrar section consolidated into QGroupBox("Renombrar archivos") in both editors
- Video editor: removed grp_prev wrapper — table is now standalone below Renombrar
✅ Video detection fixed — ALLOWED_EXTS inline set (2026-04-14, session 67)
- Changed to local inline ALLOWED_EXTS set (bypasses any module-level shadowing)
- Added [VIDEO INIT] print at module load time to confirm set type/contents
- Added [SCAN START] print at scan entry with full ALLOWED_EXTS
- Cleaner logging: [VIDEO ADDED] / [VIDEO SKIP] / [VIDEO SCAN] per file
✅ Duplicate detection audit (2026-04-14, session 67)
- VideoDuplicateScanWorker: [DUP VIDEO SCAN START] / [DUP VIDEO GROUPS] / [DUP VIDEO SCAN END] logs
- SimilarImageScanWorker: defensive check — warns if any video files appear in image scan list
✅ Video detection fixed — suffix check (2026-04-14, session 66)
- Root cause: f.suffix.lower() in VIDEO_EXTENSIONS check failing for mixed folders
- Added detailed per-file debug: suffix, suffix_lower, splitext values, both in_VIDEO_EXTENSIONS checks
- Prints VIDEO_EXTENSIONS at runtime so shadowing/redefinition is visible
- Defensive dual-check: Path.suffix AND os.path.splitext — video added if EITHER matches
- Videos now properly detected in mixed photo+video folders
✅ Video date editor UI — mirrored to photo editor (2026-04-14, session 66)
- Preview button text: "Actualizar vista previa" → "Vista previa de cambios" (matches photo editor)
- Rename section, format options (Solo fecha / Fecha + nombre / Conservar nombre), preview table
  with Nombre nuevo column — already present from session 64; confirmed full parity
✅ Video detection in mixed folders fixed (2026-04-14, session 65)
- Added detailed debug logging in scan_video_folder(): per-file is_file(), suffix, VIDEO_EXTENSIONS match
- Prints final list of matched video names; [VIDEO DEBUG] skip lines reveal filtering failures
- Fixed extension filtering for mixed photo+video folders
- VIDEO_EXTENSIONS verified: .mp4, .mov, .avi, .mkv, .m4v, .wmv, .3gp (all lowercase)
✅ Date editor defaults (2026-04-14)
- Photos: "Conservar EXIF original" + "Renombrar archivos" checked by default
- Videos: same defaults applied (Conservar + Renombrar both default-checked)
- Matches desired default behaviour
✅ Video detection in mixed folders (2026-04-14, session 64)
- Fixed: videos not recognized when in same folder as photos
- Root cause: scan_video_folder() used os.scandir+follow_symlinks=False — same Windows bug fixed for folder_tree.py in session 16
- Fix: replaced with path.glob("*") + f.is_file() (no symlink flag)
- Added debug logging: [VIDEO SCAN] Found N videos in {path}
✅ Video date editor already had full feature-parity with photo editor (2026-04-14, session 64)
- Audited: _chk_rename checkbox, _radio_date_only/plus/keep_name, preview table (5 cols), _ApplyWorker with rename — all present
- No code changes needed for Task 2

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
✅ Workers: 50-file checkpoints + zero-byte + stat-fail guards + time.sleep(0) yield
✅ Workers: error_details attribute stores full traceback for every error path
✅ Scan finish/error: explicit deleteLater() on worker+thread → no orphan threads
✅ Scan error dialog: QMessageBox with "Show Details" → full traceback visible
✅ CRASH FIX: AttributeError NoneType.setLabelText → no Cancel button + local ref guard
✅ PERF FIX: post-scan group selection lag → signal disconnect + gc.collect + 5/100ms batches
✅ TWO-PHASE PROGRESS: "Escaneando..." then "Cargando grupos..." modal dialogs
✅ PERF: group list items text-only (no PIL thumbnail) → 600+ groups load in <1s
✅ Workers: separate inner try/except for file-collection phase
✅ README.md updated: video support, duplicados features, v1.0 changelog
✅ TIMING FIX: _on_scan_progress adds repaint()+processEvents() → no freeze on window switch
✅ TIMING FIX: group list loads via simple inline loop with processEvents() per item → smooth, no QTimer batching
✅ CRASH FIX: all 3 scan workers write scan_error.log (exif_manager/) on exception → crash always leaves trace
✅ CRASH FIX: _on_scan_finished wrapped in try/except → main-thread crash now shows error + log instead of silent close
✅ CRASH FIX: replaced batched QTimer loading (_load_groups_batched/_load_next_group_batch) with simple inline loop + try/except per group → no crash on 1600+ files
✅ CRASH FIX: _add_group_item() now has internal try/except + empty-group guard → bad group data never fatal
✅ DEBUG: run_debug.cmd created → terminal stays open after crash (python main.py + pause)
✅ CRASH FIX: SimilarImageScanWorker split chained .open().convert("RGB") into separate steps with individual try/except → convert crash no longer silent; per-file [NNNN] START/SUCCESS/BAD logging with flush
✅ UX FIX: FolderTreePanel.set_scan_locked(bool) blocks folder clicks during scan → shows tooltip; DuplicatePanel.scan_busy_changed signal wired in main_window._wire_signals()
✅ CRASH FIX: SimilarImageScanWorker — with Image.open() as img → guarantees PIL buffer release; gc.collect() tightened to every 20 files (was 50) → no memory crash on 1600+ files
✅ CRASH FIX: DuplicateScanWorker — removed partial_results.emit() mid-scan; groups now rendered ONLY after finished.emit() → eliminates UI/memory conflict during scan
✅ Burst window logic separated: Exactos vs Similares (2026-04-14, session 63)
  - Exactos (MD5): burst window REMOVED — MD5 identical = always a duplicate, no timestamp check
  - Similares (fuzzy): burst window KEPT but changed 180s → 5s (same shot, different processing)
  - BURST_WINDOW = 5 is now fuzzy-only; comment clarifies it does NOT apply to MD5 scan
  - DuplicateScanWorker.run(): simple groups = [g for g in md5_map.values() if len(g) > 1]
  - SimilarImageScanWorker.run(): unchanged filter logic, now uses 5-second window
✅ Burst protection audit and fix (2026-04-14, session 62)
  - Removed stale TIMESTAMP_TOLERANCE = 4 constant from duplicate_finder.py (unused, misleading)
  - Removed stale TIMESTAMP_TOLERANCE = 4 constant from video_duplicate_finder.py
  - BURST_WINDOW = 180 is now the sole source of truth for burst detection
  - is_burst() hardened: any unreadable timestamp → return False (show as duplicate, safer)
  - is_burst() logic: if ANY timestamps[i] is None → False; max-min ≤ 180 → True (burst)
  - DuplicateScanWorker: burst groups excluded → not shown to user ✓
  - SimilarImageScanWorker: burst groups excluded → not shown to user ✓
  - Test case: identical files with same EXIF timestamp (0s diff) → correctly excluded as burst
✅ Similar duplicates: burst protection applied (2026-04-14, session 61)
  - SimilarImageScanWorker now filters burst groups after _phash_groups()
  - Similar hash + timestamp < 3 min = burst (excluded); > 3 min = duplicate (shown)
  - Reuses is_burst() + BURST_WINDOW = 180 from DuplicateScanWorker — no new code
  - Logged as [burst] with file names for debugging
✅ Smart duplicate detection with burst protection (2026-04-14)
  - Photos: MD5 match + timestamp > 3 minutes apart = duplicates (shown for dedup)
  - Photos: MD5 match + timestamp < 3 minutes apart = burst (keep all, excluded from UI)
  - Videos: MD5 match only = duplicates (no burst window)
  - Smart scoring: filename date matching EXIF DateTimeOriginal gives +1000 bonus
  - Automatically conserves file with matching name+EXIF (camera originals preferred)
  - BURST_WINDOW = 180 constant in duplicate_finder.py for easy tuning
  - Helpers: is_burst(), extract_date_from_filename(), dates_match() in duplicate_finder.py
✅ Duplicate detection expanded (2026-04-14)
  - TIMESTAMP_TOLERANCE = 4 constant (single source of truth) in duplicate_finder.py and video_duplicate_finder.py
  - Photos: _file_timestamp() reads EXIF DateTimeOriginal/Digitized/DateTime first; falls back to mtime
  - Videos: _file_timestamp() reads get_video_metadata()['creation_time'] first; falls back to mtime
  - ⏱️ icon shown on cards when 0 < diff <= 4 seconds (was 6, mtime-only)
  - Grouping still MD5-based; ⏱️ is annotation only (same file, copied at slightly different times)
✅ UX FIX: _on_scan_finished_inner now closes modal QProgressDialog BEFORE calling _batch_add_groups → modal was blocking QTimer.singleShot from firing; header label shows "Cargando N grupos…" progress instead; folder tree loading indicator was already working via folder_loading_started signal
✅ AUDIT (session 47): SimilarImageScanWorker progress bar already fully implemented — progress = pyqtSignal(int, int, str) defined, self.progress.emit(file_num, total, path.name) called every file, _begin_scan unconditionally connects worker.progress → _on_scan_progress for all worker types. No code changes needed.
✅ AUDIT (session 48): _on_scan_progress already had setMaximum/setValue/setLabelText/repaint()/processEvents(). One real gap fixed: dialog label now shows filename ("Escaneando… X/N\nfilename.jpg") matching what header label already showed.
✅ COMPLETE (session 49): Crash audit closed. Fuzzy scan stable on 1600+ files. All workers have per-file logging, GC, proper resource cleanup, and progress signals.
✅ UX (session 50): Scan progress dialog label/title now reflects active worker — "Escaneando exactos (MD5)…" / "Escaneando similares (pHash)…" / "Escaneando videos…". Three concurrent bars not possible — exact/fuzzy/video scans are mutually exclusive (one worker at a time).
✅ UX (session 51): Group-loading QProgressDialog changed setModal(True) → setModal(False) — app stays interactive during thread cleanup and group rendering; scan buttons + folder tree already locked by _scanning flag so re-entrancy is safe.
✅ SAFETY (session 52): Removed "Buscar duplicados (raíz)" button and all related code (_btn_scan_root, _on_scan_root_clicked, 5 touch-points) — prevents accidental multi-TB scans; self._root + set_root() retained (called from main_window).
✅ AUDIT (session 53): Confirmed zero remaining "(raíz)" references in duplicate_panel.py — all root scan variants fully removed in session 52. No code changes needed.
✅ UX (session 54): Added _load_next_batch() — 1 group per QTimer tick, live dialog updates. Session 46's "close dialog before batch" removed (reason was modal+QTimer conflict, fixed in session 51). _batch_add_groups unchanged (still used by _restore_groups_display).
✅ UX (session 55): THREE-PHASE loading: (1) text rows via _load_next_batch, (2) list icons via _load_next_thumbnail + non-modal "Cargando miniaturas…" dialog, (3) groups already interactable during both phases. "Conservar también" QCheckBox on delete cards — state stored in _force_keeps{group_idx→set(path_str)}, persists across group navigation, respected in _on_dedup_all. QCheckBox added to both _PhotoCard and _VideoCard.
✅ QUALITY (session 56): SimilarImageScanWorker.DEFAULT_THRESHOLD 8→3 — pHash Hamming distance ≤3 (~4.7% of 64 bits); eliminates false positives on different scenes while still catching same-image re-saves and resolution changes. No _group_similar_hashes() exists; the real entry point is _phash_groups().
✅ UX (session 57): Added cancel support to ALL 6 non-scan progress dialogs (duplicate_panel.py ×3, cleanup_dialog.py ×1, date_editor.py ×2). Group/thumbnail dialogs show actual "Cancelar" button; dedup/delete/preview/apply use setCancelButton(None) so X closes and fires canceled(). Workers get public stop_requested = False; cancel handlers set it directly and immediately close dialog + re-enable UI. _load_next_batch checks _groups_loading flag; _load_next_thumbnail guards on _thumb_progress_dlg is None. _on_*_progress() use local dialog ref guard. _scan_progress_dlg unchanged (no canceled() connection — re-entrancy crash risk, session 37).
✅ FIX (session 58): duplicate_panel.py — _btn_cancel now calls setEnabled(True) when scan starts and setEnabled(False) at all 4 exit points (cancel, normal finish, exception path, error path). Previously only setVisible was toggled so button could appear enabled/disabled inconsistently.
✅ Cancel button added to all 6 progress dialogs (2026-04-13)
  - duplicate_panel.py: group loading, thumbnail loading, dedup
  - cleanup_dialog.py: delete operation
  - date_editor.py: preview, apply
  - All workers: stop_requested flag + polling + early return on cancel
  - UI re-enables immediately when user cancels

---

## SESSION 58 — FINAL RELEASE PREP

- Renamed app: "EXIF Date Manager" → "Exif Manager & Duplicate Finder" (ui/main_window.py)
- Updated README.md with complete feature list and usage guide
- All progress dialogs fully cancellable
- Ready for GitHub public release

## SESSION 59 — DUPLICATE COPY-TIME ANNOTATION

- Duplicate cards now show ⏱️ when two identical files (same MD5) have slightly different timestamps
- TIMESTAMP_TOLERANCE = 4 seconds — single constant in duplicate_finder.py and video_duplicate_finder.py
- Photos: _file_timestamp() reads EXIF DateTimeOriginal/Digitized/DateTime → falls back to filesystem mtime
- Videos: _file_timestamp() reads get_video_metadata()['creation_time'] → falls back to filesystem mtime
- DuplicateScanWorker and VideoDuplicateScanWorker: compute group_ts_diffs (parallel list to emitted groups) after MD5 scan
- ui/duplicate_panel.py:
  - _TS_TOLERANCE_S = 4.0 module constant (mirrors TIMESTAMP_TOLERANCE)
  - _group_ts_diffs: list[float] state on DuplicatePanel, read from worker in _on_scan_finished_inner before _cleanup_scan_thread()
  - _show_group() passes ts_diff to card constructors
  - _PhotoCard and _VideoCard: ts_diff: float = 0.0 parameter; if 0 < ts_diff <= 4.0: shows ⏱️ "+Xs diferencia de copia" row (amber text, tooltip explaining timestamp diff)
- Grouping logic unchanged (still MD5-only); ⏱️ is annotation only, not a new detection criterion

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

## Session changes: Fix "Cancelar escaneo" crash (session 32)

### Root cause (three compounding issues)

1. **`_on_cancel_scan()` only set a flag** — called `worker.cancel()` then returned.
   No thread lifecycle management at all.  The worker eventually emitted `finished`,
   which triggered `_on_scan_finished()` → correct path but too late.

2. **`_on_scan_finished()` called `wait()` without a timeout** — if the worker was
   mid-computation (especially pHash comparison on a large folder), the main thread
   blocked indefinitely with no way to interrupt it.

3. **`SimilarImageScanWorker` didn't honour cancel during pHash comparison** —
   `_phash_groups()` was a pure function with no cancellation hook.  Once entered,
   it ran to completion regardless of `cancel()`.

4. **Race on app close / folder navigation** — if the user navigated away or quit while
   the thread was still running, the `QThread` object was destroyed with a live OS
   thread → crash -805306369 (`QThread: Destroyed while thread is still running`).

### Fixes

**`ui/duplicate_panel.py` — `_on_cancel_scan()` rewritten**
- Calls `worker.cancel()` (cooperative signal)
- Immediately resets `_scanning = False`, hides cancel button, re-enables scan buttons,
  sets header to `"⏹ Escaneo cancelado."`
- Calls `thread.quit()` + `thread.wait(5000)`; if still running after 5 s → `terminate()` + `wait(1000)`
- Calls `deleteLater()` on worker and thread; sets both to `None`

**`ui/duplicate_panel.py` — `_on_scan_finished()` early-return guard added**
```python
if not self._scanning:
    return   # cancel already handled by _on_cancel_scan
```
Prevents late-arriving `finished` signal from overwriting the cancel UI state or
re-running thread cleanup on already-deleted objects.

**`ui/duplicate_panel.py` — `_on_scan_finished()` `wait()` now has timeout**
```python
# Before (could block forever):
self._scan_thread.wait()
# After:
if not self._scan_thread.wait(5000):
    self._scan_thread.terminate()
    self._scan_thread.wait(1000)
```

**`ui/duplicate_panel.py` — `_on_scan_error()` early-return guard added**
Same `if not self._scanning: return` guard to handle the cancel race.

**`core/duplicate_finder.py` — `_phash_groups()` now cancellable**
- Added optional `is_cancelled: Callable[[], bool]` parameter
- Checked once per outer-loop iteration (`i` loop over N images)
- Returns `[]` immediately when cancelled
- `SimilarImageScanWorker` passes `lambda: self._cancelled`; emits `finished([])` if comparison was interrupted

### Cancel flow after fix

```
User clicks "Cancelar"
  → _on_cancel_scan():
      worker.cancel()          # sets _cancelled = True
      _scanning = False        # UI reverts immediately
      thread.quit()
      thread.wait(5000)        # cooperative: worker sees _cancelled, returns from run()
      # or terminate() if stuck
      worker.deleteLater()
      thread = None

  → (later) _on_scan_finished() fires via queued signal:
      if not _scanning: return  # ← early return — nothing else happens
```

## Session changes: Show folder path in Duplicados panel (session 31)

### `ui/duplicate_panel.py`

**New `_lbl_folder` label** — always-visible at the top of the left panel (above toggle buttons):
- Shows full path of the currently selected folder: `"D:\homelab\exif_manager\2010\Fotos"`
- Shows `"Sin carpeta seleccionada"` when no folder is active
- Updates on every `set_current_folder()` call (i.e. every folder navigation click)
- Full path also set as tooltip for truncated display
- Style: 9pt, muted grey (`#888888`), thin bottom border separator

**`set_current_folder()` updated** — sets `_lbl_folder` text + tooltip alongside the existing
`_current_folder` update.

**`_scanned_path: Optional[Path] = None`** — new instance variable; set in `_begin_scan()`
to remember which path the last scan covered (folder scan or root scan — whichever was used).

**`_begin_scan()` message updated**:
```
Before: "Escaneando carpeta (exactos)…"
After:  "Buscando exactos en:\nD:\homelab\exif_manager\2010\Fotos"
```

**`_update_header_label()` updated** — appends scanned folder to result summary:
```
3 grupos · 7 archivos · 12.4 MB duplicados
en: D:\homelab\exif_manager\2010\Fotos
```

**"No duplicates" message updated** — also appends scanned folder:
```
✓ No se encontraron duplicados.
en: D:\homelab\exif_manager\2010\Fotos
```

## Session changes: Perceptual hash similarity scan for resized duplicates (session 30)

### New feature: "Similares" scan mode in Duplicados tab

Adds a second scan mode alongside the existing MD5 exact-duplicate search.
Mode is per-session; switching does not clear existing scan results.

#### `core/duplicate_finder.py`

- Added module-level optional import: `imagehash` + `PIL.Image`; sets `IMAGEHASH_AVAILABLE: bool`
- Added `_phash_groups(hashes, threshold)` — pure function; takes list of (Path, pHash) tuples,
  runs O(N²) pairwise Hamming-distance comparison, groups similar images with path-compressed
  union-find, returns `list[list[Path]]` with groups ≥ 2 members
- Added `SimilarImageScanWorker(QObject)`:
  - Same signal contract as `DuplicateScanWorker`: `progress(int,int,str)`, `finished(list)`, `error(str)`
  - Phase 1: iterates images, opens each with PIL, computes `imagehash.phash()`, emits progress
  - Phase 2: calls `_phash_groups()`, emits "Comparando similares…" progress pulse, emits `finished`
  - Threshold default = 8 (out of 64 bits ≈ 12.5%): catches resizes, light re-saves, JPEG re-encodes
    without matching obviously different photos. Range: 3 (very strict) … 15 (permissive)
  - Graceful: if `imagehash` not installed → emits `error` with pip install instructions
  - Per-file try/except guards (same hardening as `DuplicateScanWorker`)
  - Respects `cancel()` between phases

#### `requirements.txt`

- Added `imagehash>=4.3.1` (already installed as 4.3.2)

#### `ui/duplicate_panel.py`

- Imported `SimilarImageScanWorker`, `IMAGEHASH_AVAILABLE` from `core.duplicate_finder`
- Added `self._scan_mode: str = "exact"` to `__init__`
- Added **scan-mode toggle row** in `_build_ui()` between type-toggle and header label:
  - `[Exactos]` (purple ON style) — MD5, current behaviour
  - `[Similares]` (purple ON style) — pHash; disabled + dimmed when imagehash not installed
- Added `_set_scan_mode(mode)` — updates `_scan_mode`, calls `_update_mode_style()`
- Added `_update_mode_style()` — applies ON/OFF/disabled stylesheet to both buttons
- `_begin_scan()`: when `effective_type != "video"` and `_scan_mode == "similar"`,
  uses `SimilarImageScanWorker(path)` instead of `DuplicateScanWorker(path)`
- Progress label now includes mode: `"Escaneando carpeta (similares)…"`

#### Algorithm notes

| Property | Value |
|---|---|
| Hash function | pHash (DCT-based perceptual hash, 64 bits) |
| Library | `imagehash.phash()` via Pillow |
| Comparison | Hamming distance (XOR popcount) — O(N²) pairs |
| Default threshold | 8 bits ≤ distance → similar |
| Grouping | Path-compressed union-find |
| Performance | ~200 ms for 100 images; ~5 s for 500 images (background thread) |
| False positives | Very low at threshold=8 for typical photo collections |

#### UI behaviour
- "Similares" button is **disabled** (greyed) when `imagehash` is not installed; tooltip
  shows pip install command
- Switching mode while a scan is running is not prevented — it only affects the next scan
- Both modes share the same group display / Conservar / Eliminar workflow
- Video duplicates always use MD5 regardless of scan mode (videos are too large for pHash)

## Session changes: Fix green marker missing after single-photo edit (session 29)

### Root cause (found by code trace — no app run needed)

`create_backup()` IS called and the `.exif_backup.json` IS written to disk for ALL
modes (single, folder, selection). The file is created correctly before the QThread
starts, so it exists on disk the moment `dlg.exec()` returns.

The bug was **not** in backup creation — it was in the tree refresh:

`main_window._open_date_editor_single()` and `_open_date_editor_from_filename()`
**never called `self._folder_tree.refresh_item()`** after the dialog closed.
The tree item never re-checked `has_backup()` so it stayed grey even though the
`.exif_backup.json` was already on disk.

Comparison:
| Handler | `refresh_item` called? |
|---|---|
| `_open_date_editor_folder` | ✅ line 345 |
| `_open_date_editor_selection` | ✅ line 369 |
| `_open_date_editor_single` | ❌ **missing** |
| `_open_date_editor_from_filename` | ❌ **missing** |

### Fixes

- **`ui/main_window.py`** — added `self._folder_tree.refresh_item(new_path.parent)` to
  both `_open_date_editor_single` and `_open_date_editor_from_filename`, immediately
  after `load_folder()` and before `showMessage()`.

- **`ui/date_editor.py`** — `_on_apply()` was calling `_get_target_paths()` twice:
  once at the top of the method (stored in `paths`) and again inside the backup block.
  For folder mode this ran `scan_folder()` twice. Fixed by reusing `paths` in the
  backup loop.

- **`core/backup_manager.py`** — added two debug `print()` lines to `create_backup()`:
  ```python
  print(f"[BACKUP] Writing {n} entries → {backup_path}")
  print(f"[BACKUP] File exists after write: {exists}  ({backup_path})")
  ```
  These confirm backup creation in the console. Remove once confirmed working.

## Session changes: Historial shows EXIF ANTERIOR + EXIF NUEVO (session 28)

### `core/backup_manager.py` — `append_historial` signature + format

**New signature** (breaking change — all callers updated):
```python
# Old:
append_historial(folder, original_name, new_name, original_exif, operation)
# New:
append_historial(folder, filename, operation, exif_before, exif_after=None, new_name=None)
```

**New output format** (multi-line, shows before AND after):
```
[2026-04-13 10:05:22]
Archivo: foto.jpg → nueva.jpg
Operación: fecha_editada
EXIF ANTERIOR:
  DateTimeDigitized: 2010:10:19 23:35:24
  DateTimeOriginal: 2010:10:19 23:35:24
EXIF NUEVO:
  DateTimeDigitized: 2026:04:13 08:58:56
  DateTimeOriginal: 2026:04:13 08:58:56
---
```
When `exif_after=None` (move / delete / rename-only), the "EXIF NUEVO" section is omitted.

### Callers updated — all 6 call sites

- **`ui/date_editor.py`** (`_ApplyWorker.run`):
  - `exif_before` = `original_fields` (read before write, already present)
  - `exif_after` built from `new_dt` + `self._fields`: `{field: new_dt.strftime("%Y:%m:%d %H:%M:%S") for field in self._fields}` — only when `write_exif=True` and write succeeded
  - `new_name` = `new_name_for_log` (unchanged)

- **`ui/video_date_editor.py`** (`_ApplyWorker.run`):
  - `exif_before` = `{"DateTimeOriginal": old_str}` (existing date as ISO string)
  - `exif_after` = `{"DateTimeOriginal": new_dt.strftime("%Y:%m:%d %H:%M:%S")}` — only in Cambiar mode
  - `new_name` = `applied_new_name` (unchanged)

- **`ui/folder_tree.py`**: `append_historial(src.parent, src.name, "movido", original_exif)`
- **`ui/photo_detail.py`**: `append_historial(path.parent, path.name, "renombrado", original_exif, new_name=new_name)`
- **`ui/thumbnail_grid.py`** (move): `append_historial(path.parent, path.name, "movido", original_exif)`
- **`ui/thumbnail_grid.py`** (delete): `append_historial(path.parent, path.name, "eliminado", original_exif)`

## Session changes: Unified growing backup — merge + compact historial (session 27)

### Goal
Both `.exif_backup.json` and `_historial_original.txt` should grow with every edit
(single file or batch) rather than being overwritten each time.

### `core/backup_manager.py`

**`create_backup(folder, files_data)` — new signature (breaking change)**
- Old: `create_backup(folder_path)` — scanned all images in folder, always overwrote the JSON
- New: `create_backup(folder, files_data)` — caller passes `{filename: exif_fields_dict}` for
  exactly the files being edited; function MERGES into existing backup (read → update → write)
- Merge rules:
  - If `.exif_backup.json` exists: load it first so entries for other files are preserved
  - Entries for the same filename are updated; all other entries remain untouched
  - `_meta` block gains `last_updated` timestamp on every write; `created_at` only set once
  - Corrupt/unreadable backup: falls back to fresh dict (data loss is self-healing on next write)
- Raises on I/O error so callers can show a "backup failed — continue?" dialog

**`append_historial` — format changed (signature unchanged)**
- Old: multi-line block with header, indented fields, `---` separator (~8 lines per entry)
- New: single compact pipe-separated line per entry:
  ```
  [2026-04-13 10:05:22] | foto.jpg → nueva.jpg | fecha_editada | DateTimeOriginal: 2020:01:01 12:00:00
  [2026-04-13 10:06:00] | foto.jpg | movido
  ```
- Format: `[timestamp] | Archivo (→ NombreNuevo) | Operación | Campo: Valor | …`
- All existing callers keep the same 5-arg signature — no caller changes required

### `ui/date_editor.py` — `_on_apply()` backup call updated

Changed from calling `create_backup(folder)` (which re-scanned everything) to:
```python
files_data = {}
for p in self._get_target_paths():   # single file, selection, or full folder scan
    files_data[p.name] = read_exif(p)["fields"]
n = create_backup(backup_folder, files_data)
```
- Single mode: backs up only the one photo being edited (fast, no folder scan)
- Selection mode: backs up only the selected photos
- Folder mode: backs up all images in folder (same scope as before)
- All modes: merges into existing `.exif_backup.json` without losing previous entries

### Net result
- Edit 1 photo → `.exif_backup.json` created with 1 entry; `_historial_original.txt` created with 1 line
- Edit 5 photos in same folder → backup gains 5 new entries; historial gains 5 new lines
- Folder shows GREEN marker (via `has_backup()` which checks `.exif_backup.json`)
- All history is cumulative — nothing is ever erased by a new edit

## Session changes: Fix backup JSON creation (session 26)

### Root cause

Three separate backup issues:

1. **`ui/date_editor.py`** — `create_backup()` was guarded with
   `if not keep_mode and self._mode in ("folder", "selection")` — single-file edits
   got no JSON backup at all (only the main_window undo stack, which is cleared on restart).

2. **`core/video_handler.py`** — `backup_video_metadata()` only saved `creation_time`
   (very sparse). Worse: all exceptions were silently swallowed with `except Exception: pass`,
   meaning disk full, permission errors, or JSON corruption caused silent data loss.

3. **`ui/video_date_editor.py`** — backup was done per-file INSIDE the worker thread,
   AFTER the thread started. If the first file's backup succeeded but the second failed
   and then the app was writing dates, there was a window where the file was changed but
   not backed up. Also no user feedback on backup failures.

### Fixes

- **`core/video_handler.py`** — `backup_video_metadata()` rewritten:
  - Saves all recoverable fields: datetime keys (`creation_time`, `date_modified`,
    `date_created`) converted to ISO strings; numeric/string keys (`duration_seconds`,
    `width`, `height`, `fps`, `codec_video`, `codec_audio`, `bitrate`, `size_bytes`,
    `make`, `model`, `rotation`, `format_name`) saved as-is.
  - Removed `try/except: pass` — now raises on I/O errors so callers can warn the user.

- **`ui/date_editor.py`** — backup condition extended to cover all modes:
  ```python
  # Before:
  if not keep_mode and self._mode in ("folder", "selection"):
      create_backup(self._target)
  # After:
  if not keep_mode:
      backup_folder = self._target if self._mode in ("folder", "selection") else self._target.parent
      create_backup(backup_folder)
  ```
  Single-file edits now create/update `.exif_backup.json` in the parent folder before writing.

- **`ui/video_date_editor.py`** — pre-apply backup added to `_on_apply()`:
  - Before the worker thread starts, backs up ALL files in a loop.
  - Any failures collected and shown in a `mb_question` dialog — user can abort or proceed.
  - Removed the per-file `backup_video_metadata()` call from `_ApplyWorker.run()` (redundant now).
  - Added `mb_question` to the `ui.styles` import.

## Session changes: Move buttons below badge in duplicate cards (session 25)

- **`ui/duplicate_panel.py`** — `_PhotoCard` and `_VideoCard` layout reordered:
  - **Before**: thumb → badge → metadata → path → stretch → buttons (buttons hidden at bottom)
  - **After**: thumb → badge → **buttons** → stretch → metadata → path
  - Buttons are now directly visible below the ★ Conservar / Duplicado label
  - `addStretch()` pushes metadata below buttons — always need to scroll to read metadata,
    but action buttons are always immediately reachable without scrolling

---

## Session changes: Fix freeze on large folders — batched group loading (session 34)

### Root cause

Scanning 1600+ photos/videos completed fine, but displaying results froze the UI.
The freeze happened in `_on_scan_finished` (and `_restore_groups_display`) where
**every** duplicate group's thumbnail was loaded synchronously via PIL on the main
thread — e.g. 300 groups × ~30 ms per PIL open = ~9 seconds of blocking.

### Fix summary

**`core/duplicate_finder.py` — `DuplicateScanWorker`**
- Added `partial_results = pyqtSignal(list)` signal
- Emits current groups at every 100-file checkpoint so groups appear during the scan

**`core/video_duplicate_finder.py` — `VideoDuplicateScanWorker`**
- Same `partial_results` signal + checkpoint emission

**`ui/duplicate_panel.py`**
- Added `_BATCH_SIZE = 20` constant
- `_begin_scan()`: connects `partial_results` → `_on_partial_results` (skipped for
  `SimilarImageScanWorker` which has no such signal)
- Added `_on_partial_results(groups)`: appends newly discovered groups to the list
  incrementally during scanning; first appearance selects row 0 + enables dedup
- `_on_scan_finished()`: no longer loops through all groups at once — resets display,
  initialises selections, then delegates to `_batch_add_groups(0)`
- Added `_batch_add_groups(start)`: adds `_BATCH_SIZE` items, updates header with
  "Cargando grupos… N/M", then schedules itself via `QTimer.singleShot(0, ...)`.
  Between ticks Qt processes events → UI stays responsive for any number of groups.
- `_restore_groups_display()`: same batching via `_batch_add_groups(0)` — fixes
  freeze when switching media-type tabs with a large cached result set.

### Behaviour

| Scenario | Before | After |
|---|---|---|
| Scan 1600 photos | Freeze on results display | Groups appear during scan; final sort shown in smooth 20-at-a-time batches |
| Scan 1600 videos | Same freeze | Same fix |
| Switch Fotos↔Videos tab with 300 cached groups | Freeze | Batched restore |
| Can interact while loading | No | Yes — can click groups as they appear |

---

## Session changes: Two-phase progress + text-only list items (session 39)

### Problem

After a scan of 1600+ images produced 600+ duplicate groups, the group list
population froze the UI because `_add_group_item` called `_load_pixmap()` (PIL
open + thumbnail) for every list item — 600+ synchronous disk reads before a
single group could be clicked.

### Fix (`ui/duplicate_panel.py`)

- **`_group_progress_dlg: Optional[QProgressDialog] = None`** added to state

- **`_on_scan_finished()` restructured** (9-step sequence, labelled):
  1. Close scan-phase dialog
  2. Normalise groups (Path objects) — needed for count before cleanup
  3. Show `_group_progress_dlg` ("Cargando grupos…", 0/N) immediately with
     `ApplicationModal + setMinimumDuration(0) + show() + processEvents()`
  4. `_cleanup_scan_thread()` — quit+wait runs while dialog is already visible
  5. Reset `_scanning`, buttons, UI
  6. Build `_selections` dict
  7. Cache to `_photo_groups` / `_video_groups` / `_all_groups`
  8. `gc.collect()` + `processEvents()`
  9. `_load_groups_batched()` — starts 10-item/50 ms population

- **`_add_group_item(idx)`** — **removed `_load_pixmap` call entirely**:
  - Previously: PIL open → thumbnail → `item.setIcon()` per row ← 10–50 ms each
  - Now: pure text `QListWidgetItem`, fixed height 46 px (no icon space)
  - Full-size thumbnail still loads on demand in `_show_group()` when clicked

- **`_load_groups_batched()`** replaces `_load_results_batched()`:
  - First 10 groups load immediately; updates dialog to value=10
  - Schedules `_load_next_group_batch` via `QTimer.singleShot(50)`

- **`_load_next_group_batch()`** replaces `_load_next_batch()`:
  - Loads 10 items, updates `_group_progress_dlg.setValue(end)` + `setLabelText`
  - Reschedules at 50 ms if more remain; on last batch closes dialog + header

- **`_batch_add_groups()`** kept unchanged for `_restore_groups_display` (tab switch)

### Result

| Scenario | Before | After |
|---|---|---|
| 633 groups list population | ~30 s (PIL per item) | <1 s (text only) |
| First clickable group | after full load | after first 50 ms batch |
| Group-click responsiveness | blocked during load | instant (50 ms gap) |
| Progress visible | nothing | "Cargando grupos… 50/633" |

---

## Session changes: Performance fix — post-scan group selection lag (session 38)

### Root causes identified

After a scan completes, clicking groups was laggy.  Three compounding problems:

1. **Thread not fully released**: `_on_scan_finished` did inline `quit+wait+deleteLater`
   but did NOT disconnect worker signals first.  Pending queued signals could still
   fire against a half-destroyed object during the subsequent `processEvents()`.

2. **Memory not freed before UI rebuild**: no `gc.collect()` after clearing the old
   group list.  Python's cyclic GC hadn't run, so freed `_PhotoCard` / `_VideoCard`
   objects (with PIL images in memory) competed with the new card construction.

3. **List loading blocked the event loop**: `_batch_add_groups` used `_BATCH_SIZE = 20`
   and `QTimer.singleShot(0)` — batches of 20 thumbnails loaded 20 images per tick
   with 0 ms breathing room.  User click events queued behind each 20-item batch,
   causing noticeable input lag before the full list was loaded.

### Files modified: `ui/duplicate_panel.py`

- **`import gc`** added at top of file
- **`self._batch_load_index: int = 0`** added to state-init

- **`_cleanup_scan_thread()` rewritten** (comprehensive, replaces the old stub):
  - Early-return if both `_scan_worker` and `_scan_thread` are already `None`
  - Disconnects `thread.started`, `worker.progress`, `worker.finished`, `worker.error`
    via `getattr(obj, sig).disconnect()` wrapped in individual try/except blocks
  - `isRunning()` guard before `quit()+wait(5000)` → `terminate()+wait(1000)`
  - `deleteLater()` + `= None` for both worker and thread
  - Safe for both call sites: `_on_scan_finished` (thread running) and
    `thread.finished` slot (thread already stopped; `isRunning()` == False)

- **`_on_scan_finished()` restructured**:
  - Delegates to `self._cleanup_scan_thread()` instead of inlining quit/wait/delete
  - `gc.collect()` + `QApplication.processEvents()` after resetting state and
    before starting list population — flushes freed objects and pending events
  - Calls new `self._load_results_batched()` instead of `_batch_add_groups(0)`

- **`_add_group_item(idx)`** — new helper: builds and appends one `QListWidgetItem`
  (extracted from `_batch_add_groups` so new loaders reuse the same logic)

- **`_load_results_batched()`** — new method for post-scan list population:
  - Loads first **5** groups immediately → user can click within milliseconds
  - Schedules `_load_next_batch` via `QTimer.singleShot(100)` for the rest

- **`_load_next_batch()`** — new method:
  - Loads next **5** groups, updates header, reschedules itself at 100 ms intervals
  - 100 ms gaps mean the event loop processes ~6 frames between batches → clicks
    are handled immediately between any two batches

- **`_batch_add_groups()`** kept unchanged — still used by `_restore_groups_display`
  (tab-switch restores, where fast bulk loading is acceptable)

---

## Session changes: Fix AttributeError NoneType.setLabelText in progress dialog (session 37)

### Root cause

`QProgressDialog.setValue()` calls `QApplication::processEvents()` internally.
While those events are processed, the `canceled()` signal fires (e.g. user clicks Cancel),
`_on_cancel_scan` runs and sets `self._scan_progress_dlg = None` — **after** the
`is not None` guard in `_on_scan_progress` has already passed, but **before** `setLabelText()`.
Result: `AttributeError: 'NoneType' object has no attribute 'setLabelText'`.

### Fix (`ui/duplicate_panel.py`)

- **`_begin_scan()`**:
  - `QProgressDialog("Escaneando…", None, 0, 0, self)` — `None` = **no Cancel button**
    Without a Cancel button, `canceled()` is never emitted by the dialog itself,
    so `_on_cancel_scan` can't be triggered re-entrantly during `setValue()`.
  - `setModal(True)` (ApplicationModal) instead of `WindowModal` — stricter blocking.
  - Removed `canceled.connect(self._on_cancel_scan)` — no Cancel button to connect.
  - UI's `_btn_cancel` still handles cancellation, unchanged.

- **`_on_scan_progress()`**:
  - `dlg = self._scan_progress_dlg` — captures **local reference** before any Qt call.
  - `if dlg is not None:` checks the local variable — even if re-entrant code later
    nulls `self._scan_progress_dlg`, `dlg` keeps the valid reference,
    so `setLabelText()` can never crash.
  - Simplified to `dlg.setMaximum(total)` (no redundant `maximum() != total` check).

---

## Session changes: Thread cleanup + error traceback dialog (session 36)

### Files modified

- **`core/duplicate_finder.py`** — both `DuplicateScanWorker` and `SimilarImageScanWorker`:
  - Added `self.error_details: str = ""` in `__init__`
  - Every `error.emit()` call now preceded by `self.error_details = traceback.format_exc()`
    (covers collection phase error, outer `except Exception`, and similar-scan collect phase)

- **`core/video_duplicate_finder.py`** — `VideoDuplicateScanWorker`:
  - Same three-spot pattern: `error_details` init + `format_exc()` before every `error.emit()`

- **`ui/duplicate_panel.py`**:
  - **`_on_scan_finished()`**: added explicit `deleteLater()` on `_scan_worker` + `_scan_thread`
    immediately after `quit()+wait()`, then sets both to `None`.
    `_cleanup_scan_thread` (connected to `thread.finished`) finds `None` and is a safe no-op.
  - **`_on_scan_error()`**:
    - Added `details = getattr(self._scan_worker, "error_details", "") or msg`
      **before** `deleteLater()` so the traceback is captured before the object is freed
    - Added same explicit `deleteLater()` + `= None` cleanup as `_on_scan_finished`
    - After closing the progress dialog, shows `QMessageBox.Critical` with:
      - `.setText("Se produjo un error inesperado durante el escaneo.")`
      - `.setInformativeText(msg)` — one-line summary
      - `.setDetailedText(details)` — full Python traceback behind "Show Details" button

---

## Session changes: Smooth scan progress, loading indicator, double-click expand (session 35)

### Files modified

- **`core/duplicate_finder.py`**:
  - Added `import time`
  - Checkpoint interval: 100 → **50 files**
  - Added `time.sleep(0)` at each checkpoint → releases GIL, lets Qt main thread repaint
  - Applied to both `DuplicateScanWorker` and `SimilarImageScanWorker`

- **`core/video_duplicate_finder.py`**:
  - Same changes: `import time`, 100 → 50-file checkpoint, `time.sleep(0)` yield

- **`ui/duplicate_panel.py`**:
  - Added `_scan_progress_dlg: Optional[QProgressDialog] = None` to state
  - `_begin_scan()`: creates modal `QProgressDialog` **before** `thread.start()`:
    - `setWindowModality(WindowModal)` — blocks clicks on main window during scan
    - `setMinimumDuration(0)` — appears immediately (no delay)
    - `canceled` signal wired to `_on_cancel_scan` (Cancel button cancels the worker)
    - `show()` + `processEvents()` then `thread.start()`
  - `_on_scan_progress()`: feeds dialog — sets range (once), updates value + label `"Escaneando… X/total"`
  - `_on_scan_finished()`, `_on_scan_error()`, `_on_cancel_scan()`: all close + `None` the dialog

- **`ui/folder_tree.py`**:
  - Added `folder_loading_started = pyqtSignal(Path)` signal
  - `_on_item_clicked()`: emits `folder_loading_started` **before** `folder_selected`
  - Note: double-click expand/collapse was already implemented (session 33) — no change

- **`ui/main_window.py`**:
  - Added `QTimer` to PyQt6 imports
  - `_wire_signals()`: connects `folder_tree.folder_loading_started → _on_folder_loading_started`
  - New slot `_on_folder_loading_started(path)`: shows `"Cargando carpeta: {name}…"` in status bar + `WaitCursor`
  - `_on_folder_selected()`: emits `folder_changed`, then `QTimer.singleShot(0, …)` restores `ArrowCursor` and shows path in status bar

---

## Session changes: Remove file counters, double-click expand/collapse (session 33)

### `ui/folder_tree.py`

**FIX 1 — Remove file counters from tree labels**
- Removed `_count_photos()` and `_count_videos()` methods entirely (now unused)
- `_update_item_label()` simplified: was `f"{path.name}  ({photos}) V({videos})"` — now just `item.setText(0, path.name)`
- `_make_item()` already used `path.name` only (no change needed — counts were added lazily on click)
- Net effect: tree shows clean folder names with no `(X) V(Y)` suffix clutter

**FIX 2 — Double-click folder = expand/collapse toggle**
- `_build_ui()`: added `self._tree.setExpandsOnDoubleClick(False)` to disable Qt's default expand-on-double-click (without this, connecting `itemDoubleClicked` to a toggle fires expand + collapse = no net effect)
- `_build_ui()`: added `self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)`
- New slot `_on_item_double_clicked(item, column)`:
  - Ignores placeholder items
  - If expanded → `collapseItem(item)`
  - If collapsed → `expandItem(item)` (this triggers `_on_item_expanded` for lazy loading)
