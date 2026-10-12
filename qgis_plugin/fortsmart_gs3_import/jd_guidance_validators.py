"""FortSmart — validacao de CRS/coordenadas das guias John Deere.

Somente valida: nao recorta, nao reprojeta e nao move passadas. O coletor
transforma um clone da geometria QGIS para EPSG:4326 e registra o CRS de
origem em ``extra["source_crs"]``; aqui se decide se o resultado e confiavel.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

SOURCE_CRS_UNDEFINED = "JOHN_DEERE_GUIDANCE_SOURCE_CRS_UNDEFINED"
COORDINATES_INVALID = "JOHN_DEERE_GUIDANCE_COORDINATES_INVALID"
OUTSIDE_FIELD_REGION = "JOHN_DEERE_GUIDANCE_OUTSIDE_FIELD_REGION"

GS3_FILE_MISSING = "GS3_GUIDANCE_FILE_MISSING"
GS3_CATALOG_MISSING = "GS3_GUIDANCE_CATALOG_MISSING"
GS3_FIELD_MISMATCH = "GS3_GUIDANCE_FIELD_MISMATCH"
GEN4_FIELD_MISMATCH = "GEN4_GUIDANCE_FIELD_MISMATCH"
GEN4_MASTERDATA_INCOMPLETE = "GEN4_GUIDANCE_MASTERDATA_INCOMPLETE"

_METERS_PER_DEGREE = 111_320.0

BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class GuidanceIssue:
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _points(tracks: Iterable[Sequence[tuple[float, float]]]) -> list[tuple[float, float]]:
    return [(float(p[0]), float(p[1])) for track in tracks for p in track]


def check_geographic_points(points: Sequence[tuple[float, float]]) -> GuidanceIssue | None:
    """Longitude/latitude finitas e dentro da faixa geografica."""
    for lon, lat in points:
        if not (math.isfinite(lon) and math.isfinite(lat)):
            return GuidanceIssue(COORDINATES_INVALID, "coordenada NaN/Inf")
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            return GuidanceIssue(
                COORDINATES_INVALID,
                f"({lon}, {lat}) fora da faixa lon/lat: coordenadas projetadas "
                "nao podem ser gravadas como longitude/latitude",
            )
    return None


class GuidanceCoordinateValidator:
    """CRS de origem explicito e coordenadas geograficas validas."""

    def check_source(self, extra: dict) -> GuidanceIssue | None:
        meta = extra.get("source_crs")
        if meta is None:
            return None
        status = str(meta.get("transform", "") or "")
        if status == "UNDEFINED" or not meta.get("valid", False):
            return GuidanceIssue(
                SOURCE_CRS_UNDEFINED,
                f"camada sem CRS de origem definido ({meta.get('crs') or '-'})",
            )
        if status == "FAILED":
            return GuidanceIssue(
                COORDINATES_INVALID,
                f"transformacao {meta.get('crs') or '-'} -> EPSG:4326 falhou",
            )
        return None

    def check(self, extra: dict, points: Sequence[tuple[float, float]]) -> GuidanceIssue | None:
        """NaN/Inf ficam para a validacao de geometria (politica abortar/pular)."""
        finite = [p for p in points if math.isfinite(p[0]) and math.isfinite(p[1])]
        return self.check_source(extra) or check_geographic_points(finite)


def _expanded(bbox: BBox, margin_m: float) -> BBox:
    west, south, east, north = bbox
    lat_margin = margin_m / _METERS_PER_DEGREE
    lon_margin = margin_m / (_METERS_PER_DEGREE * max(0.01, math.cos(math.radians((south + north) / 2.0))))
    return west - lon_margin, south - lat_margin, east + lon_margin, north + lat_margin


def validate_guidance_against_boundary(
    tracks: Iterable[Sequence[tuple[float, float]]],
    boundary_bbox: BBox,
    margin_m: float = 100.0,
) -> GuidanceIssue | None:
    """Envelope e centro da guia dentro do envelope do limite do proprio talhao."""
    points = _points(tracks)
    if not points:
        return GuidanceIssue(COORDINATES_INVALID, "guia sem vertices")
    west, south, east, north = _expanded(boundary_bbox, margin_m)
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    if max(lons) < west or min(lons) > east or max(lats) < south or min(lats) > north:
        return GuidanceIssue(OUTSIDE_FIELD_REGION, "envelope da guia nao intersecta o limite do talhao")
    center = ((min(lons) + max(lons)) / 2.0, (min(lats) + max(lats)) / 2.0)
    if not (west <= center[0] <= east and south <= center[1] <= north):
        return GuidanceIssue(OUTSIDE_FIELD_REGION, "centro da guia fora do limite do talhao")
    return None


def coordinates_status(
    tracks: Iterable[Sequence[tuple[float, float]]],
    boundary_bbox: BBox | None,
    margin_m: float = 100.0,
) -> GuidanceIssue | None:
    """Verificacao pos-serializacao: faixa geografica e regiao do talhao."""
    tracks = [list(track) for track in tracks]
    issue = check_geographic_points(_points(tracks))
    if issue is None and boundary_bbox is not None:
        issue = validate_guidance_against_boundary(tracks, boundary_bbox, margin_m)
    return issue
