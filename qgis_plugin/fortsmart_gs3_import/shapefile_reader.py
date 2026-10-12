"""FortSmart — leitura de shapefiles (Jacto/Stara)."""

from __future__ import annotations

from pathlib import Path

from .gs3_spatial_types import Gs3SpatialFeature, SpatialKind


def read_shapefile_polygons(shp_path: Path) -> list[list[tuple[float, float]]]:
    rings: list[list[tuple[float, float]]] = []
    try:
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject, QgsVectorLayer

        layer = QgsVectorLayer(str(shp_path), "tmp", "ogr")
        if not layer.isValid():
            return rings
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = None
        if layer.crs().isValid() and layer.crs() != wgs84:
            transform = QgsCoordinateTransform(layer.crs(), wgs84, QgsProject.instance())
        for feat in layer.getFeatures():
            geom = feat.geometry()
            if transform:
                geom.transform(transform)
            if geom.isMultipart():
                for part in geom.asMultiPolygon():
                    if part and part[0]:
                        rings.append([(p.x(), p.y()) for p in part[0]])
            else:
                poly = geom.asPolygon()
                if poly and poly[0]:
                    rings.append([(p.x(), p.y()) for p in poly[0]])
    except Exception:
        pass
    return rings


def read_shapefile_lines(shp_path: Path) -> list[list[tuple[float, float]]]:
    lines: list[list[tuple[float, float]]] = []
    try:
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject, QgsVectorLayer

        layer = QgsVectorLayer(str(shp_path), "tmp", "ogr")
        if not layer.isValid():
            return lines
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = None
        if layer.crs().isValid() and layer.crs() != wgs84:
            transform = QgsCoordinateTransform(layer.crs(), wgs84, QgsProject.instance())
        for feat in layer.getFeatures():
            geom = feat.geometry()
            if transform:
                geom.transform(transform)
            if geom.isMultipart():
                for part in geom.asMultiPolyline():
                    if part:
                        lines.append([(p.x(), p.y()) for p in part])
            else:
                line = geom.asPolyline()
                if line:
                    lines.append([(p.x(), p.y()) for p in line])
    except Exception:
        pass
    return lines


def polygon_features(
    rings: list[list[tuple[float, float]]],
    *,
    kind: SpatialKind,
    cliente: str,
    fazenda: str,
    talhao: str,
    field_erid: str,
    name: str,
    source: str,
) -> list[Gs3SpatialFeature]:
    out: list[Gs3SpatialFeature] = []
    for i, ring in enumerate(rings, start=1):
        if len(ring) < 3:
            continue
        out.append(
            Gs3SpatialFeature(
                kind=kind,
                cliente=cliente,
                fazenda=fazenda,
                talhao=talhao,
                field_erid=field_erid,
                item_erid=f"{field_erid}-{kind.value}-{i}",
                name=name or f"{kind.value}_{i}",
                source_file=source,
                catalog_file=source,
                geometry="polygon",
                points=[],
                rings=[ring],
                format_source="shapefile",
            )
        )
    return out


def line_features(
    lines: list[list[tuple[float, float]]],
    *,
    kind: SpatialKind,
    cliente: str,
    fazenda: str,
    talhao: str,
    field_erid: str,
    name: str,
    source: str,
) -> list[Gs3SpatialFeature]:
    out: list[Gs3SpatialFeature] = []
    for i, pts in enumerate(lines, start=1):
        if len(pts) < 2:
            continue
        out.append(
            Gs3SpatialFeature(
                kind=kind,
                cliente=cliente,
                fazenda=fazenda,
                talhao=talhao,
                field_erid=field_erid,
                item_erid=f"{field_erid}-line-{i}",
                name=name or f"Guia_{i}",
                source_file=source,
                catalog_file=source,
                geometry="line",
                points=pts,
                rings=[],
                format_source="shapefile",
            )
        )
    return out
