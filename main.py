"""EXIF Date Manager — entry point."""
import subprocess
import sys
import traceback
from pathlib import Path

from PyQt6.QtWidgets import QApplication, QMessageBox
from PyQt6.QtGui import QIcon, QPalette, QColor
from PyQt6.QtCore import Qt


def _check_ffmpeg() -> bool:
    """Return True if the ffmpeg binary is reachable on PATH (or bundled)."""
    # Also accept a bundled ffmpeg.exe in the project directory
    bundled = Path(__file__).parent / "ffmpeg.exe"
    if bundled.exists():
        return True
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def apply_dark_theme(app: QApplication) -> None:
    """Apply a dark Fusion palette."""
    app.setStyle("Fusion")
    palette = QPalette()

    # Base colors
    dark       = QColor(30,  30,  35)
    mid_dark   = QColor(45,  45,  50)
    mid        = QColor(60,  60,  65)
    light      = QColor(80,  80,  90)
    text       = QColor(220, 220, 225)
    bright     = QColor(255, 255, 255)
    highlight  = QColor(42,  130, 218)
    disabled   = QColor(120, 120, 130)
    link       = QColor(100, 170, 255)

    palette.setColor(QPalette.ColorRole.Window,          dark)
    palette.setColor(QPalette.ColorRole.WindowText,      text)
    palette.setColor(QPalette.ColorRole.Base,            mid_dark)
    palette.setColor(QPalette.ColorRole.AlternateBase,   dark)
    palette.setColor(QPalette.ColorRole.ToolTipBase,     mid)
    palette.setColor(QPalette.ColorRole.ToolTipText,     text)
    palette.setColor(QPalette.ColorRole.Text,            text)
    palette.setColor(QPalette.ColorRole.Button,          mid)
    palette.setColor(QPalette.ColorRole.ButtonText,      text)
    palette.setColor(QPalette.ColorRole.BrightText,      bright)
    palette.setColor(QPalette.ColorRole.Link,            link)
    palette.setColor(QPalette.ColorRole.Highlight,       highlight)
    palette.setColor(QPalette.ColorRole.HighlightedText, bright)
    palette.setColor(QPalette.ColorRole.Mid,             mid)
    palette.setColor(QPalette.ColorRole.Dark,            dark)
    palette.setColor(QPalette.ColorRole.Shadow,          QColor(0, 0, 0))
    palette.setColor(QPalette.ColorRole.Light,           light)

    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text,       disabled)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled)

    app.setPalette(palette)

    # Extra stylesheet tweaks
    app.setStyleSheet("""
        QToolTip {
            color: #dcdcdc;
            background-color: #3c3c44;
            border: 1px solid #555560;
            padding: 2px;
        }
        QGroupBox {
            font-weight: bold;
            border: 1px solid #555560;
            border-radius: 4px;
            margin-top: 8px;
            padding-top: 4px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 8px;
            padding: 0 4px;
        }
        QScrollBar:vertical {
            background: #2d2d35;
            width: 10px;
        }
        QScrollBar::handle:vertical {
            background: #555560;
            min-height: 20px;
            border-radius: 4px;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar:horizontal {
            background: #2d2d35;
            height: 10px;
        }
        QScrollBar::handle:horizontal {
            background: #555560;
            min-width: 20px;
            border-radius: 4px;
        }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
        QHeaderView::section {
            background-color: #3c3c44;
            color: #dcdcdc;
            border: none;
            border-right: 1px solid #555560;
            padding: 4px;
        }
        QTableWidget { gridline-color: #44444e; }
        QPushButton {
            padding: 4px 12px;
            border-radius: 3px;
        }
        QPushButton:hover { background-color: #555560; }
        QPushButton:pressed { background-color: #2a82da; }
        QTreeWidget::item:selected, QListWidget::item:selected {
            background-color: #2a82da;
        }
    """)


def _global_exception_hook(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    box = QMessageBox()
    box.setWindowTitle("Error inesperado")
    box.setText("Se produjo un error inesperado.")
    box.setDetailedText(msg)
    box.setIcon(QMessageBox.Icon.Critical)
    box.exec()


def main():
    app = QApplication(sys.argv)
    app.setOrganizationName("homelab")
    app.setApplicationName("ExifManager")
    app.setApplicationVersion("1.0.0")
    apply_dark_theme(app)

    _ico = Path(__file__).parent / "icon.ico"
    if _ico.exists():
        app.setWindowIcon(QIcon(str(_ico)))
    sys.excepthook = _global_exception_hook

    # Check for ffmpeg before building the UI (subprocess — safe pre-Qt)
    ffmpeg_ok = _check_ffmpeg()
    if not ffmpeg_ok:
        QMessageBox.warning(
            None,
            "FFmpeg no encontrado",
            "FFmpeg no encontrado en el PATH ni en la carpeta del programa.\n\n"
            "Las miniaturas de video y la edición de fecha de video no estarán "
            "disponibles.\n\n"
            "Descargá ffmpeg desde https://ffmpeg.org y agregalo al PATH, "
            "o colocá ffmpeg.exe junto a este programa.",
        )

    # Import after QApplication is created (Qt requires it)
    from ui.main_window import MainWindow
    window = MainWindow(ffmpeg_available=ffmpeg_ok)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
