"""FortSmart — import John Deere Gen4 (MasterData.xml + SpatialFiles/*.gjson)."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from .gs3_rcd_core import unify_field_bundles
from .gs3_spatial_types import Gs3FieldBundle, Gs3FieldImportResult, Gs3SpatialFeature, SpatialKind

NS = {"jd": "urn:schemas-johndeere-com:Setup"}


def find_gen4_root(path: Path) -> Path:
    p = Path(path)
    if p.is_file() and p.name.casefold() == "masterdata.xml":
        return p.parent
    candidates = [p, p / "Gen4"]
    for c in candidates:
        if (c / "MasterData.xml").exists():
            return c
    found = next(iter(p.rglob("MasterData.xml")), None)
    if found:
        return found.parent
    raise FileNotFoundError(f"MasterData.xml nao encontrado em {path}")


def detect_gen4(path: Path) -> bool:
    try:
        find_gen4_root(path)
        return True
    except FileNotFoundError:
        return False


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _attr(el: ET.Element, name: str, default: str = "") -> str:
    return (el.get(name) or default).strip()


def _find_spatial_filename(el: ET.Element) -> str:
    """Gen4 real: FilenameWithExtension fica em <Geometry>, nao como atributo."""
    direct = _attr(el, "FilenameWithExtension")
    if direct:
        return direct
    for child in el.iter():
        if _local(child.tag) == "FilenameWithExtension" and (child.text or "").strip():
            return child.text.strip()
    return ""


def _norm_guid(value: str) -> str:
    return (value or "").strip().upper()


def _load_gjson_geometry(path: Path) -> tuple[str, list[tuple[float, float]], list[list[tuple[float, float]]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    geom = None
    if data.get("type") == "FeatureCollection":
        feats = data.get("features") or []
        if feats:
            geom = feats[0].get("geometry")
    elif data.get("type") == "Feature":
        geom = data.get("geometry")
    elif data.get("type") in ("Polygon", "MultiPolygon", "LineString", "MultiLineString", "Point", "MultiPoint"):
        geom = data
    if not geom:
        return "point", [], []

    gtype = geom.get("type", "")
    coords = geom.get("coordinates") or []

    if gtype == "Polygon":
        rings = [[(float(p[0]), float(p[1])) for p in ring if len(p) >= 2] for ring in coords]
        return "polygon", [], rings
    if gtype == "MultiPolygon":
        rings = []
        for poly in coords:
            if poly and poly[0]:
                rings.append([(float(p[0]), float(p[1])) for p in poly[0] if len(p) >= 2])
        return "polygon", [], rings
    if gtype == "LineString":
        pts = [(float(p[0]), float(p[1])) for p in coords if len(p) >= 2]
        return "line", pts, []
    if gtype == "MultiLineString":
        pts = [(float(p[0]), float(p[1])) for p in (coords[0] if coords else []) if len(p) >= 2]
        return "line", pts, []
    if gtype == "MultiPoint":
        pts = [(float(p[0]), float(p[1])) for p in coords if len(p) >= 2]
        return "line" if len(pts) >= 2 else "point", pts, []
    if gtype == "Point" and len(coords) >= 2:
        return "point", [(float(coords[0]), float(coords[1]))], []
    return "point", [], []


def collect_gen4_fields(path: Path, *, unify_farms: bool = True) -> Gs3FieldImportResult:
    root_dir = find_gen4_root(path)
    master = root_dir / "MasterData.xml"
    spatial = root_dir / "SpatialFiles"
    tree = ET.parse(master)
    root = tree.getroot()

    clients: dict[str, str] = {}
    farms: dict[str, tuple[str, str]] = {}  # guid -> (name, client_guid)
    fields: dict[str, tuple[str, str]] = {}  # guid -> (name, farm_guid)

    for el in root.iter():
        tag = _local(el.tag)
        if tag == "Client":
            clients[_norm_guid(_attr(el, "StringGuid"))] = _attr(el, "Name", "Cliente")
        elif tag == "Farm":
            farms[_norm_guid(_attr(el, "StringGuid"))] = (
                _attr(el, "Name", "Fazenda"),
                _norm_guid(_attr(el, "Client")),
            )
        elif tag == "Field":
            farm_guid = ""
            for child in el:
                if _local(child.tag) == "Farm" and (child.text or "").strip():
                    farm_guid = _norm_guid(child.text)
            fields[_norm_guid(_attr(el, "StringGuid"))] = (
                _attr(el, "Name", "Talhao"),
                farm_guid or _norm_guid(_attr(el, "Farm")),
            )

    bundles_by_field: dict[str, Gs3FieldBundle] = {}

    def _bundle_for(field_guid: str) -> Gs3FieldBundle | None:
        field_guid = _norm_guid(field_guid)
        if not field_guid or field_guid not in fields:
            # criar bundle generico
            if field_guid not in bundles_by_field:
                bundles_by_field[field_guid or "unknown"] = Gs3FieldBundle(
                    cliente="Cliente",
                    fazenda="Fazenda",
                    talhao=field_guid or "Talhao",
                    field_erid=field_guid or "unknown",
                )
            return bundles_by_field[field_guid or "unknown"]
        tname, farm_guid = fields[field_guid]
        fname, client_guid = farms.get(farm_guid, ("Fazenda", ""))
        cname = clients.get(client_guid, "Cliente")
        if field_guid not in bundles_by_field:
            bundles_by_field[field_guid] = Gs3FieldBundle(
                cliente=cname,
                fazenda=fname,
                talhao=tname,
                field_erid=field_guid,
            )
        return bundles_by_field[field_guid]

    def _add_spatial(tag: str, el: ET.Element, kind: SpatialKind) -> None:
        filename = _find_spatial_filename(el)
        field_guid = _attr(el, "TaggedEntity")
        name = _attr(el, "Name", tag)
        if not filename or not spatial.exists():
            return
        gpath = spatial / filename
        if not gpath.exists():
            return
        gtype, points, rings = _load_gjson_geometry(gpath)
        bundle = _bundle_for(field_guid)
        if bundle is None:
            return
        if kind == SpatialKind.LINHA_AB and len(points) >= 2:
            points = points[:2]
        elif kind in (SpatialKind.CURVA_AB, SpatialKind.CURVA_ADAPTADA) and len(points) < 2 and not rings:
            return
        bundle.features.append(
            Gs3SpatialFeature(
                kind=kind,
                cliente=bundle.cliente,
                fazenda=bundle.fazenda,
                talhao=bundle.talhao,
                field_erid=bundle.field_erid,
                item_erid=_attr(el, "StringGuid") or filename,
                name=name,
                source_file=str(gpath),
                catalog_file=str(master),
                geometry=gtype,
                points=points,
                rings=rings,
                format_source="gen4",
            )
        )

    SPATIAL_TAGS: dict[str, SpatialKind] = {
        "OperationalBoundary": SpatialKind.LIMITE,
        "Boundary": SpatialKind.LIMITE,
        "ABLine": SpatialKind.LINHA_AB,
        "ABCurve": SpatialKind.CURVA_AB,
        "AdaptiveCurve": SpatialKind.CURVA_ADAPTADA,
        "Flag": SpatialKind.MARCAÇAO,
    }

    for el in root.iter():
        tag = _local(el.tag)
        kind = SPATIAL_TAGS.get(tag)
        if kind is not None:
            _add_spatial(tag, el, kind)

    bundles = sorted(bundles_by_field.values(), key=lambda b: (b.fazenda.casefold(), b.talhao.casefold()))
    farms_before = len({b.fazenda for b in bundles})
    farms_after = farms_before
    if unify_farms:
        bundles, farms_before, farms_after = unify_field_bundles(bundles)

    return Gs3FieldImportResult(
        eic_root=str(root_dir),
        catalog_count=1,
        field_count=len(bundles),
        bundles=bundles,
        farms_before_merge=farms_before,
        farms_after_merge=farms_after,
        format_source="gen4",
    )
