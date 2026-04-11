"""Shared widget styles and helpers for the dark-theme UI."""
from PyQt6.QtWidgets import QApplication, QMessageBox


# ── Button stylesheets ────────────────────────────────────────────────────────

BUTTON_STYLE = """
    QPushButton {
        border: 1px solid #555555;
        border-radius: 6px;
        padding: 5px 14px;
        background-color: #3a3a3a;
        color: #ffffff;
    }
    QPushButton:hover  { background-color: #4a4a4a; border-color: #777777; }
    QPushButton:pressed { background-color: #2a2a2a; }
    QPushButton:disabled { color: #666666; border-color: #444444; background-color: #2e2e2e; }
"""

BUTTON_PRIMARY = """
    QPushButton {
        border: 1px solid #0a5f63;
        border-radius: 6px;
        padding: 5px 16px;
        background-color: #0d7377;
        color: #ffffff;
        font-weight: bold;
    }
    QPushButton:hover  { background-color: #14a0a6; border-color: #10888d; }
    QPushButton:pressed { background-color: #0a5558; }
    QPushButton:disabled { color: #666666; border-color: #444444; background-color: #2e2e2e; }
"""

BUTTON_DANGER = """
    QPushButton {
        border: 1px solid #5a1010;
        border-radius: 6px;
        padding: 5px 14px;
        background-color: #7d1a1a;
        color: #ffffff;
        font-weight: bold;
    }
    QPushButton:hover  { background-color: #a02020; border-color: #7a1818; }
    QPushButton:pressed { background-color: #5a1010; }
    QPushButton:disabled { color: #666666; border-color: #444444; background-color: #2e2e2e; }
"""

BUTTON_SECONDARY = BUTTON_STYLE   # alias — same visual weight as standard

# Tab bar for QTabWidget (applied to the widget itself)
TAB_STYLE = """
    QTabWidget::pane {
        border: none;
        border-top: 1px solid #3a3a3a;
    }
    QTabBar::tab {
        background-color: #252525;
        color: #aaaaaa;
        padding: 0 18px;
        height: 32px;
        border: none;
        border-right: 1px solid #1a1a1a;
        min-width: 80px;
    }
    QTabBar::tab:selected {
        background-color: #0d7377;
        color: #ffffff;
        border-bottom: 2px solid #00d4ff;
    }
    QTabBar::tab:hover:!selected {
        background-color: #323232;
        color: #dddddd;
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
