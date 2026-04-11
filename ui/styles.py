"""Shared widget styles for the dark-theme UI."""

# Generic button style: flat dark tile with visible border
BUTTON_STYLE = """
    QPushButton {
        border: 1px solid #555555;
        border-radius: 4px;
        padding: 3px 10px;
        background-color: #3a3a3a;
        color: #ffffff;
    }
    QPushButton:hover {
        background-color: #4a4a4a;
        border-color: #777777;
    }
    QPushButton:pressed {
        background-color: #2a2a2a;
    }
    QPushButton:disabled {
        color: #666666;
        border-color: #444444;
        background-color: #2e2e2e;
    }
"""


def apply_button_style(btn) -> None:
    """Apply the standard dark-theme button style to *btn*."""
    btn.setStyleSheet(BUTTON_STYLE)
