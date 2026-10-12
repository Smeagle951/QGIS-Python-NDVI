"""FortSmart — export John Deere Gen4 (MasterData.xml + SpatialFiles/*.gjson).

Alinhado ao pacote USB John Deere: Boundary/AdaptiveCurve/ABLine/ABCurve/Flag
com FilenameWithExtension dentro de <Geometry> e Path=./SpatialFiles/.
"""

from __future__ import annotations

import json
import math
import shutil
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .gs3_geometry import prepare_gs3_boundary_geometry
from .gs3_spatial_types import Gs3FieldBundle, SpatialKind

NS = "urn:schemas-johndeere-com:Setup"
NSMAP = {
    "": NS,
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "xsd": "http://www.w3.org/2001/XMLSchema",
}


def _guid() -> str:
    return str(uuid.uuid4())


def _utc_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(now.microsecond / 1000):03d}Z"


def _qn(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _heading(a_lon: float, a_lat: float, b_lon: float, b_lat: float) -> float:
    dx = b_lon - a_lon
    dy = b_lat - a_lat
    heading = math.degrees(math.atan2(dx, dy))
    if heading < 0:
        heading += 360.0
    return heading


def _close_ring(ring: list[list[float]]) -> list[list[float]]:
    if ring and ring[0] != ring[-1]:
        return ring + [ring[0]]
    return ring


def _attach_geometry(parent: ET.Element, filename: str) -> None:
    """Formato Deere real: FilenameWithExtension e Path dentro de Geometry."""
    geom = ET.SubElement(parent, _qn("Geometry"))
    ET.SubElement(geom, _qn("FilenameWithExtension")).text = filename
    ET.SubElement(geom, _qn("Path")).text = "./SpatialFiles/"


def _write_boundary_gjson(path: Path, rings: list[list[tuple[float, float]]]) -> None:
    coords = []
    for ring in rings:
        if len(ring) < 3:
            continue
        pts = [[lon, lat] for lon, lat in ring]
        pts = _close_ring(pts)
        if len(pts) >= 4:
            coords.append(pts)
    if not coords:
        return
    data = {
        "type": "FeatureCollection",
        "features": [
            {
                "id": 0,
                "type": "Feature",
                "properties": {
                    "boundarytype": "Exterior",
                    "isactive": True,
                    "ispassable": True,
                    "name": None,
                    "offsetid": None,
                    "parent": None,
                    "signaltype": 5131,
                },
                "geometry": {"type": "Polygon", "coordinates": coords},
            }
        ],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _write_abline_gjson(path: Path, a: tuple[float, float], b: tuple[float, float]) -> None:
    data = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"linetype": "AB"},
                "geometry": {
                    "type": "MultiPoint",
                    "coordinates": [[a[0], a[1], 0], [b[0], b[1], 0]],
                },
            }
        ],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _write_abcurve_gjson(path: Path, points: list[tuple[float, float]]) -> None:
    coords = [[lon, lat, 0, 0] for lon, lat in points]
    data = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"curvetype": "Track0"},
                "geometry": {"type": "MultiLineString", "coordinates": [coords]},
            }
        ],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _write_adaptive_curve_gjson(path: Path, points: list[tuple[float, float]]) -> None:
    """formato Deere Gen4: Feature + MultiLineString com [lon, lat, 0, 1]."""
    coords = [[lon, lat, 0, 1] for lon, lat in points]
    data = {
        "type": "Feature",
        "geometry": {"type": "MultiLineString", "coordinates": [coords]},
    }
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _write_flag_gjson(path: Path, lon: float, lat: float) -> None:
    data = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}}
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _add_signal(parent: ET.Element, value: str = "dtiSignalTypeRTK") -> None:
    ET.SubElement(
        parent,
        _qn("SignalType"),
        {"Representation": "dtSignalType", "Value": value},
    )


def _add_adaptive_refs(parent: ET.Element, lon: float, lat: float) -> None:
    proj = ET.SubElement(parent, _qn("SpatialProjection"))
    ET.SubElement(
        proj,
        _qn("ProjectionType"),
        {"Representation": "dtProjectionType", "Value": "dtiProjectionDeere"},
    )
    ET.SubElement(
        parent,
        _qn("ElevationReferencePoint"),
        {"Representation": "vrElevation", "Value": "0", "SourceUnit": "m"},
    )
    ET.SubElement(
        parent,
        _qn("ReferenceLatitude"),
        {"Representation": "vrLatitude", "Value": f"{lat}", "SourceUnit": "arcdeg"},
    )
    ET.SubElement(
        parent,
        _qn("ReferenceLongitude"),
        {"Representation": "vrLongitude", "Value": f"{lon}", "SourceUnit": "arcdeg"},
    )


def export_gen4(bundles: list[Gs3FieldBundle], output_root: Path) -> Path:
    if not bundles:
        raise ValueError("Nenhum talhao para exportar Gen4.")

    gen4_dir = output_root / "Gen4"
    # Never mix a new export with stale SpatialFiles. Old false boundaries or
    # oversized files make the display recognize the package and then stall.
    if gen4_dir.exists():
        shutil.rmtree(gen4_dir)
    spatial = gen4_dir / "SpatialFiles"
    spatial.mkdir(parents=True, exist_ok=True)

    for prefix, uri in NSMAP.items():
        ET.register_namespace(prefix, uri)

    timestamp = _utc_now()
    source_node = _guid()
    root = ET.Element(_qn("SetupFile"))

    ET.SubElement(
        root,
        _qn("SourceApp"),
        {
            "minor": "0",
            "major": "2",
            "build": "1",
            "revision": "0",
            "nameSourceApp": "FortSmart Precision Ag",
            "SourceAppClientId": "fortsmart-precision-ag",
        },
    )
    fsv = ET.SubElement(root, _qn("FileSchemaVersion"), {"nonProductionCode": "0"})
    ET.SubElement(fsv, _qn("FileSchemaContentVersion"), {"major": "2", "minor": "54"})
    ET.SubElement(fsv, _qn("UnitOfMeasureVersion"), {"major": "1", "minor": "12"})
    ET.SubElement(fsv, _qn("RepresentationSystemVersion"), {"major": "3", "minor": "25"})

    setup = ET.SubElement(root, _qn("Setup"))

    clients: dict[str, str] = {}
    farms: dict[str, str] = {}
    deferred: list[tuple[str, ET.Element]] = []

    written = 0
    for bundle in bundles:
        cname = bundle.cliente or "Cliente"
        fname = bundle.fazenda or "Fazenda"
        if cname not in clients:
            clients[cname] = _guid()
            ET.SubElement(
                setup,
                _qn("Client"),
                {
                    "CreationDate": timestamp,
                    "SourceNode": source_node,
                    "LastModifiedDate": timestamp,
                    "Archived": "false",
                    "StringGuid": clients[cname],
                    "Name": cname,
                },
            )
        fkey = f"{cname}::{fname}"
        if fkey not in farms:
            farms[fkey] = _guid()
            ET.SubElement(
                setup,
                _qn("Farm"),
                {
                    "CreationDate": timestamp,
                    "SourceNode": source_node,
                    "LastModifiedDate": timestamp,
                    "Archived": "false",
                    "StringGuid": farms[fkey],
                    "Name": fname,
                    "Client": clients[cname],
                },
            )

        field_guid = (bundle.field_erid or _guid()).strip("{}")
        field_el = ET.SubElement(
            setup,
            _qn("Field"),
            {
                "CreationDate": timestamp,
                "SourceNode": source_node,
                "LastModifiedDate": timestamp,
                "Archived": "false",
                "StringGuid": field_guid,
                "Name": bundle.talhao,
            },
        )
        farm_ref = ET.SubElement(field_el, _qn("Farm"))
        farm_ref.text = farms[fkey]

        external_boundary_written = False
        for feature in bundle.features:
            if feature.kind == SpatialKind.LIMITE and not external_boundary_written:
                rings = feature.rings or ([feature.points] if len(feature.points) >= 3 else [])
                if not rings:
                    continue
                prepared = prepare_gs3_boundary_geometry(rings)
                # Gen4 receives only the simplified exterior operational
                # boundary. Internal/headland polygons are not separate
                # OperationalBoundary objects.
                rings = [prepared.rings[0]]
                b_guid = (feature.item_erid or _guid()).strip("{}")
                filename = f"Boundary{b_guid}.gjson"
                _write_boundary_gjson(spatial / filename, rings)
                el = ET.Element(
                    _qn("OperationalBoundary"),
                    {
                        "CreationDate": timestamp,
                        "SourceNode": source_node,
                        "LastModifiedDate": timestamp,
                        "Archived": "false",
                        "StringGuid": b_guid,
                        "Name": feature.name or "LIMITE EXTERNO",
                        "TaggedEntity": field_guid,
                    },
                )
                _add_signal(el, "dtiSignalTypeRTK")
                _attach_geometry(el, filename)
                deferred.append(("boundary", el))
                written += 1
                external_boundary_written = True

            elif feature.kind in (SpatialKind.LIMITE_INTERNO, SpatialKind.BORDADURA):
                # These are associations of the field boundary, not standalone
                # Gen4 OperationalBoundary objects.
                continue

            elif feature.kind == SpatialKind.LINHA_AB and len(feature.points) >= 2:
                a, b = feature.points[0], feature.points[1]
                g = (feature.item_erid or _guid()).strip("{}")
                filename = f"ABLine{g}.gjson"
                _write_abline_gjson(spatial / filename, a, b)
                el = ET.Element(
                    _qn("ABLine"),
                    {
                        "CreationDate": timestamp,
                        "SourceNode": source_node,
                        "LastModifiedDate": timestamp,
                        "Archived": "false",
                        "StringGuid": g,
                        "Name": feature.name or "AB",
                        "TaggedEntity": field_guid,
                        "Heading": f"{_heading(a[0], a[1], b[0], b[1]):.6f}",
                    },
                )
                _attach_geometry(el, filename)
                deferred.append(("abline", el))
                written += 1

            elif feature.kind == SpatialKind.CURVA_AB and len(feature.points) >= 2:
                g = (feature.item_erid or _guid()).strip("{}")
                filename = f"ABCurve{g}.gjson"
                _write_abcurve_gjson(spatial / filename, feature.points)
                el = ET.Element(
                    _qn("ABCurve"),
                    {
                        "CreationDate": timestamp,
                        "SourceNode": source_node,
                        "LastModifiedDate": timestamp,
                        "Archived": "false",
                        "StringGuid": g,
                        "Name": feature.name or "Curva AB",
                        "TaggedEntity": field_guid,
                    },
                )
                _attach_geometry(el, filename)
                deferred.append(("abcurve", el))
                written += 1

            elif feature.kind in (SpatialKind.CURVA_ADAPTADA, SpatialKind.TRABALHO) and len(feature.points) >= 2:
                g = (feature.item_erid or _guid()).strip("{}")
                filename = f"AdaptiveCurve{g}.gjson"
                _write_adaptive_curve_gjson(spatial / filename, feature.points)
                el = ET.Element(
                    _qn("AdaptiveCurve"),
                    {
                        "Archived": "false",
                        "StringGuid": g,
                        "Name": feature.name or "CURVA",
                        "TaggedEntity": field_guid,
                    },
                )
                _add_signal(el, "dtiSignalTypeUnknown")
                _attach_geometry(el, filename)
                _add_adaptive_refs(el, feature.points[0][0], feature.points[0][1])
                deferred.append(("adaptive", el))
                written += 1

            elif feature.kind in (SpatialKind.MARCAÇAO, SpatialKind.OBSTACULO_PONTO) and feature.points:
                lon, lat = feature.points[0]
                g = (feature.item_erid or _guid()).strip("{}")
                filename = f"Flag{g}.gjson"
                _write_flag_gjson(spatial / filename, lon, lat)
                el = ET.Element(
                    _qn("Flag"),
                    {
                        "CreationDate": timestamp,
                        "SourceNode": source_node,
                        "LastModifiedDate": timestamp,
                        "Archived": "false",
                        "StringGuid": g,
                        "Name": feature.name or "Flag",
                        "TaggedEntity": field_guid,
                    },
                )
                _attach_geometry(el, filename)
                deferred.append(("flag", el))
                written += 1

    # Ordem tipica Deere: curvas/linhas, depois limites
    order = {"adaptive": 0, "abcurve": 1, "abline": 2, "flag": 3, "boundary": 4}
    for _, el in sorted(deferred, key=lambda item: order.get(item[0], 9)):
        setup.append(el)

    if written == 0:
        raise ValueError("Nenhuma geometria valida para exportar Gen4.")

    oversized = [
        path
        for path in spatial.glob("*.gjson")
        if path.stat().st_size > 5 * 1024 * 1024
    ]
    if oversized:
        raise ValueError(
            "Gen4 contem geometria excessiva: "
            + ", ".join(f"{path.name} ({path.stat().st_size} bytes)" for path in oversized)
        )

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(gen4_dir / "MasterData.xml", encoding="utf-8", xml_declaration=True)
    return gen4_dir
