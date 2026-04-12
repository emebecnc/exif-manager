"""Shared widget styles and helpers for the dark-theme UI."""
from PyQt6.QtWidgets import QApplication, QMessageBox


# ── Button stylesheets ────────────────────────────────────────────────────────

BUTTON_STYLE = """
    QPushButton {
        border: 1px solid #505050;
        border-radius: 8px;
        padding: 5px 14px;
        background-color: #383838;
        color: #e0e0e0;
        font-size: 10pt;
    }
    QPushButton:hover  {
        background-color: #484848;
        border-color: #7a7a7a;
        color: #ffffff;
    }
    QPushButton:pressed { background-color: #282828; border-color: #606060; }
    QPushButton:disabled { color: #555555; border-color: #3a3a3a; background-color: #2a2a2a; }
"""

BUTTON_PRIMARY = """
    QPushButton {
        border: 1px solid #0a6b70;
        border-radius: 8px;
        padding: 5px 16px;
        background-color: #0d7377;
        color: #ffffff;
        font-weight: bold;
        font-size: 10pt;
    }
    QPushButton:hover  {
        background-color: #0f8f96;
        border-color: #12b0b8;
        color: #ffffff;
    }
    QPushButton:pressed { background-color: #0a5558; }
    QPushButton:disabled { color: #555555; border-color: #3a3a3a; background-color: #2a2a2a; }
"""

BUTTON_DANGER = """
    QPushButton {
        border: 1px solid #6a1515;
        border-radius: 8px;
        padding: 5px 14px;
        background-color: #852020;
        color: #ffffff;
        font-weight: bold;
        font-size: 10pt;
    }
    QPushButton:hover  {
        background-color: #a02828;
        border-color: #902020;
        color: #ffffff;
    }
    QPushButton:pressed { background-color: #601515; }
    QPushButton:disabled { color: #555555; border-color: #3a3a3a; background-color: #2a2a2a; }
"""

BUTTON_SECONDARY = BUTTON_STYLE   # alias — same visual weight as standard

# Tab bar for QTabWidget (applied to the widget itself)
TAB_STYLE = """
    QTabWidget::pane {
        border: none;
        border-top: 2px solid #2a2a2a;
        background-color: #1e1e1e;
    }
    QTabBar::tab {
        background-color: #252525;
        color: #999999;
        padding: 0 22px;
        height: 34px;
        border: none;
        border-right: 1px solid #1a1a1a;
        min-width: 90px;
        font-size: 10pt;
    }
    QTabBar::tab:selected {
        background-color: #0d7377;
        color: #ffffff;
        font-weight: bold;
        border-bottom: 2px solid #1dc8d0;
    }
    QTabBar::tab:hover:!selected {
        background-color: #2e2e2e;
        color: #dddddd;
    }
"""

# Global application stylesheet — scrollbars, lists, inputs, splitter
APP_STYLE = """
    QMainWindow, QWidget {
        background-color: #1e1e1e;
        color: #d0d0d0;
        font-size: 10pt;
    }
    QSplitter::handle {
        background-color: #2a2a2a;
        width: 2px;
        height: 2px;
    }
    QScrollBar:vertical {
        background: #1e1e1e;
        width: 10px;
        border: none;
        border-radius: 5px;
    }
    QScrollBar::handle:vertical {
        background: #484848;
        border-radius: 5px;
        min-height: 30px;
    }
    QScrollBar::handle:vertical:hover { background: #606060; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
    QScrollBar:horizontal {
        background: #1e1e1e;
        height: 10px;
        border: none;
        border-radius: 5px;
    }
    QScrollBar::handle:horizontal {
        background: #484848;
        border-radius: 5px;
        min-width: 30px;
    }
    QScrollBar::handle:horizontal:hover { background: #606060; }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
    QListWidget {
        background-color: #1a1a1a;
        border: 1px solid #333333;
        border-radius: 6px;
        outline: none;
        font-size: 10pt;
    }
    QListWidget::item {
        border-radius: 4px;
        padding: 2px 4px;
    }
    QListWidget::item:selected {
        background-color: #0d5f63;
        color: #ffffff;
    }
    QListWidget::item:hover:!selected {
        background-color: #2a2a2a;
    }
    QTreeWidget, QTreeView {
        background-color: #1a1a1a;
        border: none;
        outline: none;
        font-size: 10pt;
    }
    QTreeWidget::item:selected, QTreeView::item:selected {
        background-color: #0d5f63;
        color: #ffffff;
    }
    QTreeWidget::item:hover:!selected, QTreeView::item:hover:!selected {
        background-color: #252525;
    }
    QLineEdit, QSpinBox, QComboBox {
        background-color: #252525;
        border: 1px solid #404040;
        border-radius: 6px;
        padding: 3px 6px;
        color: #d0d0d0;
        font-size: 10pt;
    }
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
        border-color: #0d7377;
        background-color: #2a2a2a;
    }
    QStatusBar {
        background-color: #181818;
        color: #888888;
        border-top: 1px solid #2a2a2a;
        font-size: 9pt;
    }
    QLabel {
        color: #d0d0d0;
        font-size: 10pt;
    }
    QGroupBox {
        border: 1px solid #353535;
        border-radius: 8px;
        margin-top: 8px;
        padding-top: 6px;
        font-size: 10pt;
        color: #aaaaaa;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
    }
    QCheckBox {
        color: #c0c0c0;
        font-size: 10pt;
    }
    QCheckBox::indicator {
        width: 14px;
        height: 14px;
        border: 1px solid #505050;
        border-radius: 3px;
        background-color: #252525;
    }
    QCheckBox::indicator:checked {
        background-color: #0d7377;
        border-color: #14a0a6;
    }
    QRadioButton {
        color: #c0c0c0;
        font-size: 10pt;
    }
    QRadioButton::indicator {
        width: 14px;
        height: 14px;
        border: 1px solid #505050;
        border-radius: 7px;
        background-color: #252525;
    }
    QRadioButton::indicator:checked {
        background-color: #0d7377;
        border-color: #14a0a6;
    }
    QToolTip {
        background-color: #2a2a2a;
        color: #e0e0e0;
        border: 1px solid #555555;
        border-radius: 4px;
        padding: 4px 8px;
        font-size: 9pt;
    }
"""


# ── Centering helper ─────────────────────────────────────────────────────────

def center_on_screen(widget) -> None:
    """Move *widget* so it is centred on the primary screen.
    Call before show() / exec() for best results."""
    hint   = widget.sizeHint()
    screen = QApplication.primaryScreen().availableGeometry()
    x = screen.x() + max(0, (screen.width()  - hint.width())  // 2)
    y = screen.y() + max(0, (screen.height() - hint.height()) // 2)
    widget.move(x, y)


# ── Centered QMessageBox wrappers ─────────────────────────────────────────────

def mb_warning(parent, title: str, text: str) -> None:
    """Show a centred warning message box."""
    msg = QMessageBox(parent)
    msg.setWindowTitle(title)
    msg.setText(text)
    msg.setIcon(QMessageBox.Icon.Warning)
    center_on_screen(msg)
    msg.exec()


def mb_info(parent, title: str, text: str) -> None:
    """Show a centred information message box."""
    msg = QMessageBox(parent)
    msg.setWindowTitle(title)
    msg.setText(text)
    msg.setIcon(QMessageBox.Icon.Information)
    center_on_screen(msg)
    msg.exec()


def mb_question(
    parent,
    title: str,
    text: str,
    buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
    default: QMessageBox.StandardButton = QMessageBox.StandardButton.No,
):
    """Show a centred question message box and return the clicked StandardButton."""
    msg = QMessageBox(parent)
    msg.setWindowTitle(title)
    msg.setText(text)
    msg.setIcon(QMessageBox.Icon.Question)
    msg.setStandardButtons(buttons)
    msg.setDefaultButton(default)
    center_on_screen(msg)
    return msg.exec()


# ── Convenience appliers ──────────────────────────────────────────────────────

def apply_button_style(btn) -> None:
    """Standard secondary dark-theme button."""
    btn.setStyleSheet(BUTTON_STYLE)


def apply_primary_button_style(btn) -> None:
    """Teal primary action button (Aplicar, Buscar, Deduplicar…)."""
    btn.setStyleSheet(BUTTON_PRIMARY)


def apply_danger_button_style(btn) -> None:
    """Red destructive action button (Eliminar…)."""
    btn.setStyleSheet(BUTTON_DANGER)


def apply_app_style(app: QApplication) -> None:
    """Apply the global dark-theme APP_STYLE to the entire QApplication.
    Call once from main.py after QApplication is created."""
    app.setStyleSheet(APP_STYLE)
