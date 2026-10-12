"""FortSmart — importacao ISOXML (ISO 11783-10) organizada por talhao."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .gs3_rcd_core import unify_field_bundles
from .gs3_spatial_types import (
    Gs3FieldBundle,
    Gs3FieldImportResult,
    Gs3SpatialFeature,
    SpatialKind,
)

ISO_NS = "http://www.isobus.net/isobus/iso11783/10/2015"


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _clean(value: str | None, fallback: str = "") -> str:
    text = " ".join((value or "").split())
    return text if text else fallback


def _is_nullish(value: str | None) -> bool:
    return value is None or str(value).strip() in ("", "None", "null")


def _pnt_lonlat(pnt: ET.Element) -> tuple[float, float] | None:
    """ISOXML PNT: latitude=C, longitude=D; A=2 ou 10 para vertices."""
    a = pnt.get("A", "")
    if a not in ("2", "10", "7", "9"):
        return None
    try:
        lat = float(pnt.get("C", "nan"))
        lon = float(pnt.get("D", "nan"))
    except ValueError:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    if abs(lat) < 1e-12 and abs(lon) < 1e-12:
        return None
    return lon, lat


def _lsg_ring(lsg: ET.Element) -> list[tuple[float, float]]:
    ring: list[tuple[float, float]] = []
    for pnt in lsg.findall("PNT"):
        pt = _pnt_lonlat(pnt)
        if pt:
            ring.append(pt)
    return ring


def _infer_line_kind(name: str, point_count: int, gpn_type: str = "") -> SpatialKind:
    text = _clean(name, "").casefold()
    if gpn_type == "1" or (point_count == 2 and gpn_type != "3"):
        return SpatialKind.LINHA_AB
    if gpn_type == "3" or point_count > 2:
        if any(k in text for k in ("bordad", "headland", "contorno")):
            return SpatialKind.BORDADURA
        if any(k in text for k in ("trabalho", "plantadeira", "aplic", "pulv", "operac", "swath", "faixa")):
            return SpatialKind.TRABALHO
        return SpatialKind.CURVA_AB
    if any(k in text for k in ("bordad", "headland")):
        return SpatialKind.BORDADURA
    if any(k in text for k in ("trabalho", "plant", "aplic")):
        return SpatialKind.TRABALHO
    return SpatialKind.CURVA_ADAPTADA


def _pln_boundary_rings(pln: ET.Element) -> list[list[tuple[float, float]]]:
    """Extrai poligonos (anel externo + buracos) de PLN tipo 1."""
    polygons: list[list[list[tuple[float, float]]]] = []
    current: list[list[tuple[float, float]]] = []

    def flush() -> None:
        nonlocal current
        if current and len(current[0]) >= 3:
            polygons.append(current)
        current = []

    for lsg in pln.findall("LSG"):
        a_val = lsg.get("A", "")
        ring = _lsg_ring(lsg)
        if len(ring) < 3:
            continue
        if a_val == "1":
            flush()
            current = [ring]
        elif a_val == "2" and current and lsg.get("P094_Impassable") is None:
            current.append(ring)

    flush()
    return [poly for poly in polygons if poly]


def _pln_area_rings(pln: ET.Element) -> list[list[tuple[float, float]]]:
    current: list[list[tuple[float, float]]] = []
    result: list[list[tuple[float, float]]] = []

    def flush() -> None:
        nonlocal current
        if current and len(current[0]) >= 3:
            result.append(current)
        current = []

    for lsg in pln.findall("LSG"):
        a_val = lsg.get("A", "")
        ring = _lsg_ring(lsg)
        if len(ring) < 3:
            continue
        if a_val == "1":
            flush()
            current = [ring]
        elif a_val == "2" and current:
            current.append(ring)
    flush()
    return result


@dataclass
class _IsoHierarchy:
    clients: dict[str, str]
    farms: dict[str, str]
    farm_client: dict[str, str]


def _build_hierarchy(root: ET.Element) -> _IsoHierarchy:
    clients: dict[str, str] = {}
    farms: dict[str, str] = {}
    farm_client: dict[str, str] = {}

    for el in root.iter():
        tag = _local(el.tag)
        i = el.get("I", "")
        if not i:
            continue
        if tag == "CTR":
            clients[i] = _clean(el.get("A") or el.get("B"), f"Cliente {i}")
        elif tag == "FRM":
            farms[i] = _clean(el.get("A") or el.get("B"), f"Fazenda {i}")
            farm_client[i] = el.get("C") or el.get("E") or ""

    return _IsoHierarchy(clients, farms, farm_client)


def _resolve_names(pfd: ET.Element, hierarchy: _IsoHierarchy) -> tuple[str, str, str]:
    field_name = _clean(pfd.get("C") or pfd.get("B"), "Talhao")
    frm_ref = pfd.get("F") or pfd.get("FRMIdRef") or ""
    ctr_ref = pfd.get("E") or pfd.get("CTRIdRef") or hierarchy.farm_client.get(frm_ref, "")

    fazenda = _clean(hierarchy.farms.get(frm_ref, ""), "Fazenda")
    cliente = _clean(hierarchy.clients.get(ctr_ref, ""), "Cliente")
    return cliente, fazenda, field_name


def _make_feature(
    *,
    kind: SpatialKind,
    cliente: str,
    fazenda: str,
    talhao: str,
    field_id: str,
    item_id: str,
    name: str,
    geometry: str,
    source: str,
    points: list[tuple[float, float]] | None = None,
    rings: list[list[tuple[float, float]]] | None = None,
    extra: dict[str, str] | None = None,
) -> Gs3SpatialFeature:
    return Gs3SpatialFeature(
        kind=kind,
        cliente=cliente,
        fazenda=fazenda,
        talhao=talhao,
        field_erid=field_id,
        item_erid=item_id,
        name=name,
        source_file=source,
        catalog_file=source,
        geometry=geometry,
        points=list(points or []),
        rings=[list(r) for r in (rings or [])],
        format_source="isoxml",
        extra=extra or {},
    )


def _parse_pfd(pfd: ET.Element, hierarchy: _IsoHierarchy, source: str, is_v3: bool) -> Gs3FieldBundle:
    field_id = pfd.get("I") or pfd.get("A") or "0"
    cliente, fazenda, talhao = _resolve_names(pfd, hierarchy)
    features: list[Gs3SpatialFeature] = []

    # --- Limites (PLN A=1) ---
    for idx, pln in enumerate(pfd.findall("PLN")):
        if pln.get("A", "1") != "1":
            continue
        bname = _clean(pln.get("B"), talhao)
        for poly_idx, poly_rings in enumerate(_pln_boundary_rings(pln)):
            if not poly_rings:
                continue
            kind = SpatialKind.LIMITE_INTERNO if poly_idx > 0 or "intern" in bname.casefold() else SpatialKind.LIMITE
            features.append(
                _make_feature(
                    kind=kind,
                    cliente=cliente,
                    fazenda=fazenda,
                    talhao=talhao,
                    field_id=str(field_id),
                    item_id=f"{field_id}-pln-{idx}-{poly_idx}",
                    name=bname,
                    geometry="polygon",
                    source=source,
                    rings=poly_rings,
                    extra={"pln_type": "1"},
                )
            )

    # --- Zona de tratamento / bordadura (PLN A=2) ---
    for idx, pln in enumerate(pfd.findall("PLN")):
        if pln.get("A") != "2":
            continue
        tname = _clean(pln.get("B"), "Bordadura")
        for poly_idx, poly_rings in enumerate(_pln_boundary_rings(pln)):
            features.append(
                _make_feature(
                    kind=SpatialKind.BORDADURA,
                    cliente=cliente,
                    fazenda=fazenda,
                    talhao=talhao,
                    field_id=str(field_id),
                    item_id=f"{field_id}-trt-{idx}-{poly_idx}",
                    name=tname,
                    geometry="polygon",
                    source=source,
                    rings=poly_rings,
                    extra={"pln_type": "2"},
                )
            )

    # --- Obstaculos de area (PLN A=6 nao bef., A=8 bef.) ---
    for pln in pfd.findall("PLN"):
        pln_type = pln.get("A", "")
        if pln_type not in ("6", "8"):
            continue
        passable = pln_type == "8"
        for poly_idx, poly_rings in enumerate(_pln_area_rings(pln)):
            features.append(
                _make_feature(
                    kind=SpatialKind.OBSTACULO_AREA,
                    cliente=cliente,
                    fazenda=fazenda,
                    talhao=talhao,
                    field_id=str(field_id),
                    item_id=f"{field_id}-obs-a-{pln.get('I', poly_idx)}",
                    name=_clean(pln.get("B"), "Obstaculo area"),
                    geometry="polygon",
                    source=source,
                    rings=poly_rings,
                    extra={"befahrbar": "sim" if passable else "nao", "pln_type": pln_type},
                )
            )

    # --- Legacy obstaculos em LSG com P094_Impassable ---
    first_pln = None
    for pln in pfd.findall("PLN"):
        if pln.get("A", "1") == "1":
            first_pln = pln
            break
    if first_pln is None:
        first_pln = pfd.find("PLN")
    if first_pln is not None:
        for lsg in first_pln.findall("LSG"):
            if lsg.get("A") != "2" or lsg.get("P094_Impassable") is None:
                continue
            ring = _lsg_ring(lsg)
            if len(ring) >= 3:
                impass = lsg.get("P094_Impassable", "0")
                features.append(
                    _make_feature(
                        kind=SpatialKind.OBSTACULO_AREA,
                        cliente=cliente,
                        fazenda=fazenda,
                        talhao=talhao,
                        field_id=str(field_id),
                        item_id=f"{field_id}-obs-legacy",
                        name="Obstaculo (legacy)",
                        geometry="polygon",
                        source=source,
                        rings=[[ring]],
                        extra={"befahrbar": "nao" if impass != "0" else "sim"},
                    )
                )

    # --- Pontos: obstaculos, marcacoes, referencia ---
    for pnt in pfd.findall("PNT"):
        a_attr = pnt.get("A", "")
        pt = _pnt_lonlat(pnt) or (
            (float(pnt.get("D", "0")), float(pnt.get("C", "0")))
            if pnt.get("C") and pnt.get("D")
            else None
        )
        if not pt or pt == (0.0, 0.0):
            continue
        pname = _clean(pnt.get("B"), "Ponto")
        if a_attr in ("1", "2", "5", "7", "9"):
            if a_attr == "7":
                kind = SpatialKind.MARCAÇAO
            elif a_attr == "5" and not is_v3:
                kind = SpatialKind.OBSTACULO_PONTO
            elif a_attr == "2" and is_v3:
                kind = SpatialKind.OBSTACULO_PONTO
            elif a_attr == "1":
                kind = SpatialKind.MARCAÇAO
            else:
                kind = SpatialKind.OBSTACULO_PONTO
            passable = "nao" if kind == SpatialKind.OBSTACULO_PONTO and a_attr in ("2", "5") else "sim"
            features.append(
                _make_feature(
                    kind=kind,
                    cliente=cliente,
                    fazenda=fazenda,
                    talhao=talhao,
                    field_id=str(field_id),
                    item_id=f"{field_id}-pnt-{pnt.get('I', a_attr)}",
                    name=pname,
                    geometry="point",
                    source=source,
                    points=[pt],
                    extra={"pnt_type": a_attr, "befahrbar": passable},
                )
            )

    # --- Linhas diretas LSG A=5 (faixas / guias) ---
    for lsg in pfd.findall("LSG"):
        if lsg.get("A") != "5":
            continue
        line = _lsg_ring(lsg)
        if len(line) < 2:
            continue
        lname = _clean(lsg.get("B"), "Faixa")
        features.append(
            _make_feature(
                kind=_infer_line_kind(lname, len(line)),
                cliente=cliente,
                fazenda=fazenda,
                talhao=talhao,
                field_id=str(field_id),
                item_id=f"{field_id}-lsg-{lsg.get('I', '5')}",
                name=lname,
                geometry="line",
                source=source,
                points=line,
                extra={"lsg_type": "5"},
            )
        )

    # --- Padroes de guia GGP/GPN (AB reta vs curva) ---
    for ggp in pfd.findall("GGP"):
        ggp_name = _clean(ggp.get("B"), "")
        for gpn in ggp.findall("GPN"):
            lsg = None
            for child in gpn:
                if _local(child.tag) == "LSG" and child.get("A") == "5":
                    lsg = child
                    break
            if lsg is None:
                continue
            line = _lsg_ring(lsg)
            if len(line) < 2:
                continue
            gpn_type = gpn.get("C", "")
            track_name = _clean(gpn.get("B") or ggp_name, "Guia")
            kind = _infer_line_kind(track_name, len(line), gpn_type)
            features.append(
                _make_feature(
                    kind=kind,
                    cliente=cliente,
                    fazenda=fazenda,
                    talhao=talhao,
                    field_id=str(field_id),
                    item_id=f"{field_id}-gpn-{gpn.get('I', track_name)}",
                    name=track_name,
                    geometry="line",
                    source=source,
                    points=line,
                    extra={
                        "gpn_type": gpn_type or ("1" if len(line) == 2 else "3"),
                        "segment": _clean(ggp.get("B"), ""),
                    },
                )
            )

    return Gs3FieldBundle(
        cliente=cliente,
        fazenda=fazenda,
        talhao=talhao,
        field_erid=str(field_id),
        features=features,
    )


def find_taskdata_xml(input_path: Path) -> Path:
    input_path = Path(input_path)
    if input_path.is_file() and input_path.name.lower() in ("taskdata.xml", "taskdata.xml".lower()):
        return input_path
    candidates = [
        input_path / "TASKDATA" / "TASKDATA.XML",
        input_path / "TASKDATA" / "TASKDATA.xml",
        input_path / "TASKDATA.XML",
        input_path / "TASKDATA.xml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    matches = list(input_path.rglob("TASKDATA.XML")) + list(input_path.rglob("TASKDATA.xml"))
    if not matches:
        raise FileNotFoundError(
            f"Nao encontrei TASKDATA.XML em {input_path}.\n"
            "Selecione a pasta TASKDATA ou o arquivo TASKDATA.XML."
        )
    return matches[0]


def _load_taskdata_root(taskdata_path: Path) -> ET.Element:
    tree = ET.parse(taskdata_path)
    root = tree.getroot()
    if _local(root.tag) != "ISO11783_TaskData":
        raise ValueError(f"Arquivo invalido (raiz esperada ISO11783_TaskData): {taskdata_path}")
    return root


def collect_isoxml_fields(input_path: Path, unify_farms: bool = True) -> Gs3FieldImportResult:
    taskdata_path = find_taskdata_xml(input_path)
    root = _load_taskdata_root(taskdata_path)
    source = str(taskdata_path)

    version_major = root.get("VersionMajor", "4")
    is_v3 = version_major == "3"
    hierarchy = _build_hierarchy(root)

    bundles: list[Gs3FieldBundle] = []
    pfd_count = 0
    for pfd in root.iter():
        if _local(pfd.tag) != "PFD":
            continue
        pfd_count += 1
        bundle = _parse_pfd(pfd, hierarchy, source, is_v3)
        if bundle.features:
            bundles.append(bundle)

    bundles.sort(key=lambda b: (b.fazenda.casefold(), b.talhao.casefold()))
    farms_before = len({b.fazenda for b in bundles})
    farms_after = farms_before
    if unify_farms:
        bundles, farms_before, farms_after = unify_field_bundles(bundles)

    return Gs3FieldImportResult(
        eic_root=str(taskdata_path.parent),
        catalog_count=1,
        field_count=pfd_count,
        bundles=bundles,
        farms_before_merge=farms_before,
        farms_after_merge=farms_after,
        format_source="isoxml",
        iso_version=f"{version_major}.{root.get('VersionMinor', '0')}",
    )


def detect_import_format(input_path: Path) -> str:
    path = Path(input_path)
    if not path.exists():
        return "unknown"
    from .gs3_rcd_core import find_eic_root
    from .gen4_import import detect_gen4
    from .jd_data_import import detect_jd_data
    from .jacto_import import detect_jacto
    from .stara_import import detect_stara

    try:
        find_eic_root(path)
        return "gs3"
    except FileNotFoundError:
        pass
    if detect_gen4(path):
        return "gen4"
    if detect_jd_data(path):
        return "jd_data"
    try:
        find_taskdata_xml(path)
        return "isoxml"
    except FileNotFoundError:
        pass
    if detect_jacto(path):
        return "jacto"
    if detect_stara(path):
        return "stara"
    return "unknown"
