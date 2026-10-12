"""FortSmart — organizacao de camadas QGIS por talhao."""

from __future__ import annotations

from qgis.PyQt.QtCore import QMetaType, QVariant
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsFillSymbol,
    QgsGeometry,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsPointXY,
    QgsProject,
    QgsSingleSymbolRenderer,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from .fortsmart_coords import sanitize_ring, sanitize_rings
from .canonical_project import ROLE_SECTIONS, spatial_kind_default_role
from .gs3_spatial_types import (
    Gs3FieldBundle,
    Gs3SpatialFeature,
    LAYER_LABELS,
    LAYER_ORDER,
    SpatialKind,
)

STYLE: dict[SpatialKind, dict[str, str]] = {
    SpatialKind.LIMITE: {"color": "34,139,34,70", "outline_color": "22,101,52,255", "outline_width": "0.8"},
    SpatialKind.LIMITE_INTERNO: {"color": "250,204,21,50", "outline_color": "202,138,4,255", "outline_width": "0.6"},
    SpatialKind.LINHA_AB: {"color": "37,99,235,255", "width": "0.9"},
    SpatialKind.CURVA_AB: {"color": "14,165,233,255", "width": "0.8"},
    SpatialKind.CURVA_ADAPTADA: {"color": "168,85,247,255", "width": "0.8"},
    SpatialKind.BORDADURA: {"color": "234,88,12,255", "width": "1.0"},
    SpatialKind.TRABALHO: {"color": "220,38,38,255", "width": "0.9"},
    SpatialKind.MARCAÇAO: {"color": "236,72,153,255", "size": "2.4"},
    SpatialKind.OBSTACULO_PONTO: {"color": "127,29,29,255", "size": "2.8"},
    SpatialKind.OBSTACULO_AREA: {"color": "239,68,68,80", "outline_color": "185,28,28,255", "outline_width": "0.6"},
    SpatialKind.OUTRO: {"color": "100,116,139,255", "width": "0.6"},
}
POLYGON_STYLE: dict[SpatialKind, dict[str, str]] = {
    SpatialKind.BORDADURA: {"color": "234,88,12,45", "outline_color": "234,88,12,255", "outline_width": "0.7"},
}


def _qgs_str_field(name: str) -> QgsField:
    try:
        return QgsField(name, QMetaType.Type.QString)
    except Exception:
        return QgsField(name, QVariant.String)


def _transform_points(
    points: list[tuple[float, float]],
    source_crs: QgsCoordinateReferenceSystem,
    target_crs: QgsCoordinateReferenceSystem,
) -> list[QgsPointXY]:
    cleaned = sanitize_ring(points, min_points=1)
    if not cleaned:
        return []
    if source_crs == target_crs:
        return [QgsPointXY(lon, lat) for lon, lat in cleaned]

    transform = QgsCoordinateTransform(
        source_crs, target_crs, QgsProject.instance().transformContext()
    )
    out: list[QgsPointXY] = []
    for lon, lat in cleaned:
        try:
            out.append(transform.transform(QgsPointXY(lon, lat)))
        except Exception:
            continue
    return out


def _feature_fields() -> list[QgsField]:
    names = (
        "cliente",
        "fazenda",
        "talhao",
        "field_erid",
        "item_erid",
        "nome",
        "tipo",
        "ativo",
        "origem",
        "fortsmart_role",
    )
    return [_qgs_str_field(n) for n in names]


def _apply_headland_offset(
    geom: QgsGeometry,
    feature: Gs3SpatialFeature,
    target_crs: QgsCoordinateReferenceSystem,
) -> QgsGeometry:
    raw = feature.extra.get("offset_m", "").strip()
    if not raw:
        return geom
    try:
        offset_m = float(raw)
    except ValueError:
        return geom
    if offset_m <= 0:
        return geom
    if target_crs.isGeographic():
        # Aproximacao: reprojetar para UTM 21S, buffer, voltar
        utm = QgsCoordinateReferenceSystem("EPSG:31981")
        if not utm.isValid():
            return geom
        transform_to = QgsCoordinateTransform(target_crs, utm, QgsProject.instance().transformContext())
        transform_back = QgsCoordinateTransform(utm, target_crs, QgsProject.instance().transformContext())
        try:
            projected = QgsGeometry(geom)
            projected.transform(transform_to)
            buffered = projected.buffer(-offset_m, 8)
            if buffered.isEmpty():
                return geom
            buffered.transform(transform_back)
            return buffered
        except Exception:
            return geom
    buffered = geom.buffer(-offset_m, 8)
    return buffered if not buffered.isEmpty() else geom


def _geometry_from_feature(
    feature: Gs3SpatialFeature,
    target_crs: QgsCoordinateReferenceSystem,
) -> QgsGeometry | None:
    src = QgsCoordinateReferenceSystem("EPSG:4326")

    if feature.rings:
        poly_rings = []
        for ring in sanitize_rings(feature.rings, min_points=3):
            pts = _transform_points(ring, src, target_crs)
            if len(pts) >= 3:
                poly_rings.append(pts)
        if not poly_rings:
            return None
        geom = QgsGeometry.fromPolygonXY(poly_rings)
    elif not feature.points:
        return None
    else:
        pts = _transform_points(feature.points, src, target_crs)
        if feature.geometry == "polygon" and len(pts) >= 3:
            geom = QgsGeometry.fromPolygonXY([pts])
        elif feature.geometry == "line" and len(pts) >= 2:
            geom = QgsGeometry.fromPolylineXY(pts)
        elif feature.geometry == "point" and pts:
            geom = QgsGeometry.fromPointXY(pts[0])
        else:
            return None

    if geom.isEmpty():
        return None
    if not geom.isGeosValid() and feature.geometry == "polygon":
        geom = geom.makeValid()
    if feature.kind == SpatialKind.BORDADURA and feature.geometry == "polygon":
        geom = _apply_headland_offset(geom, feature, target_crs)
    return geom


# O monitor guia pela reta infinita A->B; A e B costumam estar a poucos metros.
AB_EXTENSION_M = 2000
AB_EXTENSION_EXPRESSION = (
    "transform(extend(transform($geometry, @layer_crs, 'EPSG:3857'), "
    f"{AB_EXTENSION_M}, {AB_EXTENSION_M}), 'EPSG:3857', @layer_crs)"
)


def _ab_line_symbol(style: dict[str, str]) -> QgsLineSymbol:
    """Reta AB estendida (tracejada) + segmento gravado + marcadores A e B. So visual."""
    from qgis.core import (
        QgsFontMarkerSymbolLayer,
        QgsGeometryGeneratorSymbolLayer,
        QgsMarkerLineSymbolLayer,
    )

    symbol = QgsLineSymbol.createSimple(style)
    extension = QgsGeometryGeneratorSymbolLayer.create({"geometryModifier": AB_EXTENSION_EXPRESSION})
    extension.setSymbolType(Qgis.SymbolType.Line)
    extension.setSubSymbol(
        QgsLineSymbol.createSimple({"color": style["color"], "width": "0.35", "line_style": "dash"})
    )
    symbol.insertSymbolLayer(0, extension)
    for placement, letter, color in (
        (Qgis.MarkerLinePlacement.FirstVertex, "A", "22,163,74,255"),
        (Qgis.MarkerLinePlacement.LastVertex, "B", "220,38,38,255"),
    ):
        marker = QgsMarkerSymbol.createSimple({"name": "circle", "color": color, "size": "3.2"})
        text = QgsFontMarkerSymbolLayer("Arial", letter, 2.4, QColor(255, 255, 255))
        marker.appendSymbolLayer(text)
        vertex = QgsMarkerLineSymbolLayer()
        vertex.setRotateSymbols(False)
        vertex.setPlacements(Qgis.MarkerLinePlacements(placement))
        vertex.setSubSymbol(marker)
        symbol.appendSymbolLayer(vertex)
    return symbol


def _apply_style(layer: QgsVectorLayer, kind: SpatialKind) -> None:
    style = STYLE.get(kind, STYLE[SpatialKind.OUTRO])
    if layer.geometryType() == Qgis.GeometryType.Polygon:
        symbol = QgsFillSymbol.createSimple(POLYGON_STYLE.get(kind, style))
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    elif layer.geometryType() == Qgis.GeometryType.Line and kind == SpatialKind.LINHA_AB:
        try:
            symbol = _ab_line_symbol(style)
        except Exception:
            symbol = QgsLineSymbol.createSimple(style)
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    elif layer.geometryType() == Qgis.GeometryType.Line:
        symbol = QgsLineSymbol.createSimple(style)
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    else:
        symbol = QgsMarkerSymbol.createSimple(style)
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.triggerRepaint()


def _build_typed_layer(
    features: list[Gs3SpatialFeature],
    kind: SpatialKind,
    layer_name: str,
    target_crs: QgsCoordinateReferenceSystem,
) -> QgsVectorLayer | None:
    if not features:
        return None

    geometry_types = {item.geometry for item in features if item.geometry}
    if len(geometry_types) == 1:
        actual_geometry = next(iter(geometry_types))
    elif "polygon" in geometry_types:
        actual_geometry = "polygon"
    elif "line" in geometry_types:
        actual_geometry = "line"
    else:
        actual_geometry = "point"

    if actual_geometry == "polygon":
        wkb = "MultiPolygon"
    elif actual_geometry == "point":
        wkb = "Point"
    else:
        wkb = "LineString"

    layer = QgsVectorLayer(f"{wkb}?crs={target_crs.authid()}", layer_name, "memory")
    provider = layer.dataProvider()
    provider.addAttributes(_feature_fields())
    layer.updateFields()

    feature_roles = {
        str(item.extra.get("canonical_role", "") or spatial_kind_default_role(kind)).upper()
        for item in features
    }
    feature_roles.discard("")
    layer_role = next(iter(feature_roles)) if len(feature_roles) == 1 else spatial_kind_default_role(kind)
    if layer_role:
        layer.setCustomProperty("fortsmart_generated", True)
        layer.setCustomProperty("fortsmart_role", layer_role)
        layer.setCustomProperty("fortsmart_precision/role", layer_role)
        layer.setCustomProperty("fortsmart_section", ROLE_SECTIONS.get(layer_role, ""))
        layer.setCustomProperty("fortsmart_schema_version", "1")

    qgs_features: list[QgsFeature] = []
    for item in features:
        if kind == SpatialKind.BORDADURA and not item.points and not item.rings:
            continue
        geom = _geometry_from_feature(item, target_crs)
        if geom is None or geom.isEmpty():
            continue
        if wkb == "MultiPolygon" and not geom.isMultipart():
            geom.convertToMultiType()
        feat = QgsFeature(layer.fields())
        feat.setGeometry(geom)
        feat.setAttributes(
            [
                item.cliente,
                item.fazenda,
                item.talhao,
                item.field_erid,
                item.item_erid,
                item.name,
                kind.value,
                "sim" if item.active else "nao",
                item.format_source or "GS3_2630",
                str(item.extra.get("canonical_role", "") or spatial_kind_default_role(kind)).upper(),
            ]
        )
        qgs_features.append(feat)

    if not qgs_features:
        return None
    provider.addFeatures(qgs_features)
    layer.updateExtents()
    _apply_style(layer, kind)
    return layer


SPLIT_LABELS = {
    (SpatialKind.BORDADURA, "line"): "Passadas de bordadura",
}


def split_by_geometry(
    items: list[Gs3SpatialFeature], kind: SpatialKind, label: str
) -> list[tuple[str, str, list[Gs3SpatialFeature]]]:
    """Uma camada por geometria: uma camada QGIS nao mistura poligono e linha."""
    groups: dict[str, list[Gs3SpatialFeature]] = {}
    for item in items:
        groups.setdefault(item.geometry or "point", []).append(item)
    if len(groups) == 1:
        return [("", label, items)]
    suffix = {"line": "linhas", "point": "pontos", "polygon": "area"}
    return [
        (
            geometry,
            label if geometry == "polygon" else SPLIT_LABELS.get((kind, geometry), f"{label} ({suffix[geometry]})"),
            group,
        )
        for geometry, group in sorted(groups.items(), key=lambda pair: ("polygon", "line", "point").index(pair[0]))
    ]


def _safe_group_name(name: str) -> str:
    text = " ".join((name or "Sem nome").split())
    return text[:80] if text else "Sem nome"


def add_organized_field_layers(
    bundles: list[Gs3FieldBundle],
    target_crs: QgsCoordinateReferenceSystem,
    *,
    root_group_name: str = "FortSmart GS3",
    ndvi_layer_label: str = "Todos — Limites (NDVI)",
) -> QgsVectorLayer | None:
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    root_group = root.findGroup(root_group_name)
    if root_group is None:
        root_group = root.insertGroup(0, root_group_name)

    all_boundaries: list[Gs3SpatialFeature] = []
    ndvi_layer: QgsVectorLayer | None = None
    first_layer: QgsVectorLayer | None = None

    farms: dict[str, list[Gs3FieldBundle]] = {}
    for bundle in bundles:
        farms.setdefault(bundle.fazenda, []).append(bundle)

    for farm_name in sorted(farms.keys(), key=str.casefold):
        farm_group = root_group.findGroup(_safe_group_name(farm_name))
        if farm_group is None:
            farm_group = root_group.addGroup(_safe_group_name(farm_name))

        for bundle in sorted(farms[farm_name], key=lambda b: b.talhao.casefold()):
            talhao_label = _safe_group_name(bundle.talhao)
            field_group = farm_group.findGroup(talhao_label)
            if field_group is None:
                field_group = farm_group.addGroup(talhao_label)

            for kind in LAYER_ORDER:
                items = bundle.by_kind(kind)
                if not items:
                    continue
                if kind in (SpatialKind.LIMITE, SpatialKind.LIMITE_INTERNO):
                    all_boundaries.extend(items)
                for _geometry, label, part in split_by_geometry(items, kind, LAYER_LABELS[kind]):
                    count_suffix = f" ({len(part)})" if len(part) > 1 else ""
                    layer = _build_typed_layer(part, kind, f"{label}{count_suffix}", target_crs)
                    if layer is None:
                        continue
                    project.addMapLayer(layer, False)
                    field_group.addLayer(layer)
                    if first_layer is None:
                        first_layer = layer
                field_group.setExpanded(True)

    if all_boundaries:
        ndvi_layer = _build_typed_layer(all_boundaries, SpatialKind.LIMITE, ndvi_layer_label, target_crs)
        if ndvi_layer:
            project.addMapLayer(ndvi_layer, False)
            root_group.insertLayer(0, ndvi_layer)

    root_group.setExpanded(True)
    return ndvi_layer or first_layer


def write_bundles_geopackage(
    bundles: list[Gs3FieldBundle],
    output_path,
    target_crs: QgsCoordinateReferenceSystem,
) -> list[str]:
    """Grava todos os tipos espaciais em camadas separadas de um único GPKG."""
    from pathlib import Path

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    first = True
    for kind in LAYER_ORDER:
        items = [
            feature
            for bundle in bundles
            for feature in bundle.features
            if feature.kind == kind
        ]
        for geometry, _label, part in split_by_geometry(items, kind, kind.value):
            layer_name = f"{kind.value}_{geometry}" if geometry else kind.value
            layer = _build_typed_layer(part, kind, layer_name, target_crs)
            if layer is None:
                continue
            _write_gpkg_layer(layer, layer_name, path, first)
            written.append(layer_name)
            first = False

    if not written:
        raise RuntimeError("Nenhuma geometria válida foi gravada no GeoPackage.")
    return written


def _write_gpkg_layer(layer: QgsVectorLayer, layer_name: str, path, first: bool) -> None:
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = layer_name
    opts.fileEncoding = "UTF-8"
    opts.actionOnExistingFile = (
        QgsVectorFileWriter.CreateOrOverwriteFile
        if first
        else QgsVectorFileWriter.CreateOrOverwriteLayer
    )
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer,
        str(path),
        QgsProject.instance().transformContext(),
        opts,
    )
    err = result[0] if isinstance(result, tuple) else result
    if err != QgsVectorFileWriter.NoError:
        message = result[1] if isinstance(result, tuple) and len(result) > 1 else str(err)
        raise RuntimeError(f"Falha ao gravar camada {layer_name}: {message}")


def bundles_to_boundary_layer(
    bundles: list[Gs3FieldBundle],
    target_crs: QgsCoordinateReferenceSystem,
    layer_name: str = "FortSmart Talhoes GS3 (todos)",
) -> QgsVectorLayer:
    boundaries: list[Gs3SpatialFeature] = []
    for bundle in bundles:
        boundaries.extend(bundle.by_kind(SpatialKind.LIMITE))
        boundaries.extend(bundle.by_kind(SpatialKind.LIMITE_INTERNO))
    layer = _build_typed_layer(boundaries, SpatialKind.LIMITE, layer_name, target_crs)
    if layer is None:
        raise RuntimeError("Nenhuma geometria valida foi gerada a partir do GS3_2630.")
    return layer
