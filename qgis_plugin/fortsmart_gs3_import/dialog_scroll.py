"""FortSmart — helper para dialogos compactos com area rolavel."""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QApplication,
    QFrame,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


def wrap_in_scroll(content: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setWidget(content)
    return scroll


def fit_dialog_to_screen(dialog, *, width: int = 540, height: int = 520, min_width: int = 480, min_height: int = 380) -> None:
    """Limita tamanho do dialogo para caber na tela com botoes sempre visiveis."""
    screen = QApplication.primaryScreen()
    if screen is not None:
        avail = screen.availableGeometry()
        max_w = max(min_width, int(avail.width() * 0.92))
        max_h = max(min_height, int(avail.height() * 0.88))
        width = min(width, max_w)
        height = min(height, max_h)
        dialog.setMaximumSize(max_w, max_h)

    dialog.setMinimumSize(min_width, min_height)
    dialog.resize(width, height)


def build_scroll_dialog(parent_widget: QWidget, buttons) -> QVBoxLayout:
    """Layout raiz: conteudo rolavel + botoes fixos na base."""
    root = QVBoxLayout(parent_widget)
    root.setContentsMargins(6, 6, 6, 6)
    root.setSpacing(6)

    content = QWidget()
    content_layout = QVBoxLayout(content)
    content_layout.setContentsMargins(4, 4, 4, 4)
    content_layout.setSpacing(6)

    scroll = wrap_in_scroll(content)
    root.addWidget(scroll, 1)
    root.addWidget(buttons)

    return content_layout
