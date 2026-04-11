"""Main application window — layout, menu bar, signal wiring, undo."""
from collections import deque
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QSettings, QStandardPaths
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QSplitter, QVBoxLayout,
    QMenuBar, QStatusBar, QMessageBox, QProgressDialog,
    QApplication,
)
from PyQt6.QtGui import QKeySequence, QAction

from PyQt6.QtWidgets import QTabWidget
from ui.log_viewer import LogManager, LogViewerDialog
from ui.folder_tree import FolderTreePanel
from ui.thumbnail_grid import ThumbnailGrid
from ui.photo_detail import PhotoDetailPanel
from ui.duplicate_panel import DuplicatePanel


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EXIF Date Manager")
        self.resize(1400, 800)

        # Shared state
        self._current_root: Optional[Path] = None
        self._current_folder: Optional[Path] = None
        self._current_photo: Optional[Path] = None
        self._undo_stack: deque = deque(maxlen=1)  # (path, original_fields)

        # Log manager
        data_path = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation
        )
        self._log = LogManager(Path(data_path))

        # Build UI
        self._build_ui()
        self._build_menus()
        self._wire_signals()
        self._restore_settings()
        self.showMaximized()

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(4, 4, 4, 4)

        # Main horizontal splitter: [folder tree | content area]
        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left panel: folder tree (log_manager needed for drag-drop move logging)
        self._folder_tree = FolderTreePanel(self, log_manager=self._log)
        self._main_splitter.addWidget(self._folder_tree)

        # Right side: another horizontal splitter [thumbnails | detail]
        self._content_splitter = QSplitter(Qt.Orientation.Horizontal)

        self._photo_detail = PhotoDetailPanel(self._log, self)

        # Tab widget: thumbnail grid + duplicate panel
        self._center_tabs = QTabWidget()
        self._center_tabs.setDocumentMode(True)

        self._thumbnail_grid = ThumbnailGrid(self._log, self)
        self._duplicate_panel = DuplicatePanel(self._log, self)

        self._center_tabs.addTab(self._thumbnail_grid, "📷  Fotos")
        self._center_tabs.addTab(self._duplicate_panel, "🔍  Duplicados")

        self._content_splitter.addWidget(self._center_tabs)
        self._content_splitter.addWidget(self._photo_detail)
        # Center tabs stretch; detail panel keeps its width when window resizes
        self._content_splitter.setStretchFactor(0, 1)
        self._content_splitter.setStretchFactor(1, 0)
        self._content_splitter.setHandleWidth(6)
        self._content_splitter.setChildrenCollapsible(False)

        self._main_splitter.addWidget(self._content_splitter)
        self._main_splitter.setHandleWidth(6)
        self._main_splitter.setChildrenCollapsible(False)

        # Minimum widths to prevent panels from being squeezed out
        self._folder_tree.setMinimumWidth(180)
        self._center_tabs.setMinimumWidth(400)
        self._photo_detail.setMinimumWidth(280)

        # Screen-proportional initial sizes
        screen = QApplication.primaryScreen().availableGeometry()
        total_w = screen.width()
        tree_w, detail_w = 220, 350
        grid_w = max(400, total_w - tree_w - detail_w)
        self._main_splitter.setSizes([tree_w, grid_w + detail_w])
        self._content_splitter.setSizes([grid_w, detail_w])

        root_layout.addWidget(self._main_splitter)

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Listo")

    def _build_menus(self) -> None:
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("Archivo")
        action_open = QAction("Abrir carpeta raíz…", self)
        action_open.setShortcut(QKeySequence("Ctrl+O"))
        action_open.setToolTip("Abre un selector de carpetas para establecer la carpeta raíz de la colección.")
        action_open.triggered.connect(self._folder_tree.open_root_dialog)
        file_menu.addAction(action_open)
        file_menu.addSeparator()
        action_quit = QAction("Salir", self)
        action_quit.setShortcut(QKeySequence("Ctrl+Q"))
        action_quit.setToolTip("Cierra la aplicación.")
        action_quit.triggered.connect(self.close)
        file_menu.addAction(action_quit)

        # View menu
        view_menu = menubar.addMenu("Ver")
        action_log = QAction("Registro de cambios…", self)
        action_log.setToolTip(
            "Muestra el historial completo de cambios realizados,\n"
            "con filtros por fecha y tipo de acción. Permite exportar a .txt o .csv."
        )
        action_log.triggered.connect(self._show_log_viewer)
        view_menu.addAction(action_log)

        # Tools menu
        tools_menu = menubar.addMenu("Herramientas")
        action_dupes_folder = QAction("Buscar duplicados en carpeta actual…", self)
        action_dupes_folder.setToolTip(
            "Escanea la carpeta actualmente abierta en busca de fotos idénticas."
        )
        action_dupes_folder.triggered.connect(self._show_duplicate_folder)
        tools_menu.addAction(action_dupes_folder)

        action_dupes_root = QAction("Buscar duplicados en carpeta raíz…", self)
        action_dupes_root.setToolTip(
            "Escanea toda la colección cargada en busca de fotos idénticas (MD5). "
            "Puede tardar varios minutos."
        )
        action_dupes_root.triggered.connect(self._show_duplicate_root)
        tools_menu.addAction(action_dupes_root)

        action_restore = QAction("Restaurar EXIF de carpeta actual…", self)
        action_restore.setToolTip(
            "Revierte los cambios de fecha EXIF de la carpeta actualmente\n"
            "seleccionada usando el backup automático creado antes de la última edición."
        )
        action_restore.triggered.connect(self._restore_current_folder_backup)
        tools_menu.addAction(action_restore)

        tools_menu.addSeparator()

        action_cleanup = QAction("Limpiar carpetas temporales…", self)
        action_cleanup.setToolTip(
            "Escanea toda la colección y elimina carpetas y archivos temporales:\n"
            "_thumbcache, _eliminados, _duplicados_eliminados,\n"
            "_historial_original.txt, .exif_backup.json"
        )
        action_cleanup.triggered.connect(self._show_cleanup_dialog)
        tools_menu.addAction(action_cleanup)

    def _wire_signals(self) -> None:
        # Folder selection → load thumbnails
        self._folder_tree.folder_selected.connect(self._on_folder_selected)

        # Photo selection → show detail
        self._thumbnail_grid.photo_selected.connect(self._on_photo_selected)

        # Edit folder date
        self._thumbnail_grid.edit_folder_date.connect(self._open_date_editor_folder)

        # Edit date for explicit photo selection (2+ photos)
        self._thumbnail_grid.edit_selection_date.connect(self._open_date_editor_selection)

        # Open date editor pre-filled from filename (single photo, right-click)
        self._thumbnail_grid.read_filename_date_requested.connect(
            self._open_date_editor_from_filename
        )

        # Restore backup from grid button
        self._thumbnail_grid.restore_backup_requested.connect(self._restore_folder_backup)

        # Edit single photo date from detail panel
        self._photo_detail.edit_photo_date.connect(self._open_date_editor_single)

        # Rename from detail panel
        self._photo_detail.photo_renamed.connect(self._on_photo_renamed)

        # Photos deleted (moved to trash) from grid
        self._thumbnail_grid.photos_deleted.connect(self._on_photos_deleted)

        # New folder created from grid's bottom bar
        self._thumbnail_grid.folder_created.connect(self._on_folder_created)

        # Files moved via drag & drop from grid → folder tree
        self._folder_tree.files_moved.connect(self._on_files_moved)

        # Multi-selection in grid → update detail panel with summary
        self._thumbnail_grid.multi_selection.connect(self._on_multi_selection)

        # Switch to duplicates tab when a scan starts
        self._duplicate_panel.scan_started.connect(
            lambda: self._center_tabs.setCurrentIndex(1)
        )

    # ── Slots ──────────────────────────────────────────────────────────────

    def _on_folder_selected(self, path: Path) -> None:
        self._current_folder = path
        self._thumbnail_grid.load_folder(path)
        self._status_bar.showMessage(str(path))
        self._duplicate_panel.set_current_folder(path)

    def _on_photo_selected(self, path: Path) -> None:
        self._current_photo = path
        self._photo_detail.load_photo(path)

    def _on_multi_selection(self, pairs: list) -> None:
        """Show multi-selection summary in the detail panel."""
        self._current_photo = None
        self._photo_detail.show_selection(pairs)

    def _open_date_editor_folder(self, folder_path: Path) -> None:
        from ui.date_editor import DateEditorDialog
        dlg = DateEditorDialog("folder", folder_path, self._log, self)
        if dlg.exec():
            # Resolve post-rename path of the currently displayed photo (if any)
            new_photo: Optional[Path] = None
            if self._current_photo and self._current_photo.parent == folder_path:
                new_photo = dlg.applied_renames.get(self._current_photo, self._current_photo)

            # Request re-selection, then reload the grid (sorts by new dates too)
            if new_photo:
                self._thumbnail_grid.select_after_load(new_photo)
            self._thumbnail_grid.load_folder(folder_path)

            # Update folder tree backup indicator
            self._folder_tree.refresh_item(folder_path)

            # Reload detail panel so it shows updated EXIF dates / new filename
            if new_photo:
                self._current_photo = new_photo
                self._photo_detail.load_photo(new_photo)

            self._status_bar.showMessage(f"Fechas actualizadas en {folder_path.name}")

    def _open_date_editor_selection(self, paths: list) -> None:
        if not paths:
            return
        from ui.date_editor import DateEditorDialog
        folder = paths[0].parent
        dlg = DateEditorDialog("selection", folder, self._log, self, paths=paths)
        if dlg.exec():
            # Resolve post-rename path of the currently displayed photo (if any)
            new_photo: Optional[Path] = None
            if self._current_photo and self._current_photo in paths:
                new_photo = dlg.applied_renames.get(self._current_photo, self._current_photo)

            if new_photo:
                self._thumbnail_grid.select_after_load(new_photo)
            self._thumbnail_grid.load_folder(folder)
            self._folder_tree.refresh_item(folder)

            if new_photo:
                self._current_photo = new_photo
                self._photo_detail.load_photo(new_photo)

            n = len(paths)
            self._status_bar.showMessage(
                f"Cambios aplicados a {n} foto{'s' if n != 1 else ''}"
            )

    def _open_date_editor_single(self, photo_path: Path) -> None:
        from ui.date_editor import DateEditorDialog
        # Save undo state before editing
        from core.exif_handler import read_exif
        existing = read_exif(photo_path)
        self._undo_stack.append((photo_path, dict(existing["fields"])))

        dlg = DateEditorDialog("single", photo_path, self._log, self)
        if dlg.exec():
            new_path = dlg.applied_renames.get(photo_path, photo_path)
            self._current_photo = new_path
            self._thumbnail_grid.select_after_load(new_path)
            self._photo_detail.load_photo(new_path)
            self._thumbnail_grid.load_folder(new_path.parent)
            self._status_bar.showMessage(f"Fecha actualizada: {new_path.name}")

    def _open_date_editor_from_filename(self, photo_path: Path) -> None:
        """Open the date editor for a single photo, pre-filled from the filename date."""
        from ui.date_editor import DateEditorDialog
        from core.exif_handler import read_exif
        existing = read_exif(photo_path)
        self._undo_stack.append((photo_path, dict(existing["fields"])))

        dlg = DateEditorDialog(
            "single", photo_path, self._log, self,
            prefill_from_filename=True,
        )
        if dlg.exec():
            new_path = dlg.applied_renames.get(photo_path, photo_path)
            self._current_photo = new_path
            self._thumbnail_grid.select_after_load(new_path)
            self._photo_detail.load_photo(new_path)
            self._thumbnail_grid.load_folder(new_path.parent)
            self._status_bar.showMessage(f"Fecha actualizada: {new_path.name}")

    def _on_photo_renamed(self, old_path: Path, new_path: Path) -> None:
        self._current_photo = new_path
        self._thumbnail_grid.select_after_load(new_path)
        self._thumbnail_grid.load_folder(new_path.parent)
        self._status_bar.showMessage(f"Renombrado: {old_path.name}  →  {new_path.name}")

    def _on_photos_deleted(self, moved: list) -> None:
        n = len(moved)
        self._status_bar.showMessage(
            f"{n} foto{'s' if n != 1 else ''} movida{'s' if n != 1 else ''} a _eliminados"
        )
        # Clear detail panel if the displayed photo was among those deleted
        if self._current_photo and self._current_photo in moved:
            self._current_photo = None
            self._photo_detail.clear()

    def _on_folder_created(self, new_folder: Path) -> None:
        """Expand the tree to show the new folder and select it."""
        self._folder_tree.reveal_folder(new_folder)
        self._status_bar.showMessage(f"Carpeta '{new_folder.name}' creada")

    def _on_files_moved(self, src_folder: Path, moved: list) -> None:
        """After drag-drop move: reload source grid, clear stale detail panel."""
        n = len(moved)
        dst_folder = moved[0].parent if moved else None
        self._status_bar.showMessage(
            f"{n} foto{'s' if n != 1 else ''} movida{'s' if n != 1 else ''}"
            + (f" a {dst_folder.name}" if dst_folder else "")
        )
        # Reload source folder thumbnails if currently displayed
        if self._current_folder == src_folder:
            self._thumbnail_grid.load_folder(src_folder)
        # Clear detail panel if the viewed photo was among those moved
        moved_paths = set(url.parent / url.name for url in moved)   # dst paths
        # The original paths are now gone; compare by original folder + names
        src_names = {url.name for url in moved}
        if (self._current_photo
                and self._current_photo.parent == src_folder
                and self._current_photo.name in src_names):
            self._current_photo = None
            self._photo_detail.clear()

    def _restore_folder_backup(self, folder_path: Path) -> None:
        self._restore_backup_for(folder_path)

    def _restore_current_folder_backup(self) -> None:
        if self._current_folder:
            self._restore_backup_for(self._current_folder)
        else:
            QMessageBox.information(self, "Info", "No hay carpeta seleccionada.")

    def _restore_backup_for(self, folder_path: Path) -> None:
        from core.backup_manager import has_backup, restore_backup
        if not has_backup(folder_path):
            QMessageBox.information(
                self, "Sin backup",
                f"No existe archivo de backup en:\n{folder_path}"
            )
            return

        reply = QMessageBox.question(
            self, "Restaurar backup",
            f"¿Restaurar EXIF original de todas las fotos en:\n{folder_path.name}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        result = restore_backup(folder_path)
        self._log.log(str(folder_path), "", "restore_backup",
                      "", f"ok={result['ok']} errores={result['failed']}")

        if result["errors"]:
            QMessageBox.warning(
                self, "Restauración con errores",
                f"Restaurados: {result['ok']}\nErrores: {result['failed']}\n\n" +
                "\n".join(result["errors"][:10])
            )
        else:
            QMessageBox.information(
                self, "Backup restaurado",
                f"Se restauraron {result['ok']} archivos."
            )

        self._thumbnail_grid.load_folder(folder_path)
        if self._current_photo and self._current_photo.parent == folder_path:
            self._photo_detail.load_photo(self._current_photo)

    def _show_log_viewer(self) -> None:
        dlg = LogViewerDialog(self._log, self)
        dlg.exec()

    def _show_duplicate_folder(self) -> None:
        if not self._current_folder:
            QMessageBox.information(
                self, "Sin carpeta",
                "Abrí una carpeta primero para buscar duplicados en ella."
            )
            return
        self._duplicate_panel.start_scan(self._current_folder)

    def _show_duplicate_root(self) -> None:
        if not self._current_root:
            QMessageBox.information(
                self, "Sin carpeta raíz",
                "Abrí una carpeta raíz antes de buscar duplicados."
            )
            return
        self._duplicate_panel.start_scan(self._current_root)

    def _show_cleanup_dialog(self) -> None:
        if not self._current_root:
            QMessageBox.information(
                self, "Sin carpeta raíz",
                "Abre una carpeta raíz antes de limpiar carpetas temporales."
            )
            return
        from ui.cleanup_dialog import CleanupDialog
        dlg = CleanupDialog(
            self._current_root, self._log, self,
            current_folder=self._current_folder,
        )
        dlg.exec()
        if dlg.cleaned:
            # Deleted folders may have been visible in the tree — reload it
            self._folder_tree.load_root(self._current_root)
            # If the current folder was inside a deleted subtree, clear the grid
            if self._current_folder and not self._current_folder.exists():
                self._current_folder = None
                self._thumbnail_grid.load_folder(self._current_root)
                self._photo_detail.clear()
            self._status_bar.showMessage("Limpieza completada — árbol de carpetas actualizado")

    # ── Keyboard shortcuts ─────────────────────────────────────────────────

    def keyPressEvent(self, event) -> None:
        # Ctrl+Z: undo last individual edit
        if event.key() == Qt.Key.Key_Z and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._undo_last_edit()
            return
        super().keyPressEvent(event)

    def _undo_last_edit(self) -> None:
        if not self._undo_stack:
            self._status_bar.showMessage("Nada que deshacer")
            return
        path, original_fields = self._undo_stack.pop()
        if not original_fields:
            self._status_bar.showMessage("Sin datos de EXIF previos para deshacer")
            return
        try:
            from core.exif_handler import write_exif_timestamps
            write_exif_timestamps(path, original_fields)
            self._log.log(str(path.parent), path.name, "undo", "", "restaurado")
            if self._current_photo == path:
                self._photo_detail.load_photo(path)
            self._thumbnail_grid.refresh_item(path)
            self._status_bar.showMessage(f"Deshecho: {path.name}")
        except Exception as e:
            QMessageBox.warning(self, "Error al deshacer", str(e))

    # ── Settings ───────────────────────────────────────────────────────────

    def _restore_settings(self) -> None:
        s = QSettings()
        geom = s.value("window/geometry")
        if geom:
            self.restoreGeometry(geom)
        splitter_main = s.value("splitter/main")
        if splitter_main:
            self._main_splitter.restoreState(splitter_main)
        splitter_content = s.value("splitter/content")
        if splitter_content:
            self._content_splitter.restoreState(splitter_content)
        last_root = s.value("last_root", "")
        if last_root and Path(last_root).exists():
            self._current_root = Path(last_root)
            self._folder_tree.load_root(self._current_root)
            self._duplicate_panel.set_root(self._current_root)

    def closeEvent(self, event) -> None:
        s = QSettings()
        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("splitter/main", self._main_splitter.saveState())
        s.setValue("splitter/content", self._content_splitter.saveState())
        if self._current_root:
            s.setValue("last_root", str(self._current_root))
        super().closeEvent(event)

    # ── Called by FolderTreePanel when root changes ────────────────────────

    def set_root(self, path: Path) -> None:
        self._current_root = path
        QSettings().setValue("last_root", str(path))
        self._duplicate_panel.set_root(path)
