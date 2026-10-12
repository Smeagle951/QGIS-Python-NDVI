"""Geometry preparation used only by the John Deere GS3 adapter."""

from __future__ import annotations

from dataclasses import dataclass
from math import floor


@dataclass(frozen=True)
class Gs3BoundaryConfig:
    warning_vertices: int = 10_000
    target_vertices: int = 5_000
    max_vertices: int = 20_000
    max_area_difference_percent: float = 0.5
    tolerances_m: tuple[float, ...] = (
        0.05, 0.10, 0.25, 0.50, 1.0, 2.0, 3.0, 5.0,
        8.0, 10.0, 15.0, 20.0, 30.0, 50.0,
    )


@dataclass(frozen=True)
class PreparedGs3Boundary:
    rings: list[list[tuple[float, float]]]
    original_vertices: int
    export_vertices: int
    original_area_ha: float
    export_area_ha: float
    area_difference_percent: float
    tolerance_m: float
    metric_crs: str


def _dedupe_ring(ring: list[tuple[float, float]]) -> list[tuple[float, float]]:
    clean: list[tuple[float, float]] = []
    for point in ring:
        value = (float(point[0]), float(point[1]))
        if not clean or value != clean[-1]:
            clean.append(value)
    if clean and clean[0] != clean[-1]:
        clean.append(clean[0])
    return clean


def _utm_epsg(lon: float, lat: float) -> int:
    zone = max(1, min(60, int(floor((lon + 180.0) / 6.0)) + 1))
    return (32600 if lat >= 0 else 32700) + zone


def _polygon_from_rings(rings):
    from shapely.geometry import Polygon
    from shapely.validation import make_valid

    cleaned = [_dedupe_ring(list(ring)) for ring in rings if len(ring) >= 4]
    if not cleaned:
        raise ValueError("GS3_BOUNDARY_INVALID: anel externo ausente.")
    polygon = Polygon(cleaned[0], cleaned[1:])
    if not polygon.is_valid:
        polygon = make_valid(polygon)
    if polygon.geom_type == "MultiPolygon":
        polygon = max(polygon.geoms, key=lambda geom: geom.area)
    if polygon.geom_type != "Polygon" or polygon.is_empty:
        raise ValueError("GS3_BOUNDARY_INVALID: geometria nao resulta em poligono valido.")
    return polygon


def _rings_from_polygon(polygon) -> list[list[tuple[float, float]]]:
    rings = [[(float(x), float(y)) for x, y in polygon.exterior.coords]]
    rings.extend(
        [(float(x), float(y)) for x, y in interior.coords]
        for interior in polygon.interiors
    )
    return rings


def _metric_transformers(lon: float, lat: float):
    from pyproj import CRS, Transformer

    metric_crs = CRS.from_epsg(_utm_epsg(lon, lat))
    forward = Transformer.from_crs("EPSG:4326", metric_crs, always_xy=True)
    reverse = Transformer.from_crs(metric_crs, "EPSG:4326", always_xy=True)
    return forward, reverse


def ring_area_ha(ring: list[tuple[float, float]]) -> float:
    """Metric (UTM) area of a single WGS84 ring, in hectares."""
    from shapely.geometry import Polygon
    from shapely.ops import transform

    clean = _dedupe_ring(list(ring))
    if len(clean) < 4:
        return 0.0
    polygon = Polygon(clean)
    centroid = polygon.centroid
    forward, _ = _metric_transformers(centroid.x, centroid.y)
    return float(abs(transform(forward.transform, polygon).area) / 10_000.0)


def inward_offset_ring(
    ring: list[tuple[float, float]],
    offset_m: float,
) -> list[tuple[float, float]]:
    """Exterior ring shrunk by ``offset_m`` metres (largest resulting part)."""
    from shapely.geometry import Polygon
    from shapely.ops import transform

    clean = _dedupe_ring(list(ring))
    if len(clean) < 4 or offset_m <= 0:
        return []
    polygon = Polygon(clean)
    centroid = polygon.centroid
    forward, reverse = _metric_transformers(centroid.x, centroid.y)
    metric = transform(forward.transform, polygon).buffer(0)
    shrunk = metric.buffer(-float(offset_m), join_style=2)
    if shrunk.is_empty:
        return []
    if shrunk.geom_type == "MultiPolygon":
        shrunk = max(shrunk.geoms, key=lambda geom: geom.area)
    if shrunk.geom_type != "Polygon":
        return []
    wgs84 = transform(reverse.transform, shrunk)
    return [(float(x), float(y)) for x, y in wgs84.exterior.coords]


def prepare_gs3_boundary_geometry(
    rings: list[list[tuple[float, float]]],
    *,
    config: Gs3BoundaryConfig | None = None,
) -> PreparedGs3Boundary:
    """Return a metric-validated, optionally simplified GS3 boundary copy."""
    from pyproj import CRS, Transformer
    from shapely.ops import transform

    cfg = config or Gs3BoundaryConfig()
    source = _polygon_from_rings(rings)
    centroid = source.centroid
    metric_epsg = _utm_epsg(centroid.x, centroid.y)
    metric_crs = CRS.from_epsg(metric_epsg)
    forward = Transformer.from_crs("EPSG:4326", metric_crs, always_xy=True)
    reverse = Transformer.from_crs(metric_crs, "EPSG:4326", always_xy=True)
    metric = transform(forward.transform, source)
    if metric.is_empty or not metric.is_valid or metric.area <= 0:
        raise ValueError("FIELD_AREA_INVALID: limite externo sem area metrica valida.")

    original_area_ha = float(metric.area / 10_000.0)
    original_vertices = sum(len(ring.coords) for ring in [metric.exterior, *metric.interiors])
    selected = metric
    selected_tolerance = 0.0
    selected_difference = 0.0

    if original_vertices > cfg.warning_vertices:
        acceptable = None
        for tolerance in cfg.tolerances_m:
            candidate = metric.simplify(tolerance, preserve_topology=True)
            if candidate.geom_type != "Polygon" or candidate.is_empty or not candidate.is_valid:
                continue
            difference = abs(metric.area - candidate.area) / metric.area * 100.0
            vertices = sum(len(ring.coords) for ring in [candidate.exterior, *candidate.interiors])
            if difference <= cfg.max_area_difference_percent:
                acceptable = (candidate, tolerance, difference, vertices)
                if vertices <= cfg.target_vertices:
                    break
        if acceptable is None:
            raise ValueError("GS3_BOUNDARY_SIMPLIFICATION_FAILED: nenhuma tolerancia preservou a area.")
        selected, selected_tolerance, selected_difference, export_vertices = acceptable
        if export_vertices > cfg.max_vertices:
            raise ValueError(
                "GS3_BOUNDARY_TOO_COMPLEX: "
                f"{export_vertices} vertices apos simplificacao; maximo {cfg.max_vertices}."
            )
    else:
        export_vertices = original_vertices

    export_area_ha = float(selected.area / 10_000.0)
    selected_wgs84 = transform(reverse.transform, selected)
    return PreparedGs3Boundary(
        rings=_rings_from_polygon(selected_wgs84),
        original_vertices=original_vertices,
        export_vertices=export_vertices,
        original_area_ha=original_area_ha,
        export_area_ha=export_area_ha,
        area_difference_percent=selected_difference,
        tolerance_m=selected_tolerance,
        metric_crs=f"EPSG:{metric_epsg}",
    )

