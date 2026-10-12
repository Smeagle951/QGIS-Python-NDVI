"""
FortSmart — leitura John Deere GS3 2630 (RCD / fdShape / SpatialCatalog).
"""

from __future__ import annotations

import unicodedata
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .fortsmart_coords import fix_lon_lat, sanitize_ring
from .jd_fdshape import decode_fdshape, decode_fdshape_tracks
from .gs3_spatial_types import (
    Gs3FieldBundle,
    Gs3FieldImportResult,
    Gs3SpatialFeature,
    SpatialKind,
)


@dataclass
class Gs3Boundary:
    cliente: str
    fazenda: str
    talhao: str
    field_erid: str
    boundary_erid: str
    source_file: str
    points: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class Gs3ImportResult:
    eic_root: Path
    catalog_count: int
    field_count: int
    boundaries: list[Gs3Boundary]
    farms_before_merge: int = 0
    farms_after_merge: int = 0


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _norm_guid(value: str) -> str:
    return (value or "").strip().strip("{}").lower()


def _clean_label(value: str, fallback: str = "") -> str:
    text = " ".join((value or "").split())
    return text if text and text not in {"---", "-"} else fallback


def _farm_match_key(name: str) -> str:
    text = unicodedata.normalize("NFKD", _clean_label(name, "sem fazenda"))
    text = "".join(ch for ch in text.casefold() if ch.isalnum())
    return text or "semfazenda"


@dataclass
class SetupIndex:
    clients: dict[str, str]
    farms: dict[str, dict[str, str]]
    fields: dict[str, dict[str, str]]


def parse_setup_index(setup_path: Path) -> SetupIndex:
    clients: dict[str, str] = {}
    farms: dict[str, dict[str, str]] = {}
    fields: dict[str, dict[str, str]] = {}

    if not setup_path.exists():
        return SetupIndex(clients, farms, fields)

    root = ET.parse(setup_path).getroot()
    for el in root.iter():
        tag = _local(el.tag)
        erid = _norm_guid(el.attrib.get("erid", ""))
        name = _clean_label(el.attrib.get("name", ""))
        if not erid:
            continue
        if tag == "Client" and name:
            clients[erid] = name
        elif tag == "Farm" and name:
            farms[erid] = {
                "name": name,
                "client_ref": _norm_guid(el.attrib.get("clientRef", "")),
            }
        elif tag == "Field" and name:
            fields[erid] = {
                "name": name,
                "farm_ref": _norm_guid(el.attrib.get("farmRef", "")),
            }
    return SetupIndex(clients, farms, fields)


def resolve_field_metadata(field_erid: str, setup: SetupIndex) -> tuple[str, str, str]:
    field_erid = _norm_guid(field_erid)
    field_meta = setup.fields.get(field_erid, {})
    talhao = _clean_label(field_meta.get("name", ""), field_erid or "Talhao")

    farm_ref = field_meta.get("farm_ref", "")
    farm_meta = setup.farms.get(farm_ref, {})
    fazenda = _clean_label(farm_meta.get("name", ""), "Fazenda")

    client_ref = farm_meta.get("client_ref", "")
    cliente = _clean_label(setup.clients.get(client_ref, ""), "Cliente")
    return cliente, fazenda, talhao


def parse_setup_fds(setup_path: Path) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    setup = parse_setup_index(setup_path)
    clients = setup.clients
    farms = {k: v["name"] for k, v in setup.farms.items()}
    fields = {k: v["name"] for k, v in setup.fields.items()}
    return clients, farms, fields


def read_fdshape_points(
    fdshape_path: Path,
    *,
    close_ring: bool = True,
    mbr: tuple[float, float, float, float] | None = None,
    reference: tuple[float, float] | None = None,
) -> list[tuple[float, float]]:
    """Le um ``.fdShape`` usando o decodificador canonico validado.

    ``reference`` vem de ``vrReferenceLongitude`` / ``vrReferenceLatitude`` e e
    indispensavel para ``CurvedTrackLine``, que grava deslocamentos em vez de
    coordenadas absolutas.
    """
    min_pts = 4 if close_ring else 2
    result = decode_fdshape(
        fdshape_path,
        mbr=mbr,
        reference=reference,
        close_ring=close_ring,
    )
    if not result.points:
        return []
    if result.mbr_match:
        # O MBR do catalogo ja confirmou cada ponto; as heuristicas de
        # sanitize_ring (troca lat/lon, descarte por magnitude) so poderiam
        # corromper coordenadas comprovadamente corretas.
        return result.points if len(result.points) >= min_pts else []
    return sanitize_ring(result.points, min_points=min_pts)


def _mbr_from_element(el: ET.Element) -> tuple[float, float, float, float] | None:
    for child in el.iter():
        if _local(child.tag) != "MBR":
            continue
        try:
            return (
                float(child.attrib["west"]),
                float(child.attrib["south"]),
                float(child.attrib["east"]),
                float(child.attrib["north"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _reference_from_element(el: ET.Element) -> tuple[float, float] | None:
    """Origem (lon, lat) usada pelos objetos gravados em delta."""
    ref_lon = ref_lat = None
    for child in el.iter():
        tag = _local(child.tag)
        value = child.attrib.get("value")
        if not value:
            continue
        try:
            if tag == "vrReferenceLongitude":
                ref_lon = float(value)
            elif tag == "vrReferenceLatitude":
                ref_lat = float(value)
        except (TypeError, ValueError):
            continue
    if ref_lon is None or ref_lat is None:
        return None
    return (ref_lon, ref_lat)


def _point_from_element(el: ET.Element) -> tuple[float, float] | None:
    lat = el.attrib.get("latitude")
    lon = el.attrib.get("longitude")
    if lat and lon:
        return fix_lon_lat(float(lon), float(lat))
    for child in el.iter():
        tag = _local(child.tag)
        if tag == "vrLatitude" and child.attrib.get("value"):
            lat = child.attrib["value"]
        elif tag == "vrLongitude" and child.attrib.get("value"):
            lon = child.attrib["value"]
    if lat and lon:
        return fix_lon_lat(float(lon), float(lat))
    return None


def _catalog_field_erid(root: ET.Element) -> str:
    for el in root.iter():
        if _local(el.tag) == "SpatialItems":
            ref = _norm_guid(el.attrib.get("eridFieldRef", ""))
            if ref:
                return ref
        if _local(el.tag) == "Field":
            ref = _norm_guid(el.attrib.get("erid", ""))
            if ref:
                return ref
    return ""


def _infer_curved_track_kind(name: str) -> SpatialKind:
    text = _clean_label(name, "").casefold()
    if any(k in text for k in ("bordad", "headland", "contorno")):
        return SpatialKind.BORDADURA
    if any(k in text for k in ("trabalho", "plant", "aplic", "pulv", "operac")):
        return SpatialKind.TRABALHO
    return SpatialKind.CURVA_ADAPTADA


def _parse_boundary_feature(
    el: ET.Element,
    base_dir: Path,
    meta: tuple[str, str, str, str],
    catalog_file: str,
) -> Gs3SpatialFeature | None:
    cliente, fazenda, talhao, field_erid = meta
    prefix = el.attrib.get("fileName", "")
    if not prefix:
        return None
    fdshape = base_dir / f"{prefix}.fdShape"
    if not fdshape.exists():
        return None
    mbr = _mbr_from_element(el)
    points = read_fdshape_points(
        fdshape,
        close_ring=True,
        mbr=mbr,
        reference=_reference_from_element(el),
    )
    if len(points) < 4:
        return None

    internal = False
    display_name = _clean_label(el.attrib.get("name", ""), "Limite")
    headland_erid = ""
    for child in el:
        tag = _local(child.tag)
        if tag == "NameIDPair":
            pair_name = _clean_label(child.attrib.get("Name", ""), "")
            if pair_name:
                display_name = pair_name
            if "intern" in pair_name.casefold():
                internal = True
        elif tag == "HeadlandLookup":
            headland_erid = _norm_guid(child.attrib.get("eridRef", ""))

    extra: dict[str, str] = {}
    if headland_erid:
        extra["headland_erid"] = headland_erid

    return Gs3SpatialFeature(
        kind=SpatialKind.LIMITE_INTERNO if internal else SpatialKind.LIMITE,
        cliente=cliente,
        fazenda=fazenda,
        talhao=talhao,
        field_erid=field_erid,
        item_erid=_norm_guid(el.attrib.get("erid", "")),
        name=display_name,
        source_file=str(fdshape),
        catalog_file=catalog_file,
        geometry="polygon",
        points=points,
        extra=extra,
    )


def _parse_abline_feature(
    el: ET.Element,
    meta: tuple[str, str, str, str],
    catalog_file: str,
) -> Gs3SpatialFeature | None:
    cliente, fazenda, talhao, field_erid = meta
    a_pt = b_pt = None
    sign_fixed = False
    for child in el:
        tag = _local(child.tag)
        if tag not in ("APoint", "BPoint"):
            continue
        point, fixed = _ab_point(child)
        sign_fixed = sign_fixed or fixed
        if tag == "APoint":
            a_pt = point
        else:
            b_pt = point
    if not a_pt or not b_pt:
        return None
    feature = Gs3SpatialFeature(
        kind=SpatialKind.LINHA_AB,
        cliente=cliente,
        fazenda=fazenda,
        talhao=talhao,
        field_erid=field_erid,
        item_erid=_norm_guid(el.attrib.get("erid", "")),
        name=_clean_label(el.attrib.get("name", ""), "Linha AB"),
        source_file=catalog_file,
        catalog_file=catalog_file,
        geometry="line",
        points=[a_pt, b_pt],
        active=el.attrib.get("active", "true").lower() != "false",
    )
    if sign_fixed:
        feature.extra["longitude_sign_fixed"] = "true"
    return feature


def _ab_point(el: ET.Element) -> tuple[tuple[float, float] | None, bool]:
    """Ponto A/B; o monitor as vezes grava a longitude oeste sem o sinal (54.4 em vez de -54.4)."""
    point = _point_from_element(el)
    if point is not None:
        return point, False
    try:
        lon = float(el.attrib["longitude"])
        lat = float(el.attrib["latitude"])
    except (KeyError, TypeError, ValueError):
        return None, False
    if lat < 0 < lon:
        flipped = fix_lon_lat(-lon, lat)
        if flipped is not None:
            return flipped, True
    return None, False


def _parse_abcurve_feature(
    el: ET.Element,
    base_dir: Path,
    meta: tuple[str, str, str, str],
    catalog_file: str,
) -> Gs3SpatialFeature | None:
    cliente, fazenda, talhao, field_erid = meta
    prefix = el.attrib.get("fileName", "")
    points: list[tuple[float, float]] = []
    mbr = _mbr_from_element(el)
    if prefix:
        fdshape = base_dir / f"{prefix}.fdShape"
        if fdshape.exists():
            points = read_fdshape_points(
                fdshape,
                close_ring=False,
                mbr=mbr,
                reference=_reference_from_element(el),
            )
    if len(points) < 2:
        a_pt = b_pt = None
        for child in el:
            tag = _local(child.tag)
            if tag == "APoint":
                a_pt = _point_from_element(child)
            elif tag == "BPoint":
                b_pt = _point_from_element(child)
        if a_pt and b_pt:
            points = [a_pt, b_pt]
    if len(points) < 2:
        return None
    return Gs3SpatialFeature(
        kind=SpatialKind.CURVA_AB,
        cliente=cliente,
        fazenda=fazenda,
        talhao=talhao,
        field_erid=field_erid,
        item_erid=_norm_guid(el.attrib.get("erid", "")),
        name=_clean_label(el.attrib.get("name", ""), "Curva AB"),
        source_file=str(base_dir / f"{prefix}.fdShape") if prefix else catalog_file,
        catalog_file=catalog_file,
        geometry="line",
        points=points,
    )


def _curved_track_tracks(
    fdshape: Path,
    mbr: tuple[float, float, float, float] | None,
    reference: tuple[float, float] | None,
) -> list[list[tuple[float, float]]]:
    decoded = decode_fdshape_tracks(fdshape, mbr=mbr, reference=reference)
    # Com sentinelas o arquivo e multi-trilha: emendar tudo numa linha so
    # cruza o talhao entre o fim de uma trilha e o inicio da proxima (leque).
    if len(decoded.tracks) <= 1 and decoded.sentinel_count <= 1:
        points = read_fdshape_points(fdshape, close_ring=False, mbr=mbr, reference=reference)
        return [points] if len(points) >= 2 else []
    if mbr:
        return decoded.tracks
    return [track for track in (sanitize_ring(t, min_points=2) for t in decoded.tracks) if track]


CURVED_TRACK_ROLES = {
    SpatialKind.BORDADURA: "FIELD_HEADLAND_PATH",
    SpatialKind.TRABALHO: "WORK_PATH",
    SpatialKind.CURVA_ADAPTADA: "GUIDANCE_TRACK",
}


def _track_erid(item_erid: str, index: int) -> str:
    if index == 0:
        return item_erid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"fortsmart-gs3-curvetrack|{item_erid}|{index}"))


def _parse_curved_track_feature(
    el: ET.Element,
    base_dir: Path,
    meta: tuple[str, str, str, str],
    catalog_file: str,
) -> list[Gs3SpatialFeature]:
    """Uma feicao por trilha: o CurvedTrackLine pode guardar varias passadas."""
    cliente, fazenda, talhao, field_erid = meta
    name = _clean_label(el.attrib.get("name", ""), "Curva adaptada")
    prefix = el.attrib.get("fileName", "")
    if not prefix:
        return []
    fdshape = base_dir / f"{prefix}.fdShape"
    if not fdshape.exists():
        return []
    tracks = _curved_track_tracks(fdshape, _mbr_from_element(el), _reference_from_element(el))
    item_erid = _norm_guid(el.attrib.get("erid", ""))
    kind = _infer_curved_track_kind(name)
    total = len(tracks)
    features: list[Gs3SpatialFeature] = []
    for index, points in enumerate(tracks):
        feature = Gs3SpatialFeature(
            kind=kind,
            cliente=cliente,
            fazenda=fazenda,
            talhao=talhao,
            field_erid=field_erid,
            item_erid=_track_erid(item_erid, index),
            name=name if total == 1 else f"{name} {index + 1}/{total}",
            source_file=str(fdshape),
            catalog_file=catalog_file,
            geometry="line",
            points=points,
        )
        feature.extra["canonical_role"] = CURVED_TRACK_ROLES[kind]
        if total > 1:
            feature.extra["source_item_erid"] = item_erid
            feature.extra["track_index"] = str(index + 1)
            feature.extra["track_count"] = str(total)
        features.append(feature)
    return features


def index_flag_types(eic_root: Path) -> dict[str, str]:
    """Mapeia o erid de cada NamedFlagType para o nome dado pelo produtor."""
    names: dict[str, str] = {}
    for catalog in eic_root.rglob("*.SpatialCatalog"):
        try:
            root = ET.parse(catalog).getroot()
        except (ET.ParseError, OSError):
            continue
        for el in root.iter():
            if _local(el.tag) != "NamedFlagType":
                continue
            erid = _norm_guid(el.attrib.get("erid", ""))
            name = _clean_label(el.attrib.get("name", ""), "")
            if erid and name:
                names[erid] = name
    return names


# Marcacoes cujo nome indica obstaculo fisico, e nao apenas anotacao.
_OBSTACLE_HINTS = ("poste", "rede", "pedra", "obstac", "torre", "cerca", "bueiro")


def _parse_flag_session_feature(
    el: ET.Element,
    base_dir: Path,
    meta: tuple[str, str, str, str],
    catalog_file: str,
    flag_types: dict[str, str],
) -> Gs3SpatialFeature | None:
    cliente, fazenda, talhao, field_erid = meta
    prefix = el.attrib.get("fileName", "")
    if not prefix:
        return None
    fdshape = base_dir / f"{prefix}.fdShape"
    if not fdshape.exists():
        return None

    flag_mode = (el.attrib.get("flagMode", "") or "").casefold()
    points = read_fdshape_points(
        fdshape,
        close_ring=False,
        mbr=_mbr_from_element(el),
        reference=_reference_from_element(el),
    )
    if not points:
        return None

    type_erid = _norm_guid(el.attrib.get("eridFlagTypeRef", ""))
    type_name = flag_types.get(type_erid, "")
    display_name = _clean_label(el.attrib.get("name", ""), "") or type_name or "Marcacao"

    is_obstacle = any(hint in display_name.casefold() for hint in _OBSTACLE_HINTS)
    if flag_mode == "line" and len(points) >= 2:
        geometry = "line"
        kind = SpatialKind.MARCAÇAO
    else:
        geometry = "point"
        points = points[:1]
        kind = SpatialKind.OBSTACULO_PONTO if is_obstacle else SpatialKind.MARCAÇAO

    extra = {"flag_mode": flag_mode or "point"}
    if type_erid:
        extra["flag_type_erid"] = type_erid
    if type_name:
        extra["flag_type"] = type_name
    session = _norm_guid(el.attrib.get("session", ""))
    if session:
        extra["session_erid"] = session

    return Gs3SpatialFeature(
        kind=kind,
        cliente=cliente,
        fazenda=fazenda,
        talhao=talhao,
        field_erid=field_erid,
        item_erid=_norm_guid(el.attrib.get("erid", "")),
        name=display_name,
        source_file=str(fdshape),
        catalog_file=catalog_file,
        geometry=geometry,
        points=points,
        extra=extra,
    )


def _parse_headland_feature(
    el: ET.Element,
    meta: tuple[str, str, str, str],
    catalog_file: str,
) -> Gs3SpatialFeature | None:
    cliente, fazenda, talhao, field_erid = meta
    offset = ""
    for child in el.iter():
        tag = _local(child.tag)
        if tag in ("vrHeadlandOffset", "HeadlandOffset"):
            offset = child.attrib.get("value", "")
            break
    extra: dict[str, str] = {}
    if offset:
        extra["offset_m"] = offset
    return Gs3SpatialFeature(
        kind=SpatialKind.BORDADURA,
        cliente=cliente,
        fazenda=fazenda,
        talhao=talhao,
        field_erid=field_erid,
        item_erid=_norm_guid(el.attrib.get("erid", "")),
        name=_clean_label(el.attrib.get("name", ""), "Bordadura"),
        source_file=catalog_file,
        catalog_file=catalog_file,
        geometry="polygon",
        points=[],
        extra=extra,
    )


def _resolve_headlands_in_bundle(bundle: Gs3FieldBundle) -> None:
    """Copia geometria do limite para bordadura vinculada (HeadlandLookup)."""
    headlands = {
        f.item_erid: f
        for f in bundle.features
        if f.kind == SpatialKind.BORDADURA and not f.points and not f.rings
    }
    if not headlands:
        return
    for feature in bundle.features:
        href = feature.extra.get("headland_erid", "")
        if not href or href not in headlands:
            continue
        headland = headlands[href]
        if feature.points:
            headland.points = list(feature.points)
            headland.geometry = "polygon"
            headland.extra.setdefault("offset_m", headland.extra.get("offset_m", "0"))
            headland.extra["source_boundary"] = feature.item_erid
        elif feature.rings:
            headland.rings = [list(ring) for ring in feature.rings]
            headland.geometry = "polygon"
            headland.extra.setdefault("offset_m", headland.extra.get("offset_m", "0"))
            headland.extra["source_boundary"] = feature.item_erid


def parse_spatial_catalog_full(
    catalog_path: Path,
    setup: SetupIndex,
    flag_types: dict[str, str] | None = None,
) -> list[Gs3SpatialFeature]:
    flag_types = flag_types or {}
    root = ET.parse(catalog_path).getroot()
    field_erid = _catalog_field_erid(root)
    cliente, fazenda, talhao = resolve_field_metadata(field_erid, setup)
    meta = (cliente, fazenda, talhao, field_erid)
    base_dir = catalog_path.parent
    catalog_file = str(catalog_path)
    features: list[Gs3SpatialFeature] = []

    for el in root.iter():
        tag = _local(el.tag)
        item: Gs3SpatialFeature | None = None
        if tag == "Boundary":
            item = _parse_boundary_feature(el, base_dir, meta, catalog_file)
        elif tag == "ABLine":
            item = _parse_abline_feature(el, meta, catalog_file)
        elif tag == "ABCurve":
            item = _parse_abcurve_feature(el, base_dir, meta, catalog_file)
        elif tag == "CurvedTrackLine":
            features.extend(_parse_curved_track_feature(el, base_dir, meta, catalog_file))
        elif tag == "HeadLand":
            item = _parse_headland_feature(el, meta, catalog_file)
        elif tag == "FlagSessionLayer":
            item = _parse_flag_session_feature(el, base_dir, meta, catalog_file, flag_types)
        if item:
            features.append(item)
    return features


def parse_spatial_catalog(catalog_path: Path, setup: SetupIndex) -> list[Gs3Boundary]:
    records: list[Gs3Boundary] = []
    for feature in parse_spatial_catalog_full(catalog_path, setup):
        if feature.kind not in (SpatialKind.LIMITE, SpatialKind.LIMITE_INTERNO):
            continue
        if len(feature.points) < 4:
            continue
        records.append(
            Gs3Boundary(
                cliente=feature.cliente,
                fazenda=feature.fazenda,
                talhao=feature.talhao,
                field_erid=feature.field_erid,
                boundary_erid=feature.item_erid,
                source_file=feature.source_file,
                points=feature.points,
            )
        )
    return records


def _unify_farm_name(name: str, canonical: dict[str, str]) -> str:
    key = _farm_match_key(name)
    return canonical.get(key, name)


def unify_field_bundles(bundles: list[Gs3FieldBundle]) -> tuple[list[Gs3FieldBundle], int, int]:
    if not bundles:
        return bundles, 0, 0

    before = len({b.fazenda for b in bundles})
    key_names: dict[str, list[str]] = {}
    for bundle in bundles:
        key_names.setdefault(_farm_match_key(bundle.fazenda), []).append(bundle.fazenda)

    canonical: dict[str, str] = {}
    for key, names in key_names.items():
        cleaned = [_clean_label(n) for n in names if _clean_label(n)]
        if not cleaned:
            canonical[key] = "Fazenda"
            continue
        canonical[key] = max(cleaned, key=lambda n: (names.count(n), len(n)))

    placeholder_keys = {_farm_match_key("---"), _farm_match_key("Fazenda"), "semfazenda"}

    def _client_key(name: str) -> str:
        text = unicodedata.normalize("NFKD", _clean_label(name, "cliente"))
        return "".join(ch for ch in text.casefold() if ch.isalnum()) or "cliente"

    client_counts: dict[str, dict[str, int]] = {}
    for bundle in bundles:
        farm_key = _farm_match_key(bundle.fazenda)
        if farm_key in placeholder_keys:
            continue
        client_counts.setdefault(_client_key(bundle.cliente), {})
        client_counts[_client_key(bundle.cliente)][bundle.fazenda] = (
            client_counts[_client_key(bundle.cliente)].get(bundle.fazenda, 0) + 1
        )
    dominant_by_client = {
        ck: max(farms.items(), key=lambda item: item[1])[0] for ck, farms in client_counts.items()
    }

    counts = {name: sum(1 for b in bundles if b.fazenda == name) for name in {b.fazenda for b in bundles}}
    main_farm = max(counts.items(), key=lambda item: item[1])[0] if counts else ""

    unified: list[Gs3FieldBundle] = []
    for bundle in bundles:
        fazenda = _unify_farm_name(bundle.fazenda, canonical)
        if _farm_match_key(fazenda) in placeholder_keys:
            fazenda = dominant_by_client.get(_client_key(bundle.cliente), fazenda)
        if counts.get(main_farm, 0) >= max(1, int(len(bundles) * 0.5)):
            if _farm_match_key(fazenda) in placeholder_keys:
                fazenda = main_farm

        features = []
        for feature in bundle.features:
            feat_fazenda = _unify_farm_name(feature.fazenda, canonical)
            if _farm_match_key(feat_fazenda) in placeholder_keys:
                feat_fazenda = fazenda
            features.append(
                Gs3SpatialFeature(
                    kind=feature.kind,
                    cliente=feature.cliente,
                    fazenda=feat_fazenda,
                    talhao=feature.talhao,
                    field_erid=feature.field_erid,
                    item_erid=feature.item_erid,
                    name=feature.name,
                    source_file=feature.source_file,
                    catalog_file=feature.catalog_file,
                    geometry=feature.geometry,
                    points=list(feature.points),
                    rings=[list(ring) for ring in feature.rings],
                    active=feature.active,
                    extra=dict(feature.extra),
                    format_source=feature.format_source,
                )
            )
        unified.append(
            Gs3FieldBundle(
                cliente=bundle.cliente,
                fazenda=fazenda,
                talhao=bundle.talhao,
                field_erid=bundle.field_erid,
                features=features,
            )
        )

    after = len({b.fazenda for b in unified})
    return unified, before, after


def collect_gs3_fields(input_path: Path, unify_farms: bool = True) -> Gs3FieldImportResult:
    input_path = Path(input_path)
    eic_root = find_eic_root(input_path)
    setup = parse_setup_index(eic_root / "setup.fds")

    catalog_patterns = ("*.SpatialCatalog",)
    catalogs: list[Path] = []
    for pattern in catalog_patterns:
        catalogs.extend(sorted(eic_root.rglob(pattern)))
    # Prefer ImportExport / ABCurves; evitar duplicata vazia de WaterManagement
    seen_field_cat: set[tuple[str, str]] = set()
    unique_catalogs: list[Path] = []
    for catalog in catalogs:
        key = (str(catalog.parent), catalog.name)
        if key in seen_field_cat:
            continue
        seen_field_cat.add(key)
        unique_catalogs.append(catalog)
    catalogs = unique_catalogs

    by_field: dict[str, list[Gs3SpatialFeature]] = defaultdict(list)
    meta_cache: dict[str, tuple[str, str, str]] = {}
    flag_types = index_flag_types(eic_root)
    # O mesmo objeto e declarado em varios catalogos (ImportExport repete os
    # itens da sessao). O erid e a identidade real, entao lemos so uma vez.
    seen_items: set[tuple[str, str]] = set()

    for catalog in catalogs:
        for feature in parse_spatial_catalog_full(catalog, setup, flag_types):
            if not feature.field_erid:
                continue
            identity = (feature.field_erid, feature.item_erid or feature.source_file)
            if identity in seen_items:
                continue
            seen_items.add(identity)
            by_field[feature.field_erid].append(feature)
            meta_cache[feature.field_erid] = (feature.cliente, feature.fazenda, feature.talhao)

    bundles: list[Gs3FieldBundle] = []
    for field_erid, setup_meta in setup.fields.items():
        cliente, fazenda, talhao = resolve_field_metadata(field_erid, setup)
        features = by_field.get(field_erid, [])
        if not features and field_erid not in by_field:
            bundles.append(
                Gs3FieldBundle(
                    cliente=cliente,
                    fazenda=fazenda,
                    talhao=talhao,
                    field_erid=field_erid,
                    features=[],
                )
            )
            continue
        if features:
            cliente, fazenda, talhao = meta_cache.get(field_erid, (cliente, fazenda, talhao))
        bundles.append(
            Gs3FieldBundle(
                cliente=cliente,
                fazenda=fazenda,
                talhao=talhao,
                field_erid=field_erid,
                features=features,
            )
        )

    # Talhoes presentes nos catalogos mas ausentes no setup.fds
    for field_erid, features in by_field.items():
        if field_erid in setup.fields:
            continue
        cliente, fazenda, talhao = meta_cache.get(field_erid, resolve_field_metadata(field_erid, setup))
        bundles.append(
            Gs3FieldBundle(
                cliente=cliente,
                fazenda=fazenda,
                talhao=talhao,
                field_erid=field_erid,
                features=features,
            )
        )

    for bundle in bundles:
        _resolve_headlands_in_bundle(bundle)

    bundles = [b for b in bundles if b.features]
    bundles.sort(key=lambda b: (b.fazenda.casefold(), b.talhao.casefold()))

    farms_before = len({b.fazenda for b in bundles})
    farms_after = farms_before
    if unify_farms:
        bundles, farms_before, farms_after = unify_field_bundles(bundles)

    return Gs3FieldImportResult(
        eic_root=str(eic_root),
        catalog_count=len(catalogs),
        field_count=len(setup.fields),
        bundles=bundles,
        farms_before_merge=farms_before,
        farms_after_merge=farms_after,
    )


def unify_duplicate_farms(boundaries: list[Gs3Boundary]) -> tuple[list[Gs3Boundary], int, int]:
    if not boundaries:
        return boundaries, 0, 0

    before = len({b.fazenda for b in boundaries})

    # Nome canonico por chave normalizada (Faz. Ferradura == FAZ FERRADURA).
    key_names: dict[str, list[str]] = {}
    for boundary in boundaries:
        key_names.setdefault(_farm_match_key(boundary.fazenda), []).append(boundary.fazenda)

    canonical: dict[str, str] = {}
    for key, names in key_names.items():
        cleaned = [_clean_label(n) for n in names if _clean_label(n)]
        if not cleaned:
            canonical[key] = "Fazenda"
            continue
        canonical[key] = max(cleaned, key=lambda n: (names.count(n), len(n)))

    step1: list[Gs3Boundary] = []
    for boundary in boundaries:
        key = _farm_match_key(boundary.fazenda)
        step1.append(
            Gs3Boundary(
                cliente=boundary.cliente,
                fazenda=canonical.get(key, boundary.fazenda),
                talhao=boundary.talhao,
                field_erid=boundary.field_erid,
                boundary_erid=boundary.boundary_erid,
                source_file=boundary.source_file,
                points=boundary.points,
            )
        )

    # Talhoes em fazenda placeholder (---) -> fazenda principal do mesmo cliente.
    placeholder_keys = {_farm_match_key("---"), _farm_match_key("Fazenda"), "semfazenda"}

    def _client_key(name: str) -> str:
        text = unicodedata.normalize("NFKD", _clean_label(name, "cliente"))
        return "".join(ch for ch in text.casefold() if ch.isalnum()) or "cliente"

    dominant_by_client: dict[str, str] = {}
    client_counts: dict[str, dict[str, int]] = {}
    for boundary in step1:
        client_key = _client_key(boundary.cliente)
        farm_key = _farm_match_key(boundary.fazenda)
        if farm_key in placeholder_keys:
            continue
        client_counts.setdefault(client_key, {})
        client_counts[client_key][boundary.fazenda] = client_counts[client_key].get(boundary.fazenda, 0) + 1

    for client_key, farms in client_counts.items():
        dominant_by_client[client_key] = max(farms.items(), key=lambda item: item[1])[0]

    unified: list[Gs3Boundary] = []
    for boundary in step1:
        farm_key = _farm_match_key(boundary.fazenda)
        fazenda = boundary.fazenda
        if farm_key in placeholder_keys:
            fazenda = dominant_by_client.get(_client_key(boundary.cliente), boundary.fazenda)
        unified.append(
            Gs3Boundary(
                cliente=boundary.cliente,
                fazenda=fazenda,
                talhao=boundary.talhao,
                field_erid=boundary.field_erid,
                boundary_erid=boundary.boundary_erid,
                source_file=boundary.source_file,
                points=boundary.points,
            )
        )

    # Se ainda restar placeholder, joga na fazenda majoritaria geral (caso comum: um unico cliente real).
    counts = {name: sum(1 for b in unified if b.fazenda == name) for name in {b.fazenda for b in unified}}
    if counts:
        main_farm = max(counts.items(), key=lambda item: item[1])[0]
        if counts[main_farm] >= max(1, int(len(unified) * 0.5)):
            fixed: list[Gs3Boundary] = []
            for boundary in unified:
                farm_key = _farm_match_key(boundary.fazenda)
                fazenda = main_farm if farm_key in placeholder_keys else boundary.fazenda
                fixed.append(
                    Gs3Boundary(
                        cliente=boundary.cliente,
                        fazenda=fazenda,
                        talhao=boundary.talhao,
                        field_erid=boundary.field_erid,
                        boundary_erid=boundary.boundary_erid,
                        source_file=boundary.source_file,
                        points=boundary.points,
                    )
                )
            unified = fixed

    after = len({b.fazenda for b in unified})
    return unified, before, after


def find_eic_root(input_path: Path) -> Path:
    direct = input_path / "JD4600" / "RCD" / "EIC"
    if (direct / "setup.fds").exists():
        return direct
    matches = list(input_path.rglob("setup.fds"))
    if not matches:
        raise FileNotFoundError(
            f"Nao encontrei setup.fds em {input_path}.\n"
            "Selecione a pasta GS3_2630 (raiz do pendrive exportado do monitor)."
        )
    return matches[0].parent


def collect_gs3_boundaries(input_path: Path, unify_farms: bool = True) -> Gs3ImportResult:
    input_path = Path(input_path)
    eic_root = find_eic_root(input_path)
    setup = parse_setup_index(eic_root / "setup.fds")
    catalogs = sorted(eic_root.rglob("ImportExport.SpatialCatalog"))

    boundaries: list[Gs3Boundary] = []
    for catalog in catalogs:
        boundaries.extend(parse_spatial_catalog(catalog, setup))

    farms_before = len({b.fazenda for b in boundaries})
    farms_after = farms_before
    if unify_farms:
        boundaries, farms_before, farms_after = unify_duplicate_farms(boundaries)

    return Gs3ImportResult(
        eic_root=eic_root,
        catalog_count=len(catalogs),
        field_count=len(setup.fields),
        boundaries=boundaries,
        farms_before_merge=farms_before,
        farms_after_merge=farms_after,
    )
