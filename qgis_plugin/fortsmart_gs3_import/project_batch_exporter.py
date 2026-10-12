"""FortSmart — exportacao em lote padronizada do projeto QGIS (GPKG + SHP)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .qgis_layer_source import (
    LayerSourceMode,
    get_all_vector_layers,
    find_layer_by_exact_spec,
    find_layers_for_spec,
    merge_vector_layers,
    resolve_source_layers,
    sanitize_filename,
)

EXPORT_LAYERS: list[dict] = [
    {
        "order": 1,
        "qgis_name": "01_Limite_Externo",
        "label": "01 Limite Externo",
        "filename": "01_limite_externo",
        "source": "external_boundary",
        "role": "EXTERNAL_BOUNDARY",
        "default_export": True,
    },
    {
        "order": 2,
        "qgis_name": "02_Bordadura",
        "label": "02 Bordadura",
        "filename": "02_bordadura",
        "source": "headland",
        "role": "HEADLAND",
        "default_export": True,
    },
    {
        "order": 3,
        "qgis_name": "03_Limite_Interno",
        "label": "03 Limite Interno",
        "filename": "03_limite_interno",
        "source": "inner_boundary",
        "role": "INTERNAL_BOUNDARY",
        "default_export": True,
    },
    {
        "order": 4,
        "qgis_name": "04_Linha_AB",
        "label": "04 Linha A-B",
        "filename": "04_linha_ab",
        "source": "ab_line",
        "role": "AB_LINE",
        "default_export": True,
    },
    {
        "order": 5,
        "qgis_name": "05_Passadas",
        "label": "05 Passadas",
        "filename": "05_passadas",
        "source": "spraying_passes",
        "role": "GUIDANCE_TRACK",
        "default_export": True,
    },
    {
        "order": 6,
        "qgis_name": "06_Linhas_Plantio",
        "label": "06 Linhas Plantio",
        "filename": "06_linhas_plantio",
        "source": "planting_rows",
        "role": "WORK_PATH",
        "default_export": False,
    },
    {
        "order": 7,
        "qgis_name": "07_Linha_Trator",
        "label": "07 Linha Trator",
        "filename": "07_linha_trator",
        "source": "tractor_paths",
        "role": "TRACTOR_PATH",
        "default_export": True,
    },
    {
        "order": 8,
        "qgis_name": "08_Rastros_Pneus",
        "label": "08 Rastros Pneus",
        "filename": "08_rastros_pneus",
        "source": "tire_tracks",
        "role": "TIRE_TRACK",
        "default_export": True,
    },
    {
        "order": 9,
        "qgis_name": "09_Obstaculos_RTK",
        "label": "09 Obstaculos RTK",
        "filename": "09_obstaculos_rtk",
        "source": "obstacles",
        "role": "OBSTACLE",
        "default_export": True,
    },
    {
        "order": 10,
        "qgis_name": "10_Zonas_Seguranca",
        "label": "10 Zonas Seguranca",
        "filename": "10_zonas_seguranca",
        "source": "safety_zones",
        "role": "SAFETY_ZONE",
        "default_export": True,
    },
    {
        "order": 11,
        "qgis_name": "11_Desvios_Obstaculos",
        "label": "11 Desvios Obstaculos",
        "filename": "11_desvios_obstaculos",
        "source": "obstacle_deviations",
        "role": "OBSTACLE_DETOUR",
        "default_export": True,
    },
    {
        "order": 12,
        "qgis_name": "12_Linhas_Finais",
        "label": "12 Linhas Finais",
        "filename": "12_linhas_finais",
        "source": "final_paths",
        "role": "FINAL_WORK_PATH",
        "default_export": True,
    },
    {
        "order": 13,
        "qgis_name": "13_Shutoff",
        "label": "13 Shutoff",
        "filename": "13_shutoff",
        "source": "shutoff",
        "role": "SHUTOFF",
        "default_export": True,
    },
]


def default_project_folder_name(when: date | None = None) -> str:
    d = when or date.today()
    return f"FORTSMART_PROJETO_{d.year}_{d.month:02d}_{d.day:02d}"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").casefold())


@dataclass
class LayerValidation:
    spec: dict
    layer_name: str | None = None
    layer_id: str | None = None
    matched_layer_ids: list[str] = field(default_factory=list)
    feature_count: int = 0
    found: bool = False
    crs_authid: str = ""
    geometry_type: str = ""

    @property
    def ok(self) -> bool:
        return self.found and self.feature_count > 0

    def status_line(self) -> str:
        mark = "OK" if self.ok else ("--" if not self.found else "0")
        name = self.layer_name or "(nao encontrada)"
        if self.ok:
            n = self.feature_count
            feat = "feicao" if n == 1 else "feicoes"
            return f"OK  {self.spec['label']:<24} {n} {feat}  <- {name}"
        if not self.found:
            return f"--  {self.spec['label']:<24} sem camada no QGIS"
        return f"!!  {self.spec['label']:<24} 0 feicoes  ({name})"


@dataclass
class ProjectValidation:
    layers: list[LayerValidation] = field(default_factory=list)
    project_crs: str = ""
    units: str = ""

    @property
    def exportable_count(self) -> int:
        return sum(1 for lv in self.layers if lv.ok)

    def summary_text(self) -> str:
        lines = [
            "VALIDACAO DO PROJETO",
            "--------------------------------",
            "",
        ]
        for lv in self.layers:
            lines.append(lv.status_line())
        lines.extend(
            [
                "",
                f"PROJECAO: {self.project_crs or 'N/A'}",
                f"UNIDADE: {self.units or 'N/A'}",
                "",
                f"Camadas prontas para exportar: {self.exportable_count}/{len(self.layers)}",
            ]
        )
        return "\n".join(lines)


@dataclass
class ExportResult:
    output_dir: Path
    exported_files: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def find_layer_by_spec(spec: dict, candidates=None):
    from .qgis_layer_source import get_all_vector_layers

    pool = candidates if candidates is not None else get_all_vector_layers()
    return find_layer_by_exact_spec(spec, pool)


def resolve_layers_for_spec(
    spec: dict,
    source_mode: LayerSourceMode,
    candidates=None,
) -> tuple[list, str]:
    """Retorna (lista de camadas QGIS, descricao do mapeamento)."""
    pool = candidates if candidates is not None else resolve_source_layers(source_mode)

    if source_mode == LayerSourceMode.EXACT_NAMES:
        layer = find_layer_by_exact_spec(spec, pool)
        if layer is None:
            return [], ""
        return [layer], layer.name()

    if source_mode == LayerSourceMode.AUTO_MAP:
        matched = find_layers_for_spec(spec, pool)
        if not matched:
            return [], ""
        names = ", ".join(l.name() for l in matched)
        return matched, names

    layer = find_layer_by_exact_spec(spec, pool)
    if layer is None:
        return [], ""
    return [layer], layer.name()


def validate_project(
    selected_orders: list[int] | None = None,
    *,
    source_mode: LayerSourceMode = LayerSourceMode.VISIBLE,
    scope_mode: LayerSourceMode | None = None,
    selected_layer_ids: set[str] | None = None,
) -> ProjectValidation:
    from qgis.core import Qgis, QgsProject

    project = QgsProject.instance()
    crs = project.crs()
    units = "metros"
    if crs.isValid():
        if crs.isGeographic():
            units = "graus"
        elif crs.mapUnits() == Qgis.DistanceUnit.Meters:
            units = "metros"
        else:
            units = str(crs.mapUnits())

    result = ProjectValidation(project_crs=crs.authid() if crs.isValid() else "", units=units)
    scope = scope_mode or (
        source_mode
        if source_mode in (LayerSourceMode.VISIBLE, LayerSourceMode.SELECTED)
        else LayerSourceMode.VISIBLE
    )
    if source_mode == LayerSourceMode.EXACT_NAMES:
        candidates = get_all_vector_layers()
    else:
        candidates = resolve_source_layers(scope)
    if selected_layer_ids is not None:
        candidates = [layer for layer in candidates if layer.id() in selected_layer_ids]

    for spec in EXPORT_LAYERS:
        if selected_orders is not None and spec["order"] not in selected_orders:
            continue
        matched, map_desc = resolve_layers_for_spec(spec, source_mode, candidates)
        lv = LayerValidation(spec=spec)
        if not matched:
            result.layers.append(lv)
            continue
        layer = merge_vector_layers(matched, spec["filename"]) if len(matched) > 1 else matched[0]
        if layer is None:
            result.layers.append(lv)
            continue
        lv.found = True
        lv.layer_name = map_desc or layer.name()
        lv.matched_layer_ids = [l.id() for l in matched]
        lv.feature_count = layer.featureCount()
        lv.crs_authid = layer.crs().authid() if layer.crs().isValid() else ""
        gtype = layer.geometryType()
        lv.geometry_type = {0: "Ponto", 1: "Linha", 2: "Poligono"}.get(int(gtype), str(gtype))
        result.layers.append(lv)

    return result


def _write_vector(layer, dest_path: Path, driver: str, layer_name: str) -> None:
    from qgis.core import QgsProject, QgsVectorFileWriter

    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = driver
    opts.layerName = layer_name
    opts.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile

    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer,
        str(dest_path),
        QgsProject.instance().transformContext(),
        opts,
    )
    err = result[0] if isinstance(result, tuple) else result
    if err != QgsVectorFileWriter.NoError:
        msg = result[1] if isinstance(result, tuple) and len(result) > 1 else str(err)
        raise RuntimeError(f"Falha ao gravar {dest_path.name}: {msg}")


def _with_export_field(layer, field_name: str | None):
    """Add/replace talhao in an in-memory copy; never edit source layers."""
    if not field_name:
        return layer
    from qgis.PyQt.QtCore import QVariant
    from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsVectorLayer, QgsWkbTypes

    geometry = QgsWkbTypes.displayString(layer.wkbType())
    crs = layer.crs().authid()
    if not crs:
        raise ValueError(f"Camada {layer.name()} sem CRS valido.")
    copy = QgsVectorLayer(f"{geometry}?crs={crs}", layer.name(), "memory")
    if not copy.isValid():
        raise ValueError(f"Nao foi possivel preparar a camada {layer.name()} para exportacao.")
    fields = list(layer.fields())
    names = {field.name().casefold(): i for i, field in enumerate(fields)}
    if "talhao" not in names:
        fields.append(QgsField("talhao", QVariant.String))
    copy.dataProvider().addAttributes(fields)
    copy.updateFields()
    output = []
    for feature in layer.getFeatures():
        new_feature = QgsFeature(copy.fields())
        new_feature.setGeometry(QgsGeometry(feature.geometry()))
        values = list(feature.attributes())
        if "talhao" in names:
            values[names["talhao"]] = field_name
        else:
            values.append(field_name)
        new_feature.setAttributes(values)
        output.append(new_feature)
    copy.dataProvider().addFeatures(output)
    copy.updateExtents()
    return copy


def _write_unified_shapefiles(layers: list, output_dir: Path) -> tuple[list[str], list[str]]:
    """Une camadas por tipo geométrico; SHP não suporta geometrias mistas."""
    from qgis.PyQt.QtCore import QVariant
    from qgis.core import (
        QgsCoordinateTransform, QgsDistanceArea, QgsFeature, QgsField, QgsGeometry,
        QgsProject, QgsUnitTypes, QgsVectorLayer, QgsWkbTypes,
    )

    type_info = {
        0: ("MultiPoint", "fortsmart_pontos"),
        1: ("MultiLineString", "fortsmart_linhas"),
        2: ("MultiPolygon", "fortsmart_poligonos"),
    }
    grouped: dict[int, list] = {0: [], 1: [], 2: []}
    for layer in layers:
        key = int(layer.geometryType())
        if key in grouped and layer.featureCount() > 0:
            grouped[key].append(layer)

    files: list[str] = []
    warnings: list[str] = []
    project = QgsProject.instance()
    for key, source_layers in grouped.items():
        if not source_layers:
            continue
        ref = next((layer for layer in source_layers if layer.crs().isValid()), None)
        if ref is None:
            warnings.append(f"SHP unificado {type_info[key][1]}: nenhuma camada possui CRS válido")
            continue
        geometry_name, base = type_info[key]
        memory = QgsVectorLayer(f"{geometry_name}?crs={ref.crs().authid()}", base, "memory")
        provider = memory.dataProvider()
        provider.addAttributes([
            QgsField("camada", QVariant.String, len=120),
            QgsField("nome", QVariant.String, len=160),
            QgsField("id_origem", QVariant.LongLong),
            QgsField("area_ha", QVariant.Double, len=20, prec=6),
        ])
        memory.updateFields()
        output_features = []
        for layer in source_layers:
            if not layer.crs().isValid():
                warnings.append(f"SHP unificado ignorou {layer.name()}: CRS inválido")
                continue
            transform = None
            if layer.crs() != ref.crs():
                transform = QgsCoordinateTransform(layer.crs(), ref.crs(), project)
            measure = QgsDistanceArea()
            measure.setSourceCrs(layer.crs(), project.transformContext())
            ellipsoid = project.ellipsoid()
            measure.setEllipsoid(ellipsoid if ellipsoid and ellipsoid != "NONE" else "WGS84")
            field_lookup = {field.name().casefold(): field.name() for field in layer.fields()}
            for feature in layer.getFeatures():
                geom = QgsGeometry(feature.geometry())
                if geom.isNull() or geom.isEmpty():
                    warnings.append(f"SHP unificado ignorou {layer.name()} / feição {feature.id()}: sem geometria")
                    continue
                try:
                    if transform and geom.transform(transform) != 0:
                        raise ValueError("falha na transformação de CRS")
                    if not QgsWkbTypes.isMultiType(geom.wkbType()):
                        geom.convertToMultiType()
                    name = ""
                    for candidate in ("nome", "name", "nome_talhao", "nome_pivo", "talhao", "pivo", "label"):
                        field_name = field_lookup.get(candidate)
                        if field_name and feature[field_name] not in (None, ""):
                            name = str(feature[field_name]).strip()
                            if name:
                                break
                    if not name:
                        name = layer.name() if layer.featureCount() == 1 else f"{layer.name()} — {feature.id()}"
                    area = None
                    if key == 2:
                        area = measure.convertAreaMeasurement(measure.measureArea(feature.geometry()), QgsUnitTypes.AreaHectares)
                    merged = QgsFeature(memory.fields())
                    merged.setGeometry(geom)
                    merged.setAttributes([layer.name(), name, feature.id(), area])
                    output_features.append(merged)
                except Exception as exc:
                    warnings.append(f"SHP unificado ignorou {layer.name()} / feição {feature.id()}: {exc}")
        if not output_features:
            warnings.append(f"SHP unificado {base}: nenhuma feição válida")
            continue
        provider.addFeatures(output_features)
        memory.updateExtents()
        shp_path = output_dir / f"{base}.shp"
        _write_vector(memory, shp_path, "ESRI Shapefile", base)
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            side = output_dir / f"{base}{ext}"
            if side.exists():
                files.append(side.name)
    return files, warnings


def _layer_from_validation(lv: LayerValidation):
    from qgis.core import QgsProject

    if not lv.matched_layer_ids:
        return None
    layers = []
    for lid in lv.matched_layer_ids:
        layer = QgsProject.instance().mapLayer(lid)
        if layer is not None:
            layers.append(layer)
    if not layers:
        return None
    spec = lv.spec
    return merge_vector_layers(layers, spec["filename"]) if len(layers) > 1 else layers[0]


def export_project_batch(
    output_dir: Path,
    *,
    export_gpkg: bool = True,
    export_shp: bool = True,
    shp_mode: str = "separate",
    export_geojson: bool = False,
    export_kml: bool = False,
    selected_orders: list[int] | None = None,
    source_mode: LayerSourceMode = LayerSourceMode.VISIBLE,
    scope_mode: LayerSourceMode | None = None,
    write_manifest: bool = True,
    selected_layer_ids: set[str] | None = None,
    field_name: str | None = None,
) -> ExportResult:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    validation = validate_project(
        selected_orders, source_mode=source_mode, scope_mode=scope_mode,
        selected_layer_ids=selected_layer_ids,
    )
    result = ExportResult(output_dir=output_dir)

    exported_layers_meta: list[dict] = []
    kml_layers: list = []
    shp_layers: list = []

    for lv in validation.layers:
        spec = lv.spec
        if not lv.ok:
            if lv.found and lv.feature_count == 0:
                result.skipped.append(f"{spec['filename']} (vazia)")
            elif not lv.found:
                result.skipped.append(f"{spec['filename']} (sem camada no QGIS)")
            continue

        layer = _layer_from_validation(lv)
        if layer is None:
            result.errors.append(f"{spec['filename']}: camada removida do projeto")
            continue
        layer = _with_export_field(layer, field_name)

        base = spec["filename"]
        layer_meta: dict = {
            "order": spec["order"],
            "source": spec["source"],
            "qgis_layers": lv.layer_name,
            "qgis_layer_ids": lv.matched_layer_ids,
            "filename_base": base,
            "feature_count": lv.feature_count,
            "geometry_type": lv.geometry_type,
            "crs": lv.crs_authid,
            "files": [],
        }

        try:
            if export_gpkg:
                gpkg_path = output_dir / f"{base}.gpkg"
                _write_vector(layer, gpkg_path, "GPKG", base)
                result.exported_files.append(gpkg_path.name)
                layer_meta["files"].append(gpkg_path.name)

            if export_shp and shp_mode == "separate":
                shp_path = output_dir / f"{base}.shp"
                _write_vector(layer, shp_path, "ESRI Shapefile", base)
                for ext in (".shx", ".dbf", ".prj", ".cpg"):
                    side = output_dir / f"{base}{ext}"
                    if side.exists():
                        result.exported_files.append(side.name)
                if shp_path.exists():
                    result.exported_files.append(shp_path.name)
                    layer_meta["files"].append(shp_path.name)

            if export_shp and shp_mode == "unified":
                shp_layers.append(layer)
                layer_meta["files"].append("fortsmart_[tipo].shp")

            if export_geojson:
                geo_path = output_dir / f"{base}.geojson"
                _write_vector(layer, geo_path, "GeoJSON", base)
                result.exported_files.append(geo_path.name)
                layer_meta["files"].append(geo_path.name)

            if export_kml:
                kml_layers.append(layer)
                layer_meta["files"].append("fortsmart_projeto.kml")

            exported_layers_meta.append(layer_meta)
        except Exception as exc:
            result.errors.append(f"{base}: {exc}")

    if export_kml and kml_layers:
        try:
            from .kml_exporter import write_kml_layers
            kml_path = output_dir / "fortsmart_projeto.kml"
            kml_warnings = write_kml_layers(kml_layers, kml_path, strict=False)
            result.exported_files.append(kml_path.name)
            result.errors.extend(f"KML ignorou {warning}" for warning in kml_warnings)
        except Exception as exc:
            result.errors.append(f"fortsmart_projeto.kml: {exc}")

    if export_shp and shp_mode == "unified" and shp_layers:
        try:
            shp_files, shp_warnings = _write_unified_shapefiles(shp_layers, output_dir)
            result.exported_files.extend(shp_files)
            result.errors.extend(shp_warnings)
        except Exception as exc:
            result.errors.append(f"Shapefile unificado: {exc}")

    if write_manifest:
        manifest = {
            "format": "fortsmart_project_v1",
            "exported_at": date.today().isoformat(),
            "folder": output_dir.name,
            "projection": validation.project_crs,
            "units": validation.units,
            "source_mode": source_mode.value,
            "talhao": field_name,
            "formats": {
                "geopackage": export_gpkg,
                "shapefile": export_shp,
                "shapefile_mode": shp_mode,
                "geojson": export_geojson,
                "kml": export_kml,
            },
            "layers": exported_layers_meta,
            "skipped": result.skipped,
            "errors": result.errors,
            "pipeline": {
                "operational": [
                    "05_passadas",
                    "07_linha_trator",
                    "08_rastros_pneus",
                    "13_shutoff",
                ],
                "optional_internal": ["06_linhas_plantio"],
                "note": "12_linhas_finais deve conter geometria desviada (preview), nao linha original.",
            },
        }
        manifest_path = output_dir / "fortsmart_project.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        result.exported_files.append(manifest_path.name)

    return result


def export_active_layers_direct(
    output_dir: Path,
    *,
    source_mode: LayerSourceMode = LayerSourceMode.VISIBLE,
    export_gpkg: bool = True,
    export_shp: bool = True,
    shp_mode: str = "separate",
    export_geojson: bool = False,
    export_kml: bool = False,
    write_manifest: bool = True,
    selected_layer_ids: set[str] | None = None,
    field_name: str | None = None,
) -> ExportResult:
    """Exporta cada camada vetorial ativa do QGIS com o nome da camada (sem renomear 01-13)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    from qgis.core import QgsProject

    layers = resolve_source_layers(source_mode)
    if selected_layer_ids is not None:
        layers = [layer for layer in layers if layer.id() in selected_layer_ids]
    result = ExportResult(output_dir=output_dir)
    exported_layers_meta: list[dict] = []
    kml_layers: list = []
    shp_layers: list = []
    used_names: set[str] = set()

    crs = QgsProject.instance().crs()
    validation_stub = ProjectValidation(
        project_crs=crs.authid() if crs.isValid() else "",
        units="metros",
    )

    for layer in sorted(layers, key=lambda l: l.name().casefold()):
        if layer.featureCount() == 0:
            result.skipped.append(f"{layer.name()} (vazia)")
            continue
        layer = _with_export_field(layer, field_name)

        base = sanitize_filename(layer.name())
        suffix = 2
        while base in used_names:
            base = f"{sanitize_filename(layer.name())}_{suffix}"
            suffix += 1
        used_names.add(base)

        layer_meta: dict = {
            "qgis_layer": layer.name(),
            "qgis_layer_id": layer.id(),
            "filename_base": base,
            "feature_count": layer.featureCount(),
            "files": [],
        }

        try:
            if export_gpkg:
                gpkg_path = output_dir / f"{base}.gpkg"
                _write_vector(layer, gpkg_path, "GPKG", base)
                result.exported_files.append(gpkg_path.name)
                layer_meta["files"].append(gpkg_path.name)

            if export_shp and shp_mode == "separate":
                shp_path = output_dir / f"{base}.shp"
                _write_vector(layer, shp_path, "ESRI Shapefile", base)
                for ext in (".shx", ".dbf", ".prj", ".cpg"):
                    side = output_dir / f"{base}{ext}"
                    if side.exists():
                        result.exported_files.append(side.name)
                if shp_path.exists():
                    result.exported_files.append(shp_path.name)
                    layer_meta["files"].append(shp_path.name)

            if export_shp and shp_mode == "unified":
                shp_layers.append(layer)
                layer_meta["files"].append("fortsmart_[tipo].shp")

            if export_geojson:
                geo_path = output_dir / f"{base}.geojson"
                _write_vector(layer, geo_path, "GeoJSON", base)
                result.exported_files.append(geo_path.name)
                layer_meta["files"].append(geo_path.name)

            if export_kml:
                kml_layers.append(layer)
                layer_meta["files"].append("fortsmart_projeto.kml")

            exported_layers_meta.append(layer_meta)
        except Exception as exc:
            result.errors.append(f"{layer.name()}: {exc}")

    if export_kml and kml_layers:
        try:
            from .kml_exporter import write_kml_layers
            kml_path = output_dir / "fortsmart_projeto.kml"
            kml_warnings = write_kml_layers(kml_layers, kml_path, strict=False)
            result.exported_files.append(kml_path.name)
            result.errors.extend(f"KML ignorou {warning}" for warning in kml_warnings)
        except Exception as exc:
            result.errors.append(f"fortsmart_projeto.kml: {exc}")

    if export_shp and shp_mode == "unified" and shp_layers:
        try:
            shp_files, shp_warnings = _write_unified_shapefiles(shp_layers, output_dir)
            result.exported_files.extend(shp_files)
            result.errors.extend(shp_warnings)
        except Exception as exc:
            result.errors.append(f"Shapefile unificado: {exc}")

    if write_manifest:
        manifest = {
            "format": "fortsmart_qgis_layers_v1",
            "formats": {"geopackage": export_gpkg, "shapefile": export_shp, "shapefile_mode": shp_mode, "geojson": export_geojson, "kml": export_kml},
            "exported_at": date.today().isoformat(),
            "folder": output_dir.name,
            "source_mode": source_mode.value,
            "talhao": field_name,
            "projection": validation_stub.project_crs,
            "layers": exported_layers_meta,
            "skipped": result.skipped,
            "errors": result.errors,
        }
        manifest_path = output_dir / "fortsmart_project.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        result.exported_files.append(manifest_path.name)

    return result
