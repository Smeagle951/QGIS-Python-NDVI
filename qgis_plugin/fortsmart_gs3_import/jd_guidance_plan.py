"""FortSmart — plano comum das passadas planejadas para monitores John Deere.

As passadas sao identificadas pelo fortsmart_role canonico (WORK_PATH e
FIELD_HEADLAND_PATH), nunca pelo nome da camada nem pelo tipo de geometria.
O plano e semantico; cada adapter (GS3 ou Gen4) traduz para o proprio formato.
"""

from __future__ import annotations

import math
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .canonical_project import ROLE_KIND_MAP
from .gs3_spatial_types import Gs3FieldBundle, Gs3SpatialFeature, SpatialKind
from .jd_guidance_validators import GuidanceCoordinateValidator, validate_guidance_against_boundary


# Rollback individual por plataforma e por tipo de passada.
ENABLE_GS3_PLANTING_PASSES = True
ENABLE_GS3_BORDER_PASSES = True
ENABLE_GEN4_PLANTING_PASSES = True
ENABLE_GEN4_BORDER_PASSES = True
# Guidance exige as duas chaves; qualquer uma em False volta ao pacote sem passadas.
ENABLE_GS3_PLANTING_GUIDANCE = True
ENABLE_GS3_BORDER_GUIDANCE = True
ENABLE_GEN4_PLANTING_GUIDANCE = True
ENABLE_GEN4_BORDER_GUIDANCE = True

# Passa a GS3_DEVICE_VERIFIED / GEN4_DEVICE_VERIFIED somente apos teste no monitor real.
GS3_DEVICE_STATUS = "READY_FOR_DEVICE_TEST"
GEN4_DEVICE_STATUS = "READY_FOR_DEVICE_TEST"
GS3_PER_PASS_STATUS = "DEVICE_TEST_REQUIRED"

GS3 = "gs3"
GEN4 = "gen4"

PLANTING_PASS = "PLANTING_PASS"
BORDER_PASS = "BORDER_PASS"
PASS_CATEGORIES = (PLANTING_PASS, BORDER_PASS)
PASS_CATEGORY_BY_ROLE = {
    "WORK_PATH": PLANTING_PASS,
    "FIELD_HEADLAND_PATH": BORDER_PASS,
}
PASS_NAME_PREFIX = {PLANTING_PASS: "P", BORDER_PASS: "B"}
PASS_GROUP_NAME = {PLANTING_PASS: "PLANTIO", BORDER_PASS: "BORDADURA"}
PASS_LABEL = {PLANTING_PASS: "Planting passes", BORDER_PASS: "Border passes"}
PASS_SHORT_LABEL = {PLANTING_PASS: "Planting", BORDER_PASS: "Border"}
PLANTING_GUIDANCE = "PLANTING_GUIDANCE"
BORDER_GUIDANCE = "BORDER_GUIDANCE"
GUIDANCE_CATEGORY = {PLANTING_PASS: PLANTING_GUIDANCE, BORDER_PASS: BORDER_GUIDANCE}

GS3_INCLUDE_ROLES = frozenset(
    {
        "EXTERNAL_BOUNDARY",
        "OPERATIONAL_BOUNDARY",
        "INTERNAL_BOUNDARY",
        "HEADLAND",
        "AB_LINE",
        "AB_CURVE",
        "GUIDANCE_TRACK",
    }
)
SEMANTIC_MAPPING = {
    GS3: {
        "EXTERNAL_BOUNDARY": "Boundary",
        "OPERATIONAL_BOUNDARY": "Boundary",
        "INTERNAL_BOUNDARY": "Boundary/NameIDPair",
        "HEADLAND": "HeadLand",
        "AB_LINE": "ABLine",
        "AB_CURVE": "CurvedTrackLine",
        "GUIDANCE_TRACK": "CurvedTrackLine",
        "WORK_PATH": "CurvedTrackLine/PLANTING_PASS",
        "FIELD_HEADLAND_PATH": "CurvedTrackLine/BORDER_PASS",
    },
    GEN4: {
        "EXTERNAL_BOUNDARY": "OperationalBoundary",
        "OPERATIONAL_BOUNDARY": "OperationalBoundary",
        "AB_LINE": "ABLine",
        "AB_CURVE": "ABCurve",
        "GUIDANCE_TRACK": "AdaptiveCurve",
        "WORK_PATH": "Guidance/Tracks/AdaptiveCurve/PLANTING_PASS",
        "FIELD_HEADLAND_PATH": "Guidance/Tracks/AdaptiveCurve/BORDER_PASS",
    },
}
SKIP_GROUPS = {
    "work_paths": ("FINAL_WORK_PATH", "WORK_OFF", "WORK_ON", "SHUTOFF"),
    "headland_turns": ("HEADLAND_TURN",),
    "maneuvers": ("TRACTOR_PATH", "TIRE_TRACK", "OBSTACLE_DETOUR", "LATERAL_DETOUR_PATH"),
    "coverage": ("COVERAGE", "OVERLAP", "GAP", "IMPLEMENT_SWATH"),
}
SKIP_ROLES = frozenset(role for roles in SKIP_GROUPS.values() for role in roles)
# Nome de camada reconhecido ("linha plantio", "31_linhas_finais") e atributo
# "tipo" sao identidade valida; substring solta e geometria nao sao.
UNTRUSTED_RESOLUTIONS = frozenset({"legacy_name", "geometry_heuristic"})
FINAL_WORK_PATH = "FINAL_WORK_PATH"
NAME_HINTS = {
    PLANTING_PASS: ("PASSADAS_PLANTIO",),
    BORDER_PASS: ("PASSADAS_BORDADURA",),
}
IGNORED_NAME_MATCH = "ignored_layer_name_match_without_role"

GROUPING_PER_CATEGORY = "per_category"
GROUPING_PER_PASS = "per_pass"
GROUPINGS = (GROUPING_PER_CATEGORY, GROUPING_PER_PASS)
INVALID_ABORT = "abort"
INVALID_SKIP = "skip"

EMPTY_PACKAGE_MESSAGE = (
    "Pacote incompleto: foram solicitadas linhas de guia, mas nenhuma foi serializada."
)
INCOMPLETE_PACKAGE_MESSAGE = "Pacote incompleto: linhas solicitadas não foram serializadas."
FIELD_MISMATCH = "JOHN_DEERE_GUIDANCE_FIELD_MISMATCH"
NOT_COLLECTED = "JOHN_DEERE_GUIDANCE_NOT_COLLECTED"
PASS_NAME_PT = {PLANTING_PASS: "passadas de plantio", BORDER_PASS: "passadas de bordadura"}
ROLE_BY_PASS_CATEGORY = {category: role for role, category in PASS_CATEGORY_BY_ROLE.items()}


def not_collected_message(category: str, source_count: int) -> str:
    if source_count > 0:
        return (
            f"{NOT_COLLECTED}: Foram encontradas {PASS_NAME_PT[category]} no QGIS, "
            f"mas nenhuma chegou ao plano de exportação. "
            f"({source_count} feicao(oes) {ROLE_BY_PASS_CATEGORY[category]} nos talhoes selecionados.)"
        )
    return (
        f"{NOT_COLLECTED}: Exportacao de {PASS_NAME_PT[category]} solicitada, mas nenhuma "
        f"feicao {ROLE_BY_PASS_CATEGORY[category]} chegou ao plano de exportação dos talhoes selecionados. "
        f"{PASS_NAME_HELP[category]}"
    )


PASS_NAME_HELP = {
    PLANTING_PASS: (
        "Camadas de linhas aceitas pelo nome: 'linha plantio', 'passadas plantio', 'plantio', "
        "'trabalho' ou 'linhas finais' (usadas quando o talhao nao tem outra camada de plantio)."
    ),
    BORDER_PASS: (
        "Camadas de linhas aceitas pelo nome: 'passadas bordadura', 'linhas bordadura', "
        "'linhas cabeceira' ou camada de linhas chamada 'bordadura'."
    ),
}

# Mesmo namespace de identidade do export GS3 (cliente/fazenda).
IDENTITY_NAMESPACE = uuid.UUID("2345eeb4-e436-4d22-9474-3b858699f48a")
_EARTH_RADIUS_M = 6_371_008.8


class GuidanceGeometryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _enabled(platform: str, category: str) -> bool:
    flags = {
        (GS3, PLANTING_PASS): ENABLE_GS3_PLANTING_PASSES and ENABLE_GS3_PLANTING_GUIDANCE,
        (GS3, BORDER_PASS): ENABLE_GS3_BORDER_PASSES and ENABLE_GS3_BORDER_GUIDANCE,
        (GEN4, PLANTING_PASS): ENABLE_GEN4_PLANTING_PASSES and ENABLE_GEN4_PLANTING_GUIDANCE,
        (GEN4, BORDER_PASS): ENABLE_GEN4_BORDER_PASSES and ENABLE_GEN4_BORDER_GUIDANCE,
    }
    return bool(flags.get((platform, category), False))


guidance_enabled = _enabled


def layer_not_collected_errors(audit: Iterable[dict], categories: Iterable[str]) -> list[str]:
    """Camada de passada com feicoes no talhao exportado, mas nada coletado."""
    wanted = set(categories)
    errors = []
    for entry in audit:
        role = str(entry.get("fortsmart_role") or "").upper()
        if PASS_CATEGORY_BY_ROLE.get(role) not in wanted or entry.get("field_scope") is False:
            continue
        features = int(entry.get("features", 0) or 0)
        if features > 0 and int(entry.get("collected", 0) or 0) == 0:
            errors.append(
                f"JOHN_DEERE_{role}_NOT_COLLECTED: camada '{entry.get('layer_name', '-')}' "
                f"({role}) tem {features} feicao(oes), mas nenhuma foi coletada "
                f"(motivo: {entry.get('reason') or '-'})."
            )
    return errors


@dataclass(frozen=True)
class JohnDeereGuidanceOptions:
    include_planting_passes: bool = False
    include_border_passes: bool = False
    export_gs3: bool = True
    export_gen4: bool = True
    grouping: str = GROUPING_PER_CATEGORY
    gs3_straightness_tolerance_m: float = 0.10
    # Densidade de vertices das trilhas Deere aceitas (10-25 m entre pontos).
    max_segment_m: float = 20.0
    # Desvio maximo ao remover vertices redundantes (abaixo da precisao RTK).
    thinning_tolerance_m: float = 0.02
    max_planting_passes: int = 0
    max_border_passes: int = 0
    field_association_margin_m: float = 100.0
    allow_legacy_alias: bool = False
    invalid_geometry_policy: str = INVALID_ABORT
    # Feicoes WORK_PATH / FIELD_HEADLAND_PATH contadas direto nas camadas QGIS
    # dos talhoes selecionados, antes da coleta: ((categoria, total), ...).
    source_pass_counts: tuple = ()
    # Diagnostico por camada (coletor): layer_name, internal_key, role, visible...
    collection_audit: tuple = ()

    def source_count(self, category: str) -> int:
        return int(dict(self.source_pass_counts).get(category, 0) or 0)

    def wants(self, category: str) -> bool:
        if category == PLANTING_PASS:
            return bool(self.include_planting_passes)
        if category == BORDER_PASS:
            return bool(self.include_border_passes)
        return False

    def platform_requested(self, platform: str, category: str) -> bool:
        destination = self.export_gs3 if platform == GS3 else self.export_gen4
        return bool(destination and self.wants(category) and _enabled(platform, category))

    def requested(self, category: str) -> bool:
        """GS3: compatibilidade com o writer GS3."""
        return self.platform_requested(GS3, category)

    def gen4_requested(self, category: str) -> bool:
        return self.platform_requested(GEN4, category)

    def any_platform_requested(self, platform: str) -> bool:
        return any(self.platform_requested(platform, category) for category in PASS_CATEGORIES)

    @property
    def any_requested(self) -> bool:
        return self.any_platform_requested(GS3)

    @property
    def any_gen4_requested(self) -> bool:
        return self.any_platform_requested(GEN4)

    def prepared(self, category: str) -> bool:
        return any(self.platform_requested(platform, category) for platform in (GS3, GEN4))

    def limit(self, category: str) -> int:
        value = self.max_planting_passes if category == PLANTING_PASS else self.max_border_passes
        return max(0, int(value or 0))


# --------------------------------------------------------------------------
# Geometria
# --------------------------------------------------------------------------


def _local_xy(
    points: Sequence[tuple[float, float]],
    origin: tuple[float, float],
) -> list[tuple[float, float]]:
    lon0, lat0 = origin
    scale = math.pi / 180.0 * _EARTH_RADIUS_M
    k = math.cos(math.radians(lat0))
    return [((lon - lon0) * k * scale, (lat - lat0) * scale) for lon, lat in points]


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    (x, y), = _local_xy([b], a)
    return math.hypot(x, y)


def _point_segment_distance(
    p: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 0.0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def max_chord_deviation_m(points: Sequence[tuple[float, float]]) -> float:
    """Maior distancia (m) de um vertice ao segmento entre as extremidades."""
    if len(points) < 3:
        return 0.0
    xy = _local_xy(points, points[0])
    first, last = xy[0], xy[-1]
    return max(_point_segment_distance(p, first, last) for p in xy[1:-1])


def is_effectively_straight(
    points: Sequence[tuple[float, float]],
    tolerance_m: float = 0.10,
) -> bool:
    """Reta pela deflexao maxima da corda, nao pela quantidade de vertices."""
    if len(points) < 2:
        return False
    xy = _local_xy(points, points[0])
    if math.hypot(xy[-1][0] - xy[0][0], xy[-1][1] - xy[0][1]) <= max(tolerance_m, 1e-6):
        return False
    return max_chord_deviation_m(points) <= tolerance_m


def split_line_parts(
    points: Sequence[tuple[float, float]],
    part_breaks: Iterable[int] = (),
) -> list[list[tuple[float, float]]]:
    cuts = sorted({int(index) for index in part_breaks if 0 < int(index) < len(points)})
    bounds = [0, *cuts, len(points)]
    return [list(points[start:end]) for start, end in zip(bounds, bounds[1:]) if end > start]


def parse_part_breaks(raw: object) -> list[int]:
    values: list[int] = []
    for token in str(raw or "").replace(";", ",").split(","):
        token = token.strip()
        if token.isdigit():
            values.append(int(token))
    return values


def _densify(track: list[tuple[float, float]], max_segment_m: float) -> list[tuple[float, float]]:
    if max_segment_m <= 0.0 or len(track) < 2:
        return list(track)
    out = [track[0]]
    for a, b in zip(track, track[1:]):
        steps = int(math.ceil(_distance_m(a, b) / max_segment_m))
        for step in range(1, steps):
            t = step / steps
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        out.append(b)
    return out


# Only generator artefacts are thinned: centimetre spacing and enough vertices
# to push one Gen4 gjson past GEN4_MAX_GJSON_BYTES (~23 bytes per vertex).
OVER_DENSE_SPACING_M = 0.10
OVER_DENSE_MIN_VERTICES = 100_000


def _over_dense(track: Sequence[tuple[float, float]]) -> bool:
    if len(track) <= OVER_DENSE_MIN_VERTICES:
        return False
    length = sum(_distance_m(a, b) for a, b in zip(track, track[1:]))
    return length / (len(track) - 1) < OVER_DENSE_SPACING_M


def thin_track(
    track: Sequence[tuple[float, float]],
    tolerance_m: float,
) -> tuple[list[tuple[float, float]], float]:
    """Douglas-Peucker em metros locais; devolve a trilha e o desvio maximo real (m)."""
    if tolerance_m <= 0.0 or len(track) < 3:
        return list(track), 0.0
    xy = _local_xy(track, track[0])
    keep = [False] * len(track)
    keep[0] = keep[-1] = True
    deviation = 0.0
    stack = [(0, len(track) - 1)]
    while stack:
        start, end = stack.pop()
        if end - start < 2:
            continue
        ax, ay = xy[start]
        dx, dy = xy[end][0] - ax, xy[end][1] - ay
        length_sq = dx * dx + dy * dy
        farthest, index = -1.0, start
        for i in range(start + 1, end):
            px, py = xy[i][0] - ax, xy[i][1] - ay
            if length_sq > 0.0:
                t = min(1.0, max(0.0, (px * dx + py * dy) / length_sq))
                px, py = px - t * dx, py - t * dy
            dist_sq = px * px + py * py
            if dist_sq > farthest:
                farthest, index = dist_sq, i
        farthest = math.sqrt(farthest)
        if farthest > tolerance_m:
            keep[index] = True
            stack.append((start, index))
            stack.append((index, end))
        else:
            deviation = max(deviation, farthest)
    return [point for point, kept in zip(track, keep) if kept], deviation


@dataclass
class PreparedGuidance:
    tracks: list[list[tuple[float, float]]]
    original_vertices: int
    export_vertices: int
    removed_duplicates: int
    dropped_parts: int
    part_count: int
    straight: bool
    max_chord_deviation_m: float
    length_m: float
    thinned_vertices: int = 0
    thinning_deviation_m: float = 0.0


def prepare_john_deere_guidance_geometry(
    points: Sequence[tuple[float, float]],
    *,
    part_breaks: Iterable[int] = (),
    straightness_tolerance_m: float = 0.10,
    max_segment_m: float = 0.0,
    duplicate_tolerance_m: float = 0.001,
    min_part_length_m: float = 0.5,
    thinning_tolerance_m: float = 0.0,
) -> PreparedGuidance:
    """Copia limpa de uma passada WGS84 (lon, lat), sem recorte.

    O coletor ja converte uma copia da geometria QGIS para EPSG:4326; a camada
    original nao e tocada. Coordenadas fora da faixa geografica sao rejeitadas
    em vez de reprojetadas as cegas. Cada parte valida de uma MultiLineString
    vira uma trilha interna, na ordem original. Com thinning_tolerance_m > 0,
    vertices redundantes saem enquanto o desvio ficar dentro da tolerancia; as
    extremidades e o comprimento da passada sao preservados.
    """
    if not points:
        raise GuidanceGeometryError("GUIDANCE_EMPTY", "geometria vazia")
    for lon, lat in points:
        if not (math.isfinite(lon) and math.isfinite(lat)):
            raise GuidanceGeometryError("GUIDANCE_INVALID_COORDINATE", "coordenada NaN/Inf")
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            raise GuidanceGeometryError(
                "GUIDANCE_NOT_WGS84",
                f"coordenada fora de WGS84 ({lon}, {lat})",
            )

    parts = split_line_parts(points, part_breaks)
    tracks: list[list[tuple[float, float]]] = []
    removed = 0
    dropped = 0
    for part in parts:
        clean: list[tuple[float, float]] = []
        for point in part:
            pt = (float(point[0]), float(point[1]))
            if clean and _distance_m(clean[-1], pt) < duplicate_tolerance_m:
                removed += 1
                continue
            clean.append(pt)
        length = sum(_distance_m(a, b) for a, b in zip(clean, clean[1:]))
        if len(clean) < 2 or length < min_part_length_m:
            dropped += 1
            continue
        tracks.append(clean)
    if not tracks:
        raise GuidanceGeometryError(
            "GUIDANCE_DEGENERATE",
            "passada sem segmento valido (menos de 2 vertices distintos)",
        )

    deviation = max(max_chord_deviation_m(track) for track in tracks)
    straight = all(is_effectively_straight(track, straightness_tolerance_m) for track in tracks)
    length = sum(_distance_m(a, b) for track in tracks for a, b in zip(track, track[1:]))
    thinned_tracks = [
        thin_track(track, thinning_tolerance_m) if _over_dense(track) else (list(track), 0.0)
        for track in tracks
    ]
    thinned = sum(len(track) - len(kept) for track, (kept, _dev) in zip(tracks, thinned_tracks))
    exported = [_densify(kept, max_segment_m) for kept, _dev in thinned_tracks]
    return PreparedGuidance(
        tracks=exported,
        original_vertices=len(points),
        export_vertices=sum(len(track) for track in exported),
        removed_duplicates=removed,
        dropped_parts=dropped,
        part_count=len(parts),
        straight=straight,
        max_chord_deviation_m=deviation,
        length_m=length,
        thinned_vertices=thinned,
        thinning_deviation_m=max(dev for _kept, dev in thinned_tracks),
    )


prepare_gs3_guidance_geometry = prepare_john_deere_guidance_geometry


# --------------------------------------------------------------------------
# Plano de exportacao
# --------------------------------------------------------------------------


def _role(feature: Gs3SpatialFeature) -> str:
    return str(feature.extra.get("canonical_role", "") or "").strip().upper()


def normalized_uuid(value: object) -> str:
    try:
        return str(uuid.UUID(str(value or "").strip().strip("{}")))
    except ValueError:
        return ""


def final_lines_are_planting(bundle: Gs3FieldBundle) -> bool:
    """Talhao sem WORK_PATH: as linhas finais sao as passadas de plantio dele."""
    roles = {_role(feature) for feature in bundle.features}
    return "WORK_PATH" not in roles and FINAL_WORK_PATH in roles


def pass_category(
    feature: Gs3SpatialFeature,
    *,
    allow_legacy_alias: bool = False,
    final_as_planting: bool = False,
) -> str | None:
    """Categoria de passada pela role canonica, ou None."""
    role = _role(feature)
    category = PASS_CATEGORY_BY_ROLE.get(role)
    if category is None and final_as_planting and role == FINAL_WORK_PATH:
        category = PLANTING_PASS
    if category is None and role not in ROLE_KIND_MAP:
        layer_role = str(feature.extra.get("layer_role", "") or "").strip().upper()
        category = PASS_CATEGORY_BY_ROLE.get(layer_role)
    if category is None:
        return None
    resolution = str(feature.extra.get("role_resolution", "") or "")
    if resolution in UNTRUSTED_RESOLUTIONS and not allow_legacy_alias:
        return None
    return category


def _name_hint(feature: Gs3SpatialFeature) -> str | None:
    """Somente diagnostico: nome parecido com passada, sem role confiavel."""
    text = re.sub(r"[^A-Z0-9]+", "_", f"{feature.source_file} {feature.name}".upper())
    for category, hints in NAME_HINTS.items():
        if any(hint in text for hint in hints):
            return category
    return None


def _pass_number(feature: Gs3SpatialFeature) -> int | None:
    raw = feature.extra.get("pass_number")
    try:
        number = int(float(str(raw)))
        if number > 0:
            return number
    except (TypeError, ValueError):
        pass
    match = re.search(r"(\d+)\s*$", str(feature.extra.get("pass_label", "") or ""))
    if match and int(match.group(1)) > 0:
        return int(match.group(1))
    return None


def _boundary_bbox(bundle: Gs3FieldBundle) -> tuple[float, float, float, float] | None:
    points: list[tuple[float, float]] = []
    for feature in bundle.features:
        role = _role(feature)
        if role in ("EXTERNAL_BOUNDARY", "OPERATIONAL_BOUNDARY") or (
            not role and feature.kind == SpatialKind.LIMITE
        ):
            for ring in feature.rings or [feature.points]:
                points.extend(ring)
    if not points:
        return None
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return min(lons), min(lats), max(lons), max(lats)


@dataclass
class GuidancePassItem:
    category: str
    field_erid: str
    talhao: str
    pass_number: int
    name: str
    item_erid: str
    tracks: list[list[tuple[float, float]]]
    straight: bool
    max_chord_deviation_m: float
    original_vertices: int
    export_vertices: int
    part_count: int = 1
    source_layer: str = ""


@dataclass
class FieldGuidancePlan:
    field_erid: str
    talhao: str
    items: dict[str, list[GuidancePassItem]] = field(
        default_factory=lambda: {category: [] for category in PASS_CATEGORIES}
    )
    detected: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    skipped_roles: Counter = field(default_factory=Counter)
    skipped_by_role: Counter = field(default_factory=Counter)
    invalid: list[dict] = field(default_factory=list)
    # CRS/coordenadas/regiao: sempre abortam, independente da politica de geometria.
    coordinate_errors: list[dict] = field(default_factory=list)
    boundary_bbox: tuple[float, float, float, float] | None = None
    uuid_errors: int = 0
    warnings: list[str] = field(default_factory=list)
    planting_source_role: str = "WORK_PATH"

    @property
    def invalid_geometries(self) -> int:
        return len(self.invalid)

    def included(self, category: str) -> int:
        return len(self.items[category])


@dataclass
class JohnDeereGuidanceExportPlan:
    options: JohnDeereGuidanceOptions
    include_roles: frozenset
    skip_roles: frozenset
    fields: dict[str, FieldGuidancePlan] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def for_field(self, field_erid: str) -> FieldGuidancePlan:
        key = normalized_uuid(field_erid)
        return self.fields.get(key) or FieldGuidancePlan(field_erid=key, talhao="")

    def detected_count(self, category: str) -> int:
        return sum(plan.detected[category] for plan in self.fields.values())

    def included_count(self, category: str) -> int:
        return sum(plan.included(category) for plan in self.fields.values())

    def ignored_name_matches(self, category: str | None = None) -> int:
        key = IGNORED_NAME_MATCH if category is None else f"{IGNORED_NAME_MATCH}:{category}"
        return sum(plan.skipped[key] for plan in self.fields.values())

    def preflight_errors(self, platform: str, exported_field_erids: Iterable[str]) -> list[str]:
        """Erros que impedem gerar o pacote, antes de qualquer writer."""
        exported = {normalized_uuid(erid) for erid in exported_field_erids}
        tag = "GS3" if platform == GS3 else "GEN4"
        requested = [c for c in PASS_CATEGORIES if self.options.platform_requested(platform, c)]
        errors: list[str] = layer_not_collected_errors(self.options.collection_audit, requested)
        for category in PASS_CATEGORIES:
            if not self.options.platform_requested(platform, category):
                continue
            detected = self.detected_count(category)
            included = self.included_count(category)
            ignored = self.ignored_name_matches(category)
            if detected == 0 and ignored:
                errors.append(
                    f"{IGNORED_NAME_MATCH}: {ignored} feicao(oes) com nome de "
                    f"{PASS_GROUP_NAME[category].lower()} sem fortsmart_role valido; "
                    "nenhuma passada foi identificada pelo papel canonico."
                )
            elif detected == 0:
                errors.append(not_collected_message(category, self.options.source_count(category)))
            elif detected and included == 0:
                errors.append(
                    f"{tag}_GUIDANCE_SERIALIZATION_EMPTY: Foram detectadas {detected} passadas "
                    f"({category}), mas nenhuma e elegivel para o pacote."
                )
        if self.options.invalid_geometry_policy == INVALID_ABORT:
            for plan in self.fields.values():
                for entry in plan.invalid:
                    if self.options.platform_requested(platform, entry["category"]):
                        errors.append(
                            f"JOHN_DEERE_GUIDANCE_INVALID_GEOMETRY: Field {plan.talhao} "
                            f"pass={entry['pass']} status=invalid_geometry ({entry['reason']})"
                        )
        for plan in self.fields.values():
            for entry in plan.coordinate_errors:
                if self.options.platform_requested(platform, entry["category"]):
                    errors.append(
                        f"{entry['code']}: Field {plan.talhao} pass={entry['pass']} {entry['message']}"
                    )
        for plan in self.fields.values():
            wanted = sum(
                plan.included(category)
                for category in PASS_CATEGORIES
                if self.options.platform_requested(platform, category)
            )
            if wanted and plan.field_erid not in exported:
                errors.append(
                    f"{FIELD_MISMATCH} ({tag}_GUIDANCE_FIELD_MISMATCH): {wanted} passada(s) do talhao "
                    f"{plan.talhao} sem Field {tag} exportado (limite externo ausente ou talhao nao selecionado)."
                )
        return errors

    def preflight_report(self) -> dict:
        return {
            "include_roles": sorted(self.include_roles),
            "skip_roles": sorted(self.skip_roles),
            "options": {
                "planting": self.options.include_planting_passes,
                "border": self.options.include_border_passes,
                "gs3": self.options.export_gs3,
                "gen4": self.options.export_gen4,
                "grouping": self.options.grouping,
                "max_planting_passes": self.options.max_planting_passes,
                "max_border_passes": self.options.max_border_passes,
                "invalid_geometry_policy": self.options.invalid_geometry_policy,
            },
            "fields": [
                {
                    "name": plan.talhao,
                    "field_erid": plan.field_erid,
                    "planting_source_role": plan.planting_source_role,
                    "planting": {
                        "detected": plan.detected[PLANTING_PASS],
                        "eligible": plan.included(PLANTING_PASS),
                    },
                    "border": {
                        "detected": plan.detected[BORDER_PASS],
                        "eligible": plan.included(BORDER_PASS),
                    },
                    "skipped_by_role": dict(plan.skipped_by_role),
                    "skipped": dict(plan.skipped),
                    "invalid": list(plan.invalid),
                    "coordinate_errors": list(plan.coordinate_errors),
                    "warnings": list(plan.warnings),
                }
                for plan in self.fields.values()
            ],
            "source_pass_counts": dict(self.options.source_pass_counts),
            "collection": [dict(entry) for entry in self.options.collection_audit],
            "warnings": list(self.warnings),
        }


GS3ExportPlan = JohnDeereGuidanceExportPlan
GS3PassOptions = JohnDeereGuidanceOptions
GS3PassItem = GuidancePassItem
GS3FieldPassPlan = FieldGuidancePlan


def build_john_deere_guidance_plan(
    bundles: Sequence[Gs3FieldBundle],
    options: JohnDeereGuidanceOptions | None = None,
) -> JohnDeereGuidanceExportPlan:
    """Plano explicito: o que entra e o que fica fora do pacote John Deere."""
    options = options or JohnDeereGuidanceOptions()
    if options.grouping not in GROUPINGS:
        raise ValueError(f"Agrupamento GS3 invalido: {options.grouping}")
    include_roles = set(GS3_INCLUDE_ROLES)
    for role, category in PASS_CATEGORY_BY_ROLE.items():
        if options.prepared(category):
            include_roles.add(role)
    plan = JohnDeereGuidanceExportPlan(
        options=options,
        include_roles=frozenset(include_roles),
        skip_roles=SKIP_ROLES,
    )
    coordinates = GuidanceCoordinateValidator()

    for bundle in bundles:
        field_erid = normalized_uuid(bundle.field_erid)
        if not field_erid:
            plan.warnings.append(f"Talhao '{bundle.talhao}' sem field_erid valido: passadas ignoradas.")
            continue
        field_plan = plan.fields.get(field_erid) or FieldGuidancePlan(field_erid, bundle.talhao)
        plan.fields[field_erid] = field_plan
        bbox = _boundary_bbox(bundle)
        field_plan.boundary_bbox = bbox
        candidates: dict[str, list[tuple[int, Gs3SpatialFeature, PreparedGuidance]]] = {
            category: [] for category in PASS_CATEGORIES
        }
        final_as_planting = final_lines_are_planting(bundle)
        if final_as_planting:
            field_plan.planting_source_role = FINAL_WORK_PATH

        for index, feature in enumerate(bundle.features):
            role = _role(feature)
            if role in SKIP_ROLES and not (final_as_planting and role == FINAL_WORK_PATH):
                group = next(name for name, roles in SKIP_GROUPS.items() if role in roles)
                field_plan.skipped_roles[group] += 1
                field_plan.skipped_by_role[role] += 1
                continue
            category = pass_category(
                feature,
                allow_legacy_alias=options.allow_legacy_alias,
                final_as_planting=final_as_planting,
            )
            if category is None:
                hint = _name_hint(feature) if role not in GS3_INCLUDE_ROLES else None
                if hint is not None:
                    field_plan.skipped[IGNORED_NAME_MATCH] += 1
                    field_plan.skipped[f"{IGNORED_NAME_MATCH}:{hint}"] += 1
                continue
            field_plan.detected[category] += 1
            if not options.prepared(category):
                field_plan.skipped[f"{category}_nao_solicitada"] += 1
                continue
            label = feature.extra.get("pass_label") or (
                f"{PASS_NAME_PREFIX[category]}{_pass_number(feature)}"
                if _pass_number(feature)
                else f"#{index + 1}"
            )
            if feature.geometry != "line":
                field_plan.invalid.append(
                    {"category": category, "pass": label, "reason": f"geometria {feature.geometry}"}
                )
                field_plan.warnings.append(
                    f"{bundle.talhao}: passada {label} status=invalid_geometry "
                    f"(geometria {feature.geometry}, esperado linha)."
                )
                continue
            issue = coordinates.check(feature.extra, feature.points)
            if issue is not None:
                field_plan.coordinate_errors.append(
                    {"category": category, "pass": label, "code": issue.code, "message": issue.message}
                )
                field_plan.warnings.append(f"{bundle.talhao}: passada {label} {issue}.")
                continue
            try:
                prepared = prepare_john_deere_guidance_geometry(
                    feature.points,
                    part_breaks=parse_part_breaks(feature.extra.get("part_breaks")),
                    straightness_tolerance_m=options.gs3_straightness_tolerance_m,
                    max_segment_m=options.max_segment_m,
                    thinning_tolerance_m=options.thinning_tolerance_m,
                )
            except GuidanceGeometryError as exc:
                field_plan.invalid.append({"category": category, "pass": label, "reason": str(exc)})
                field_plan.warnings.append(
                    f"{bundle.talhao}: passada {label} status=invalid_geometry ({exc})."
                )
                continue
            issue = (
                validate_guidance_against_boundary(prepared.tracks, bbox, options.field_association_margin_m)
                if bbox is not None
                else None
            )
            if issue is not None:
                field_plan.skipped["fora_do_talhao"] += 1
                field_plan.coordinate_errors.append(
                    {"category": category, "pass": label, "code": issue.code, "message": issue.message}
                )
                field_plan.warnings.append(f"{bundle.talhao}: passada {label} {issue}.")
                continue
            if prepared.thinned_vertices >= max(100, prepared.original_vertices // 2):
                field_plan.warnings.append(
                    f"{bundle.talhao}: passada {label} com vertices excessivos "
                    f"({prepared.original_vertices} -> {prepared.export_vertices}); "
                    f"{prepared.thinned_vertices} redundantes removidos, desvio maximo "
                    f"{prepared.thinning_deviation_m * 100:.1f} cm "
                    f"(tolerancia {options.thinning_tolerance_m * 100:.1f} cm, sem recorte)."
                )
            candidates[category].append((index, feature, prepared))

        used_erids: set[str] = set()
        for category, entries in candidates.items():
            entries.sort(key=lambda entry: (_pass_number(entry[1]) or 10**9, entry[0]))
            limit = options.limit(category)
            if limit and len(entries) > limit:
                field_plan.skipped[f"{category}_limite_teste"] += len(entries) - limit
                entries = entries[:limit]
            used_names: Counter = Counter()
            for sequence, (index, feature, prepared) in enumerate(entries, start=1):
                number = _pass_number(feature) or sequence
                name = f"{PASS_NAME_PREFIX[category]}{number}"
                used_names[name] += 1
                if used_names[name] > 1:
                    name = f"{name}.{used_names[name]}"
                item_erid = normalized_uuid(feature.item_erid)
                if not item_erid or item_erid in used_erids:
                    field_plan.uuid_errors += 1
                    item_erid = str(
                        uuid.uuid5(
                            IDENTITY_NAMESPACE,
                            f"guidance-pass|{field_erid}|{category}|{index}|{feature.item_erid}",
                        )
                    )
                used_erids.add(item_erid)
                field_plan.items[category].append(
                    GuidancePassItem(
                        category=category,
                        field_erid=field_erid,
                        talhao=bundle.talhao,
                        pass_number=number,
                        name=name,
                        item_erid=item_erid,
                        tracks=prepared.tracks,
                        straight=prepared.straight,
                        max_chord_deviation_m=prepared.max_chord_deviation_m,
                        original_vertices=prepared.original_vertices,
                        export_vertices=prepared.export_vertices,
                        part_count=prepared.part_count,
                        source_layer=feature.source_file,
                    )
                )
    return plan


build_gs3_export_plan = build_john_deere_guidance_plan


def guidance_group_name(category: str, items: Sequence[GuidancePassItem]) -> str:
    if len(items) == 1:
        return items[0].name
    return f"{PASS_GROUP_NAME[category]} {items[0].name}-{items[-1].name}"


def guidance_plan_for(
    bundles: Sequence[Gs3FieldBundle],
    options: JohnDeereGuidanceOptions | None,
    platforms: Iterable[str],
) -> JohnDeereGuidanceExportPlan | None:
    """Plano sobre os bundles nao filtrados; None mantem o pacote atual."""
    if options is None:
        return None
    if not any(options.any_platform_requested(platform) for platform in platforms):
        return None
    return build_john_deere_guidance_plan(bundles, options)


def format_preflight_report(report: dict) -> list[str]:
    lines = []
    if report.get("collection"):
        lines.append("QGIS COLLECTION")
        lines.extend(format_layer_audit(entry) for entry in report["collection"])
        lines.append("")
    lines.append("GUIDANCE PREFLIGHT")
    for row in report.get("fields", []):
        lines.append(f"Field: {row.get('name', '-')}")
        for key, label in (("planting", "Planting"), ("border", "Border")):
            counts = row.get(key, {})
            lines.append(
                f"{label}: detected = {counts.get('detected', 0)} eligible = {counts.get('eligible', 0)}"
            )
        for role, count in sorted(row.get("skipped_by_role", {}).items()):
            lines.append(f"{role}: detected = {count} include = 0")
        ignored = row.get("skipped", {}).get(IGNORED_NAME_MATCH, 0)
        if ignored:
            lines.append(f"{IGNORED_NAME_MATCH}: {ignored}")
        for entry in row.get("invalid", []):
            lines.append(f"pass = {entry['pass']} status = invalid_geometry ({entry['reason']})")
        for entry in row.get("coordinate_errors", []):
            lines.append(f"pass = {entry['pass']} status = {entry['code']} ({entry['message']})")
        lines.append("")
    return lines


# --------------------------------------------------------------------------
# Preview e diagnostico da coleta QGIS
# --------------------------------------------------------------------------

BOUNDARY = "BOUNDARY"
INTERNAL_BOUNDARY = "INTERNAL_BOUNDARY"
HEADLAND = "HEADLAND"
PREVIEW_LABELS = (
    (BOUNDARY, "Limites"),
    (INTERNAL_BOUNDARY, "Limites internos"),
    (HEADLAND, "Bordaduras"),
    (PLANTING_PASS, "Passadas de plantio"),
    (BORDER_PASS, "Passadas de bordadura"),
)
_ROLE_PREVIEW = {
    "EXTERNAL_BOUNDARY": BOUNDARY,
    "OPERATIONAL_BOUNDARY": BOUNDARY,
    "INTERNAL_BOUNDARY": INTERNAL_BOUNDARY,
    "HEADLAND": HEADLAND,
}
_KIND_PREVIEW = {
    SpatialKind.LIMITE: BOUNDARY,
    SpatialKind.LIMITE_INTERNO: INTERNAL_BOUNDARY,
    SpatialKind.BORDADURA: HEADLAND,
}


def john_deere_preview_counts(
    bundles: Sequence[Gs3FieldBundle],
    *,
    allow_legacy_alias: bool = False,
) -> dict[str, int]:
    """Mesma classificacao do plano: passadas pela role, nunca pelo nome."""
    counts = {key: 0 for key, _ in PREVIEW_LABELS}
    for bundle in bundles:
        final_as_planting = final_lines_are_planting(bundle)
        for feature in bundle.features:
            category = pass_category(
                feature, allow_legacy_alias=allow_legacy_alias, final_as_planting=final_as_planting
            )
            if category is not None:
                counts[category] += 1
                continue
            role = _role(feature)
            key = _ROLE_PREVIEW.get(role) if role else _KIND_PREVIEW.get(feature.kind)
            if key is not None:
                counts[key] += 1
    return counts


def guidance_block_reasons(
    counts: dict[str, int],
    options: JohnDeereGuidanceOptions | None,
    platforms: Iterable[str],
) -> list[str]:
    """Passadas solicitadas que nao chegaram ao plano: exportacao bloqueada."""
    if options is None:
        return []
    platforms = list(platforms)
    return [
        not_collected_message(category, options.source_count(category))
        for category in PASS_CATEGORIES
        if any(options.platform_requested(platform, category) for platform in platforms)
        and counts.get(category, 0) == 0
    ]


def format_preview_counts(counts: dict[str, int]) -> list[str]:
    return [f"  • {label}: {counts.get(key, 0)}" for key, label in PREVIEW_LABELS]


PASS_OPTION_LABEL = {
    PLANTING_PASS: "Exportar passadas de plantio",
    BORDER_PASS: "Exportar passadas de bordadura",
}


def detected_pass_counts(bundles: Sequence[Gs3FieldBundle], audit: Iterable[dict]) -> dict[str, int]:
    """Passadas coletadas + camadas de passada do talhao que ficaram de fora (ex.: ocultas)."""
    counts = john_deere_preview_counts(bundles)
    for entry in pass_layer_audit(audit):
        category = PASS_CATEGORY_BY_ROLE[str(entry.get("fortsmart_role")).upper()]
        if not entry.get("collected") and entry.get("field_scope") is not False:
            counts[category] += int(entry.get("features", 0) or 0)
    return counts


def final_lines_source_lines(bundles: Sequence[Gs3FieldBundle]) -> list[str]:
    """Talhoes cujas passadas de plantio vem da camada de linhas finais."""
    lines = []
    for bundle in bundles:
        if not final_lines_are_planting(bundle):
            continue
        finals = [feature for feature in bundle.features if _role(feature) == FINAL_WORK_PATH]
        layers = sorted({feature.source_file for feature in finals if feature.source_file})
        lines.append(
            f"  • {bundle.talhao}: {len(finals)} linha(s) finais usadas como passadas de plantio "
            f"(camada {', '.join(layers) or '-'}; talhao sem WORK_PATH)"
        )
    return ["Passadas de plantio a partir de linhas finais:", *lines] if lines else []


def multiple_external_boundary_errors(bundles: Sequence[Gs3FieldBundle]) -> list[str]:
    """Talhoes (grupos QGIS) com mais de um limite externo, que o monitor nao aceita."""
    errors = []
    for bundle in bundles:
        exteriors = [
            feature for feature in bundle.features
            if feature.kind == SpatialKind.LIMITE and (feature.rings or len(feature.points) >= 3)
        ]
        if len(exteriors) <= 1:
            continue
        details = []
        for feature in exteriors:
            ring = feature.rings[0] if feature.rings else feature.points
            area_m2 = abs(_ring_area_m2(ring))
            details.append(f"{feature.source_file or feature.name or '-'} {area_m2 / 10000:.2f} ha")
        errors.append(
            f"JOHN_DEERE_FIELD_MULTIPLE_EXTERNAL_BOUNDARIES: talhao '{bundle.talhao}' tem "
            f"{len(exteriors)} limites externos ({'; '.join(details)}). O monitor aceita 1 limite "
            "externo por talhao: mova as camadas de cada talhao para um grupo proprio no QGIS."
        )
    return errors


def require_single_external_boundary(bundles: Sequence[Gs3FieldBundle]) -> None:
    errors = multiple_external_boundary_errors(bundles)
    if errors:
        raise ValueError("\n".join(errors))


def _ring_area_m2(ring: Sequence[tuple[float, float]]) -> float:
    if len(ring) < 3:
        return 0.0
    xy = _local_xy(ring, ring[0])
    return 0.5 * sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(xy, xy[1:] + xy[:1]))


def unrequested_passes(
    counts: dict[str, int],
    options: JohnDeereGuidanceOptions | None,
    platforms: Iterable[str],
) -> list[tuple[str, int]]:
    """Passadas existentes no projeto cuja exportacao nao foi marcada."""
    platforms = list(platforms)
    return [
        (category, counts.get(category, 0))
        for category in PASS_CATEGORIES
        if counts.get(category, 0)
        and not (options is not None and any(options.platform_requested(p, category) for p in platforms))
    ]


def guidance_inclusion_lines(
    counts: dict[str, int],
    options: JohnDeereGuidanceOptions | None,
    platforms: Iterable[str],
) -> list[str]:
    labels = dict(PREVIEW_LABELS)
    platforms = list(platforms)
    lines = []
    for category in PASS_CATEGORIES:
        total = counts.get(category, 0)
        if not total:
            continue
        targets = [
            platform.upper() for platform in platforms
            if options is not None and options.platform_requested(platform, category)
        ]
        if targets:
            lines.append(f"  ✓ {labels[category]}: {total} serão exportadas ({' + '.join(targets)})")
        else:
            lines.append(
                f"  ✗ {labels[category]}: {total} NÃO serão exportadas — marque "
                f"'{PASS_OPTION_LABEL[category]}' em JOHN DEERE — GUIDANCE"
            )
    return ["Linhas de guia no pacote:", *lines] if lines else []


def _flag(value) -> str:
    if value is None:
        return "-"
    return "true" if value else "false"


def format_layer_audit(entry: dict) -> str:
    collected = int(entry.get("collected", 0) or 0)
    features = int(entry.get("features", 0) or 0)
    line = (
        f"{entry.get('layer_name', '-')} | internal_key={entry.get('internal_key') or '-'} "
        f"| role={entry.get('fortsmart_role') or '-'} | identity={entry.get('resolution') or '-'} "
        f"| visible={_flag(entry.get('visible'))} "
        f"| selected={_flag(entry.get('selected'))} "
        f"| collected={_flag(collected > 0)} ({collected}/{features}) "
        f"| reason_if_skipped={entry.get('reason') or '-'}"
    )
    if entry.get("field_id"):
        line += f" | field_id={entry['field_id']}"
    return line


def source_pass_counts(audit: Iterable[dict]) -> tuple:
    """Feicoes de passada nas camadas QGIS dos talhoes selecionados (antes da coleta)."""
    totals = Counter()
    for entry in audit:
        category = PASS_CATEGORY_BY_ROLE.get(str(entry.get("fortsmart_role") or "").upper())
        if category is not None and entry.get("in_scope"):
            totals[category] += int(entry.get("features", 0) or 0)
    return tuple((category, totals[category]) for category in PASS_CATEGORIES if totals[category])


def pass_layer_audit(audit: Iterable[dict]) -> list[dict]:
    return [
        entry for entry in audit
        if str(entry.get("fortsmart_role") or "").upper() in PASS_CATEGORY_BY_ROLE
    ]
