"""FortSmart — passadas planejadas como guidance GS3 2630 (CurvedTrackLine).

Contrato CurvedTrackLine/CurveTrack dos pacotes Deere aceitos pelo monitor:

* fdShape linear de secao unica, cabecalho identico ao Boundary/CurveTrack;
* registros ``<ddf>`` = (dlon, dlat, alt) relativos a vrReferenceLongitude e
  vrReferenceLatitude, que sao o primeiro vertice da primeira trilha;
* cada trilha e precedida por um sentinela alt=999.9 que repete o ultimo
  registro da trilha anterior (o primeiro sentinela e (0, 0)); pontos alt=1.0;
* um unico CurvedTrackLine pode conter varias trilhas.

Este modulo nao conhece o Gen4; o plano comum fica em jd_guidance_plan.
"""

from __future__ import annotations

import struct
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from . import jd_guidance_plan as plan_module
from .jd_guidance_plan import (  # noqa: F401  (API publica reexportada)
    BORDER_PASS,
    EMPTY_PACKAGE_MESSAGE,
    FIELD_MISMATCH,
    GROUPING_PER_CATEGORY,
    GROUPING_PER_PASS,
    GROUPINGS,
    GS3_DEVICE_STATUS,
    GS3_INCLUDE_ROLES,
    GS3_PER_PASS_STATUS,
    IDENTITY_NAMESPACE,
    IGNORED_NAME_MATCH,
    INCOMPLETE_PACKAGE_MESSAGE,
    INVALID_ABORT,
    INVALID_SKIP,
    PASS_CATEGORIES,
    PASS_CATEGORY_BY_ROLE,
    PASS_GROUP_NAME,
    PASS_LABEL,
    PASS_NAME_PREFIX,
    PLANTING_PASS,
    SKIP_GROUPS,
    SKIP_ROLES,
    FieldGuidancePlan,
    GS3ExportPlan,
    GS3FieldPassPlan,
    GS3PassItem,
    GS3PassOptions,
    GuidanceGeometryError,
    GuidancePassItem,
    JohnDeereGuidanceExportPlan,
    JohnDeereGuidanceOptions,
    PreparedGuidance,
    build_gs3_export_plan,
    build_john_deere_guidance_plan,
    guidance_group_name,
    is_effectively_straight,
    max_chord_deviation_m,
    normalized_uuid,
    parse_part_breaks,
    pass_category,
    prepare_gs3_guidance_geometry,
    prepare_john_deere_guidance_geometry,
    split_line_parts,
)
from .jd_guidance_validators import (
    GS3_CATALOG_MISSING,
    GS3_FIELD_MISMATCH,
    GS3_FILE_MISSING,
    coordinates_status,
)


GS3_PASS_GUIDANCE_STATUS = GS3_DEVICE_STATUS
GS3_SKIP_ROLES = SKIP_ROLES
GS3_SEMANTIC_MAPPING = plan_module.SEMANTIC_MAPPING[plan_module.GS3]

CURVETRACK_SENTINEL_ALT = 999.9
CURVETRACK_POINT_ALT = 1.0
FDSHAPE_HEADER_SIZE = 64
FDSHAPE_RECORD_SIZE = 20

STRAIGHT_WRITER_NOTE = (
    "STRAIGHT_TRACK_WRITER_NOT_VERIFIED: passadas retas tambem sao "
    "serializadas como CurvedTrackLine (formato comprovado)."
)

_ZERO_SOURCE_NODE = "{00000000-0000-0000-0000-000000000000}"


def __getattr__(name: str):
    if name in ("ENABLE_GS3_PLANTING_PASSES", "ENABLE_GS3_BORDER_PASSES"):
        return getattr(plan_module, name)
    raise AttributeError(name)


# --------------------------------------------------------------------------
# Serializacao CurvedTrackLine / CurveTrack
# --------------------------------------------------------------------------


def write_curvetrack_fdshape(
    tracks: Sequence[Sequence[tuple[float, float]]],
) -> tuple[bytes, tuple[float, float]]:
    """fdShape CurveTrack com uma ou mais trilhas (delta + sentinela)."""
    tracks = [list(track) for track in tracks if len(track) >= 2]
    if not tracks:
        raise ValueError("CurveTrack sem trilhas.")
    ref_lon, ref_lat = tracks[0][0]
    records = bytearray()
    count = 0
    previous = (0.0, 0.0)
    for track in tracks:
        records.extend(struct.pack("<ddf", previous[0], previous[1], CURVETRACK_SENTINEL_ALT))
        count += 1
        for lon, lat in track:
            previous = (lon - ref_lon, lat - ref_lat)
            records.extend(struct.pack("<ddf", previous[0], previous[1], CURVETRACK_POINT_ALT))
            count += 1
    header = bytearray(FDSHAPE_HEADER_SIZE)
    header[16:18] = b"\x01\x02"
    struct.pack_into("<Q", header, 32, 1)
    struct.pack_into("<Q", header, 40, 0)
    struct.pack_into("<Q", header, 48, 1)
    struct.pack_into("<Q", header, 56, count)
    return bytes(header) + bytes(records), (ref_lon, ref_lat)


def decode_curvetrack_fdshape(
    payload: bytes,
    reference: tuple[float, float],
) -> list[list[tuple[float, float]]]:
    """Inverso de write_curvetrack_fdshape: trilhas absolutas (lon, lat)."""
    if len(payload) < FDSHAPE_HEADER_SIZE or payload[16:18] != b"\x01\x02":
        raise ValueError("fdShape CurveTrack com cabecalho invalido.")
    sections, reserved, version, count = struct.unpack_from("<QQQQ", payload, 32)
    if (sections, reserved, version) != (1, 0, 1):
        raise ValueError("fdShape CurveTrack com secoes/versao inesperadas.")
    if FDSHAPE_HEADER_SIZE + count * FDSHAPE_RECORD_SIZE != len(payload):
        raise ValueError("fdShape CurveTrack com tamanho incompativel.")
    ref_lon, ref_lat = reference
    tracks: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] | None = None
    for index in range(int(count)):
        dlon, dlat, alt = struct.unpack_from(
            "<ddf", payload, FDSHAPE_HEADER_SIZE + index * FDSHAPE_RECORD_SIZE
        )
        if abs(alt - CURVETRACK_SENTINEL_ALT) < 0.01:
            current = []
            tracks.append(current)
            continue
        if current is None:
            raise ValueError("fdShape CurveTrack sem sentinela inicial.")
        current.append((ref_lon + dlon, ref_lat + dlat))
    return [track for track in tracks if track]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def curved_track_line_xml(
    *,
    erid: str,
    file_name: str,
    name: str,
    tracks: Sequence[Sequence[tuple[float, float]]],
    reference: tuple[float, float],
) -> str:
    lons = [p[0] for track in tracks for p in track]
    lats = [p[1] for track in tracks for p in track]
    ref_lon, ref_lat = reference
    return f"""    <CurvedTrackLine lastModified="{_utc_now()}" sourceNode="{_ZERO_SOURCE_NODE}" erid="{{{erid}}}" spatialGeometryType="point" fileName="{file_name}" name="{_esc(name)}">
      <spatial:MBR uomSource="arcdeg" uomTarget="arcdeg" north="{max(lats)}" south="{min(lats)}" east="{max(lons)}" west="{min(lons)}" />
      <spatial:dtSignalType value="dtiSignalTypeUnknown" definedTypeRepresentation="dtSignalType" />
      <rcdscbase:vrEastShiftComponent value="0" sourceUOM="m" variableRepresentation="vrEastShiftComponent" />
      <rcdscbase:vrNorthShiftComponent value="0" sourceUOM="m" variableRepresentation="vrNorthShiftComponent" />
      <rcdscbase:vrReferenceLatitude value="{ref_lat}" sourceUOM="arcdeg" variableRepresentation="vrLatitude" />
      <rcdscbase:vrReferenceLongitude value="{ref_lon}" sourceUOM="arcdeg" variableRepresentation="vrLongitude" />
    </CurvedTrackLine>"""


def guidance_group_erid(field_erid: str, category: str) -> str:
    return str(uuid.uuid5(IDENTITY_NAMESPACE, f"guidance-group|{field_erid}|{category}"))


@dataclass
class GS3GuidanceObject:
    category: str
    erid: str
    file_name: str
    name: str
    pass_names: list[str]
    track_count: int
    tracks: list[list[tuple[float, float]]]


@dataclass
class GS3FieldGuidanceResult:
    field_erid: str
    field_dir: Path
    xml_items: list[str] = field(default_factory=list)
    objects: list[GS3GuidanceObject] = field(default_factory=list)


class GS3PassGuidanceWriter:
    """Escreve as passadas planejadas de um Field como CurvedTrackLine."""

    def __init__(self, options: JohnDeereGuidanceOptions) -> None:
        self.options = options

    def write_field(self, field_plan: FieldGuidancePlan, field_dir: Path) -> GS3FieldGuidanceResult:
        result = GS3FieldGuidanceResult(field_erid=field_plan.field_erid, field_dir=field_dir)
        for category in PASS_CATEGORIES:
            items = field_plan.items[category]
            if not items or not self.options.requested(category):
                continue
            if self.options.grouping == GROUPING_PER_PASS:
                groups = [[item] for item in items]
            else:
                groups = [items]
            for group in groups:
                if self.options.grouping == GROUPING_PER_PASS:
                    erid = group[0].item_erid
                else:
                    erid = guidance_group_erid(field_plan.field_erid, category)
                tracks = [track for item in group for track in item.tracks]
                payload, reference = write_curvetrack_fdshape(tracks)
                file_name = f"CurveTrack{erid}"
                field_dir.mkdir(parents=True, exist_ok=True)
                path = field_dir / f"{file_name}.fdShape"
                if path.exists():
                    raise ValueError(f"GS3_GUIDANCE_FILE_COLLISION: {path.name}")
                path.write_bytes(payload)
                name = guidance_group_name(category, group)
                result.xml_items.append(
                    curved_track_line_xml(
                        erid=erid,
                        file_name=file_name,
                        name=name,
                        tracks=tracks,
                        reference=reference,
                    )
                )
                result.objects.append(
                    GS3GuidanceObject(
                        category=category,
                        erid=erid,
                        file_name=file_name,
                        name=name,
                        pass_names=[item.name for item in group],
                        track_count=len(tracks),
                        tracks=tracks,
                    )
                )
        return result


# --------------------------------------------------------------------------
# Validacao pos-serializacao
# --------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_value(element: ET.Element, name: str) -> float | None:
    for child in element:
        if _local(child.tag) == name:
            try:
                return float(child.attrib.get("value", ""))
            except ValueError:
                return None
    return None


def _empty_counts() -> dict[str, int]:
    return {
        "detected": 0,
        "included": 0,
        "requested": 0,
        "serialized": 0,
        "serialized_tracks": 0,
        "objects": 0,
        "abcurve_files": 0,
        "curve_track_files": 0,
        "catalog_objects": 0,
        "straight": 0,
        "curved": 0,
    }


def validate_gs3_pass_guidance(
    plan: JohnDeereGuidanceExportPlan,
    results: dict[str, GS3FieldGuidanceResult],
    eic_root: Path,
) -> dict:
    """Contagens e referencias cruzadas das passadas no pacote GS3 montado."""
    errors: list[str] = []
    fields_report: list[dict] = []
    erid_owner: dict[str, str] = {}
    for catalog in eic_root.rglob("ImportExport.SpatialCatalog"):
        try:
            root = ET.parse(catalog).getroot()
        except ET.ParseError:
            continue
        for element in root.iter():
            if _local(element.tag) == "CurvedTrackLine":
                erid = normalized_uuid(element.attrib.get("erid"))
                if erid in erid_owner and erid_owner[erid] != str(catalog.parent):
                    errors.append(f"CurvedTrackLine {erid} repetido em mais de um Field.")
                erid_owner[erid] = str(catalog.parent)

    for field_erid, field_plan in plan.fields.items():
        result = results.get(field_erid)
        counts = {category: _empty_counts() for category in PASS_CATEGORIES}
        missing_files = 0
        orphan_files = 0
        catalog_objects = 0
        coordinates = "-"
        boundaries = headlands = 0
        if result is not None and (result.field_dir / "ImportExport.SpatialCatalog").is_file():
            catalog_text = (result.field_dir / "ImportExport.SpatialCatalog").read_text(
                encoding="utf-8", errors="replace"
            )
            boundaries = catalog_text.count("<Boundary ")
            headlands = catalog_text.count("<HeadLand ")
        for category in PASS_CATEGORIES:
            row = counts[category]
            row["detected"] = field_plan.detected[category]
            if plan.options.requested(category):
                row["included"] = row["requested"] = field_plan.included(category)
                row["straight"] = sum(1 for item in field_plan.items[category] if item.straight)
                row["curved"] = row["included"] - row["straight"]

        if result is not None and result.objects:
            catalog_path = result.field_dir / "ImportExport.SpatialCatalog"
            catalog_root = None
            if catalog_path.is_file():
                try:
                    catalog_root = ET.parse(catalog_path).getroot()
                except ET.ParseError as exc:
                    errors.append(f"{field_plan.talhao}: SpatialCatalog invalido ({exc}).")
            else:
                errors.append(f"{field_plan.talhao}: ImportExport.SpatialCatalog ausente.")
            elements: dict[str, ET.Element] = {}
            field_ref = ""
            referenced_files: set[str] = set()
            if catalog_root is not None:
                for element in catalog_root.iter():
                    tag = _local(element.tag)
                    if tag == "SpatialItems":
                        field_ref = normalized_uuid(element.attrib.get("eridFieldRef"))
                    elif tag == "CurvedTrackLine":
                        elements[normalized_uuid(element.attrib.get("erid"))] = element
                        referenced_files.add(element.attrib.get("fileName", ""))
            if catalog_root is not None and field_ref != field_erid:
                errors.append(
                    f"{GS3_FIELD_MISMATCH} ({FIELD_MISMATCH}): {field_plan.talhao} "
                    f"eridFieldRef={field_ref or '-'} difere do Field {field_erid}."
                )
            missing_reported: set[str] = set()

            for obj in result.objects:
                if not normalized_uuid(obj.erid):
                    errors.append(f"{field_plan.talhao}: UUID invalido {obj.erid} em {obj.name}.")
                    continue
                element = elements.get(normalized_uuid(obj.erid))
                if element is None:
                    errors.append(
                        f"{GS3_CATALOG_MISSING}: {field_plan.talhao}: {obj.name} ausente do SpatialCatalog."
                    )
                    continue
                catalog_objects += 1
                counts[obj.category]["catalog_objects"] += 1
                if element.attrib.get("fileName") != obj.file_name or obj.file_name != f"CurveTrack{obj.erid}":
                    errors.append(f"{field_plan.talhao}: fileName incoerente em {obj.name}.")
                shape = result.field_dir / f"{obj.file_name}.fdShape"
                if not shape.is_file():
                    missing_files += 1
                    missing_reported.add(obj.file_name)
                    errors.append(f"{GS3_FILE_MISSING}: {field_plan.talhao}: arquivo ausente {shape.name}.")
                    continue
                counts[obj.category]["curve_track_files"] += 1
                ref_lat = _child_value(element, "vrReferenceLatitude")
                ref_lon = _child_value(element, "vrReferenceLongitude")
                if ref_lat is None or ref_lon is None:
                    errors.append(f"{field_plan.talhao}: {obj.name} sem referencia lat/lon.")
                    continue
                try:
                    decoded = decode_curvetrack_fdshape(shape.read_bytes(), (ref_lon, ref_lat))
                except ValueError as exc:
                    errors.append(f"{field_plan.talhao}: {shape.name} ilegivel ({exc}).")
                    continue
                if len(decoded) != obj.track_count:
                    errors.append(
                        f"{field_plan.talhao}: {obj.name} com {len(decoded)} trilhas, esperado {obj.track_count}."
                    )
                    continue
                drift = max(
                    (
                        max(abs(a[0] - b[0]), abs(a[1] - b[1]))
                        for got, want in zip(decoded, obj.tracks)
                        for a, b in zip(got, want)
                    ),
                    default=0.0,
                )
                if drift > 1e-9 or any(len(g) != len(w) for g, w in zip(decoded, obj.tracks)):
                    errors.append(f"{field_plan.talhao}: geometria de {obj.name} alterada na serializacao.")
                    continue
                issue = coordinates_status(
                    decoded, field_plan.boundary_bbox, plan.options.field_association_margin_m
                )
                if issue is not None:
                    coordinates = "INVALID"
                    errors.append(f"{issue.code}: {field_plan.talhao} {obj.name} no GS3 ({issue.message}).")
                    continue
                if coordinates == "-":
                    coordinates = "OK"
                counts[obj.category]["objects"] += 1
                counts[obj.category]["serialized"] += len(obj.pass_names)
                counts[obj.category]["serialized_tracks"] += len(decoded)

            for file_name in sorted(referenced_files - missing_reported):
                if file_name and not (result.field_dir / f"{file_name}.fdShape").is_file():
                    missing_files += 1
                    errors.append(
                        f"{GS3_FILE_MISSING}: {field_plan.talhao}: CurvedTrackLine sem arquivo {file_name}.fdShape."
                    )
            for shape in result.field_dir.glob("CurveTrack*.fdShape"):
                if shape.stem not in referenced_files:
                    orphan_files += 1
                    errors.append(f"{GS3_CATALOG_MISSING}: {field_plan.talhao}: arquivo orfao {shape.name}.")

        for category in PASS_CATEGORIES:
            row = counts[category]
            if not plan.options.requested(category):
                continue
            if row["detected"] and row["serialized"] == 0:
                errors.append(
                    "GS3_GUIDANCE_SERIALIZATION_EMPTY: "
                    f"Foram detectadas {row['detected']} passadas, "
                    "mas nenhuma foi adicionada ao pacote GS3. "
                    f"(Field {field_plan.talhao}, {category})"
                )
            elif row["serialized"] != row["included"]:
                errors.append(
                    "GS3_GUIDANCE_COUNT_MISMATCH: "
                    f"{field_plan.talhao} {category} incluidas={row['included']}, "
                    f"serializadas={row['serialized']}."
                )

        fields_report.append(
            {
                "name": field_plan.talhao,
                "field_erid": field_erid,
                "boundary": boundaries,
                "headland": headlands,
                "planting": counts[PLANTING_PASS],
                "border": counts[BORDER_PASS],
                "skipped": dict(field_plan.skipped),
                "skipped_by_role": dict(field_plan.skipped_by_role),
                "skipped_work_paths": field_plan.skipped_roles["work_paths"],
                "skipped_headland_turns": field_plan.skipped_roles["headland_turns"],
                "skipped_maneuvers": field_plan.skipped_roles["maneuvers"],
                "skipped_coverage": field_plan.skipped_roles["coverage"],
                "invalid_geometries": field_plan.invalid_geometries,
                "uuid_errors": field_plan.uuid_errors,
                "missing_files": missing_files,
                "orphan_files": orphan_files,
                "catalog_objects": catalog_objects,
                "coordinates": coordinates,
                "warnings": list(field_plan.warnings),
            }
        )

    totals = {
        category: {
            key: sum(row["planting" if category == PLANTING_PASS else "border"][key] for row in fields_report)
            for key in _empty_counts()
        }
        for category in PASS_CATEGORIES
    }
    device_status = (
        GS3_PER_PASS_STATUS if plan.options.grouping == GROUPING_PER_PASS else GS3_DEVICE_STATUS
    )
    return {
        "status": device_status if not errors else "GS3_GUIDANCE_INCOMPLETE",
        "device_status": device_status,
        "grouping": plan.options.grouping,
        "requested": {category: plan.options.requested(category) for category in PASS_CATEGORIES},
        "gs3_object": "CurvedTrackLine",
        "gs3_guidance_types": {category: "CURVE_TRACK" for category in PASS_CATEGORIES},
        "straight_writer": STRAIGHT_WRITER_NOTE,
        "straightness_tolerance_m": plan.options.gs3_straightness_tolerance_m,
        "include_roles": sorted(plan.include_roles),
        "skip_roles": sorted(plan.skip_roles),
        "totals": totals,
        "fields": fields_report,
        "warnings": list(plan.warnings),
        "errors": errors,
    }


def guidance_failure_message(errors: Sequence[str]) -> str:
    headline = (
        EMPTY_PACKAGE_MESSAGE
        if any("SERIALIZATION_EMPTY" in error for error in errors)
        else INCOMPLETE_PACKAGE_MESSAGE
    )
    return headline + "\n" + "\n".join(errors)


def format_gs3_guidance_report(report: dict) -> list[str]:
    lines = ["", "GS3 Guidance Report", f"Agrupamento: {report.get('grouping', '-')}"]
    for row in report.get("fields", []):
        lines.append(f"Field: {row.get('name', '-')}")
        for key, category in (("planting", PLANTING_PASS), ("border", BORDER_PASS)):
            counts = row.get(key, {})
            label = PASS_LABEL[category]
            lines.extend(
                [
                    f"{label} detected: {counts.get('detected', 0)}",
                    f"{label} included: {counts.get('included', 0)}",
                    f"{label} serialized: {counts.get('serialized', 0)}",
                    f"{label} serialized tracks: {counts.get('serialized_tracks', 0)}",
                    f"{label} CurveTrack files: {counts.get('curve_track_files', 0)}",
                    f"{label} catalog objects: {counts.get('catalog_objects', 0)}",
                ]
            )
        lines.extend(
            [
                f"Skipped work paths: {row.get('skipped_work_paths', 0)}",
                f"Skipped HeadLand Turn: {row.get('skipped_headland_turns', 0)}",
                f"Skipped maneuvers: {row.get('skipped_maneuvers', 0)}",
                f"Invalid geometries: {row.get('invalid_geometries', 0)}",
                f"UUID errors: {row.get('uuid_errors', 0)}",
                f"Missing files: {row.get('missing_files', 0)}",
                f"Orphan files: {row.get('orphan_files', 0)}",
                f"Coordinates: {row.get('coordinates', '-')}",
            ]
        )
        for warning in row.get("warnings", []):
            lines.append(f"Aviso: {warning}")
        lines.append("")
    for error in report.get("errors", []):
        lines.append(f"Erro: {error}")
    lines.append(f"Status: {report.get('status', 'UNKNOWN')}")
    return lines
