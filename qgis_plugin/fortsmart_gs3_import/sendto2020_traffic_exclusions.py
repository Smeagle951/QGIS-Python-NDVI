# -*- coding: utf-8 -*-
"""Modo opcional do SendTo2020: boundary + exclusoes dos rastros do pulverizador.

Usado somente quando o usuario escolhe BOUNDARY_WITH_TRAFFIC_EXCLUSIONS. Le apenas a
geracao operacional ACTIVE gravada pelo FortSmart Precision no GeoPackage do talhao
(conexao somente leitura) e nunca recalcula rastros, larguras, margens ou WORK_OFF.
O limite externo preparado pelo caminho normal e copiado; as exclusoes entram como
aneis internos da mesma feicao.
"""

from __future__ import annotations

import copy
import json
import math
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import precision2020_boundary as safe_boundary


BOUNDARY_ONLY = "BOUNDARY_ONLY"
BOUNDARY_WITH_TRAFFIC_EXCLUSIONS = "BOUNDARY_WITH_TRAFFIC_EXCLUSIONS"
BOUNDARY_MODES = (BOUNDARY_ONLY, BOUNDARY_WITH_TRAFFIC_EXCLUSIONS)
DEFAULT_BOUNDARY_MODE = BOUNDARY_ONLY
BOUNDARY_MODE_LABELS = {
    BOUNDARY_ONLY: "Boundary normal",
    BOUNDARY_WITH_TRAFFIC_EXCLUSIONS: "Boundary + exclusoes dos rastros",
}

# Conceito interno do adaptador 20|20; nao e um role canonico do projeto.
TRAFFIC_EXCLUSION = "TRAFFIC_EXCLUSION"

INTERIOR_SAFE = "INTERIOR_SAFE"
TOUCHES_EXTERIOR = "TOUCHES_EXTERIOR"
CROSSES_EXTERIOR = "CROSSES_EXTERIOR"
OUTSIDE_FIELD = "OUTSIDE_FIELD"
INVALID = "INVALID"

NO_ACTIVE_GENERATION = "SENDTO2020_TRAFFIC_NO_ACTIVE_GENERATION"
STALE_GENERATION = "SENDTO2020_TRAFFIC_STALE_GENERATION"
DRAFT_GENERATION = "SENDTO2020_TRAFFIC_DRAFT_GENERATION"
MULTIPLE_ACTIVE = "SENDTO2020_TRAFFIC_MULTIPLE_ACTIVE"
FIELD_ID_MISMATCH = "SENDTO2020_TRAFFIC_FIELD_ID_MISMATCH"
GENERATION_INVALID = "SENDTO2020_TRAFFIC_GENERATION_INVALID"
WORKING_CRS_INVALID = "SENDTO2020_TRAFFIC_WORKING_CRS_INVALID"
NO_TIRE_TRACKS = "SENDTO2020_TRAFFIC_NO_TIRE_TRACKS"
TRACKS_OUTSIDE_FIELD = "SENDTO2020_TRAFFIC_TRACKS_OUTSIDE_FIELD"
EXCLUSION_TOUCHES = "SENDTO2020_TRAFFIC_EXCLUSION_TOUCHES_EXTERIOR"
EXCLUSION_CROSSES = "SENDTO2020_TRAFFIC_EXCLUSION_CROSSES_EXTERIOR"
EXCLUSION_INVALID = "SENDTO2020_TRAFFIC_EXCLUSION_INVALID"
ENCLOSED_ISLAND = "SENDTO2020_TRAFFIC_ENCLOSED_ISLAND"
WORK_OFF_MISMATCH = "SENDTO2020_TRAFFIC_WORK_OFF_MISMATCH"
TOO_COMPLEX = "SENDTO2020_TRAFFIC_TOO_COMPLEX"
COMPOSITION_FAILED = "SENDTO2020_TRAFFIC_COMPOSITION_FAILED"
POSTWRITE_FAILED = "SENDTO2020_TRAFFIC_POSTWRITE_FAILED"
SAFE_PIPELINE_REQUIRED = "SENDTO2020_TRAFFIC_SAFE_PIPELINE_REQUIRED"
BOUNDARY_MODE_INVALID = "SENDTO2020_BOUNDARY_MODE_INVALID"

MSG_NO_ACTIVE = "Não existe resultado operacional ativo de cruzamento para este talhão."
MSG_STALE = "O resultado de cruzamento está desatualizado. Recalcule e salve antes de exportar."
MSG_DRAFT = "Existe um cálculo ainda não persistido. Salve o resultado operacional antes da exportação."
MSG_TOUCHES = (
    "A exclusão de tráfego toca o limite externo. "
    "Esse caso ainda não foi validado fisicamente no 20|20."
)
MSG_CROSSES = (
    "A exclusão de tráfego cruza o limite externo (parte do rastro fica fora do talhão). "
    "Esse caso ainda não foi validado fisicamente no 20|20."
)

TRAFFIC_REPORT_JSON = "SENDTO2020_TRAFFIC_EXCLUSIONS_REPORT.json"
TRAFFIC_REPORT_TEXT = "SENDTO2020_TRAFFIC_EXCLUSIONS_REPORT.txt"
TRAFFIC_PREVIEW_DIR = "traffic_exclusions_preview"

# Contrato de persistencia do FortSmart Precision (somente leitura, sem importar o plugin).
PLANNER_GROUP_PREFIX = "FORTSMART - "
PLANNER_STORAGE_DIR = "fortsmart_data"
PLANNER_OPERATION_TYPE = "PLANTING_SHUTOFF"
STATUS_ACTIVE = "ACTIVE"
STATUS_STALE = "STALE"
STATUS_DRAFT = "DRAFT"
STATUS_FAILED = "FAILED"
TIRE_SIDES = ("LEFT", "RIGHT")
WORK_OFF = "WORK_OFF"
TIRE_REASONS = ("SPRAYER_TIRE_TRACK", "SAFETY_MARGIN")


class TrafficExclusionError(ValueError):
    def __init__(self, code: str, message: str, *, details: Sequence[str] = ()) -> None:
        text = f"{code}: {message}"
        if details:
            text += "\n" + "\n".join(f"- {item}" for item in details)
        super().__init__(text)
        self.code = code
        self.message = message
        self.details = list(details)


# --------------------------------------------------------------------------
# Localizacao da geracao no projeto
# --------------------------------------------------------------------------

def planner_field_id(field_name: str | None) -> str:
    """Mesmo field_id que o FortSmart Precision grava no manifest (canonical_field_name)."""
    name = re.sub(r"\s+", " ", str(field_name or "")).strip()
    if name.upper().startswith(PLANNER_GROUP_PREFIX.upper()):
        name = name[len(PLANNER_GROUP_PREFIX):].strip()
    name = re.sub(r"\s*\(EPSG:\d+\)\s*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"^Boundary\s*[-\u2014\u2013]\s*", "", name, flags=re.IGNORECASE)
    return name.strip() or "PROJETO"


def planner_storage_slug(field_id: str) -> str:
    return re.sub(r"[^\w.-]+", "_", field_id, flags=re.UNICODE).strip("._") or "PROJETO"


def planner_field_store_path(project_dir: str | Path, field_name: str | None) -> Path:
    slug = planner_storage_slug(planner_field_id(field_name))
    return Path(project_dir) / PLANNER_STORAGE_DIR / slug / f"fortsmart_{slug}.gpkg"


@dataclass(frozen=True)
class TrafficGenerationSource:
    store_path: Path
    field_id: str
    field_label: str = ""


def planner_traffic_sources(
    project_dir: str | Path | None,
    field_labels: Mapping[str, str],
) -> dict[str, "TrafficGenerationSource"]:
    """Nome exportado do talhao -> GeoPackage operacional do grupo QGIS de origem."""
    if not str(project_dir or "").strip():
        raise TrafficExclusionError(
            NO_ACTIVE_GENERATION,
            MSG_NO_ACTIVE,
            details=["O projeto QGIS nao esta salvo; o resultado operacional fica em "
                     f"{PLANNER_STORAGE_DIR}/ ao lado do projeto."],
        )
    return {
        export_name: TrafficGenerationSource(
            store_path=planner_field_store_path(project_dir, label),
            field_id=planner_field_id(label),
            field_label=label,
        )
        for export_name, label in field_labels.items()
    }


# --------------------------------------------------------------------------
# Leitura da geracao ACTIVE (somente leitura)
# --------------------------------------------------------------------------

Point = tuple[float, float]


@dataclass(frozen=True)
class TrafficTireTrack:
    track_id: str
    spray_pass_id: str
    source_feature_id: str
    side: str
    physical_tire_width_m: float
    safety_margin_m: float
    effective_exclusion_width_m: float
    points: tuple[Point, ...]


@dataclass(frozen=True)
class TrafficPlantingRow:
    source_row_id: str
    points: tuple[Point, ...]


@dataclass(frozen=True)
class TrafficWorkOff:
    segment_id: str
    source_row_id: str
    tire_track_id: str
    spray_pass_id: str
    tire_side: str
    reason: str
    points: tuple[Point, ...]
    length_m: float


@dataclass(frozen=True)
class ActiveTrafficGeneration:
    generation_id: str
    field_id: str
    field_name: str
    status: str
    working_crs: str
    source_crs: str
    created_at: str
    algorithm_version: str
    tire_tracks: tuple[TrafficTireTrack, ...]
    planting_rows: tuple[TrafficPlantingRow, ...]
    work_off: tuple[TrafficWorkOff, ...]
    ignored_work_off: int = 0


def _points(value: str) -> tuple[Point, ...]:
    return tuple((float(item[0]), float(item[1])) for item in json.loads(value))


class OperationalGenerationReader:
    """Le a geracao operacional sem criar tabelas nem alterar o GeoPackage."""

    def __init__(self, store_path: str | Path) -> None:
        self.path = Path(store_path)

    def _connect(self):
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def load_active(self, field_id: str, operation_type: str = PLANNER_OPERATION_TYPE) -> ActiveTrafficGeneration:
        if not self.path.is_file():
            raise TrafficExclusionError(
                NO_ACTIVE_GENERATION, MSG_NO_ACTIVE,
                details=[f"GeoPackage operacional nao encontrado: {self.path}"],
            )
        with closing(self._connect()) as connection:
            tables = {
                row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if "fortsmart_operation_manifest" not in tables:
                raise TrafficExclusionError(
                    NO_ACTIVE_GENERATION, MSG_NO_ACTIVE,
                    details=[f"{self.path.name} nao contem resultados operacionais."],
                )
            manifests = connection.execute(
                "SELECT generation_id, field_id, status FROM fortsmart_operation_manifest "
                "WHERE operation_type = ?",
                (operation_type,),
            ).fetchall()
            own = [row for row in manifests if row["field_id"] == field_id]
            active = [row for row in own if row["status"] == STATUS_ACTIVE]
            if len(active) > 1:
                raise TrafficExclusionError(
                    MULTIPLE_ACTIVE,
                    f"Existem {len(active)} geracoes ACTIVE para o talhao {field_id}; a persistencia esta inconsistente.",
                )
            if not active:
                others = sorted({row["field_id"] for row in manifests if row["status"] == STATUS_ACTIVE})
                statuses = {row["status"] for row in own}
                if others and not own:
                    raise TrafficExclusionError(
                        FIELD_ID_MISMATCH,
                        f"A geracao ativa deste GeoPackage pertence a outro talhao ({', '.join(others)}); "
                        f"esperado {field_id}.",
                    )
                if STATUS_STALE in statuses:
                    raise TrafficExclusionError(STALE_GENERATION, MSG_STALE)
                if statuses & {STATUS_DRAFT, STATUS_FAILED}:
                    raise TrafficExclusionError(DRAFT_GENERATION, MSG_DRAFT)
                raise TrafficExclusionError(NO_ACTIVE_GENERATION, MSG_NO_ACTIVE)
            return self._load(connection, active[0]["generation_id"])

    @staticmethod
    def _load(connection, generation_id: str) -> ActiveTrafficGeneration:
        manifest = connection.execute(
            "SELECT * FROM fortsmart_operation_manifest WHERE generation_id = ?", (generation_id,)
        ).fetchone()
        try:
            tracks = tuple(
                TrafficTireTrack(
                    track_id=str(item["track_id"] or ""),
                    spray_pass_id=str(item["spray_pass_id"] or ""),
                    source_feature_id=str(item["source_feature_id"] or ""),
                    side=str(item["side"] or "").upper(),
                    physical_tire_width_m=float(item["physical_tire_width_m"]),
                    safety_margin_m=float(item["safety_margin_m"]),
                    effective_exclusion_width_m=float(item["effective_exclusion_width_m"]),
                    points=_points(item["geometry_json"]),
                )
                for item in connection.execute(
                    "SELECT * FROM fortsmart_operation_tire_tracks WHERE generation_id = ? ORDER BY track_id",
                    (generation_id,),
                )
            )
            rows = tuple(
                TrafficPlantingRow(str(item["source_row_id"]), _points(item["geometry_json"]))
                for item in connection.execute(
                    "SELECT source_row_id, geometry_json FROM fortsmart_operation_planting_rows "
                    "WHERE generation_id = ? ORDER BY source_row_id",
                    (generation_id,),
                )
            )
            work_off: list[TrafficWorkOff] = []
            ignored = 0
            for item in connection.execute(
                "SELECT * FROM fortsmart_operation_segments WHERE generation_id = ? ORDER BY segment_id",
                (generation_id,),
            ):
                if item["state"] != WORK_OFF or item["reason"] not in TIRE_REASONS:
                    ignored += 1
                    continue
                work_off.append(
                    TrafficWorkOff(
                        segment_id=str(item["segment_id"]),
                        source_row_id=str(item["source_row_id"]),
                        tire_track_id=str(item["tire_track_id"] or ""),
                        spray_pass_id=str(item["spray_pass_id"] or ""),
                        tire_side=str(item["tire_side"] or ""),
                        reason=str(item["reason"]),
                        points=_points(item["geometry_json"]),
                        length_m=float(item["length_m"]),
                    )
                )
        except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError) as exc:
            raise TrafficExclusionError(
                GENERATION_INVALID, f"Geracao {generation_id} ilegivel ({exc})."
            ) from exc
        generation = ActiveTrafficGeneration(
            generation_id=str(manifest["generation_id"]),
            field_id=str(manifest["field_id"]),
            field_name=str(manifest["field_name"]),
            status=str(manifest["status"]),
            working_crs=str(manifest["working_crs"] or ""),
            source_crs=str(manifest["source_crs"] or ""),
            created_at=str(manifest["created_at"]),
            algorithm_version=str(manifest["algorithm_version"]),
            tire_tracks=tracks,
            planting_rows=rows,
            work_off=tuple(work_off),
            ignored_work_off=ignored,
        )
        _validate_generation(generation)
        return generation


def _validate_generation(generation: ActiveTrafficGeneration) -> None:
    problems: list[str] = []
    track_ids = set()
    for track in generation.tire_tracks:
        label = track.track_id or "(sem tire_track_id)"
        if not track.track_id:
            problems.append("rastro sem tire_track_id")
        elif track.track_id in track_ids:
            problems.append(f"{label}: tire_track_id duplicado")
        track_ids.add(track.track_id)
        if track.side not in TIRE_SIDES:
            problems.append(f"{label}: lado '{track.side}' (esperado LEFT/RIGHT)")
        width = track.effective_exclusion_width_m
        if not math.isfinite(width) or width <= 0:
            problems.append(f"{label}: effective_exclusion_width_m invalido ({width})")
        if len(track.points) < 2 or not all(math.isfinite(v) for p in track.points for v in p):
            problems.append(f"{label}: geometria do rastro invalida")
    for item in generation.work_off:
        if not item.tire_track_id:
            problems.append(f"WORK_OFF {item.segment_id}: sem tire_track_id")
        elif item.tire_track_id not in track_ids:
            problems.append(f"WORK_OFF {item.segment_id}: tire_track_id {item.tire_track_id} inexistente")
    if problems:
        raise TrafficExclusionError(
            GENERATION_INVALID,
            f"A geracao ativa {generation.generation_id} tem dados inconsistentes.",
            details=problems[:20],
        )


# --------------------------------------------------------------------------
# Geometria (sempre em CRS metrico)
# --------------------------------------------------------------------------

class MetricProjector:
    """EPSG:4326 <-> working_crs projetado em metros. Buffer em graus e recusado."""

    def __init__(self, working_crs: str) -> None:
        try:
            from pyproj import CRS, Transformer
        except ImportError as exc:  # pragma: no cover - pyproj acompanha o QGIS
            raise RuntimeError("SENDTO2020_DEPENDENCY_MISSING: pyproj obrigatorio.") from exc
        if not str(working_crs or "").strip():
            raise TrafficExclusionError(WORKING_CRS_INVALID, "A geracao ativa nao informa o working_crs.")
        try:
            crs = CRS.from_user_input(working_crs)
        except Exception as exc:
            raise TrafficExclusionError(
                WORKING_CRS_INVALID, f"working_crs '{working_crs}' nao reconhecido ({exc})."
            ) from exc
        units = {str(axis.unit_name).lower() for axis in crs.axis_info[:2]}
        if not crs.is_projected or not units <= {"metre", "meter", "m"}:
            raise TrafficExclusionError(
                WORKING_CRS_INVALID,
                f"working_crs '{working_crs}' nao e projetado em metros; o buffer dos rastros "
                "precisa ser calculado em metros.",
            )
        self.working_crs = working_crs
        self._to_metric = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        self._to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)

    @staticmethod
    def _apply(transformer, geometry):
        from shapely.ops import transform

        out = transform(transformer.transform, geometry)
        if out.is_empty or not all(math.isfinite(value) for value in out.bounds):
            raise TrafficExclusionError(WORKING_CRS_INVALID, "a transformacao de CRS gerou coordenadas invalidas.")
        return out

    def to_metric(self, geometry):
        return self._apply(self._to_metric, geometry)

    def to_wgs84(self, geometry):
        return self._apply(self._to_wgs84, geometry)


def _polygons(geometry) -> list:
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry] if geometry.area > 0 else []
    if hasattr(geometry, "geoms"):
        return [poly for item in geometry.geoms for poly in _polygons(item)]
    return []


def _lines(geometry) -> list:
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "LineString":
        return [geometry]
    if hasattr(geometry, "geoms"):
        return [line for item in geometry.geoms for line in _lines(item)]
    return []


def _line(points: Sequence[Point]):
    from shapely.geometry import LineString

    clean: list[Point] = []
    for x, y in points:
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        if not clean or (x, y) != clean[-1]:
            clean.append((float(x), float(y)))
    if len(clean) < 2:
        return None
    line = LineString(clean)
    return line if line.length > 0 else None


def tire_track_exclusion(
    points: Sequence[Point],
    effective_exclusion_width_m: float,
    *,
    quad_segs: int = 8,
    axis_simplify_m: float = 0.0,
):
    """Faixa do rastro: buffer(eixo, effective_exclusion_width_m / 2) em metros.

    Retorna (poligono, compensacao_m). Quando o eixo e simplificado, o raio cresce na
    mesma tolerancia para que a faixa continue cobrindo o rastro original.
    """
    line = _line(points)
    width = float(effective_exclusion_width_m)
    if line is None or not math.isfinite(width) or width <= 0:
        return None, 0.0
    compensation = 0.0
    if axis_simplify_m > 0:
        simplified = line.simplify(axis_simplify_m, preserve_topology=False)
        if len(simplified.coords) < len(line.coords) and simplified.length > 0:
            line = simplified
            compensation = float(axis_simplify_m)
    return line.buffer(width / 2.0 + compensation, quad_segs=quad_segs), compensation


def classify_traffic_exclusion(exclusion, field_geometry, *, clearance_m: float) -> str:
    if exclusion is None or exclusion.is_empty or not exclusion.is_valid or exclusion.area <= 0:
        return INVALID
    if not exclusion.intersects(field_geometry) or exclusion.intersection(field_geometry).area <= 0:
        return OUTSIDE_FIELD
    if not field_geometry.covers(exclusion):
        return CROSSES_EXTERIOR
    if exclusion.distance(field_geometry.boundary) <= clearance_m:
        return TOUCHES_EXTERIOR
    return INTERIOR_SAFE


@dataclass
class TrafficExclusion:
    """Faixa de um unico rastro de pneu (conceito TRAFFIC_EXCLUSION do adaptador)."""

    tire_track_id: str
    spray_pass_id: str
    tire_side: str
    generation_id: str
    physical_tire_width_m: float
    safety_margin_m: float
    effective_exclusion_width_m: float
    geometry: Any
    clipped: Any
    classification: str
    axis_compensation_m: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "concept": TRAFFIC_EXCLUSION,
            "tire_track_id": self.tire_track_id,
            "spray_pass_id": self.spray_pass_id,
            "tire_side": self.tire_side,
            "generation_id": self.generation_id,
            "physical_tire_width_m": self.physical_tire_width_m,
            "safety_margin_m": self.safety_margin_m,
            "effective_exclusion_width_m": self.effective_exclusion_width_m,
            "axis_compensation_m": self.axis_compensation_m,
            "classification": self.classification,
            "area_m2": round(self.geometry.area, 4) if self.geometry is not None else 0.0,
            "clipped_area_m2": round(self.clipped.area, 4) if self.clipped is not None else 0.0,
        }


def build_traffic_exclusions(
    generation: ActiveTrafficGeneration,
    field_metric,
    *,
    clearance_m: float = 0.05,
    quad_segs: int = 8,
    axis_simplify_m: float = 0.0,
) -> list[TrafficExclusion]:
    """Uma exclusao por TIRE_TRACK persistido (LEFT/RIGHT separados), sem alterar a geracao."""
    exclusions: list[TrafficExclusion] = []
    for track in generation.tire_tracks:
        geometry, compensation = tire_track_exclusion(
            track.points,
            track.effective_exclusion_width_m,
            quad_segs=quad_segs,
            axis_simplify_m=axis_simplify_m,
        )
        classification = classify_traffic_exclusion(geometry, field_metric, clearance_m=clearance_m)
        clipped = None
        if classification not in (INVALID, OUTSIDE_FIELD):
            clipped = geometry.intersection(field_metric)
        exclusions.append(
            TrafficExclusion(
                tire_track_id=track.track_id,
                spray_pass_id=track.spray_pass_id,
                tire_side=track.side,
                generation_id=generation.generation_id,
                physical_tire_width_m=track.physical_tire_width_m,
                safety_margin_m=track.safety_margin_m,
                effective_exclusion_width_m=track.effective_exclusion_width_m,
                geometry=geometry,
                clipped=clipped,
                classification=classification,
                axis_compensation_m=compensation,
            )
        )
    return exclusions


@dataclass
class MergedTrafficExclusion:
    exclusion_id: str
    geometry: Any
    tire_track_ids: tuple[str, ...]
    spray_pass_ids: tuple[str, ...]
    tire_sides: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "exclusion_id": self.exclusion_id,
            "tire_track_ids": list(self.tire_track_ids),
            "spray_pass_ids": list(self.spray_pass_ids),
            "tire_sides": list(self.tire_sides),
            "area_m2": round(self.geometry.area, 4),
            "vertices": len(self.geometry.exterior.coords),
            "islands": len(self.geometry.interiors),
        }


@dataclass
class TrafficMergeResult:
    exclusions: list[MergedTrafficExclusion]
    merged: int
    fragments_removed: int
    area_removed_m2: float


def merge_traffic_exclusions(
    exclusions: Sequence[TrafficExclusion],
    *,
    min_fragment_area_m2: float = 0.01,
) -> TrafficMergeResult:
    """Dissolve apenas faixas que se tocam/intersectam; nunca cria envoltoria."""
    import shapely
    from shapely.ops import unary_union
    from shapely.validation import make_valid

    sources = []
    for item in exclusions:
        geometry = shapely.remove_repeated_points(item.geometry)
        if not geometry.is_valid:
            geometry = make_valid(geometry)
        sources.append((item, geometry))
    union = unary_union([geometry for _, geometry in sources]) if sources else None
    polygons = sorted(_polygons(union), key=lambda poly: (poly.bounds[0], poly.bounds[1]))
    merged: list[MergedTrafficExclusion] = []
    removed = 0
    removed_area = 0.0
    combined = 0
    for polygon in polygons:
        if polygon.area < min_fragment_area_m2:
            removed += 1
            removed_area += polygon.area
            continue
        members = [item for item, geometry in sources if geometry.intersects(polygon)]
        if len(members) > 1:
            combined += 1
        merged.append(
            MergedTrafficExclusion(
                exclusion_id=f"TX_{len(merged) + 1:03d}",
                geometry=polygon,
                tire_track_ids=tuple(item.tire_track_id for item in members),
                spray_pass_ids=tuple(sorted({item.spray_pass_id for item in members})),
                tire_sides=tuple(sorted({item.tire_side for item in members})),
            )
        )
    return TrafficMergeResult(merged, combined, removed, removed_area)


@dataclass
class OperationalBoundary2020:
    geometry: Any
    part_holes: list[list[int]]
    external_area_m2: float
    exclusion_area_m2: float
    plantable_area_m2: float
    exterior_rings: int
    interior_rings: int


def build_2020_operational_boundary(external_boundary, traffic_exclusions: Sequence[Any]) -> OperationalBoundary2020:
    """Copia o limite externo e acrescenta cada exclusao como anel interno.

    O limite recebido nao e alterado; cada exclusao precisa estar contida em
    exatamente uma parte e nao pode ter ilhas internas.
    """
    from shapely.geometry import MultiPolygon, Polygon
    from shapely.ops import unary_union
    from shapely.validation import explain_validity

    parts = _polygons(external_boundary)
    if not parts:
        raise TrafficExclusionError(COMPOSITION_FAILED, "limite externo vazio.")
    assignment: list[list[int]] = [[] for _ in parts]
    for index, exclusion in enumerate(traffic_exclusions):
        if exclusion.geom_type != "Polygon" or exclusion.interiors:
            raise TrafficExclusionError(
                COMPOSITION_FAILED, f"exclusao {index + 1} nao e um poligono simples sem ilhas."
            )
        owners = [number for number, part in enumerate(parts) if part.contains(exclusion)]
        if len(owners) != 1:
            raise TrafficExclusionError(
                COMPOSITION_FAILED,
                f"exclusao {index + 1} nao esta contida em exatamente uma parte do limite ({len(owners)}).",
            )
        assignment[owners[0]].append(index)
    polygons = []
    for part, indexes in zip(parts, assignment):
        holes = [list(ring.coords) for ring in part.interiors]
        holes.extend(list(traffic_exclusions[index].exterior.coords) for index in indexes)
        polygons.append(Polygon(list(part.exterior.coords), holes))
    geometry = polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
    if not geometry.is_valid:
        raise TrafficExclusionError(COMPOSITION_FAILED, f"boundary composto invalido ({explain_validity(geometry)}).")
    external_area = sum(part.area for part in parts)
    exclusion_area = unary_union(list(traffic_exclusions)).area if traffic_exclusions else 0.0
    expected = external_area - exclusion_area
    if abs(geometry.area - expected) > max(1e-6, external_area * 1e-9):
        raise TrafficExclusionError(
            COMPOSITION_FAILED,
            f"area composta {geometry.area:.4f} m2 difere da esperada {expected:.4f} m2.",
        )
    return OperationalBoundary2020(
        geometry=geometry,
        part_holes=assignment,
        external_area_m2=external_area,
        exclusion_area_m2=exclusion_area,
        plantable_area_m2=geometry.area,
        exterior_rings=len(polygons),
        interior_rings=sum(len(polygon.interiors) for polygon in polygons),
    )


def validate_2020_exclusions_against_work_off(
    exclusion_geometry,
    planting_rows: Sequence[TrafficPlantingRow],
    work_off: Sequence[TrafficWorkOff],
    *,
    tolerance_m: float = 0.30,
    min_piece_m: float = 0.05,
) -> dict[str, Any]:
    """Compara linhas x exclusoes exportadas (OFF simulado) com o WORK_OFF persistido.

    WORK_OFF e somente auditoria: nunca vira geometria exportada.
    """
    from shapely.ops import unary_union
    from shapely.prepared import prep

    zone = exclusion_geometry if exclusion_geometry is not None and not exclusion_geometry.is_empty else None
    zone_tolerance = zone.buffer(tolerance_m) if zone is not None else None
    known_rows = {row.source_row_id for row in planting_rows}
    persisted_by_row: dict[str, list] = {}
    unexplained: list[dict[str, Any]] = []
    explained = 0
    for item in work_off:
        identity = {
            "segment_id": item.segment_id,
            "source_row_id": item.source_row_id,
            "tire_track_id": item.tire_track_id,
            "spray_pass_id": item.spray_pass_id,
            "tire_side": item.tire_side,
        }
        line = _line(item.points)
        if line is None:
            unexplained.append({**identity, "reason": "geometria WORK_OFF invalida"})
            continue
        if item.source_row_id not in known_rows:
            unexplained.append({**identity, "reason": "linha de plantio ausente na geracao"})
            continue
        persisted_by_row.setdefault(item.source_row_id, []).append(line)
        outside = line.length if zone_tolerance is None else line.difference(zone_tolerance).length
        if outside > tolerance_m:
            unexplained.append(
                {**identity, "reason": "WORK_OFF fora das exclusoes", "length_m": round(line.length, 3),
                 "outside_m": round(outside, 3)}
            )
        else:
            explained += 1

    simulated = 0
    unexpected: list[dict[str, Any]] = []
    if zone is not None:
        prepared_zone = prep(zone)
        for row in planting_rows:
            line = _line(row.points)
            if line is None or not prepared_zone.intersects(line):
                continue
            pieces = [piece for piece in _lines(line.intersection(zone)) if piece.length >= min_piece_m]
            if not pieces:
                continue
            persisted = persisted_by_row.get(row.source_row_id)
            covered = unary_union(persisted).buffer(tolerance_m) if persisted else None
            for piece in pieces:
                simulated += 1
                missing = piece.length if covered is None else piece.difference(covered).length
                if missing > tolerance_m:
                    unexpected.append(
                        {"source_row_id": row.source_row_id, "length_m": round(piece.length, 3),
                         "missing_m": round(missing, 3)}
                    )
    return {
        "status": "PASS" if not unexplained and not unexpected else "FAIL",
        "work_off_events": len(work_off),
        "explained": explained,
        "unexplained": len(unexplained),
        "simulated_intersections": simulated,
        "unexpected_intersections": len(unexpected),
        "tolerance_m": tolerance_m,
        "unexplained_items": unexplained[:50],
        "unexpected_items": unexpected[:50],
    }


# --------------------------------------------------------------------------
# Payload, modo e diagnostico
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Precision2020BoundaryPayload:
    field_id: str
    field_name: str
    external_geometry: dict[str, Any]
    inner_exclusion_geometries: tuple[dict[str, Any], ...]
    export_geometry: dict[str, Any]
    source_crs: str
    export_crs: str
    generation_id: str
    mode: str


@dataclass
class TrafficExclusionConfig:
    clearance_m: float = 0.05
    min_fragment_area_m2: float = 0.01
    work_off_tolerance_m: float = 0.30
    min_work_off_piece_m: float = 0.05
    buffer_quad_segs: int = 8
    axis_simplify_m: float = 0.01
    area_tolerance_percent: float = 0.1


@dataclass
class _FieldTrafficPlan:
    diagnostic: dict[str, Any]
    projector: MetricProjector
    payload: Precision2020BoundaryPayload
    expected_interior_rings: int
    plantable_area_m2: float


def _ha(value_m2: float) -> float:
    return round(value_m2 / 10_000.0, 6)


def _geojson(geometry) -> dict[str, Any]:
    from shapely.geometry import mapping

    return json.loads(json.dumps(mapping(geometry)))


def _ring_list(ring) -> list[tuple[float, float]]:
    return [(float(x), float(y)) for x, y in ring.coords]


class SendTo2020TrafficExclusionMode:
    """Converte apenas TIRE_TRACK da geracao ACTIVE em aneis internos do boundary 20|20."""

    mode = BOUNDARY_WITH_TRAFFIC_EXCLUSIONS

    def __init__(
        self,
        sources: Mapping[str, TrafficGenerationSource] | None,
        config: TrafficExclusionConfig | None = None,
        *,
        reader_factory: Callable[[Path], OperationalGenerationReader] = OperationalGenerationReader,
    ) -> None:
        self.sources = dict(sources or {})
        self.config = config or TrafficExclusionConfig()
        self.reader_factory = reader_factory
        self.fields: list[dict[str, Any]] = []
        self._plans: dict[int, _FieldTrafficPlan] = {}
        self._previews: dict[str, list[dict[str, Any]]] = {}

    def _source_for(self, bundle) -> TrafficGenerationSource:
        source = self.sources.get(bundle.talhao) or self.sources.get(bundle.field_erid)
        if source is None:
            raise TrafficExclusionError(
                NO_ACTIVE_GENERATION, MSG_NO_ACTIVE,
                details=[f"Nenhum GeoPackage operacional associado ao talhao {bundle.talhao}."],
            )
        return source

    def apply(self, field_plan, *, vertex_max: int = 10_000):
        """Retorna um novo PreparedField com as exclusoes como aneis internos."""
        bundle = field_plan.bundle
        diagnostic: dict[str, Any] = {
            "field": bundle.talhao,
            "field_erid": bundle.field_erid,
            "mode": self.mode,
            "status": "BUILDING",
            "warnings": [],
        }
        self.fields.append(diagnostic)
        try:
            return self._apply(field_plan, diagnostic, vertex_max)
        except TrafficExclusionError as exc:
            diagnostic["status"] = "BLOCKED"
            diagnostic["error_code"] = exc.code
            diagnostic["error"] = exc.message
            diagnostic["details"] = list(exc.details)
            raise
        except Exception as exc:
            diagnostic["status"] = "FAILED"
            diagnostic["error"] = str(exc)
            raise

    def _apply(self, field_plan, diagnostic: dict[str, Any], vertex_max: int):
        from shapely.geometry import MultiPolygon, Polygon, shape
        from shapely.ops import unary_union
        from shapely.validation import explain_validity

        cfg = self.config
        bundle = field_plan.bundle
        prepared = field_plan.prepared
        source = self._source_for(bundle)
        diagnostic["store"] = str(source.store_path)
        diagnostic["expected_field_id"] = source.field_id
        generation = self.reader_factory(source.store_path).load_active(source.field_id)
        diagnostic.update(
            {
                "generation_id": generation.generation_id,
                "generation_status": generation.status,
                "generation_field_id": generation.field_id,
                "generation_created_at": generation.created_at,
                "algorithm_version": generation.algorithm_version,
                "working_crs": generation.working_crs,
            }
        )
        if generation.status != STATUS_ACTIVE:
            raise TrafficExclusionError(NO_ACTIVE_GENERATION, MSG_NO_ACTIVE)
        if generation.field_id != source.field_id:
            raise TrafficExclusionError(
                FIELD_ID_MISMATCH,
                f"A geracao {generation.generation_id} pertence ao talhao {generation.field_id}, "
                f"nao a {source.field_id}.",
            )

        projector = MetricProjector(generation.working_crs)
        boundary_source = dict(field_plan.feature.extra.get("source_geometry") or {})
        boundary_crs = str(boundary_source.get("crs") or "")
        diagnostic["boundary_source_crs"] = boundary_crs
        if boundary_crs and not boundary_source.get("crs_geographic") and boundary_crs != generation.working_crs:
            diagnostic["warnings"].append(
                f"CRS do limite ({boundary_crs}) difere do working_crs da geracao "
                f"({generation.working_crs}); o limite foi transformado para {generation.working_crs}."
            )

        external_wgs84 = shape(prepared.geojson_geometry())
        external_metric = projector.to_metric(external_wgs84)
        if not external_metric.is_valid:
            raise TrafficExclusionError(
                COMPOSITION_FAILED,
                f"limite invalido no working_crs ({explain_validity(external_metric)}).",
            )
        if not generation.tire_tracks:
            raise TrafficExclusionError(NO_TIRE_TRACKS, "A geracao ativa nao possui rastros de pneu.")

        exclusions = build_traffic_exclusions(
            generation,
            external_metric,
            clearance_m=cfg.clearance_m,
            quad_segs=cfg.buffer_quad_segs,
            axis_simplify_m=cfg.axis_simplify_m,
        )
        counts = {
            name: sum(1 for item in exclusions if item.classification == name)
            for name in (INTERIOR_SAFE, TOUCHES_EXTERIOR, CROSSES_EXTERIOR, OUTSIDE_FIELD, INVALID)
        }
        preview = self._previews.setdefault(bundle.talhao, [])
        for item in exclusions:
            if item.geometry is not None and not item.geometry.is_empty:
                preview.append(
                    {"type": "Feature", "properties": {"layer": "tire_track_exclusion", **item.as_dict()},
                     "geometry": _geojson(projector.to_wgs84(item.geometry))}
                )
        diagnostic.update(
            {
                "traffic_tracks": len(generation.tire_tracks),
                "exclusion_polygons": len(exclusions),
                "interior_safe": counts[INTERIOR_SAFE],
                "touches_exterior": counts[TOUCHES_EXTERIOR],
                "crosses_exterior": counts[CROSSES_EXTERIOR],
                "outside_discarded": counts[OUTSIDE_FIELD],
                "invalid": counts[INVALID],
                "tracks": [item.as_dict() for item in exclusions],
                "ignored_non_tire_work_off": generation.ignored_work_off,
            }
        )
        if counts[OUTSIDE_FIELD]:
            diagnostic["warnings"].append(
                f"{counts[OUTSIDE_FIELD]} rastro(s) totalmente fora do limite descartado(s): "
                + ", ".join(i.tire_track_id for i in exclusions if i.classification == OUTSIDE_FIELD)
            )
        if counts[OUTSIDE_FIELD] == len(exclusions):
            raise TrafficExclusionError(
                TRACKS_OUTSIDE_FIELD,
                "Nenhum rastro de pneu cai dentro do limite exportado; confira se a geracao e o "
                f"working_crs ({generation.working_crs}) correspondem a este talhao.",
            )

        blocking: list[tuple[str, str]] = []

        def ids(classification: str) -> str:
            return ", ".join(
                f"{i.tire_track_id} ({i.tire_side}, passada {i.spray_pass_id})"
                for i in exclusions if i.classification == classification
            )

        if counts[INVALID]:
            blocking.append((EXCLUSION_INVALID, f"Exclusao de trafego invalida: {ids(INVALID)}"))
        if counts[TOUCHES_EXTERIOR]:
            blocking.append((EXCLUSION_TOUCHES, f"{MSG_TOUCHES} Rastros: {ids(TOUCHES_EXTERIOR)}"))
        if counts[CROSSES_EXTERIOR]:
            blocking.append((EXCLUSION_CROSSES, f"{MSG_CROSSES} Rastros: {ids(CROSSES_EXTERIOR)}"))

        safe = [item for item in exclusions if item.classification == INTERIOR_SAFE]
        merge = merge_traffic_exclusions(safe, min_fragment_area_m2=cfg.min_fragment_area_m2)
        islands = [item for item in merge.exclusions if item.geometry.interiors]
        if islands:
            blocking.append(
                (ENCLOSED_ISLAND,
                 "Exclusoes de trafego fecham uma area plantavel (ilha) dentro delas; caso nao suportado: "
                 + ", ".join(f"{i.exclusion_id} [{', '.join(i.tire_track_ids)}]" for i in islands))
            )
        diagnostic.update(
            {
                "merged": merge.merged,
                "exported_exclusions": len(merge.exclusions),
                "fragments_removed": merge.fragments_removed,
                "area_removed_m2": round(merge.area_removed_m2, 6),
                "exclusions": [item.as_dict() for item in merge.exclusions],
            }
        )

        comparison = [item.geometry for item in merge.exclusions]
        comparison.extend(
            item.clipped for item in exclusions
            if item.classification in (TOUCHES_EXTERIOR, CROSSES_EXTERIOR) and item.clipped is not None
        )
        work_off = validate_2020_exclusions_against_work_off(
            unary_union(comparison) if comparison else None,
            generation.planting_rows,
            generation.work_off,
            tolerance_m=cfg.work_off_tolerance_m,
            min_piece_m=cfg.min_work_off_piece_m,
        )
        diagnostic["work_off"] = work_off
        for item in generation.work_off:
            line = _line(item.points)
            if line is not None:
                preview.append(
                    {"type": "Feature",
                     "properties": {"layer": "work_off_audit", "segment_id": item.segment_id,
                                    "source_row_id": item.source_row_id, "tire_track_id": item.tire_track_id,
                                    "spray_pass_id": item.spray_pass_id, "tire_side": item.tire_side,
                                    "reason": item.reason},
                     "geometry": _geojson(projector.to_wgs84(line))}
                )
        if work_off["status"] != "PASS":
            blocking.append(
                (WORK_OFF_MISMATCH,
                 f"WORK_OFF persistido nao confere com as exclusoes: {work_off['unexplained']} sem explicacao, "
                 f"{work_off['unexpected_intersections']} OFF extra inesperado(s) "
                 f"(tolerancia {cfg.work_off_tolerance_m} m).")
            )
        if blocking:
            raise TrafficExclusionError(
                blocking[0][0],
                blocking[0][1] if len(blocking) == 1 else "Exportacao com exclusoes bloqueada.",
                details=[message for _, message in blocking] if len(blocking) > 1 else (),
            )
        if not merge.exclusions:
            raise TrafficExclusionError(
                COMPOSITION_FAILED, "Nenhuma exclusao interna restou apos a limpeza de micro-fragmentos."
            )

        operational = build_2020_operational_boundary(
            external_metric, [item.geometry for item in merge.exclusions]
        )
        hole_rings = [
            _ring_list(projector.to_wgs84(Polygon(item.geometry.exterior)).exterior)
            for item in merge.exclusions
        ]
        parts = copy.deepcopy(prepared.parts)
        for part, indexes in zip(parts, operational.part_holes):
            part.extend(hole_rings[index] for index in indexes)
        polygons = [Polygon(part[0], part[1:]) for part in parts]
        export_geometry = polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
        if not export_geometry.is_valid:
            raise TrafficExclusionError(
                COMPOSITION_FAILED,
                f"boundary com exclusoes invalido em EPSG:4326 ({explain_validity(export_geometry)}).",
            )
        added_vertices = sum(len(ring) for ring in hole_rings)
        total_vertices = sum(len(ring) for part in parts for ring in part)
        if total_vertices > vertex_max:
            raise TrafficExclusionError(
                TOO_COMPLEX,
                f"boundary com exclusoes tem {total_vertices} vertices > limite {vertex_max}.",
            )
        new_prepared = replace(
            prepared,
            parts=parts,
            holes=prepared.holes + len(hole_rings),
            export_vertices=prepared.export_vertices + added_vertices,
            export_area_ha=max(0.0, prepared.export_area_ha - operational.exclusion_area_m2 / 10_000.0),
            bbox=safe_boundary._bbox(parts),
            centroid=(float(export_geometry.centroid.x), float(export_geometry.centroid.y)),
        )
        result = field_plan.result
        result.geometry.update(
            {
                "boundary_mode": self.mode,
                "holes": new_prepared.holes,
                "traffic_exclusion_holes": len(hole_rings),
                "export_vertices": new_prepared.export_vertices,
                "area_ha": new_prepared.export_area_ha,
                "export_bbox": new_prepared.bbox,
                "export_centroid": list(new_prepared.centroid),
            }
        )
        payload = Precision2020BoundaryPayload(
            field_id=source.field_id,
            field_name=bundle.talhao,
            external_geometry=prepared.geojson_geometry(),
            inner_exclusion_geometries=tuple(
                {"type": "Polygon", "coordinates": [ring]} for ring in hole_rings
            ),
            export_geometry=new_prepared.geojson_geometry(),
            source_crs=generation.working_crs,
            export_crs=safe_boundary.PRECISION2020_EXPORT_CRS,
            generation_id=generation.generation_id,
            mode=self.mode,
        )
        preview.append(
            {"type": "Feature", "properties": {"layer": "boundary_export", "field": bundle.talhao},
             "geometry": new_prepared.geojson_geometry()}
        )
        for item, ring in zip(merge.exclusions, hole_rings):
            preview.append(
                {"type": "Feature", "properties": {"layer": "exported_exclusion", **item.as_dict()},
                 "geometry": {"type": "Polygon", "coordinates": [ring]}}
            )
        diagnostic.update(
            {
                "exterior_rings": operational.exterior_rings,
                "interior_rings": operational.interior_rings,
                "original_interior_rings": prepared.holes,
                "external_area_m2": round(operational.external_area_m2, 4),
                "excluded_area_m2": round(operational.exclusion_area_m2, 4),
                "plantable_area_m2": round(operational.plantable_area_m2, 4),
                "external_area_ha": _ha(operational.external_area_m2),
                "excluded_area_ha": _ha(operational.exclusion_area_m2),
                "plantable_area_ha": _ha(operational.plantable_area_m2),
                "export_vertices": total_vertices,
                "status": "READY_TO_WRITE",
            }
        )
        new_plan = replace(field_plan, prepared=new_prepared)
        self._plans[id(new_plan)] = _FieldTrafficPlan(
            diagnostic=diagnostic,
            projector=projector,
            payload=payload,
            expected_interior_rings=sum(len(part) - 1 for part in parts),
            plantable_area_m2=operational.plantable_area_m2,
        )
        return new_plan

    def payload_for(self, field_plan) -> Precision2020BoundaryPayload | None:
        plan = self._plans.get(id(field_plan))
        return plan.payload if plan is not None else None

    def validate_written(self, field_plan, base_path: Path) -> dict[str, Any]:
        """Reabre o SHP: aneis internos, EPSG:4326 e coerencia de area plantavel."""
        import fiona
        from shapely.geometry import shape

        plan = self._plans[id(field_plan)]
        diagnostic = plan.diagnostic
        try:
            with fiona.open(Path(base_path).with_suffix(".shp"), "r") as source:
                records = list(source)
                epsg = source.crs.to_epsg() if hasattr(source.crs, "to_epsg") else None
        except Exception as exc:
            diagnostic["status"] = "BLOCKED"
            raise TrafficExclusionError(POSTWRITE_FAILED, f"SHP nao reabre ({exc}).") from exc
        problems: list[str] = []
        if len(records) != 1:
            problems.append(f"{len(records)} feicoes gravadas, esperado 1")
        geometry = shape(records[0]["geometry"]) if records and records[0].get("geometry") else None
        interior = 0
        area = 0.0
        if geometry is None or geometry.is_empty:
            problems.append("geometria reaberta vazia")
        else:
            interior = sum(len(polygon.interiors) for polygon in _polygons(geometry))
            if interior != plan.expected_interior_rings:
                problems.append(f"{interior} aneis internos reabertos, esperado {plan.expected_interior_rings}")
            area = plan.projector.to_metric(geometry).area
            limit = plan.plantable_area_m2 * self.config.area_tolerance_percent / 100.0
            if abs(area - plan.plantable_area_m2) > max(limit, 0.01):
                problems.append(
                    f"area plantavel reaberta {area:.2f} m2 difere de {plan.plantable_area_m2:.2f} m2"
                )
        if epsg != 4326:
            problems.append(f"CRS reaberto EPSG:{epsg}, esperado EPSG:4326")
        postwrite = {
            "feature_count": len(records),
            "interior_rings": interior,
            "crs": f"EPSG:{epsg}",
            "plantable_area_m2": round(area, 4),
            "status": "PASS" if not problems else "FAIL",
        }
        diagnostic["postwrite"] = postwrite
        if problems:
            diagnostic["status"] = "BLOCKED"
            raise TrafficExclusionError(POSTWRITE_FAILED, "; ".join(problems))
        diagnostic["status"] = "EXPORTED"
        return postwrite

    def report_entry(self, field_plan) -> dict[str, Any]:
        plan = self._plans.get(id(field_plan))
        if plan is None:
            return {"boundary_mode": self.mode}
        diagnostic = plan.diagnostic
        return {
            "boundary_mode": self.mode,
            "generation_id": diagnostic.get("generation_id", ""),
            "traffic_exclusions": diagnostic.get("exported_exclusions", 0),
            "excluded_area_ha": diagnostic.get("excluded_area_ha", 0.0),
            "plantable_area_ha": diagnostic.get("plantable_area_ha", 0.0),
        }

    def summary(self) -> dict[str, Any]:
        statuses = {item.get("status") for item in self.fields}
        return {
            "mode": self.mode,
            "concept": TRAFFIC_EXCLUSION,
            "status": "EXPORTED" if self.fields and statuses == {"EXPORTED"} else "BLOCKED",
            "config": {
                "clearance_m": self.config.clearance_m,
                "min_fragment_area_m2": self.config.min_fragment_area_m2,
                "work_off_tolerance_m": self.config.work_off_tolerance_m,
                "buffer_quad_segs": self.config.buffer_quad_segs,
                "axis_simplify_m": self.config.axis_simplify_m,
            },
            "fields": self.fields,
        }

    def write_report(self, root: Path) -> None:
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        summary = self.summary()
        (root / TRAFFIC_REPORT_JSON).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        (root / TRAFFIC_REPORT_TEXT).write_text(
            "\n".join(format_traffic_report(summary)) + "\n", encoding="utf-8"
        )
        if self._previews:
            folder = root / TRAFFIC_PREVIEW_DIR
            folder.mkdir(parents=True, exist_ok=True)
            for name, features in self._previews.items():
                safe = re.sub(r"[^\w.-]+", "_", name, flags=re.UNICODE).strip("._") or "talhao"
                (folder / f"{safe}.geojson").write_text(
                    json.dumps({"type": "FeatureCollection", "features": features}, default=str),
                    encoding="utf-8",
                )


def format_traffic_report(summary: dict[str, Any]) -> list[str]:
    lines = [
        "SENDTO2020 - BOUNDARY + EXCLUSOES DE TRAFEGO",
        f"Modo: {summary.get('mode')}",
        f"Status: {summary.get('status')}",
        "",
    ]
    for item in summary.get("fields", []):
        work_off = item.get("work_off") or {}
        lines.extend(
            [
                f"Talhao: {item.get('field')}",
                f"Geracao: {item.get('generation_id', '-')} ({item.get('generation_status', '-')})",
                f"Working CRS: {item.get('working_crs', '-')}",
                f"Rastros de pneu: {item.get('traffic_tracks', 0)}",
                f"Poligonos de exclusao: {item.get('exclusion_polygons', 0)}",
                f"Mesclados: {item.get('merged', 0)}",
                f"Interior safe: {item.get('interior_safe', 0)}",
                f"Tocando borda: {item.get('touches_exterior', 0)}",
                f"Cruzando borda: {item.get('crosses_exterior', 0)}",
                f"Fora do talhao (descartados): {item.get('outside_discarded', 0)}",
                f"Exclusoes exportadas: {item.get('exported_exclusions', 0)}",
                f"Micro-fragmentos removidos: {item.get('fragments_removed', 0)} "
                f"({item.get('area_removed_m2', 0.0)} m2)",
                f"Eventos WORK_OFF: {work_off.get('work_off_events', 0)} "
                f"(explicados {work_off.get('explained', 0)}, sem explicacao {work_off.get('unexplained', 0)}, "
                f"OFF extra {work_off.get('unexpected_intersections', 0)})",
                f"Aneis externos/internos: {item.get('exterior_rings', '-')}/{item.get('interior_rings', '-')}",
                f"Area externa: {item.get('external_area_ha', '-')} ha",
                f"Area excluida: {item.get('excluded_area_ha', '-')} ha",
                f"Area plantavel: {item.get('plantable_area_ha', '-')} ha",
                f"Resultado: {item.get('status')}",
            ]
        )
        if item.get("error"):
            lines.append(f"Erro: {item.get('error_code', '')} {item['error']}")
        lines.extend(f"Detalhe: {detail}" for detail in item.get("details", []))
        lines.extend(f"Aviso: {warning}" for warning in item.get("warnings", []))
        lines.append("")
    lines.append("Talhao | Geracao | Rastros | Exclusoes | WORK_OFF | Avisos | Resultado")
    for item in summary.get("fields", []):
        lines.append(
            f"{item.get('field')} | {item.get('generation_id', '-')} | {item.get('traffic_tracks', 0)} | "
            f"{item.get('exported_exclusions', 0)} | {(item.get('work_off') or {}).get('work_off_events', 0)} | "
            f"{len(item.get('warnings', []))} | {item.get('status')}"
        )
    return lines


def traffic_success_lines(summary: dict[str, Any]) -> list[str]:
    lines = ["Boundary + exclusoes dos rastros (validacao fisica no 20|20 pendente):"]
    for item in summary.get("fields", []):
        lines.append(
            f"{item.get('field')}: geracao {item.get('generation_id', '-')}, "
            f"{item.get('exported_exclusions', 0)} exclusao(oes), "
            f"{item.get('excluded_area_ha', 0)} ha excluidos, "
            f"{item.get('plantable_area_ha', 0)} ha plantaveis - {item.get('status')}"
        )
    return lines
