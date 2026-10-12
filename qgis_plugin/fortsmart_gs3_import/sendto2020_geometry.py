"""Target-specific boundary preparation for Precision Planting SendTo2020."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .gs3_geometry import Gs3BoundaryConfig, prepare_gs3_boundary_geometry


@dataclass(frozen=True)
class SendTo2020Config:
    vertex_warning: int = 10_000
    vertex_target: int = 2_000
    vertex_max: int = 10_000
    max_area_difference_percent: float = 0.5


@dataclass(frozen=True)
class PreparedSendTo2020Boundary:
    rings: list[list[tuple[float, float]]]
    source_components: int
    warnings: list[str]
    original_vertices: int
    export_vertices: int
    original_area_ha: float
    export_area_ha: float
    area_difference_percent: float
    tolerance_m: float
    metric_crs: str


def _closed(ring) -> list[tuple[float, float]]:
    result = []
    for point in ring:
        lon, lat = float(point[0]), float(point[1])
        if not isfinite(lon) or not isfinite(lat):
            raise ValueError("SENDTO2020_GEOMETRY_TYPE_INVALID: coordenada NaN/Inf.")
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError(
                "SENDTO2020_CRS_TRANSFORM_FAILED: coordenadas fora de EPSG:4326."
            )
        value = (lon, lat)
        if not result or value != result[-1]:
            result.append(value)
    if result and result[0] != result[-1]:
        result.append(result[0])
    return result


def prepare_sendto2020_boundary(
    rings: list[list[tuple[float, float]]],
    *,
    config: SendTo2020Config | None = None,
) -> PreparedSendTo2020Boundary:
    from shapely.geometry import Polygon

    cfg = config or SendTo2020Config()
    clean = [_closed(ring) for ring in rings if len(ring) >= 4]
    if not clean:
        raise ValueError("SENDTO2020_GEOMETRY_TYPE_INVALID: polygon vazio.")

    shell = Polygon(clean[0])
    if shell.is_empty or shell.area <= 0:
        raise ValueError("SENDTO2020_GEOMETRY_TYPE_INVALID: anel externo invalido.")
    holes = []
    components = [[clean[0]]]
    for ring in clean[1:]:
        candidate = Polygon(ring)
        if candidate.is_empty or candidate.area <= 0:
            continue
        if shell.contains(candidate.representative_point()):
            holes.append(ring)
        else:
            components.append([ring])
    components[0].extend(holes)

    prepared_components = []
    boundary_cfg = Gs3BoundaryConfig(
        warning_vertices=cfg.vertex_warning,
        target_vertices=cfg.vertex_target,
        max_vertices=cfg.vertex_max,
        max_area_difference_percent=cfg.max_area_difference_percent,
    )
    for component in components:
        try:
            prepared_components.append(
                prepare_gs3_boundary_geometry(component, config=boundary_cfg)
            )
        except ValueError as exc:
            message = str(exc)
            if "TOO_COMPLEX" in message:
                raise ValueError("SENDTO2020_BOUNDARY_TOO_COMPLEX: " + message) from exc
            raise ValueError("SENDTO2020_AREA_DISTORTION: " + message) from exc

    # A valid reference SHP contains one polygon feature. When a canonical
    # MultiPolygon has detached islands, select the principal component and
    # make that data reduction explicit in the export report.
    principal = max(prepared_components, key=lambda item: item.original_area_ha)
    warnings = []
    if len(prepared_components) > 1:
        warnings.append(
            "SENDTO2020_MULTIPART_BOUNDARY_WARNING: "
            f"{len(prepared_components) - 1} componente(s) secundario(s) nao exportado(s)."
        )
    if principal.area_difference_percent > cfg.max_area_difference_percent:
        raise ValueError(
            "SENDTO2020_AREA_DISTORTION: "
            f"{principal.area_difference_percent:.6f}% > {cfg.max_area_difference_percent:.6f}%."
        )
    if principal.export_vertices > cfg.vertex_max:
        raise ValueError(
            "SENDTO2020_BOUNDARY_TOO_COMPLEX: "
            f"{principal.export_vertices} vertices > {cfg.vertex_max}."
        )
    return PreparedSendTo2020Boundary(
        rings=principal.rings,
        source_components=len(prepared_components),
        warnings=warnings,
        original_vertices=principal.original_vertices,
        export_vertices=principal.export_vertices,
        original_area_ha=principal.original_area_ha,
        export_area_ha=principal.export_area_ha,
        area_difference_percent=principal.area_difference_percent,
        tolerance_m=principal.tolerance_m,
        metric_crs=principal.metric_crs,
    )

