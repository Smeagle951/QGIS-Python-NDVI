"""Talhao identity shared by monitor and project exports.

Layer-tree groups are the user's field partition.  No geometry is moved or
clipped here: the export only includes layers belonging to the chosen group.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


SECTIONS = {"00_REFERENCIAS", "10_PLANEJAMENTO", "20_OBSTACULOS", "30_OPERACAO"}
FIELD_NAME_RE = re.compile(r"^(?:TL|TALH[AÃ]O|FIELD)[ _-]*[A-Z0-9.-]+$", re.IGNORECASE)


@dataclass
class FieldExportGroup:
    key: str
    label: str
    layer_ids: set[str]
    has_field_group: bool = True


def is_placeholder(name: str) -> bool:
    text = " ".join(str(name or "").split())
    return not text or bool(re.fullmatch(r"talh[aã]o[ _-]*\d+", text, re.IGNORECASE))


def _field_node(layer, root):
    node = root.findLayer(layer.id())
    if node is None:
        return None
    ancestors = []
    parent = node.parent()
    while parent is not None and parent != root:
        ancestors.append(parent)
        parent = parent.parent()

    # Strongest signal: a talhao group owns the canonical section groups.
    # This avoids treating nested groups such as "Bordadura" or "Linhas" as
    # independent fields and asking a name for every work line.
    for candidate in ancestors:
        try:
            child_names = {
                (child.name() or "").strip().upper()
                for child in candidate.children()
                if hasattr(child, "name")
            }
        except Exception:
            child_names = set()
        if child_names & SECTIONS:
            return candidate

    # Imported/user projects commonly use TL-12 or Talhao 12 without the
    # canonical section folders. Prefer that semantic ancestor explicitly.
    for candidate in ancestors:
        if FIELD_NAME_RE.fullmatch((candidate.name() or "").strip()):
            return candidate

    # Compatibility fallback for FortSmart > Fazenda > Talhao trees.
    for candidate in ancestors:
        name = (candidate.name() or "").strip()
        if name.upper() not in SECTIONS and not name.startswith("FortSmart"):
            return candidate
    return None


def _group_key(node, root) -> str:
    parts = []
    while node is not None and node != root:
        parts.append(node.name())
        node = node.parent()
    return "group:" + "/".join(reversed(parts))


def group_export_layers(layers, root) -> list[FieldExportGroup]:
    groups: dict[str, FieldExportGroup] = {}
    for layer in layers:
        node = _field_node(layer, root)
        if node is None:
            key, label = f"layer:{layer.id()}", f"Sem grupo: {layer.name()}"
        else:
            key, label = _group_key(node, root), node.name()
        group = groups.setdefault(key, FieldExportGroup(key, label, set(), node is not None))
        group.layer_ids.add(layer.id())
    return sorted(groups.values(), key=lambda group: group.label.casefold())


def export_name_map(groups: list[FieldExportGroup], names: dict[str, str]) -> dict[str, str]:
    """Validate edited names and return one field name per selected layer."""
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for group in groups:
        name = " ".join(names.get(group.key, "").split())
        if not name:
            raise ValueError(f"Informe o nome real do talhao para o grupo '{group.label}'.")
        if name.casefold() in used:
            raise ValueError(f"Nome de talhao repetido: {name}.")
        used.add(name.casefold())
        mapping.update({layer_id: name for layer_id in group.layer_ids})
    return mapping


def choose_export_fields(parent, layers, root) -> tuple[list[FieldExportGroup], dict[str, str]] | None:
    """Select field groups and optionally replace their display names."""
    from qgis.PyQt.QtWidgets import (
        QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
        QLineEdit, QMessageBox, QScrollArea, QVBoxLayout, QWidget,
    )

    groups = group_export_layers(layers, root)
    if not groups:
        raise ValueError("Nenhuma camada de talhao disponivel para exportar.")
    dialog = QDialog(parent)
    dialog.setWindowTitle("Talhoes a exportar")
    dialog.resize(540, min(600, 160 + len(groups) * 55))
    layout = QVBoxLayout(dialog)
    hint = QLabel("Selecione apenas os talhoes e confira o nome de cada um. "
                  "Limites, bordaduras, obstaculos e linhas sao identificados "
                  "automaticamente dentro do respectivo grupo.")
    hint.setWordWrap(True)
    layout.addWidget(hint)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    body = QWidget()
    rows = QVBoxLayout(body)
    controls = []
    for group in groups:
        row = QHBoxLayout()
        checked = QCheckBox(group.label)
        checked.setChecked(True)
        name = QLineEdit(group.label if group.has_field_group and not is_placeholder(group.label) else "")
        name.setPlaceholderText("Nome real do talhao")
        name.setToolTip(f"{len(group.layer_ids)} camada(s) neste grupo")
        checked.toggled.connect(name.setEnabled)
        row.addWidget(checked, 2)
        row.addWidget(name, 3)
        rows.addLayout(row)
        controls.append((group, checked, name))
    rows.addStretch()
    scroll.setWidget(body)
    layout.addWidget(scroll)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.button(QDialogButtonBox.Ok).setText("Continuar exportacao")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    while dialog.exec_() == QDialog.Accepted:
        selected = [group for group, checked, _ in controls if checked.isChecked()]
        if not selected:
            QMessageBox.warning(dialog, "FortSmart", "Selecione ao menos um talhao.")
            continue
        names = {group.key: edit.text() for group, checked, edit in controls if checked.isChecked()}
        try:
            export_name_map(selected, names)
        except ValueError as exc:
            QMessageBox.warning(dialog, "FortSmart", str(exc))
            continue
        return selected, names
    return None
