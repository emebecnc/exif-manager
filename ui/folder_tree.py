"""Left panel: navigable folder tree with lazy loading and backup indicators."""
import shutil
from collections import deque
from pathlib import Path
from typing import Optional, List

from PyQt6.QtCore import Qt, pyqtSignal, QUrl
from PyQt6.QtGui import QColor, QBrush
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QTreeWidget, QTreeWidgetItem, QFileDialog, QLabel,
    QApplication, QMessageBox,
)

from core.backup_manager import has_backup, append_historial
from core.exif_handler import read_exif
from core.file_scanner import count_images, list_subdirs, root_is_available, unique_dest
from ui.styles import mb_warning

_PLACEHOLDER = "__loading__"


class _DropTree(QTreeWidget):
    """QTreeWidget that accepts file-URL drops and moves files to the target folder."""

    # Emitted after a successful drop: (source_folder, list_of_new_destination_paths)
    files_moved = pyqtSignal(Path, list)

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel          # FolderTreePanel — gives access to _log
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        item = self.itemAt(event.position().toPoint())
        if item and event.mimeData().hasUrls():
            # Highlight the folder under the cursor so the user sees the target
            self.setCurrentItem(item)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        item = self.itemAt(event.position().toPoint())
        if not item:
            event.ignore()
            return
        dst_folder = Path(item.data(0, Qt.ItemDataRole.UserRole) or "")
        if not dst_folder.is_dir():
            event.ignore()
            return

        moved: List[Path] = []
        src_folder: Optional[Path] = None
        errors: List[str] = []

        for url in event.mimeData().urls():
            src = Path(url.toLocalFile())
            if not src.is_file():
                continue
            if src_folder is None:
                src_folder = src.parent
            # Don't move a file to its own folder — silently skip
            if src.parent == dst_folder:
                continue
            try:
                dst_file = unique_dest(src, dst_folder)
                # Log before moving — captures current state in source folder
                original_exif = read_exif(src)["fields"]
                append_historial(src.parent, src.name, None, original_exif, "movido")
                shutil.move(str(src), str(dst_file))
                moved.append(dst_file)
                if self._panel._log:
                    self._panel._log.log(
                        str(src.parent), src.name, "move", str(src), str(dst_file)
                    )
            except Exception as e:
                errors.append(f"{src.name}: {e}")

        if errors:
            mb_warning(
                self, "Errores al mover",
                "\n".join(errors[:10]),
            )

        if moved and src_folder is not None:
            self.files_moved.emit(src_folder, moved)
            event.acceptProposedAction()
        else:
            event.ignore()


class FolderTreePanel(QWidget):
    folder_selected = pyqtSignal(Path)
    files_moved = pyqtSignal(Path, list)   # (source_folder, [new_dst_paths])

    def __init__(self, main_window=None, log_manager=None, parent=None):
        super().__init__(parent)
        self._main_window = main_window
        self._log = log_manager
        self._root: Optional[Path] = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Top button row
        btn_row = QHBoxLayout()
        self._btn_open = QPushButton("Abrir carpeta raíz…")
        self._btn_open.setToolTip(
            "Selecciona la carpeta raíz que contiene todas tus fotos.\n"
            "Las subcarpetas se cargan en el árbol de forma progresiva."
        )
        self._btn_open.clicked.connect(self.open_root_dialog)
        btn_row.addWidget(self._btn_open)
        layout.addLayout(btn_row)

        # Root path label
        self._lbl_root = QLabel("")
        self._lbl_root.setWordWrap(True)
        self._lbl_root.setStyleSheet("font-size: 10px; color: #888;")
        layout.addWidget(self._lbl_root)

        # Tree widget (subclass handles drop)
        self._tree = _DropTree(self)
        self._tree.setHeaderLabel("Carpetas")
        self._tree.setAnimated(True)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemClicked.connect(self._on_item_clicked)
        self._tree.files_moved.connect(self.files_moved)   # re-emit on panel
        layout.addWidget(self._tree)

    # ── Public API ─────────────────────────────────────────────────────────

    def open_root_dialog(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "Seleccionar carpeta raíz",
            str(self._root) if self._root else "",
            QFileDialog.Option.DontUseNativeDialog,
        )
        if path:
            self.load_root(Path(path))
            if self._main_window:
                self._main_window.set_root(Path(path))

    def load_root(self, root: Path) -> None:
        if not root_is_available(root):
            return
        self._root = root
        self._lbl_root.setText(str(root))
        self._tree.clear()
        root_item = self._make_item(root)
        self._tree.addTopLevelItem(root_item)
        self._tree.expandItem(root_item)

    def refresh_item(self, folder_path: Path) -> None:
        """Refresh backup indicator for an item matching folder_path."""
        item = self._find_item(folder_path)
        if item:
            self._apply_backup_indicator(item, folder_path)

    def reveal_folder(self, folder_path: Path) -> None:
        """Ensure folder_path appears in the tree, select it, emit folder_selected.

        If the parent was never expanded (still has placeholder), force-loads its
        children first.  If the item is already present, just scrolls to it.
        """
        parent_path = folder_path.parent
        parent_item = self._find_item(parent_path)
        if parent_item is None:
            return

        # Force-expand the parent if it only has the lazy-load placeholder
        if (parent_item.childCount() == 1 and
                parent_item.child(0).text(0) == _PLACEHOLDER):
            parent_item.takeChild(0)
            for subdir in list_subdirs(parent_path):
                parent_item.addChild(self._make_item(subdir))

        # Find the target item (may already exist, or add it now)
        target = self._find_item(folder_path)
        if target is None:
            target = self._make_item(folder_path)
            parent_item.addChild(target)

        self._tree.expandItem(parent_item)
        self._tree.setCurrentItem(target)
        self._tree.scrollToItem(target)
        self.folder_selected.emit(folder_path)

    # ── Tree building ──────────────────────────────────────────────────────

    def _make_item(self, path: Path) -> QTreeWidgetItem:
        count = count_images(path)
        label = f"{path.name}  ({count})"
        item = QTreeWidgetItem([label])
        item.setData(0, Qt.ItemDataRole.UserRole, str(path))

        self._apply_backup_indicator(item, path)

        # Add placeholder child so expand arrow appears if there are subdirs
        subdirs = list_subdirs(path)
        if subdirs:
            placeholder = QTreeWidgetItem([_PLACEHOLDER])
            item.addChild(placeholder)

        return item

    def _apply_backup_indicator(self, item: QTreeWidgetItem, path: Path) -> None:
        if has_backup(path):
            item.setForeground(0, QBrush(QColor(80, 200, 120)))
            item.setToolTip(0, "Carpeta procesada (backup EXIF existe)")
        else:
            item.setForeground(0, QBrush(QColor(220, 220, 225)))
            item.setToolTip(0, "")

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        # Check if we need to lazy-load children
        if item.childCount() == 1 and item.child(0).text(0) == _PLACEHOLDER:
            item.takeChild(0)  # remove placeholder
            path_str = item.data(0, Qt.ItemDataRole.UserRole)
            if not path_str:
                return
            path = Path(path_str)
            subdirs = list_subdirs(path)
            for subdir in subdirs:
                child = self._make_item(subdir)
                item.addChild(child)

    def _on_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        if item.text(0) == _PLACEHOLDER:
            return
        path_str = item.data(0, Qt.ItemDataRole.UserRole)
        if path_str:
            self.folder_selected.emit(Path(path_str))

    def _find_item(self, target: Path) -> Optional[QTreeWidgetItem]:
        """BFS search for tree item matching target path."""
        queue: deque = deque(
            self._tree.topLevelItem(i) for i in range(self._tree.topLevelItemCount())
        )
        while queue:
            item = queue.popleft()
            if item is None:
                continue
            path_str = item.data(0, Qt.ItemDataRole.UserRole)
            if path_str and Path(path_str) == target:
                return item
            for i in range(item.childCount()):
                queue.append(item.child(i))
        return None
