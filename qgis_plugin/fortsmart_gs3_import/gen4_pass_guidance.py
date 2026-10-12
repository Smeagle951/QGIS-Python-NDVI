"""FortSmart — passadas planejadas como AdaptiveCurve Gen4 (Guidance/Tracks).

Contrato dos pacotes Deere aceitos (Gen4/MasterData.xml + SpatialFiles):

* ``AdaptiveCurve{UUID}.gjson`` = um Feature GeoJSON com MultiLineString e
  coordenadas ``[lon, lat, 0, 1]``; cada passada e uma linha do MultiLineString;
* ``<Setup><Guidance><Tracks><AdaptiveCurve ... TaggedEntity="{Field}">`` com
  SignalType, Geometry, SpatialProjection(ProjectionType,
  ElevationReferencePoint) e ReferenceLatitude/Longitude do primeiro vertice.

Roda depois do export Gen4 existente, sem alterar os objetos ja gravados.
Este modulo nao conhece o GS3; o plano comum fica em jd_guidance_plan.
"""

from __future__ import annotations

import json
import shutil
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .gen4_export import NSMAP, _add_signal, _attach_geometry, _qn
from .jd_guidance_plan import (
    BORDER_PASS,
    EMPTY_PACKAGE_MESSAGE,
    FIELD_MISMATCH,
    GEN4_DEVICE_STATUS,
    IDENTITY_NAMESPACE,
    INCOMPLETE_PACKAGE_MESSAGE,
    PASS_CATEGORIES,
    PASS_SHORT_LABEL,
    PLANTING_PASS,
    JohnDeereGuidanceExportPlan,
    guidance_group_name,
    normalized_uuid,
)
from .jd_guidance_validators import GEN4_FIELD_MISMATCH, GEN4_MASTERDATA_INCOMPLETE, coordinates_status


GEN4_GUIDANCE_DEBUG = "fortsmart_gen4_guidance_debug.json"
GEN4_MAX_GJSON_BYTES = 5 * 1024 * 1024


def gen4_guidance_erid(field_erid: str, category: str) -> str:
    return str(uuid.uuid5(IDENTITY_NAMESPACE, f"gen4-guidance-group|{field_erid}|{category}"))


def write_adaptive_curve_tracks_gjson(path: Path, tracks: Sequence[Sequence[tuple[float, float]]]) -> None:
    data = {
        "geometry": {
            "coordinates": [[[lon, lat, 0, 1] for lon, lat in track] for track in tracks],
            "type": "MultiLineString",
        },
        "type": "Feature",
    }
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def adaptive_curve_element(
    *,
    erid: str,
    name: str,
    field_guid: str,
    filename: str,
    reference: tuple[float, float],
) -> ET.Element:
    element = ET.Element(
        _qn("AdaptiveCurve"),
        {"Archived": "false", "StringGuid": erid, "Name": name, "TaggedEntity": field_guid},
    )
    _add_signal(element, "dtiSignalTypeUnknown")
    _attach_geometry(element, filename)
    projection = ET.SubElement(element, _qn("SpatialProjection"))
    ET.SubElement(
        projection,
        _qn("ProjectionType"),
        {"Representation": "dtProjectionType", "Value": "dtiProjectionDeere"},
    )
    ET.SubElement(
        projection,
        _qn("ElevationReferencePoint"),
        {"Representation": "vrElevation", "Value": "0", "SourceUnit": "m"},
    )
    lon, lat = reference
    ET.SubElement(
        element,
        _qn("ReferenceLatitude"),
        {"Representation": "vrLatitude", "Value": f"{lat}", "SourceUnit": "arcdeg"},
    )
    ET.SubElement(
        element,
        _qn("ReferenceLongitude"),
        {"Representation": "vrLongitude", "Value": f"{lon}", "SourceUnit": "arcdeg"},
    )
    return element


@dataclass
class Gen4GuidanceObject:
    category: str
    field_erid: str
    erid: str
    file_name: str
    name: str
    pass_names: list[str]
    tracks: list[list[tuple[float, float]]]


class Gen4PassGuidanceRouter:
    """Acrescenta as passadas do plano ao pacote Gen4 ja gerado."""

    def __init__(self, plan: JohnDeereGuidanceExportPlan) -> None:
        self.plan = plan

    def apply(self, gen4_dir: Path) -> dict:
        gen4_dir = Path(gen4_dir)
        pre_errors: list[str] = []
        try:
            objects = self._write(gen4_dir, pre_errors)
            report = validate_gen4_pass_guidance(self.plan, objects, gen4_dir, pre_errors)
        except Exception as exc:  # writer failure must never look like success
            report = _empty_report(self.plan, [*pre_errors, f"GEN4_GUIDANCE_WRITER_FAILED: {exc}"])
        (gen4_dir.parent / GEN4_GUIDANCE_DEBUG).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if report["errors"]:
            shutil.rmtree(gen4_dir, ignore_errors=True)
            headline = (
                EMPTY_PACKAGE_MESSAGE
                if any("SERIALIZATION_EMPTY" in error for error in report["errors"])
                else INCOMPLETE_PACKAGE_MESSAGE
            )
            raise RuntimeError(headline + "\n" + "\n".join(report["errors"]))
        return report

    def _write(self, gen4_dir: Path, errors: list[str]) -> list[Gen4GuidanceObject]:
        master = gen4_dir / "MasterData.xml"
        spatial = gen4_dir / "SpatialFiles"
        for prefix, uri in NSMAP.items():
            ET.register_namespace(prefix, uri)
        tree = ET.parse(master)
        setup = tree.getroot().find(_qn("Setup"))
        if setup is None:
            raise ValueError("MasterData.xml sem Setup.")
        fields = {
            normalized_uuid(element.get("StringGuid")): element.get("StringGuid")
            for element in setup.findall(_qn("Field"))
        }

        objects: list[Gen4GuidanceObject] = []
        elements: list[ET.Element] = []
        for field_erid, field_plan in self.plan.fields.items():
            for category in PASS_CATEGORIES:
                items = field_plan.items[category]
                if not items or not self.plan.options.gen4_requested(category):
                    continue
                field_guid = fields.get(field_erid)
                if field_guid is None:
                    errors.append(
                        f"{GEN4_FIELD_MISMATCH} ({FIELD_MISMATCH}): {field_plan.talhao} "
                        "sem Field no MasterData.xml Gen4."
                    )
                    continue
                tracks = [track for item in items for track in item.tracks]
                erid = gen4_guidance_erid(field_erid, category)
                filename = f"AdaptiveCurve{erid}.gjson"
                path = spatial / filename
                if path.exists():
                    raise ValueError(f"GEN4_GUIDANCE_FILE_COLLISION: {filename}")
                write_adaptive_curve_tracks_gjson(path, tracks)
                name = guidance_group_name(category, items)
                elements.append(
                    adaptive_curve_element(
                        erid=erid,
                        name=name,
                        field_guid=field_guid,
                        filename=filename,
                        reference=tracks[0][0],
                    )
                )
                objects.append(
                    Gen4GuidanceObject(
                        category=category,
                        field_erid=field_erid,
                        erid=erid,
                        file_name=filename,
                        name=name,
                        pass_names=[item.name for item in items],
                        tracks=tracks,
                    )
                )

        if elements:
            guidance = setup.find(_qn("Guidance"))
            if guidance is None:
                guidance = ET.Element(_qn("Guidance"))
                children = list(setup)
                # Deere packages place Guidance before the OperationalBoundary list.
                index = next(
                    (i for i, child in enumerate(children) if child.tag == _qn("OperationalBoundary")),
                    len(children),
                )
                setup.insert(index, guidance)
            tracks_element = guidance.find(_qn("Tracks"))
            if tracks_element is None:
                tracks_element = ET.SubElement(guidance, _qn("Tracks"))
            tracks_element.extend(elements)
            ET.indent(tree, space="  ")
            tree.write(master, encoding="utf-8", xml_declaration=True)
        return objects


Gen4GuidanceRouter = Gen4PassGuidanceRouter


def _empty_counts() -> dict[str, int]:
    return {
        "detected": 0,
        "requested": 0,
        "serialized": 0,
        "serialized_tracks": 0,
        "adaptive_curve_files": 0,
        "masterdata_refs": 0,
    }


def _empty_report(plan: JohnDeereGuidanceExportPlan, errors: list[str]) -> dict:
    return {
        "status": "GEN4_GUIDANCE_INCOMPLETE",
        "device_status": GEN4_DEVICE_STATUS,
        "requested": {category: plan.options.gen4_requested(category) for category in PASS_CATEGORIES},
        "gen4_object": "Guidance/Tracks/AdaptiveCurve",
        "totals": {category: _empty_counts() for category in PASS_CATEGORIES},
        "fields": [],
        "errors": errors,
    }


def _same_tracks(coordinates: list, tracks: list[list[tuple[float, float]]]) -> bool:
    if len(coordinates) != len(tracks):
        return False
    for line, track in zip(coordinates, tracks):
        if len(line) != len(track):
            return False
        for point, (lon, lat) in zip(line, track):
            if len(point) != 4 or point[0] != lon or point[1] != lat or point[2:] != [0, 1]:
                return False
    return True


def validate_gen4_pass_guidance(
    plan: JohnDeereGuidanceExportPlan,
    objects: list[Gen4GuidanceObject],
    gen4_dir: Path,
    pre_errors: list[str] | None = None,
) -> dict:
    """Contagens e referencias cruzadas das passadas no pacote Gen4 montado."""
    errors: list[str] = list(pre_errors or [])
    spatial = gen4_dir / "SpatialFiles"
    root = ET.parse(gen4_dir / "MasterData.xml").getroot()
    setup = root.find(_qn("Setup"))
    field_guids = {
        normalized_uuid(element.get("StringGuid"))
        for element in (setup.findall(_qn("Field")) if setup is not None else [])
    }
    boundaries: dict[str, int] = {}
    for element in root.iter(_qn("OperationalBoundary")):
        tagged = normalized_uuid(element.get("TaggedEntity"))
        boundaries[tagged] = boundaries.get(tagged, 0) + 1
    track_guids: set[str] = set()
    adaptive: dict[str, ET.Element] = {}
    referenced: set[str] = set()
    for element in root.iter(_qn("AdaptiveCurve")):
        guid = normalized_uuid(element.get("StringGuid"))
        adaptive[guid] = element
        filename = element.findtext(f"{_qn('Geometry')}/{_qn('FilenameWithExtension')}") or ""
        referenced.add(filename)
        if filename and not (spatial / filename).is_file():
            errors.append(f"GEN4_GUIDANCE_ORPHAN_REFERENCE: {filename} referenciado sem arquivo.")
    for tracks_element in root.iter(_qn("Tracks")):
        for element in tracks_element.findall(_qn("AdaptiveCurve")):
            track_guids.add(normalized_uuid(element.get("StringGuid")))

    counts: dict[str, dict[str, dict[str, int]]] = {}
    for field_erid, field_plan in plan.fields.items():
        counts[field_erid] = {category: _empty_counts() for category in PASS_CATEGORIES}
        for category in PASS_CATEGORIES:
            row = counts[field_erid][category]
            row["detected"] = field_plan.detected[category]
            if plan.options.gen4_requested(category):
                row["requested"] = field_plan.included(category)

    invalid_coordinates: set[str] = set()
    for obj in objects:
        label = f"{plan.for_field(obj.field_erid).talhao} {obj.name}"
        guid = normalized_uuid(obj.erid)
        element = adaptive.get(guid)
        if not guid or element is None or guid not in track_guids:
            errors.append(f"GEN4_GUIDANCE_MISSING_REFERENCE: {label} ausente de Guidance/Tracks.")
            continue
        tagged = normalized_uuid(element.get("TaggedEntity"))
        if tagged != obj.field_erid or tagged not in field_guids:
            errors.append(f"{GEN4_FIELD_MISMATCH} ({FIELD_MISMATCH}): {label} TaggedEntity={tagged or '-'}.")
            continue
        filename = element.findtext(f"{_qn('Geometry')}/{_qn('FilenameWithExtension')}")
        if filename != obj.file_name or filename != f"AdaptiveCurve{obj.erid}.gjson":
            errors.append(f"GEN4_GUIDANCE_FILENAME_MISMATCH: {label}.")
            continue
        if not element.get("Name") or not element.findtext(f"{_qn('Geometry')}/{_qn('Path')}"):
            errors.append(f"{GEN4_MASTERDATA_INCOMPLETE}: {label} sem Name ou Geometry/Path.")
            continue
        field_row = counts.setdefault(obj.field_erid, {c: _empty_counts() for c in PASS_CATEGORIES})
        field_row[obj.category]["masterdata_refs"] += 1
        path = spatial / filename
        if not path.is_file():
            errors.append(f"GEN4_GUIDANCE_MISSING_FILE: {filename}.")
            continue
        if path.stat().st_size > GEN4_MAX_GJSON_BYTES:
            errors.append(f"GEN4_GUIDANCE_FILE_TOO_LARGE: {filename} ({path.stat().st_size} bytes).")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            geometry = data["geometry"]
            valid = data.get("type") == "Feature" and geometry.get("type") == "MultiLineString"
            coordinates = geometry["coordinates"]
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            errors.append(f"GEN4_GUIDANCE_INVALID_GJSON: {filename} ({exc}).")
            continue
        if not valid or not _same_tracks(coordinates, obj.tracks):
            errors.append(f"GEN4_GUIDANCE_GEOMETRY_MISMATCH: {label}.")
            continue
        try:
            ref_lat = float(element.find(_qn("ReferenceLatitude")).get("Value"))
            ref_lon = float(element.find(_qn("ReferenceLongitude")).get("Value"))
        except (AttributeError, TypeError, ValueError):
            errors.append(f"GEN4_GUIDANCE_REFERENCE_INVALID: {label}.")
            continue
        if (ref_lon, ref_lat) != obj.tracks[0][0]:
            errors.append(f"GEN4_GUIDANCE_REFERENCE_INVALID: {label}.")
            continue
        field_plan = plan.for_field(obj.field_erid)
        issue = coordinates_status(
            [[(point[0], point[1]) for point in line] for line in coordinates],
            field_plan.boundary_bbox,
            plan.options.field_association_margin_m,
        )
        if issue is not None:
            invalid_coordinates.add(obj.field_erid)
            errors.append(f"{issue.code}: {label} no Gen4 ({issue.message}).")
            continue
        row = counts.setdefault(obj.field_erid, {c: _empty_counts() for c in PASS_CATEGORIES})[obj.category]
        row["serialized"] += len(obj.pass_names)
        row["serialized_tracks"] += len(coordinates)
        row["adaptive_curve_files"] += 1

    orphan_files = [path.name for path in spatial.glob("AdaptiveCurve*.gjson") if path.name not in referenced]
    for name in orphan_files:
        errors.append(f"GEN4_GUIDANCE_ORPHAN_FILE: {name}.")

    fields_report: list[dict] = []
    for field_erid, field_plan in plan.fields.items():
        field_counts = counts[field_erid]
        for category in PASS_CATEGORIES:
            row = field_counts[category]
            if not plan.options.gen4_requested(category):
                continue
            if row["detected"] and row["serialized"] == 0:
                errors.append(
                    "GEN4_GUIDANCE_SERIALIZATION_EMPTY: "
                    f"Foram detectadas {row['detected']} passadas, "
                    "mas nenhuma foi adicionada ao pacote Gen4. "
                    f"(Field {field_plan.talhao}, {category})"
                )
            elif row["serialized"] != row["requested"]:
                errors.append(
                    "GEN4_GUIDANCE_COUNT_MISMATCH: "
                    f"{field_plan.talhao} {category} solicitadas={row['requested']}, "
                    f"serializadas={row['serialized']}."
                )
        serialized = sum(field_counts[category]["serialized"] for category in PASS_CATEGORIES)
        fields_report.append(
            {
                "name": field_plan.talhao,
                "field_erid": field_erid,
                "boundary": boundaries.get(field_erid, 0),
                "planting": field_counts[PLANTING_PASS],
                "border": field_counts[BORDER_PASS],
                "coordinates": (
                    "INVALID" if field_erid in invalid_coordinates else "OK" if serialized else "-"
                ),
            }
        )

    totals = {
        category: {
            key: sum(row["planting" if category == PLANTING_PASS else "border"][key] for row in fields_report)
            for key in _empty_counts()
        }
        for category in PASS_CATEGORIES
    }
    return {
        "status": GEN4_DEVICE_STATUS if not errors else "GEN4_GUIDANCE_INCOMPLETE",
        "device_status": GEN4_DEVICE_STATUS,
        "requested": {category: plan.options.gen4_requested(category) for category in PASS_CATEGORIES},
        "gen4_object": "Guidance/Tracks/AdaptiveCurve",
        "totals": totals,
        "fields": fields_report,
        "orphan_files": orphan_files,
        "errors": errors,
    }


def format_gen4_guidance_report(report: dict) -> list[str]:
    lines = ["", "Gen4 Guidance Report"]
    for row in report.get("fields", []):
        lines.append(f"Field: {row.get('name', '-')}")
        for key, category in (("planting", PLANTING_PASS), ("border", BORDER_PASS)):
            counts = row.get(key, {})
            lines.extend(
                [
                    f"{PASS_SHORT_LABEL[category]}:",
                    f"detected = {counts.get('detected', 0)}",
                    f"requested = {counts.get('requested', 0)}",
                    f"serialized = {counts.get('serialized', 0)}",
                    f"adaptive_curve_files = {counts.get('adaptive_curve_files', 0)}",
                    f"masterdata_refs = {counts.get('masterdata_refs', 0)}",
                ]
            )
        lines.append(f"coordinates = {row.get('coordinates', '-')}")
        lines.append("")
    for error in report.get("errors", []):
        lines.append(f"Erro: {error}")
    lines.append(f"Status: {report.get('status', 'UNKNOWN')}")
    return lines
