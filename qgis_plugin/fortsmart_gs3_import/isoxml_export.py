"""FortSmart — escritor ISOXML (ISO 11783-10) a partir de FortSmartFieldBundle."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .export_collector import collect_bundles_from_project  # noqa: F401 — reexport API
from .gs3_spatial_types import Gs3FieldBundle, SpatialKind
from .isoxml_linklist import LinkListBuilder

ISO_NS = "http://www.isobus.net/isobus/iso11783/10/2015"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sub(parent: ET.Element, tag: str, attrib: dict | None = None) -> ET.Element:
    return ET.SubElement(parent, tag, attrib or {})


def _write_pnt(parent: ET.Element, idx: int, lon: float, lat: float, ptype: str = "2") -> None:
    _sub(parent, "PNT", {"A": ptype, "B": str(idx), "C": f"{lat:.9f}", "D": f"{lon:.9f}"})


def _write_lsg_line(parent: ET.Element, points: list[tuple[float, float]], lsg_id: int, name: str = "") -> None:
    lsg = _sub(parent, "LSG", {"A": "5", "B": name, "C": str(lsg_id)})
    for i, (lon, lat) in enumerate(points, start=1):
        _write_pnt(lsg, i, lon, lat)


def _write_pln_polygon(parent: ET.Element, rings: list[list[tuple[float, float]]], pln_id: int, pln_type: str, name: str) -> None:
    pln = _sub(parent, "PLN", {"A": pln_type, "B": name, "C": "0", "I": f"PLN{pln_id:04d}"})
    for ring_idx, ring in enumerate(rings, start=1):
        lsg_type = "1" if ring_idx == 1 else "2"
        lsg = _sub(pln, "LSG", {"A": lsg_type, "B": str(ring_idx), "C": str(pln_id * 10 + ring_idx)})
        for i, (lon, lat) in enumerate(ring, start=1):
            _write_pnt(lsg, i, lon, lat)


def export_isoxml_taskdata(
    bundles: list[Gs3FieldBundle],
    output_root: Path,
    *,
    version_major: str = "4",
    version_minor: str = "3",
    software_version: str = "2.2.0",
) -> Path:
    if not bundles:
        raise ValueError("Nenhum talhao para exportar ISOXML.")

    taskdata_dir = output_root / "TASKDATA"
    taskdata_dir.mkdir(parents=True, exist_ok=True)
    out_file = taskdata_dir / "TASKDATA.XML"
    use_linklist = int(version_major) >= 4
    linklist = LinkListBuilder(software_version=software_version) if use_linklist else None

    root = ET.Element(
        "ISO11783_TaskData",
        {
            "VersionMajor": version_major,
            "VersionMinor": version_minor,
            "ManagementSoftwareManufacturer": "FortSmart",
            "ManagementSoftwareVersion": software_version,
            "DataTransferOrigin": "1",
        },
    )

    if use_linklist:
        _sub(root, "AFE", {"A": "LINKLIST.XML", "B": "1", "C": "", "D": "1"})

    clients: dict[str, str] = {}
    farms: dict[str, str] = {}
    for bundle in bundles:
        ck = bundle.cliente or "Cliente"
        fk = f"{ck}::{bundle.fazenda or 'Fazenda'}"
        clients.setdefault(ck, ck)
        farms.setdefault(fk, bundle.fazenda or "Fazenda")

    ctr_id = 1
    frm_id = 1
    ctr_map: dict[str, str] = {}
    frm_map: dict[str, str] = {}

    for cname in sorted(clients.keys()):
        cid = f"CTR{ctr_id:04d}"
        ctr_map[cname] = cid
        _sub(root, "CTR", {"A": cname, "B": cname, "I": cid})
        if linklist:
            linklist.add(cid, cname)
        ctr_id += 1

    for fkey in sorted(farms.keys()):
        cname, fname = fkey.split("::", 1)
        fid = f"FRM{frm_id:04d}"
        frm_map[fkey] = fid
        _sub(root, "FRM", {"A": fname, "B": fname, "C": ctr_map.get(cname, "CTR0001"), "I": fid})
        if linklist:
            linklist.add(fid, fname)
        frm_id += 1

    pfd_id = 1
    pln_id = 1
    ggp_id = 1
    gpn_id = 1

    for bundle in bundles:
        fkey = f"{bundle.cliente or 'Cliente'}::{bundle.fazenda or 'Fazenda'}"
        pfd_i = f"PFD{pfd_id:04d}"
        pfd = _sub(
            root,
            "PFD",
            {
                "A": str(pfd_id),
                "C": bundle.talhao,
                "D": "0",
                "E": ctr_map.get(bundle.cliente, "CTR0001"),
                "F": frm_map.get(fkey, "FRM0001"),
                "I": pfd_i,
            },
        )
        if linklist:
            linklist.add(pfd_i, bundle.talhao)

        for feature in bundle.features:
            if feature.kind in (SpatialKind.LIMITE, SpatialKind.LIMITE_INTERNO):
                rings = feature.rings or ([feature.points] if feature.points else [])
                if not rings:
                    continue
                pln_i = f"PLN{pln_id:04d}"
                _write_pln_polygon(pfd, rings, pln_id, "1", feature.name or bundle.talhao)
                if linklist:
                    linklist.add(pln_i, feature.name or bundle.talhao)
                pln_id += 1
            elif feature.kind == SpatialKind.BORDADURA and feature.rings:
                pln_i = f"PLN{pln_id:04d}"
                _write_pln_polygon(pfd, feature.rings, pln_id, "2", feature.name or "Bordadura")
                if linklist:
                    linklist.add(pln_i, feature.name or "Bordadura")
                pln_id += 1
            elif feature.kind == SpatialKind.BORDADURA and len(feature.points) >= 2:
                ggp_i = f"GGP{ggp_id:04d}"
                gpn_i = f"GPN{gpn_id:04d}"
                name = feature.name or "Bordadura"
                ggp = _sub(pfd, "GGP", {"A": str(ggp_id), "B": name, "G": ggp_i})
                gpn = _sub(ggp, "GPN", {"A": str(gpn_id), "B": name, "C": "3", "G": gpn_i})
                _write_lsg_line(gpn, feature.points, gpn_id, name)
                if linklist:
                    linklist.add(ggp_i, name)
                    linklist.add(gpn_i, name)
                ggp_id += 1
                gpn_id += 1
            elif feature.kind == SpatialKind.OBSTACULO_AREA:
                rings = feature.rings or ([feature.points] if feature.points else [])
                if rings:
                    pln_type = "8" if feature.extra.get("befahrbar") == "sim" else "6"
                    pln_i = f"PLN{pln_id:04d}"
                    _write_pln_polygon(pfd, rings, pln_id, pln_type, feature.name or "Obstaculo")
                    if linklist:
                        linklist.add(pln_i, feature.name or "Obstaculo")
                    pln_id += 1
            elif feature.kind in (SpatialKind.OBSTACULO_PONTO, SpatialKind.MARCAÇAO) and feature.points:
                lon, lat = feature.points[0]
                ptype = "5" if feature.kind == SpatialKind.OBSTACULO_PONTO else "7"
                _sub(pfd, "PNT", {"A": ptype, "B": feature.name, "C": f"{lat:.9f}", "D": f"{lon:.9f}"})
            elif feature.kind in (
                SpatialKind.LINHA_AB,
                SpatialKind.CURVA_AB,
                SpatialKind.CURVA_ADAPTADA,
                SpatialKind.TRABALHO,
            ) and len(feature.points) >= 2:
                ggp_i = f"GGP{ggp_id:04d}"
                gpn_i = f"GPN{gpn_id:04d}"
                ggp = _sub(pfd, "GGP", {"A": str(ggp_id), "B": feature.name or "Guia", "G": ggp_i})
                gpn_type = "1" if feature.kind == SpatialKind.LINHA_AB and len(feature.points) == 2 else "3"
                gpn = _sub(ggp, "GPN", {"A": str(gpn_id), "B": feature.name or "Guia", "C": gpn_type, "G": gpn_i})
                _write_lsg_line(gpn, feature.points, gpn_id, feature.name or "Guia")
                if linklist:
                    linklist.add(ggp_i, feature.name or "Guia")
                    linklist.add(gpn_i, feature.name or "Guia")
                ggp_id += 1
                gpn_id += 1

        pfd_id += 1

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(out_file, encoding="utf-8", xml_declaration=True)
    if linklist and linklist.entries:
        linklist.write(taskdata_dir / "LINKLIST.XML")
    return taskdata_dir
