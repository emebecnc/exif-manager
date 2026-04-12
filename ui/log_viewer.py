"""Log system: LogManager (shared state) + LogViewerDialog."""
import csv
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import QObject, pyqtSignal, Qt, QStandardPaths
from PyQt6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QComboBox, QDateEdit, QFileDialog,
    QDialogButtonBox, QHeaderView, QAbstractItemView,
)
from PyQt6.QtCore import QDate


@dataclass
class LogEntry:
    timestamp: datetime
    folder: str
    filename: str
    action: str
    old_value: str = ""
    new_value: str = ""


ACTION_LABELS = {
    "write_exif":         "Escribir EXIF",
    "restore_backup":     "Restaurar backup",
    "delete":             "Eliminar foto",
    "delete_duplicate":   "Eliminar duplicado",
    "undo":               "Deshacer",
    "create_backup":      "Crear backup",
    "rename":             "Renombrar",
    "cleanup":            "Limpieza",
    "copy":               "Copiar",
    "move":               "Mover",
    "move_folder":        "Mover carpeta",
    "copy_folder":        "Copiar carpeta",
    "delete_folder":      "Eliminar carpeta",
}

_ALL_ACTIONS = "Todas"
_CSV_HEADERS = ["Timestamp", "Carpeta", "Archivo", "Acción", "Anterior", "Nuevo"]


class LogManager(QObject):
    """Shared log state. Thread-safe append; persists to CSV on disk."""

    new_entry = pyqtSignal(object)  # LogEntry

    def __init__(self, app_data_dir: Optional[Path] = None, parent=None):
        super().__init__(parent)
        self._entries: List[LogEntry] = []

        if app_data_dir is None:
            data = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
            app_data_dir = Path(data)
        app_data_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = app_data_dir / "log.csv"
        self._load_from_disk()

    # ── Public API ────────────────────────────────────────────────────────

    def log(
        self,
        folder: str,
        filename: str,
        action: str,
        old_value: str = "",
        new_value: str = "",
    ) -> None:
        entry = LogEntry(
            timestamp=datetime.now(),
            folder=folder,
            filename=filename,
            action=action,
            old_value=old_value,
            new_value=new_value,
        )
        self._entries.append(entry)
        self._append_to_disk(entry)
        self.new_entry.emit(entry)

    @property
    def entries(self) -> List[LogEntry]:
        return list(self._entries)

    def export_txt(self, dest_path: Path) -> None:
        with open(dest_path, "w", encoding="utf-8") as f:
            for e in self._entries:
                f.write(
                    f"[{e.timestamp:%Y-%m-%d %H:%M:%S}] "
                    f"{e.folder} / {e.filename} | "
                    f"{e.action} | {e.old_value} → {e.new_value}\n"
                )

    def export_csv(self, dest_path: Path) -> None:
        with open(dest_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(_CSV_HEADERS)
            for e in self._entries:
                writer.writerow([
                    e.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    e.folder, e.filename, e.action, e.old_value, e.new_value,
                ])

    # ── Private ───────────────────────────────────────────────────────────

    def _load_from_disk(self) -> None:
        if not self._log_file.exists():
            return
        try:
            with open(self._log_file, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        ts = datetime.strptime(row["Timestamp"], "%Y-%m-%d %H:%M:%S")
                        self._entries.append(LogEntry(
                            timestamp=ts,
                            folder=row.get("Carpeta", ""),
                            filename=row.get("Archivo", ""),
                            action=row.get("Acción", ""),
                            old_value=row.get("Anterior", ""),
                            new_value=row.get("Nuevo", ""),
                        ))
                    except Exception:
                        pass
        except Exception:
            pass

    def _append_to_disk(self, entry: LogEntry) -> None:
        try:
            write_header = not self._log_file.exists()
            with open(self._log_file, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if write_header:
                    writer.writerow(_CSV_HEADERS)
                writer.writerow([
                    entry.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    entry.folder, entry.filename, entry.action,
                    entry.old_value, entry.new_value,
                ])
        except Exception:
            pass


class LogViewerDialog(QDialog):
    """Dialog showing all log entries with filtering and export."""

    def __init__(self, log_manager: LogManager, parent=None):
        super().__init__(parent)
        self.setWindowIcon(QApplication.instance().windowIcon())
        self._log = log_manager
        self.setWindowTitle("Registro de cambios")
        self.resize(900, 500)
        self._build_ui()
        self._populate_table(self._log.entries)
        self._log.new_entry.connect(self._on_new_entry)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Filter bar
        filter_bar = QHBoxLayout()
        filter_bar.addWidget(QLabel("Desde:"))
        self._date_from = QDateEdit()
        self._date_from.setCalendarPopup(True)
        self._date_from.setDate(QDate.currentDate().addMonths(-3))
        self._date_from.setToolTip("Fecha de inicio del rango de búsqueda.")
        filter_bar.addWidget(self._date_from)

        filter_bar.addWidget(QLabel("Hasta:"))
        self._date_to = QDateEdit()
        self._date_to.setCalendarPopup(True)
        self._date_to.setDate(QDate.currentDate())
        self._date_to.setToolTip("Fecha de fin del rango de búsqueda.")
        filter_bar.addWidget(self._date_to)

        filter_bar.addWidget(QLabel("Acción:"))
        self._action_combo = QComboBox()
        self._action_combo.addItem(_ALL_ACTIONS)
        for a in ACTION_LABELS.values():
            self._action_combo.addItem(a)
        self._action_combo.setToolTip("Filtra por tipo de operación registrada.")
        filter_bar.addWidget(self._action_combo)

        btn_filter = QPushButton("Filtrar")
        btn_filter.setToolTip("Aplica los filtros de fecha y tipo de acción seleccionados.")
        btn_filter.clicked.connect(self._on_filter)
        filter_bar.addWidget(btn_filter)
        filter_bar.addStretch()
        layout.addLayout(filter_bar)

        # Table
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(_CSV_HEADERS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._table)

        # Buttons
        btn_bar = QHBoxLayout()
        btn_txt = QPushButton("Exportar .txt")
        btn_txt.setToolTip("Guarda el registro de cambios en un archivo de texto plano.")
        btn_txt.clicked.connect(self._export_txt)
        btn_csv = QPushButton("Exportar .csv")
        btn_csv.setToolTip(
            "Guarda el registro de cambios en formato CSV,\n"
            "compatible con Excel y Google Sheets."
        )
        btn_csv.clicked.connect(self._export_csv)
        btn_close = QPushButton("Cerrar")
        btn_close.setToolTip("Cierra el registro de cambios.")
        btn_close.clicked.connect(self.accept)
        btn_bar.addWidget(btn_txt)
        btn_bar.addWidget(btn_csv)
        btn_bar.addStretch()
        btn_bar.addWidget(btn_close)
        layout.addLayout(btn_bar)

    def _populate_table(self, entries: List[LogEntry]) -> None:
        self._table.setRowCount(0)
        for entry in entries:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, 0, QTableWidgetItem(entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")))
            self._table.setItem(row, 1, QTableWidgetItem(entry.folder))
            self._table.setItem(row, 2, QTableWidgetItem(entry.filename))
            self._table.setItem(row, 3, QTableWidgetItem(ACTION_LABELS.get(entry.action, entry.action)))
            self._table.setItem(row, 4, QTableWidgetItem(entry.old_value))
            self._table.setItem(row, 5, QTableWidgetItem(entry.new_value))
        self._table.scrollToBottom()

    def _on_new_entry(self, entry: LogEntry) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, 0, QTableWidgetItem(entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")))
        self._table.setItem(row, 1, QTableWidgetItem(entry.folder))
        self._table.setItem(row, 2, QTableWidgetItem(entry.filename))
        self._table.setItem(row, 3, QTableWidgetItem(ACTION_LABELS.get(entry.action, entry.action)))
        self._table.setItem(row, 4, QTableWidgetItem(entry.old_value))
        self._table.setItem(row, 5, QTableWidgetItem(entry.new_value))
        self._table.scrollToBottom()

    def _on_filter(self) -> None:
        d_from = self._date_from.date().toPyDate()
        d_to = self._date_to.date().toPyDate()
        action_label = self._action_combo.currentText()

        # Reverse lookup label → key
        action_key = None
        if action_label != _ALL_ACTIONS:
            for k, v in ACTION_LABELS.items():
                if v == action_label:
                    action_key = k
                    break

        filtered = [
            e for e in self._log.entries
            if d_from <= e.timestamp.date() <= d_to
            and (action_key is None or e.action == action_key)
        ]
        self._populate_table(filtered)

    def _export_txt(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Exportar como texto", "", "Text files (*.txt)")
        if path:
            self._log.export_txt(Path(path))

    def _export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Exportar como CSV", "", "CSV files (*.csv)")
        if path:
            self._log.export_csv(Path(path))
