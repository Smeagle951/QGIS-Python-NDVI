"""Dialogo e logica de importacao FortSmart (GS3 + ISOXML)."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsFillSymbol,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsSingleSymbolRenderer,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from .field_layers import add_organized_field_layers, write_bundles_geopackage
from .gs3_rcd_core import Gs3Boundary, Gs3ImportResult, collect_gs3_boundaries, collect_gs3_fields
from .gs3_spatial_types import Gs3FieldImportResult
from .jd_data_import import collect_jd_data_fields, find_companion_gen4, scan_jd_data
from .monitor_registry import read_fields
from .isoxml_import import detect_import_format

DEFAULT_FORTSMART_HOME = Path(r"C:\Users\fortu\Desktop\QGIS_Python_NDVI")

FORMAT_CHOICES = [
    ("auto", "Detectar automaticamente"),
    ("jd_data", "John Deere JD-Data (pasta log + Gen4)"),
    ("gs3", "John Deere GS3 2630 (pendrive GS3_2630)"),
    ("gen4", "John Deere Gen4 (MasterData.xml)"),
    ("isoxml", "ISOXML / ISO 11783-10 (TASKDATA.XML)"),
    ("jacto", "Jacto OMNI (OMNI/Data shapefile)"),
    ("stara", "Stara Topper (DADOS shapefile)"),
]


def fortsmart_home() -> Path:
    env = os.environ.get("FORTSMART_HOME", "").strip()
    if env:
        return Path(env)
    plugin_dir = Path(__file__).resolve().parent
    candidate = plugin_dir.parent.parent
    if (candidate / "data").exists() or (candidate / "scripts").exists():
        return candidate
    return DEFAULT_FORTSMART_HOME


OUTPUT_ROOT = fortsmart_home() / "data" / "output"


def message(title: str, text: str, icon=QMessageBox.Information) -> None:
    QMessageBox(icon, title, text).exec_()


def _count_features(result: Gs3FieldImportResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for bundle in result.bundles:
        for feature in bundle.features:
            counts[feature.kind.value] = counts.get(feature.kind.value, 0) + 1
    return counts


def _fields_without_boundary(result: Gs3FieldImportResult) -> list[str]:
    names: list[str] = []
    for bundle in result.bundles:
        if not any(f.kind.value in ("limite", "limite_interno") for f in bundle.features):
            if bundle.features:
                names.append(bundle.talhao)
    return sorted(names, key=str.casefold)


def boundaries_to_wgs84_rings(boundary: Gs3Boundary) -> list[list[QgsPointXY]]:
    return [[QgsPointXY(lon, lat) for lon, lat in boundary.points]]


def transform_rings(
    rings: list[list[QgsPointXY]],
    source_crs: QgsCoordinateReferenceSystem,
    target_crs: QgsCoordinateReferenceSystem,
) -> list[list[QgsPointXY]]:
    if source_crs == target_crs:
        return rings
    transform = QgsCoordinateTransform(source_crs, target_crs, QgsProject.instance())
    return [[transform.transform(pt) for pt in ring] for ring in rings]


def polygon_area_ha(geometry: QgsGeometry, layer_crs: QgsCoordinateReferenceSystem) -> float:
    try:
        metric = QgsCoordinateReferenceSystem("EPSG:3857")
        if layer_crs.isValid() and layer_crs.isGeographic():
            to_metric = QgsCoordinateTransform(layer_crs, metric, QgsProject.instance())
            geom = QgsGeometry(geometry)
            geom.transform(to_metric)
            return geom.area() / 10_000.0
        return geometry.area() / 10_000.0
    except Exception:
        return 0.0


def build_memory_layer(
    boundaries: list[Gs3Boundary],
    target_crs: QgsCoordinateReferenceSystem,
    layer_name: str = "FortSmart Talhoes",
) -> QgsVectorLayer:
    src_crs = QgsCoordinateReferenceSystem("EPSG:4326")
    layer = QgsVectorLayer(f"MultiPolygon?crs={target_crs.authid()}", layer_name, "memory")
    provider = layer.dataProvider()
    provider.addAttributes(
        [
            QgsField("cliente", QVariant.String),
            QgsField("fazenda", QVariant.String),
            QgsField("talhao", QVariant.String),
            QgsField("field_erid", QVariant.String),
            QgsField("boundary_erid", QVariant.String),
            QgsField("area_ha", QVariant.Double),
            QgsField("origem", QVariant.String),
        ]
    )
    layer.updateFields()

    features: list[QgsFeature] = []
    for boundary in boundaries:
        rings = transform_rings(boundaries_to_wgs84_rings(boundary), src_crs, target_crs)
        geom = QgsGeometry.fromPolygonXY(rings)
        if geom.isEmpty():
            continue
        if not geom.isGeosValid():
            geom = geom.makeValid()
        feat = QgsFeature(layer.fields())
        feat.setGeometry(geom)
        feat.setAttributes(
            [
                boundary.cliente,
                boundary.fazenda,
                boundary.talhao,
                boundary.field_erid,
                boundary.boundary_erid,
                round(polygon_area_ha(geom, target_crs), 4),
                boundary.source_file,
            ]
        )
        features.append(feat)

    if not features:
        raise RuntimeError("Nenhuma geometria valida foi gerada.")
    provider.addFeatures(features)
    layer.updateExtents()
    return layer


def apply_talhao_style(layer: QgsVectorLayer) -> None:
    symbol = QgsFillSymbol.createSimple(
        {"color": "34,139,34,90", "outline_color": "22,101,52,255", "outline_width": "0.6"}
    )
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.triggerRepaint()


def write_geopackage(layer: QgsVectorLayer, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = "talhoes"
    opts.fileEncoding = "UTF-8"
    opts.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, str(output_path), QgsProject.instance().transformContext(), opts
    )
    err_code = result[0] if isinstance(result, tuple) else result
    err_msg = result[1] if isinstance(result, tuple) and len(result) > 1 else ""
    if err_code != QgsVectorFileWriter.NoError:
        raise RuntimeError(f"Falha ao gravar GeoPackage: {err_msg or err_code}")


def add_layers_to_project(
    boundaries: list[Gs3Boundary],
    target_crs: QgsCoordinateReferenceSystem,
    group_by_farm: bool,
    save_gpkg: Path | None,
    group_name: str = "FortSmart GS3",
) -> QgsVectorLayer:
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    group = root.findGroup(group_name)
    if group is None:
        group = root.insertGroup(0, group_name)

    combined = build_memory_layer(boundaries, target_crs, f"{group_name} (todos)")
    if save_gpkg:
        write_geopackage(combined, save_gpkg)

    if not group_by_farm:
        layer = combined
        if save_gpkg:
            layer = QgsVectorLayer(f"{save_gpkg}|layername=talhoes", layer.name(), "ogr")
            if not layer.isValid():
                raise RuntimeError(f"GeoPackage invalido: {save_gpkg}")
        apply_talhao_style(layer)
        project.addMapLayer(layer, False)
        group.addLayer(layer)
        return layer

    for farm_name in sorted({b.fazenda for b in boundaries}, key=str.casefold):
        farm_bounds = [b for b in boundaries if b.fazenda == farm_name]
        farm_layer = build_memory_layer(farm_bounds, target_crs, f"Talhoes — {farm_name}")
        apply_talhao_style(farm_layer)
        project.addMapLayer(farm_layer, False)
        farm_group = group.findGroup(farm_name) or group.addGroup(farm_name)
        farm_group.addLayer(farm_layer)

    all_layer = combined
    if save_gpkg:
        file_layer = QgsVectorLayer(f"{save_gpkg}|layername=talhoes", combined.name(), "ogr")
        if file_layer.isValid():
            all_layer = file_layer
    apply_talhao_style(all_layer)
    project.addMapLayer(all_layer, False)
    group.addLayer(all_layer)
    return all_layer


class FortSmartImportDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("FortSmart — Importar dados agricolas")
        self.setMinimumWidth(620)

        self.format_combo = QComboBox()
        for key, label in FORMAT_CHOICES:
            self.format_combo.addItem(label, key)

        self.input_edit = QLineEdit("E:/GS3_2630")
        browse_in = QPushButton("Origem…")
        browse_in.clicked.connect(self._browse_input)

        self.output_edit = QLineEdit(str(OUTPUT_ROOT / "talhoes_fortsmart.gpkg"))
        browse_out = QPushButton("Salvar…")
        browse_out.clicked.connect(self._browse_output)

        self.use_project_crs = QCheckBox("Reprojetar para CRS do projeto QGIS")
        self.use_project_crs.setChecked(True)

        self.organize_by_field = QCheckBox(
            "Organizar por talhao (limite, linha AB, curva, bordadura, obstaculo...)"
        )
        self.organize_by_field.setChecked(True)

        self.group_by_farm = QCheckBox("Modo legado: uma camada por fazenda (sem subpastas por talhao)")
        self.group_by_farm.setChecked(False)

        self.unify_farms = QCheckBox("Unificar fazendas duplicadas (nomes diferentes, mesma fazenda)")
        self.unify_farms.setChecked(True)

        self.field_filter = QComboBox()
        self.field_filter.setEditable(True)
        self.field_filter.addItem("(Todos os talhoes)", "")

        self.save_gpkg = QCheckBox("Salvar GeoPackage completo (limites, curvas e marcacoes)")
        self.save_gpkg.setChecked(True)

        self.info_label = QLabel(
            "Importa GS3, Gen4, JD-Data ou ISOXML com camadas separadas por talhao.\n"
            "JD-Data: selecione a pasta JD-Data (log/*.jdl) — o plugin busca o Gen4 companheiro.\n"
            "Gen4: selecione MasterData.xml ou a pasta que tambem contem SpatialFiles."
        )
        self.info_label.setWordWrap(True)

        in_row = QHBoxLayout()
        in_row.addWidget(self.input_edit, 1)
        in_row.addWidget(browse_in)
        out_row = QHBoxLayout()
        out_row.addWidget(self.output_edit, 1)
        out_row.addWidget(browse_out)

        form = QFormLayout()
        form.addRow("Formato", self.format_combo)
        form.addRow("Pasta / arquivo origem", in_row)
        form.addRow("Filtrar talhao", self.field_filter)
        form.addRow("GeoPackage completo", out_row)
        form.addRow("", self.use_project_crs)
        form.addRow("", self.organize_by_field)
        form.addRow("", self.group_by_farm)
        form.addRow("", self.unify_farms)
        form.addRow("", self.save_gpkg)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.info_label)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self.input_edit.editingFinished.connect(self._refresh_field_choices)
        self.format_combo.currentIndexChanged.connect(self._refresh_field_choices)

    def _browse_input(self):
        fmt = self.format_combo.currentData()
        if fmt in ("isoxml", "gen4"):
            file_label = "MasterData.xml" if fmt == "gen4" else "TASKDATA.XML"
            path, _ = QFileDialog.getOpenFileName(
                self,
                f"Selecione {file_label} ou cancele para escolher a pasta",
                "",
                "XML (*.xml);;Todos (*.*)",
            )
            if not path:
                path = QFileDialog.getExistingDirectory(self, f"Ou selecione a pasta de {file_label}")
        else:
            hint = {
                "jd_data": "JD-Data (pasta com log/*.jdl)",
                "gs3": "GS3_2630",
                "jacto": "OMNI (pasta com OMNI/Data)",
                "stara": "DADOS (pasta Stara)",
            }.get(fmt, "pendrive ou pasta de dados")
            path = QFileDialog.getExistingDirectory(self, f"Selecione {hint}")
        if path:
            self.input_edit.setText(path)
            self._refresh_field_choices()

    def _refresh_field_choices(self):
        current = self.field_filter.currentData() or self.field_filter.currentText()
        self.field_filter.blockSignals(True)
        self.field_filter.clear()
        self.field_filter.addItem("(Todos os talhoes)", "")
        try:
            path = Path(self.input_edit.text().strip())
            if path.is_file() and path.name.casefold() == "masterdata.xml":
                master = path
            elif path.is_dir():
                master = path / "MasterData.xml"
                if not master.exists():
                    nested = path / "Gen4" / "MasterData.xml"
                    master = nested if nested.exists() else master
            else:
                master = None

            if master and master.exists():
                root = ET.parse(master).getroot()
                names = sorted(
                    {
                        (el.get("Name") or "").strip()
                        for el in root.iter()
                        if el.tag.split("}")[-1] == "Field" and (el.get("Name") or "").strip()
                    },
                    key=str.casefold,
                )
                for name in names:
                    self.field_filter.addItem(name, name)
            elif path.is_dir():
                gen4 = find_companion_gen4(path)
                if gen4 and (gen4 / "MasterData.xml").exists():
                    root = ET.parse(gen4 / "MasterData.xml").getroot()
                    names = sorted(
                        {
                            (el.get("Name") or "").strip()
                            for el in root.iter()
                            if el.tag.split("}")[-1] == "Field" and (el.get("Name") or "").strip()
                        },
                        key=str.casefold,
                    )
                    for name in names:
                        self.field_filter.addItem(name, name)
        except Exception:
            pass
        index = self.field_filter.findData(current)
        if index >= 0:
            self.field_filter.setCurrentIndex(index)
        self.field_filter.blockSignals(False)

    def _browse_output(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "GeoPackage de limites", self.output_edit.text(), "GeoPackage (*.gpkg)"
        )
        if path:
            if not path.lower().endswith(".gpkg"):
                path += ".gpkg"
            self.output_edit.setText(path)

    def values(self) -> dict:
        filter_data = self.field_filter.currentData()
        filter_text = self.field_filter.currentText().strip()
        field_filter = str(filter_data).strip() if filter_data else ""
        if not field_filter and filter_text != "(Todos os talhoes)":
            field_filter = filter_text
        return {
            "format": self.format_combo.currentData(),
            "input_path": Path(self.input_edit.text().strip()),
            "output_path": Path(self.output_edit.text().strip()),
            "field_filter": field_filter,
            "use_project_crs": self.use_project_crs.isChecked(),
            "organize_by_field": self.organize_by_field.isChecked(),
            "group_by_farm": self.group_by_farm.isChecked(),
            "unify_farms": self.unify_farms.isChecked(),
            "save_gpkg": self.save_gpkg.isChecked(),
        }


def _resolve_format(values: dict) -> str:
    fmt = values.get("format", "auto")
    if fmt != "auto":
        return fmt
    detected = detect_import_format(values["input_path"])
    if detected == "unknown":
        raise RuntimeError(
            "Formato nao reconhecido.\n"
            "Selecione JD-Data, GS3_2630, Gen4/MasterData.xml, TASKDATA.XML, OMNI/ (Jacto) ou DADOS/ (Stara)."
        )
    return detected


def _profile_and_group(fmt: str) -> tuple[str, str]:
    mapping = {
        "isoxml": ("isoxml_v4", "FortSmart ISOXML"),
        "gs3": ("john_deere_gs3", "FortSmart GS3"),
        "gen4": ("john_deere_gen4", "FortSmart Gen4"),
        "jd_data": ("john_deere_gen4", "FortSmart JD-Data"),
        "jacto": ("jacto", "FortSmart Jacto"),
        "stara": ("stara", "FortSmart Stara"),
    }
    return mapping.get(fmt, ("john_deere_gs3", "FortSmart GS3"))


def _import_organized(values: dict, fmt: str, target_crs: QgsCoordinateReferenceSystem):
    unify = values.get("unify_farms", True)
    profile_id, group_name = _profile_and_group(fmt)
    jd_scan = None
    if fmt == "jd_data":
        result, jd_scan = collect_jd_data_fields(values["input_path"], unify_farms=unify)
    else:
        result = read_fields(values["input_path"], profile_id=profile_id, unify_farms=unify)

    if not result.bundles:
        raise RuntimeError(f"Nenhum dado espacial encontrado em {values['input_path']}.")

    field_filter = values.get("field_filter", "").strip()
    if field_filter:
        selected = [
            bundle
            for bundle in result.bundles
            if bundle.talhao.casefold() == field_filter.casefold()
        ]
        if not selected:
            available = ", ".join(bundle.talhao for bundle in result.bundles[:12])
            raise RuntimeError(
                f"Talhao '{field_filter}' nao encontrado.\n"
                f"Disponiveis: {available}"
            )
        result.bundles = selected
        result.field_count = len(selected)

    final_layer = add_organized_field_layers(
        result.bundles,
        target_crs,
        root_group_name=group_name,
        ndvi_layer_label="Todos — Limites",
    )
    if values["save_gpkg"]:
        write_bundles_geopackage(result.bundles, values["output_path"], target_crs)
    return result, final_layer, group_name, jd_scan


def run_import(values: dict, iface) -> tuple[object, QgsVectorLayer | None, object | None]:
    target_crs = QgsProject.instance().crs()
    if not values["use_project_crs"] or not target_crs.isValid():
        target_crs = QgsCoordinateReferenceSystem("EPSG:4326")

    fmt = _resolve_format(values)
    save_path = values["output_path"] if values["save_gpkg"] else None
    jd_scan = None

    if values.get("organize_by_field", True) and not values.get("group_by_farm"):
        result, final_layer, _group, jd_scan = _import_organized(values, fmt, target_crs)
    else:
        if fmt == "isoxml":
            iso_result = read_fields(values["input_path"], profile_id="isoxml_v4", unify_farms=values.get("unify_farms", True))
            boundaries = []
            for bundle in iso_result.bundles:
                for feature in bundle.features:
                    if feature.kind.value not in ("limite", "limite_interno"):
                        continue
                    pts = feature.rings[0] if feature.rings else feature.points
                    if len(pts) < 4:
                        continue
                    boundaries.append(
                        Gs3Boundary(
                            cliente=feature.cliente,
                            fazenda=feature.fazenda,
                            talhao=feature.talhao,
                            field_erid=feature.field_erid,
                            boundary_erid=feature.item_erid,
                            source_file=feature.source_file,
                            points=pts,
                        )
                    )
            result = Gs3ImportResult(
                eic_root=iso_result.eic_root,
                catalog_count=iso_result.catalog_count,
                field_count=iso_result.field_count,
                boundaries=boundaries,
                farms_before_merge=iso_result.farms_before_merge,
                farms_after_merge=iso_result.farms_after_merge,
            )
            group_name = "FortSmart ISOXML"
        else:
            result = collect_gs3_boundaries(values["input_path"], unify_farms=values.get("unify_farms", True))
            group_name = "FortSmart GS3"

        if not result.boundaries:
            raise RuntimeError(f"Nenhum limite encontrado em {values['input_path']}.")
        final_layer = add_layers_to_project(
            result.boundaries, target_crs, values["group_by_farm"], save_path, group_name=group_name
        )

    if iface and final_layer:
        iface.setActiveLayer(final_layer)
        try:
            iface.mapCanvas().setExtent(final_layer.extent())
            iface.mapCanvas().refresh()
        except Exception:
            pass
    return result, final_layer, jd_scan


def _build_summary(result: object, values: dict, fmt: str, elapsed: float, jd_scan=None) -> str:
    if hasattr(result, "bundles"):
        counts = _count_features(result)
        fmt_labels = {
            "isoxml": "ISOXML",
            "gs3": "GS3 2630",
            "gen4": "John Deere Gen4",
            "jd_data": "John Deere JD-Data + Gen4",
            "jacto": "Jacto OMNI",
            "stara": "Stara DADOS",
        }
        summary = (
            f"Importacao FortSmart concluida em {elapsed:.1f}s\n"
            f"Formato: {fmt_labels.get(fmt, fmt)}\n\n"
        )
        if jd_scan is not None:
            summary += "\n".join(jd_scan.summary_lines) + "\n\n"
        summary += (
            f"Origem geometria: {result.eic_root}\n"
            f"Talhoes com dados: {len(result.bundles)}\n"
            f"Fazendas (antes/depois): {result.farms_before_merge} / {result.farms_after_merge}\n"
        )
        if getattr(result, "iso_version", ""):
            summary += f"Versao ISOXML: {result.iso_version}\n"
        summary += "\nItens importados:\n"
        labels = {
            "limite": "Limites",
            "limite_interno": "Limites internos",
            "linha_ab": "Linhas AB",
            "curva_ab": "Curvas AB",
            "curva_adaptada": "Curvas adaptadas",
            "bordadura": "Bordaduras",
            "trabalho": "Trabalhos / faixas",
            "marcacao": "Marcacoes",
            "obstaculo_ponto": "Obstaculos ponto",
            "obstaculo_area": "Obstaculos area",
        }
        for key, label in labels.items():
            if counts.get(key):
                summary += f"  • {label}: {counts[key]}\n"
        no_lim = _fields_without_boundary(result)
        if no_lim:
            summary += (
                f"\nTalhoes sem limite no pendrive ({len(no_lim)}): "
                + ", ".join(no_lim[:8])
                + ("..." if len(no_lim) > 8 else "")
                + "\n  (linhas/curvas foram importadas; limite nao veio no export GS3)\n"
            )
        if fmt == "gs3":
            summary += (
                "\nNota GS3: bandeiras/marcacoes/obstaculos pontuais normalmente "
                "NAO sao exportados para o pendrive.\n"
                "Para esses dados use ISOXML (TASKDATA) ou Operations Center / Apex.\n"
            )
        if fmt == "jd_data":
            summary += (
                "\nNota JD-Data: os arquivos .jdl sao logs de operacao da maquina.\n"
                "Limites, curvas e marcacoes vieram do Gen4 companheiro (nao do .jdl).\n"
            )
        summary += (
            "\nPainel: Fazenda → Talhao → camadas separadas.\n"
            "Expanda cada talhao para ver Linha AB, Curva AB, Bordadura, etc.\n"
            "Use o grupo 'Todos — Limites' para visualizar os contornos juntos."
        )
        return summary

    summary = (
        f"Importacao concluida em {elapsed:.1f}s\n\n"
        f"Limites importados: {len(result.boundaries)}\n"
        f"Fazendas: {result.farms_before_merge} / {result.farms_after_merge}\n"
    )
    return summary


def run(iface) -> None:
    dialog = FortSmartImportDialog(iface)
    if dialog.exec_() != QDialog.Accepted:
        return
    values = dialog.values()
    if not values["input_path"].exists():
        message("FortSmart", f"Origem nao encontrada:\n{values['input_path']}", QMessageBox.Warning)
        return
    try:
        started = datetime.now()
        fmt = _resolve_format(values)
        result, _layer, jd_scan = run_import(values, iface)
        elapsed = (datetime.now() - started).total_seconds()
        summary = _build_summary(result, values, fmt, elapsed, jd_scan=jd_scan)
        if values["save_gpkg"]:
            summary += f"\n\nGeoPackage: {values['output_path']}"
        message("FortSmart Import", summary, QMessageBox.Information)
    except Exception as exc:
        message("FortSmart — erro", str(exc), QMessageBox.Critical)
        raise
