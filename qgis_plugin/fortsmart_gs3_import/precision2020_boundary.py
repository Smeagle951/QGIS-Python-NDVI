"""FortSmart — limite externo seguro para Precision Planting 20|20 (SendTo2020).

Somente o caminho BOUNDARY -> geometria -> CRS -> shapefile. Cliente, Fazenda,
Talhao, nomes, pastas e campos DBF continuam no exportador SendTo2020.

A transformacao QGIS (QgsCoordinateTransform) acontece no coletor, que anota em
``feature.extra["source_geometry"]`` o CRS de origem, o resultado da
transformacao, o envelope e o centro de origem. Aqui esses dados sao conferidos
de forma independente (pyproj) antes de qualquer arquivo ser gravado.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .gs3_geometry import Gs3BoundaryConfig, prepare_gs3_boundary_geometry
from .gs3_spatial_types import Gs3FieldBundle, Gs3SpatialFeature


# Rollback A/B: False volta ao fluxo SendTo2020 anterior.
ENABLE_PRECISION2020_SAFE_BOUNDARY = True
# Somente desenvolvimento: GeoJSON/SHP extras na pasta de diagnostico, nunca no pacote.
PRECISION2020_BOUNDARY_DEBUG = False

PRECISION2020_DEVICE_STATUS = "READY_FOR_DEVICE_TEST"

BOUNDARY_NOT_FOUND = "PRECISION2020_BOUNDARY_NOT_FOUND"
BOUNDARY_EMPTY = "PRECISION2020_BOUNDARY_EMPTY"
BOUNDARY_INVALID = "PRECISION2020_BOUNDARY_INVALID"
BOUNDARY_INVALID_GEOMETRY_TYPE = "PRECISION2020_BOUNDARY_INVALID_GEOMETRY_TYPE"
SOURCE_CRS_UNDEFINED = "PRECISION2020_SOURCE_CRS_UNDEFINED"
CRS_TRANSFORM_FAILED = "PRECISION2020_CRS_TRANSFORM_FAILED"
BOUNDARY_COORDINATES_INVALID = "PRECISION2020_BOUNDARY_COORDINATES_INVALID"
SHAPEFILE_WRITE_FAILED = "PRECISION2020_SHAPEFILE_WRITE_FAILED"
SHAPEFILE_INCOMPLETE = "PRECISION2020_SHAPEFILE_INCOMPLETE"
POSTWRITE_VALIDATION_FAILED = "PRECISION2020_BOUNDARY_POSTWRITE_VALIDATION_FAILED"
BOUNDARY_FIELD_MISMATCH = "PRECISION2020_BOUNDARY_FIELD_MISMATCH"
REGION_MISMATCH = "PRECISION2020_BOUNDARY_REGION_MISMATCH"

EVENT_DETECTED = "precision2020_boundary_detected"
EVENT_PREPARED = "precision2020_boundary_geometry_prepared"
EVENT_TRANSFORMED = "precision2020_boundary_crs_transformed"
EVENT_WRITTEN = "precision2020_boundary_written"
EVENT_REOPENED = "precision2020_boundary_reopened"
EVENT_VALIDATED = "precision2020_boundary_validated"

SOURCE_CRS_NOT_PROVIDED = "NOT_PROVIDED"
TRANSFORM_IDENTITY = "IDENTITY"
TRANSFORM_OK = "OK"
TRANSFORM_FAILED = "FAILED"
TRANSFORM_UNDEFINED = "UNDEFINED"

UNDEFINED_CRS_MESSAGE = "Não foi possível determinar o sistema de coordenadas do limite do talhão."
REGION_MESSAGE = (
    "Boundary transformado para localização incompatível com a região esperada do projeto."
)
REPORT_TEXT = "PRECISION2020_EXPORT_REPORT.txt"
REPORT_JSON = "PRECISION2020_EXPORT_REPORT.json"

_EARTH_RADIUS_M = 6_371_008.8
_LOG = logging.getLogger("fortsmart.precision2020")

Ring = list[tuple[float, float]]
PolygonRings = list[Ring]


@dataclass(frozen=True)
class Precision2020ExportConfig:
    """Unica configuracao do CRS gravado para o 20|20."""

    boundary_crs: str = "EPSG:4326"
    # Diagnostico regional opcional (xmin, ymin, xmax, ymax) no CRS de exportacao.
    expected_region_bbox: tuple[float, float, float, float] | None = None
    region_policy: str = "warn"
    # Diferenca maxima aceitavel entre o centro reprojetado e o exportado.
    location_tolerance_m: float = 10.0
    multi_field_distance_warning_km: float = 100.0
    vertex_warning: int = 10_000
    vertex_target: int = 2_000
    vertex_max: int = 10_000
    max_area_difference_percent: float = 0.5


PRECISION2020_EXPORT_CRS = Precision2020ExportConfig().boundary_crs


class Precision2020BoundaryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _epsg_code(crs: str) -> int | None:
    text = str(crs or "").strip().upper()
    if text.startswith("EPSG:"):
        try:
            return int(text.split(":", 1)[1])
        except ValueError:
            return None
    return None


def _is_geographic(crs: str) -> bool:
    return _epsg_code(crs) in (4326, 4674, 4618, 4225)


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def _bbox(parts: Sequence[PolygonRings]) -> list[float]:
    xs = [x for part in parts for ring in part for x, _ in ring]
    ys = [y for part in parts for ring in part for _, y in ring]
    return [min(xs), min(ys), max(xs), max(ys)]


# --------------------------------------------------------------------------
# Identificacao do limite
# --------------------------------------------------------------------------

def _role(feature: Gs3SpatialFeature) -> str:
    return str(
        feature.extra.get("fortsmart_role") or feature.extra.get("canonical_role") or ""
    ).strip().upper()


def _identity_source(feature: Gs3SpatialFeature) -> str:
    if feature.extra.get("fortsmart_role"):
        return "fortsmart_role"
    if feature.extra.get("canonical_role"):
        resolution = str(feature.extra.get("role_resolution") or "")
        if resolution in ("", "fortsmart_role", "feature_role", "layer_metadata", "internal_key"):
            return "canonical_role"
        return f"canonical_role ({resolution})"
    if feature.extra.get("fortsmart_internal_key"):
        return "internal_key"
    return "legacy_kind"


def _same_field(a: str, b: str) -> bool:
    return str(a or "").strip().strip("{}").casefold() == str(b or "").strip().strip("{}").casefold()


def select_field_boundary(
    bundle: Gs3FieldBundle,
    is_external: Callable[[Gs3SpatialFeature], bool],
) -> tuple[Gs3SpatialFeature, dict[str, Any]]:
    """O EXTERNAL_BOUNDARY do proprio talhao (field_uuid), nunca o da camada inteira."""
    candidates = [feature for feature in bundle.features if is_external(feature)]
    foreign = [feature for feature in candidates if not _same_field(feature.field_erid, bundle.field_erid)]
    own = [feature for feature in candidates if _same_field(feature.field_erid, bundle.field_erid)]
    info = {"detected": len(candidates), "selected": len(own), "foreign": len(foreign)}
    if len(candidates) > 1:
        raise ValueError(
            "SENDTO2020_BOUNDARY_MULTIPLE_FEATURES: O boundary destinado ao "
            "20|20 deve conter somente o EXTERNAL_BOUNDARY do talhao. "
            f"Foram encontrados {len(candidates)} poligonos"
            + (f" ({len(foreign)} de outro field_uuid)." if foreign else ".")
        )
    if foreign:
        raise Precision2020BoundaryError(
            BOUNDARY_FIELD_MISMATCH,
            f"limite de outro talhao no pacote de {bundle.talhao} "
            f"(field_uuid esperado {bundle.field_erid}, recebido {foreign[0].field_erid}).",
        )
    if not own:
        raise Precision2020BoundaryError(
            BOUNDARY_NOT_FOUND, f"Talhao {bundle.talhao} sem EXTERNAL_BOUNDARY."
        )
    info["identity"] = _identity_source(own[0])
    return own[0], info


# --------------------------------------------------------------------------
# Geometria
# --------------------------------------------------------------------------

def boundary_polygon_parts(feature: Gs3SpatialFeature) -> list[PolygonRings]:
    """Clone das partes do poligono (com buracos), sem tocar a feicao original."""
    parts = feature.extra.get("polygon_parts")
    if parts:
        return [[[tuple(point) for point in ring] for ring in part] for part in copy.deepcopy(parts)]
    rings = feature.rings or ([feature.points] if feature.points else [])
    rings = [[tuple(point) for point in ring] for ring in copy.deepcopy(rings)]
    if not rings:
        return []
    try:
        from shapely.geometry import Polygon
    except ImportError:
        return [rings]
    result: list[PolygonRings] = [[rings[0]]]
    shells: list[Any] = []
    try:
        shells.append(Polygon(rings[0]) if len(rings[0]) >= 3 else None)
    except Exception:
        shells.append(None)
    for ring in rings[1:]:
        try:
            point = Polygon(ring).representative_point() if len(ring) >= 4 else None
        except Exception:
            point = None
        owner = next(
            (index for index, shell in enumerate(shells) if shell is not None and point is not None and shell.contains(point)),
            None,
        )
        if owner is None:
            result.append([ring])
            try:
                shells.append(Polygon(ring) if len(ring) >= 3 else None)
            except Exception:
                shells.append(None)
        else:
            result[owner].append(ring)
    return result


@dataclass
class PreparedPrecision2020Boundary:
    parts: list[PolygonRings]
    geometry_type: str
    valid_before: bool
    invalid_reason: str
    made_valid: bool
    removed_duplicates: int
    closed_rings: int
    dropped_rings: int
    holes: int
    original_vertices: int
    export_vertices: int
    original_area_ha: float
    export_area_ha: float
    area_difference_percent: float
    tolerance_m: float
    bbox: list[float]
    centroid: tuple[float, float]
    make_valid_area_change_percent: float | None = None

    @property
    def part_count(self) -> int:
        return len(self.parts)

    def geojson_geometry(self) -> dict[str, Any]:
        if self.geometry_type == "Polygon":
            return {"type": "Polygon", "coordinates": self.parts[0]}
        return {"type": "MultiPolygon", "coordinates": self.parts}


def _clean_ring(raw: Sequence[Sequence[float]], stats: dict[str, int]) -> Ring:
    ring: Ring = []
    for point in raw:
        if len(point) < 2:
            continue
        x, y = float(point[0]), float(point[1])
        if not math.isfinite(x) or not math.isfinite(y):
            raise Precision2020BoundaryError(BOUNDARY_COORDINATES_INVALID, "coordenada NaN/Inf no limite.")
        value = (x, y)
        if ring and value == ring[-1]:
            stats["removed_duplicates"] += 1
            continue
        ring.append(value)
    if len(ring) >= 3 and ring[0] != ring[-1]:
        ring.append(ring[0])
        stats["closed_rings"] += 1
    if len(ring) < 4:
        stats["dropped_rings"] += 1
        return []
    return ring


def _check_coordinates(parts: Sequence[PolygonRings], config: Precision2020ExportConfig) -> None:
    if not _is_geographic(config.boundary_crs):
        return
    xmin, ymin, xmax, ymax = _bbox(parts)
    if -180 <= xmin <= xmax <= 180 and -90 <= ymin <= ymax <= 90:
        return
    hint = (
        " Os valores parecem metros de um CRS projetado (ex.: UTM) gravados sem transformacao."
        if max(abs(xmin), abs(xmax)) > 1000 or max(abs(ymin), abs(ymax)) > 1000
        else ""
    )
    raise Precision2020BoundaryError(
        BOUNDARY_COORDINATES_INVALID,
        f"envelope ({xmin:.6f}, {ymin:.6f}, {xmax:.6f}, {ymax:.6f}) fora de longitude [-180, 180] "
        f"/ latitude [-90, 90] em {config.boundary_crs}.{hint}",
    )


def _polygons_of(geometry) -> list:
    kind = geometry.geom_type
    if kind == "Polygon":
        return [geometry] if not geometry.is_empty and geometry.area > 0 else []
    if kind in ("MultiPolygon", "GeometryCollection"):
        return [poly for item in geometry.geoms for poly in _polygons_of(item)]
    return []


def _rings_of(polygon) -> PolygonRings:
    rings = [[(float(x), float(y)) for x, y in polygon.exterior.coords]]
    rings.extend([(float(x), float(y)) for x, y in interior.coords] for interior in polygon.interiors)
    return rings


def prepare_precision2020_boundary(
    parts: Sequence[PolygonRings],
    *,
    config: Precision2020ExportConfig | None = None,
) -> PreparedPrecision2020Boundary:
    """Normaliza um clone do limite: 2D, aneis fechados, valido, partes preservadas."""
    from shapely.geometry import MultiPolygon, Polygon
    from shapely.validation import explain_validity, make_valid

    cfg = config or Precision2020ExportConfig()
    stats = {"removed_duplicates": 0, "closed_rings": 0, "dropped_rings": 0}
    clean_parts: list[PolygonRings] = []
    for part in parts:
        if not part:
            continue
        shell = _clean_ring(part[0], stats)
        if not shell:
            stats["dropped_rings"] += max(0, len(part) - 1)
            continue
        holes = [ring for ring in (_clean_ring(raw, stats) for raw in part[1:]) if ring]
        clean_parts.append([shell, *holes])
    if not clean_parts:
        raise Precision2020BoundaryError(BOUNDARY_EMPTY, "limite sem anel externo com area.")
    _check_coordinates(clean_parts, cfg)

    polygons = [Polygon(part[0], part[1:]) for part in clean_parts]
    geometry = polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
    if geometry.is_empty:
        raise Precision2020BoundaryError(BOUNDARY_EMPTY, "limite vazio.")
    valid_before = bool(geometry.is_valid)
    reason = "" if valid_before else explain_validity(geometry)
    made_valid = False
    area_change: float | None = None
    if not valid_before:
        input_area = sum(abs(polygon.area) for polygon in polygons)
        repaired = make_valid(geometry)
        fixed = _polygons_of(repaired)
        if not fixed:
            if geometry.area <= 0:
                raise Precision2020BoundaryError(BOUNDARY_EMPTY, f"limite sem area ({reason}).")
            raise Precision2020BoundaryError(BOUNDARY_INVALID, f"geometria invalida ({reason}); makeValid sem poligono.")
        geometry = fixed[0] if len(fixed) == 1 else MultiPolygon(fixed)
        if not geometry.is_valid:
            raise Precision2020BoundaryError(
                BOUNDARY_INVALID, f"geometria invalida ({reason}); continua invalida apos makeValid."
            )
        made_valid = True
        clean_parts = [_rings_of(polygon) for polygon in fixed]
        if input_area > 0:
            area_change = abs(geometry.area - input_area) / input_area * 100.0
    if geometry.area <= 0:
        raise Precision2020BoundaryError(BOUNDARY_EMPTY, "limite com area zero.")

    boundary_cfg = Gs3BoundaryConfig(
        warning_vertices=cfg.vertex_warning,
        target_vertices=cfg.vertex_target,
        max_vertices=cfg.vertex_max,
        max_area_difference_percent=cfg.max_area_difference_percent,
    )
    prepared_parts: list[PolygonRings] = []
    original_vertices = export_vertices = 0
    original_area = export_area = 0.0
    tolerance = 0.0
    for part in clean_parts:
        try:
            prepared = prepare_gs3_boundary_geometry(part, config=boundary_cfg)
        except ValueError as exc:
            message = str(exc)
            if "TOO_COMPLEX" in message:
                raise ValueError("SENDTO2020_BOUNDARY_TOO_COMPLEX: " + message) from exc
            raise ValueError("SENDTO2020_AREA_DISTORTION: " + message) from exc
        if prepared.area_difference_percent > cfg.max_area_difference_percent:
            raise ValueError(
                "SENDTO2020_AREA_DISTORTION: "
                f"{prepared.area_difference_percent:.6f}% > {cfg.max_area_difference_percent:.6f}%."
            )
        prepared_parts.append(prepared.rings)
        original_vertices += prepared.original_vertices
        export_vertices += prepared.export_vertices
        original_area += prepared.original_area_ha
        export_area += prepared.export_area_ha
        tolerance = max(tolerance, prepared.tolerance_m)
    if export_vertices > cfg.vertex_max:
        raise ValueError(f"SENDTO2020_BOUNDARY_TOO_COMPLEX: {export_vertices} vertices > {cfg.vertex_max}.")

    final = (
        Polygon(prepared_parts[0][0], prepared_parts[0][1:])
        if len(prepared_parts) == 1
        else MultiPolygon([Polygon(part[0], part[1:]) for part in prepared_parts])
    )
    if final.is_empty or not final.is_valid or len(_polygons_of(final)) != len(prepared_parts):
        raise Precision2020BoundaryError(
            BOUNDARY_INVALID, f"geometria preparada invalida ({explain_validity(final)})."
        )
    _check_coordinates(prepared_parts, cfg)
    difference = abs(original_area - export_area) / original_area * 100.0 if original_area else 0.0
    return PreparedPrecision2020Boundary(
        parts=prepared_parts,
        geometry_type="Polygon" if len(prepared_parts) == 1 else "MultiPolygon",
        valid_before=valid_before,
        invalid_reason=reason,
        made_valid=made_valid,
        removed_duplicates=stats["removed_duplicates"],
        closed_rings=stats["closed_rings"],
        dropped_rings=stats["dropped_rings"],
        holes=sum(len(part) - 1 for part in prepared_parts),
        original_vertices=original_vertices,
        export_vertices=export_vertices,
        original_area_ha=original_area,
        export_area_ha=export_area,
        area_difference_percent=difference,
        tolerance_m=tolerance,
        bbox=_bbox(prepared_parts),
        centroid=(float(final.centroid.x), float(final.centroid.y)),
        make_valid_area_change_percent=area_change,
    )


# --------------------------------------------------------------------------
# CRS
# --------------------------------------------------------------------------

def require_source_crs(source: dict[str, Any]) -> None:
    """Sem CRS de origem as coordenadas nao tem significado; nunca assumir um EPSG."""
    if not source:
        return
    if (
        not source.get("crs_valid")
        or str(source.get("transform") or "").upper() == TRANSFORM_UNDEFINED
        or not (str(source.get("crs") or "").strip() or source.get("crs_wkt"))
    ):
        raise Precision2020BoundaryError(SOURCE_CRS_UNDEFINED, UNDEFINED_CRS_MESSAGE)


def crs_preflight(
    feature: Gs3SpatialFeature,
    prepared_centroid: tuple[float, float],
    config: Precision2020ExportConfig,
) -> dict[str, Any]:
    """Confere o CRS de origem e a transformacao feita pelo coletor QGIS."""
    source = dict(feature.extra.get("source_geometry") or {})
    target = config.boundary_crs
    if not source:
        return {
            "source": SOURCE_CRS_NOT_PROVIDED,
            "target": target,
            "transform": TRANSFORM_IDENTITY,
            "verified": "NOT_PROVIDED",
            "warning": (
                "CRS de origem nao informado pelo coletor; coordenadas recebidas em "
                f"{target} validadas somente pela faixa geografica."
            ),
        }
    require_source_crs(source)
    source_crs = str(source.get("crs") or "").strip()
    status = str(source.get("transform") or "").upper()
    if status in (TRANSFORM_FAILED, TRANSFORM_UNDEFINED) or status not in (TRANSFORM_OK, TRANSFORM_IDENTITY):
        raise Precision2020BoundaryError(
            CRS_TRANSFORM_FAILED,
            f"transformacao {source_crs or 'CRS personalizado'} -> {target} retornou {status or 'vazio'}.",
        )
    same = _same_crs(source_crs, target)
    if status == TRANSFORM_OK and same:
        status = TRANSFORM_IDENTITY
    result: dict[str, Any] = {
        "source": source_crs or "CRS personalizado (WKT)",
        "source_geographic": bool(source.get("crs_geographic")),
        "target": target,
        "transform": TRANSFORM_IDENTITY if same else status,
        "source_bbox": source.get("bbox"),
        "source_has_z": bool(source.get("has_z")),
        "source_has_m": bool(source.get("has_m")),
        "source_wkb_type": source.get("wkb_type", ""),
        "source_parts": source.get("parts"),
        "source_valid": source.get("valid"),
    }
    errors: list[float] = []
    qgis_error = source.get("roundtrip_centroid_error_m")
    if qgis_error is not None:
        result["qgis_roundtrip_centroid_error_m"] = float(qgis_error)
        errors.append(float(qgis_error))
    centroid = source.get("centroid")
    if centroid and not same:
        projected = _project_point(source_crs, source.get("crs_wkt"), centroid, target)
        if projected is None:
            result["verified"] = "NOT_VERIFIED"
            result["warning"] = (
                "Conferencia independente da transformacao indisponivel (pyproj ausente ou CRS nao reconhecido)."
            )
        else:
            distance = _haversine_m(projected, prepared_centroid) if _is_geographic(target) else math.dist(projected, prepared_centroid)
            result["independent_centroid"] = list(projected)
            result["independent_centroid_error_m"] = distance
            errors.append(distance)
    elif centroid and same:
        distance = _haversine_m(tuple(centroid), prepared_centroid) if _is_geographic(target) else math.dist(centroid, prepared_centroid)
        result["independent_centroid_error_m"] = distance
        errors.append(distance)
    worst = max(errors) if errors else None
    result["centroid_error_m"] = worst
    if worst is not None:
        result.setdefault("verified", "YES")
        if worst > config.location_tolerance_m:
            raise Precision2020BoundaryError(
                CRS_TRANSFORM_FAILED,
                f"centro do limite exportado a {worst:.1f} m do centro de origem reprojetado "
                f"({source_crs} -> {target}); tolerancia {config.location_tolerance_m:.1f} m.",
            )
    else:
        result.setdefault("verified", "NOT_VERIFIED")
    return result


def _same_crs(a: str, b: str) -> bool:
    return bool(a) and str(a).strip().upper() == str(b).strip().upper()


def _project_point(
    source_crs: str,
    source_wkt: str | None,
    point: Sequence[float],
    target: str,
) -> tuple[float, float] | None:
    try:
        from pyproj import CRS, Transformer
    except ImportError:
        return None
    crs = None
    for candidate in (source_crs, source_wkt):
        if not candidate:
            continue
        try:
            crs = CRS.from_user_input(str(candidate))
            break
        except Exception:
            continue
    if crs is None:
        return None
    try:
        transformer = Transformer.from_crs(crs, CRS.from_user_input(target), always_xy=True)
        x, y = transformer.transform(float(point[0]), float(point[1]))
    except Exception:
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return float(x), float(y)


def region_check(prepared: PreparedPrecision2020Boundary, config: Precision2020ExportConfig) -> dict[str, Any]:
    if config.expected_region_bbox is None:
        return {"status": "NOT_CONFIGURED"}
    xmin, ymin, xmax, ymax = config.expected_region_bbox
    x, y = prepared.centroid
    if xmin <= x <= xmax and ymin <= y <= ymax:
        return {"status": "PASS"}
    if config.region_policy == "abort":
        raise Precision2020BoundaryError(REGION_MISMATCH, REGION_MESSAGE)
    return {"status": "WARNING", "warning": f"{REGION_MISMATCH}: {REGION_MESSAGE}"}


# --------------------------------------------------------------------------
# Escrita e validacao pos-escrita
# --------------------------------------------------------------------------

def _shp_record_shapes(path: Path) -> list[dict[str, Any]]:
    data = path.read_bytes()
    if len(data) < 100 or struct.unpack(">i", data[:4])[0] != 9994:
        raise Precision2020BoundaryError(POSTWRITE_VALIDATION_FAILED, f"{path.name} sem cabecalho SHP.")
    records = []
    position = 100
    while position + 8 <= len(data):
        _, length = struct.unpack(">ii", data[position:position + 8])
        shape_type = struct.unpack("<i", data[position + 8:position + 12])[0]
        parts = points = 0
        if shape_type in (5, 15, 25):
            parts, points = struct.unpack("<ii", data[position + 44:position + 52])
        records.append({"shape_type": shape_type, "parts": parts, "points": points})
        position += 8 + length * 2
    return records


def _shx_count(path: Path) -> int:
    return max(0, (path.stat().st_size - 100) // 8)


def _dbf_count(path: Path) -> int:
    data = path.read_bytes()
    return struct.unpack_from("<I", data, 4)[0] if len(data) >= 8 else 0


def _prj_epsg(path: Path) -> int | None:
    text = path.read_text(encoding="ascii", errors="replace")
    try:
        from pyproj import CRS

        return CRS.from_wkt(text).to_epsg()
    except Exception:
        marker = text.rfind('AUTHORITY["EPSG"')
        if marker < 0:
            return None
        digits = "".join(ch for ch in text[marker + 16:marker + 40] if ch.isdigit())
        return int(digits) if digits else None


class Precision2020BoundaryPostWriteValidator:
    """Reabre os arquivos gravados; nunca confia apenas no retorno do writer."""

    def validate(
        self,
        base_path: Path,
        prepared: PreparedPrecision2020Boundary,
        config: Precision2020ExportConfig,
    ) -> dict[str, Any]:
        files = {ext: base_path.with_suffix(ext) for ext in (".shp", ".shx", ".dbf", ".prj")}
        status = {path.name: path.is_file() for path in files.values()}
        missing = [name for name, present in status.items() if not present and not name.endswith(".prj")]
        if missing:
            raise Precision2020BoundaryError(SHAPEFILE_INCOMPLETE, "arquivos ausentes: " + ", ".join(missing))
        stems = {path.stem for path in files.values()}
        if len(stems) != 1:
            raise Precision2020BoundaryError(SHAPEFILE_INCOMPLETE, f"nomes base diferentes: {sorted(stems)}.")

        shapes = _shp_record_shapes(files[".shp"])
        counts = {"shp": len(shapes), "shx": _shx_count(files[".shx"]), "dbf": _dbf_count(files[".dbf"])}
        problems: list[str] = []
        if set(counts.values()) != {1}:
            problems.append(f"contagem de feicoes SHP/SHX/DBF = {counts}, esperado 1")
        if shapes and shapes[0]["shape_type"] != 5:
            problems.append(f"tipo SHP {shapes[0]['shape_type']} (esperado 5, Polygon 2D sem Z/M)")
        expected_rings = sum(len(part) for part in prepared.parts)
        if shapes and shapes[0]["parts"] != expected_rings:
            problems.append(f"{shapes[0]['parts']} aneis gravados, esperado {expected_rings}")

        prj_epsg = _prj_epsg(files[".prj"]) if files[".prj"].is_file() else None
        target_epsg = _epsg_code(config.boundary_crs)
        if files[".prj"].is_file() and prj_epsg != target_epsg:
            problems.append(f".prj declara EPSG:{prj_epsg}, coordenadas em {config.boundary_crs}")

        try:
            import fiona
            from shapely.geometry import shape
        except ImportError as exc:
            raise RuntimeError("SENDTO2020_DEPENDENCY_MISSING: Fiona/Shapely obrigatorios.") from exc
        try:
            with fiona.open(files[".shp"], "r") as source:
                records = list(source)
                reopened_epsg = source.crs.to_epsg() if hasattr(source.crs, "to_epsg") else None
        except Exception as exc:
            raise Precision2020BoundaryError(POSTWRITE_VALIDATION_FAILED, f"SHP nao reabre ({exc}).") from exc
        geometry = shape(records[0]["geometry"]) if records and records[0].get("geometry") else None
        if geometry is None or geometry.is_empty:
            problems.append("geometria reaberta vazia")
        else:
            if geometry.geom_type != prepared.geometry_type:
                problems.append(f"tipo reaberto {geometry.geom_type}, esperado {prepared.geometry_type}")
            if not geometry.is_valid:
                problems.append("geometria reaberta invalida")
            if geometry.has_z:
                problems.append("geometria reaberta com Z")
            bounds = list(geometry.bounds)
            if max(abs(a - b) for a, b in zip(bounds, prepared.bbox)) > 1e-9:
                problems.append(f"bbox reaberto {bounds} difere do preparado {prepared.bbox}")
            centroid = (geometry.centroid.x, geometry.centroid.y)
            drift = _haversine_m(centroid, prepared.centroid) if _is_geographic(config.boundary_crs) else math.dist(centroid, prepared.centroid)
            if drift > 0.01:
                problems.append(f"centro reaberto deslocado {drift:.3f} m")
        if reopened_epsg != target_epsg:
            problems.append(f"CRS reaberto EPSG:{reopened_epsg}, esperado {config.boundary_crs}")
        if problems:
            raise Precision2020BoundaryError(POSTWRITE_VALIDATION_FAILED, "; ".join(problems))
        return {
            "feature_count": 1,
            "counts": counts,
            "geometry_type": prepared.geometry_type,
            "geometry_valid": True,
            "geometry_empty": False,
            "parts": prepared.part_count,
            "rings": expected_rings,
            "bbox": list(geometry.bounds),
            "crs": f"EPSG:{reopened_epsg}",
            "prj_crs": f"EPSG:{prj_epsg}" if prj_epsg else None,
            "files": {name: "OK" if present else "MISSING" for name, present in status.items()},
            "location_sanity": "PASS",
        }


@dataclass
class Precision2020BoundaryResult:
    field: str
    field_erid: str
    filename: str = ""
    status: str = "BUILDING"
    events: list[str] = field(default_factory=list)
    boundary: dict[str, Any] = field(default_factory=dict)
    geometry: dict[str, Any] = field(default_factory=dict)
    crs: dict[str, Any] = field(default_factory=dict)
    region: dict[str, Any] = field(default_factory=dict)
    postwrite: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "field_erid": self.field_erid,
            "filename": self.filename,
            "status": self.status,
            "events": list(self.events),
            "boundary": self.boundary,
            "geometry": self.geometry,
            "crs": self.crs,
            "region": self.region,
            "postwrite": self.postwrite,
            "warnings": list(self.warnings),
            "error": self.error,
        }


@dataclass
class PreparedField:
    bundle: Gs3FieldBundle
    feature: Gs3SpatialFeature
    prepared: PreparedPrecision2020Boundary
    result: Precision2020BoundaryResult
    source_parts: list[PolygonRings]


class Precision2020BoundaryExporter:
    """Preflight -> clone -> normalizacao -> CRS -> escrita -> reabertura."""

    def __init__(self, config: Precision2020ExportConfig | None = None) -> None:
        self.config = config or Precision2020ExportConfig()
        if _epsg_code(self.config.boundary_crs) != 4326:
            raise ValueError(
                f"PRECISION2020_EXPORT_CRS {self.config.boundary_crs} sem .prj definido; "
                "o pacote SendTo2020 aceito pelo monitor usa EPSG:4326."
            )
        self.results: list[Precision2020BoundaryResult] = []
        self.warnings: list[str] = []

    def _event(self, result: Precision2020BoundaryResult, event: str) -> None:
        result.events.append(event)
        _LOG.info("%s field=%s", event, result.field)

    def prepare_field(
        self,
        bundle: Gs3FieldBundle,
        is_external: Callable[[Gs3SpatialFeature], bool],
    ) -> PreparedField:
        result = Precision2020BoundaryResult(field=bundle.talhao, field_erid=bundle.field_erid)
        self.results.append(result)
        try:
            feature, info = select_field_boundary(bundle, is_external)
            result.boundary = info
            self._event(result, EVENT_DETECTED)
            if feature.geometry.lower() != "polygon":
                raise Precision2020BoundaryError(
                    BOUNDARY_INVALID_GEOMETRY_TYPE,
                    f"EXTERNAL_BOUNDARY de {bundle.talhao} e {feature.geometry}; aceito somente Polygon/MultiPolygon.",
                )
            if info["identity"] == "legacy_kind" or "(" in info["identity"]:
                result.warnings.append(
                    f"Limite de {bundle.talhao} identificado por {info['identity']}, nao por fortsmart_role."
                )
            source = dict(feature.extra.get("source_geometry") or {})
            require_source_crs(source)
            source_parts = boundary_polygon_parts(feature)
            if not source_parts:
                raise Precision2020BoundaryError(BOUNDARY_EMPTY, f"EXTERNAL_BOUNDARY de {bundle.talhao} vazio.")
            prepared = prepare_precision2020_boundary(source_parts, config=self.config)
            result.geometry = {
                "type": prepared.geometry_type,
                "valid": "YES" if prepared.valid_before else "NO",
                "invalid_reason": prepared.invalid_reason,
                "made_valid": prepared.made_valid,
                "make_valid_area_change_percent": prepared.make_valid_area_change_percent,
                "empty": "NO",
                "parts": prepared.part_count,
                "holes": prepared.holes,
                "source_parts": source.get("parts", len(source_parts)),
                "has_z": "YES" if source.get("has_z") else "NO",
                "has_m": "YES" if source.get("has_m") else "NO",
                "export_has_z": False,
                "export_has_m": False,
                "removed_duplicates": prepared.removed_duplicates,
                "closed_rings": prepared.closed_rings,
                "dropped_rings": prepared.dropped_rings,
                "original_vertices": prepared.original_vertices,
                "export_vertices": prepared.export_vertices,
                "area_ha": prepared.export_area_ha,
                "area_difference_percent": prepared.area_difference_percent,
                "export_bbox": prepared.bbox,
                "export_centroid": list(prepared.centroid),
            }
            source_count = int(source.get("parts") or len(source_parts))
            if source_count > prepared.part_count:
                lost = source_count - prepared.part_count
                if not prepared.dropped_rings and not prepared.made_valid:
                    raise Precision2020BoundaryError(
                        BOUNDARY_INVALID,
                        f"{lost} parte(s) do limite de {bundle.talhao} desapareceram na preparacao.",
                    )
                result.warnings.append(
                    f"{bundle.talhao}: {source_count} partes viraram {prepared.part_count} "
                    "(partes encostadas unidas pelo makeValid)."
                    if prepared.made_valid
                    else f"{bundle.talhao}: {lost} parte(s) degenerada(s) sem area removida(s) do limite."
                )
            if prepared.made_valid:
                change = prepared.make_valid_area_change_percent
                result.warnings.append(
                    f"Limite de {bundle.talhao} corrigido com makeValid ({prepared.invalid_reason})"
                    + (f"; variacao de area {change:.4f}%." if change is not None else ".")
                )
            self._event(result, EVENT_PREPARED)
            result.crs = crs_preflight(feature, prepared.centroid, self.config)
            if result.crs.get("warning"):
                result.warnings.append(f"{bundle.talhao}: {result.crs['warning']}")
            result.region = region_check(prepared, self.config)
            if result.region.get("warning"):
                result.warnings.append(f"{bundle.talhao}: {result.region['warning']}")
            self._event(result, EVENT_TRANSFORMED)
            result.status = "READY_TO_WRITE"
            return PreparedField(bundle, feature, prepared, result, source_parts)
        except Exception as exc:
            result.status = "ABORTED"
            result.error = str(exc)
            raise

    def write_field(
        self,
        field_plan: PreparedField,
        base_path: Path,
        attrs: dict[str, str],
        write_geometry: Callable[[Path, dict[str, Any], dict[str, str]], None],
        debug_root: Path | None = None,
    ) -> dict[str, Any]:
        result = field_plan.result
        result.filename = base_path.name
        try:
            try:
                write_geometry(base_path, field_plan.prepared.geojson_geometry(), attrs)
            except Exception as exc:
                raise Precision2020BoundaryError(SHAPEFILE_WRITE_FAILED, str(exc)) from exc
            self._event(result, EVENT_WRITTEN)
            postwrite = Precision2020BoundaryPostWriteValidator().validate(
                base_path, field_plan.prepared, self.config
            )
            self._event(result, EVENT_REOPENED)
            result.postwrite = postwrite
            self._event(result, EVENT_VALIDATED)
            result.status = PRECISION2020_DEVICE_STATUS
            if debug_root is not None and PRECISION2020_BOUNDARY_DEBUG:
                self._write_debug(field_plan, base_path, debug_root)
            return postwrite
        except Exception as exc:
            result.status = "ABORTED"
            result.error = str(exc)
            raise

    def check_field_spread(self) -> None:
        ready = [r for r in self.results if r.geometry.get("export_centroid")]
        if len(ready) < 2 or not _is_geographic(self.config.boundary_crs):
            return
        lons = sorted(r.geometry["export_centroid"][0] for r in ready)
        lats = sorted(r.geometry["export_centroid"][1] for r in ready)
        median = (lons[len(lons) // 2], lats[len(lats) // 2])
        limit_m = self.config.multi_field_distance_warning_km * 1000.0
        for item in ready:
            distance = _haversine_m(tuple(item.geometry["export_centroid"]), median)
            if distance > limit_m:
                message = (
                    f"{REGION_MISMATCH}: {item.field} a {distance / 1000.0:.0f} km dos demais talhoes. {REGION_MESSAGE}"
                )
                item.warnings.append(message)
                item.region = {"status": "WARNING", "warning": message}

    def _write_debug(self, field_plan: PreparedField, base_path: Path, debug_root: Path) -> None:
        folder = debug_root / base_path.name
        folder.mkdir(parents=True, exist_ok=True)

        def feature_collection(geometry: dict[str, Any]) -> str:
            return json.dumps(
                {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": geometry}]}
            )

        source = field_plan.source_parts
        source_geometry = (
            {"type": "Polygon", "coordinates": source[0]}
            if len(source) == 1
            else {"type": "MultiPolygon", "coordinates": source}
        )
        (folder / "boundary_source.geojson").write_text(feature_collection(source_geometry), encoding="utf-8")
        (folder / "boundary_export.geojson").write_text(
            feature_collection(field_plan.prepared.geojson_geometry()), encoding="utf-8"
        )
        for ext in (".shp", ".shx", ".dbf", ".prj"):
            if base_path.with_suffix(ext).is_file():
                shutil.copy2(base_path.with_suffix(ext), folder / f"boundary_export{ext}")

    def summary(self) -> dict[str, Any]:
        statuses = {result.status for result in self.results}
        return {
            "pipeline": "Precision2020BoundaryExporter",
            "export_crs": self.config.boundary_crs,
            "status": PRECISION2020_DEVICE_STATUS if statuses == {PRECISION2020_DEVICE_STATUS} else "ABORTED",
            "fields": [result.as_dict() for result in self.results],
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------
# Relatorios
# --------------------------------------------------------------------------

def _bbox_lines(label: str, bbox: Sequence[float] | None) -> list[str]:
    if not bbox:
        return [label, "n/d"]
    return [label, f"xmin = {bbox[0]}", f"ymin = {bbox[1]}", f"xmax = {bbox[2]}", f"ymax = {bbox[3]}"]


def format_precision2020_report(summary: dict[str, Any]) -> list[str]:
    lines = ["FortSmart Precision Planting 20|20 — Boundary", f"Export CRS: {summary.get('export_crs')}", ""]
    for row in summary.get("fields", []):
        geometry, crs, post = row.get("geometry", {}), row.get("crs", {}), row.get("postwrite", {})
        boundary = row.get("boundary", {})
        lines += [
            "PRECISION 20|20 BOUNDARY PREFLIGHT",
            "",
            f"Field: {row['field']}",
            f"Field UUID: {row['field_erid']}",
            "",
            "Boundary:",
            f"detected = {boundary.get('detected', 0)}",
            f"selected = {boundary.get('selected', 0)}",
            f"identity = {boundary.get('identity', '-')}",
            "",
            "Geometry:",
            f"type = {geometry.get('type', '-')}",
            f"valid = {geometry.get('valid', '-')}"
            + (f" ({geometry.get('invalid_reason')}; makeValid aplicado)" if geometry.get("made_valid") else ""),
            f"empty = {geometry.get('empty', '-')}",
            f"parts = {geometry.get('parts', '-')}",
            f"holes = {geometry.get('holes', '-')}",
            f"has_z = {geometry.get('has_z', '-')}",
            f"has_m = {geometry.get('has_m', '-')}",
            f"vertices = {geometry.get('export_vertices', '-')}",
            f"area_ha = {geometry.get('area_ha', 0.0):.4f}" if geometry.get("area_ha") is not None else "area_ha = -",
            "",
            "CRS:",
            f"source = {crs.get('source', '-')}",
            f"target = {crs.get('target', '-')}",
            f"transform = {crs.get('transform', '-')}",
            f"verified = {crs.get('verified', '-')}"
            + (
                f" (centro a {crs['centroid_error_m']:.3f} m)"
                if isinstance(crs.get("centroid_error_m"), (int, float))
                else ""
            ),
            "",
        ]
        lines += _bbox_lines("Source bbox:", crs.get("source_bbox")) + [""]
        lines += _bbox_lines("Export bbox:", geometry.get("export_bbox")) + [""]
        if post:
            lines += ["FILES"]
            lines += [f"{name}: {state}" for name, state in post.get("files", {}).items()]
            lines += [
                "",
                "POSTWRITE",
                f"Feature count: {post.get('feature_count')}",
                f"Geometry valid: {'YES' if post.get('geometry_valid') else 'NO'}",
                f"Location sanity: {post.get('location_sanity')}",
                "",
            ]
        lines += [f"Events: {', '.join(row.get('events', [])) or '-'}"]
        lines += [f"Aviso: {warning}" for warning in row.get("warnings", [])]
        if row.get("error"):
            lines.append(f"Erro: {row['error']}")
        lines += ["", "Status:", row.get("status", "-"), ""]
    lines += [f"Aviso: {warning}" for warning in summary.get("warnings", [])]
    lines += ["STATUS", summary.get("status", "-")]
    return lines


def write_precision2020_report(root: Path, summary: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / REPORT_JSON).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / REPORT_TEXT).write_text("\n".join(format_precision2020_report(summary)) + "\n", encoding="utf-8")


def precision2020_success_lines(summary: dict[str, Any]) -> list[str]:
    if summary.get("status") != PRECISION2020_DEVICE_STATUS:
        return [f"Pacote 20|20 incompleto: verifique {REPORT_TEXT}."]
    lines = ["Precision Planting 20|20"]
    for row in summary.get("fields", []):
        lines += [
            "",
            f"Talhão: {row['field']}",
            "",
            "Boundary",
            "✓ encontrado",
            "✓ geometria válida" + (" (corrigida com makeValid)" if row["geometry"].get("made_valid") else ""),
            f"✓ CRS validado ({row['crs'].get('source')} → {row['crs'].get('target')})",
            f"✓ transformação concluída ({row['crs'].get('transform')})",
            "✓ shapefile gravado",
            "✓ shapefile reaberto",
            "✓ coordenadas verificadas",
            "",
            "Arquivos:",
        ]
        lines += [f"✓ {name}" for name, state in row["postwrite"].get("files", {}).items() if state == "OK"]
        lines += [f"Aviso: {warning}" for warning in row.get("warnings", [])]
    lines += ["", "Status:", PRECISION2020_DEVICE_STATUS]
    return lines
