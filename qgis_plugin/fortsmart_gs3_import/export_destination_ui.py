"""FortSmart — painel GUI: onde salvar pastas exportadas."""

from __future__ import annotations

from pathlib import Path

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class ExportDestinationPanel(QGroupBox):
    """Selecao de pasta pai + nome + preview do caminho completo e arvore."""

    path_changed = pyqtSignal()

    def __init__(
        self,
        title: str = "Onde salvar (pasta organizada)",
        default_parent: Path | str | None = None,
        default_folder_name: str = "FORTSMART_EXPORT",
        parent: QWidget | None = None,
    ):
        super().__init__(title, parent)
        self._default_parent = Path(default_parent) if default_parent else Path.home()

        self.parent_edit = QLineEdit(str(self._default_parent))
        self.folder_name_edit = QLineEdit(default_folder_name)

        browse_parent = QPushButton("Escolher pasta pai…")
        browse_parent.clicked.connect(self._browse_parent)

        parent_row = QHBoxLayout()
        parent_row.addWidget(self.parent_edit, 1)
        parent_row.addWidget(browse_parent)

        self.full_path_label = QLabel("")
        self.full_path_label.setWordWrap(True)
        font = QFont()
        font.setBold(True)
        self.full_path_label.setFont(font)
        self.full_path_label.setStyleSheet(
            "QLabel { background: #f0fdf4; border: 1px solid #86efac; padding: 4px; border-radius: 4px; }"
        )

        self.tree_preview = QTextEdit()
        self.tree_preview.setReadOnly(True)
        self.tree_preview.setMaximumHeight(72)
        self.tree_preview.setMinimumHeight(52)
        self.tree_preview.setStyleSheet(
            "QTextEdit { background: #fafafa; font-family: Consolas, monospace; font-size: 11px; }"
        )

        form = QFormLayout()
        form.addRow("Pasta pai (no PC)", parent_row)
        form.addRow("Nome da pasta do projeto", self.folder_name_edit)
        form.addRow("Caminho completo", self.full_path_label)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("Estrutura que sera criada:"))
        layout.addWidget(self.tree_preview)

        self.parent_edit.textChanged.connect(self._emit_changed)
        self.folder_name_edit.textChanged.connect(self._emit_changed)

        self._tree_lines: list[str] = []
        self._update_full_path_label()

    def _emit_changed(self) -> None:
        self._update_full_path_label()
        self._refresh_tree()
        self.path_changed.emit()

    def _browse_parent(self) -> None:
        from qgis.PyQt.QtWidgets import QFileDialog

        path = QFileDialog.getExistingDirectory(
            self,
            "Escolher pasta pai — aqui sera criada a pasta organizada",
            self.parent_edit.text() or str(self._default_parent),
        )
        if path:
            self.parent_edit.setText(path)

    def _update_full_path_label(self) -> None:
        self.full_path_label.setText(str(self.full_path()))

    def full_path(self) -> Path:
        parent = Path(self.parent_edit.text().strip() or str(self._default_parent))
        name = self.folder_name_edit.text().strip() or "FORTSMART_EXPORT"
        return parent / name

    def parent_path(self) -> Path:
        return Path(self.parent_edit.text().strip() or str(self._default_parent))

    def folder_name(self) -> str:
        return self.folder_name_edit.text().strip() or "FORTSMART_EXPORT"

    def set_tree_lines(self, lines: list[str]) -> None:
        self._tree_lines = lines
        self._refresh_tree()

    def _refresh_tree(self) -> None:
        root = self.full_path()
        header = f"{root}\\"
        body = "\n".join(self._tree_lines) if self._tree_lines else "  (selecione camadas/formatos)"
        self.tree_preview.setPlainText(f"{header}\n{body}")


MONITOR_FOLDER_PREVIEW: dict[str, list[str]] = {
    "john_deere_dual": [
        "Gen4/",
        "  MasterData.xml",
        "  SpatialFiles/",
        "    Boundary*.gjson",
        "    AdaptiveCurve*.gjson",
        "GS3_2630/",
        "  {Fazenda}/RCD/EIC/",
        "    setup.fds + host + global.ver",
        "    Fields/{bucket}/{uuid}/",
        "      Boundary*.fdShape + .fdData",
        "      CurveTrack*.fdShape",
        "      ImportExport / ExportOnly / WaterManagement",
    ],
    "john_deere_gs3": [
        "GS3_2630/",
        "  {Fazenda}/RCD/EIC/",
        "    setup.fds",
        "    Fields/{talhao}/",
        "      Boundary*.fdShape",
        "      CurveTrack / ABCurve",
        "      ImportExport.SpatialCatalog",
    ],
    "john_deere_gen4": [
        "Gen4/",
        "  MasterData.xml",
        "  SpatialFiles/",
        "    Boundary*.gjson",
        "    AdaptiveCurve*.gjson",
        "    ABLine*.gjson",
    ],
    "isoxml_v4": [
        "TASKDATA/",
        "  TASKDATA.XML",
        "  LINKLIST.XML",
    ],
    "isoxml_v3": [
        "TASKDATA/",
        "  TASKDATA.XML",
    ],
    "cnh": ["TASKDATA/", "  TASKDATA.XML", "  LINKLIST.XML"],
    "fendt": ["TASKDATA/", "  TASKDATA.XML", "  LINKLIST.XML"],
    "jacto": [
        "OMNI/Data/",
        "  {Fazenda}/{Talhao}/",
        "    _perimeter.shp",
        "    _runline.shp",
    ],
    "sendto2020": [
        "SendTo2020/",
        "  {Talhao}.shp (+ .dbf .shx .prj .cpg)",
        "  {Talhao}.mcd",
    ],
    "stara": [
        "DADOS/",
        "  TALHOES/{Talhao}/LIMITE.shp",
        "  GUIAS/{Talhao}/LINHAS.shp",
    ],
    "precision_planting": ["TASKDATA/", "  TASKDATA.XML"],
}


def project_export_tree_lines(
    selected_orders: list[int],
    export_gpkg: bool,
    export_shp: bool,
    export_geojson: bool,
    layer_specs: list[dict],
    export_kml: bool = False,
    unify_shp: bool = False,
) -> list[str]:
    lines: list[str] = []
    for spec in layer_specs:
        if spec["order"] not in selected_orders:
            continue
        base = spec["filename"]
        parts: list[str] = []
        if export_gpkg:
            parts.append(f"{base}.gpkg")
        if export_shp and not unify_shp:
            parts.append(f"{base}.shp (+ .dbf .shx .prj)")
        if export_geojson:
            parts.append(f"{base}.geojson")
        if parts:
            lines.append(f"  {spec['label']}")
            for p in parts:
                lines.append(f"    {p}")
    if export_kml:
        lines.append("  fortsmart_projeto.kml (todas as camadas)")
    if export_shp and unify_shp:
        lines.extend([
            "  SHP unificados por tipo de geometria:",
            "    fortsmart_poligonos.shp",
            "    fortsmart_linhas.shp",
            "    fortsmart_pontos.shp",
        ])
    lines.append("  fortsmart_project.json")
    return lines
