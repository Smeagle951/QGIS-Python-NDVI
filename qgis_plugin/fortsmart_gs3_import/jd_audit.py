"""Auditoria da leitura John Deere GS3: catalogo, decodificacao e relatorio.

Percorre o diretorio ``EIC`` original, decodifica cada ``.fdShape`` e mede a
integridade sem depender de nenhuma fonte externa: o proprio ``MBR`` gravado no
``SpatialCatalog`` e usado como referencia independente das coordenadas.

Nao altera nenhum arquivo de origem.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .jd_fdshape import MODE_DELTA, decode_fdshape

# Todo o conteudo espacial do RCD e gravado em graus decimais (uomSource="arcdeg")
# sobre WGS84. Nao ha reprojecao envolvida na leitura.
JD_CRS = "EPSG:4326"

# Tipos de objeto do SpatialCatalog que o leitor sabe interpretar.
KNOWN_ITEM_TAGS = {
    "Boundary",
    "CurvedTrackLine",
    "FlagSessionLayer",
    "ABLine",
    "AdaptiveCurve",
    "Headland",
}


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _norm_guid(value: str) -> str:
    return (value or "").strip().strip("{}").lower()


@dataclass
class CatalogItem:
    """Um objeto espacial declarado no SpatialCatalog."""

    tag: str
    erid: str
    name: str
    file_name: str
    field_erid: str
    catalog_file: str
    geometry_type: str = ""
    mbr: tuple[float, float, float, float] | None = None
    reference: tuple[float, float] | None = None
    east_shift_mm: float = 0.0
    north_shift_mm: float = 0.0
    extra_refs: dict[str, str] = field(default_factory=dict)


@dataclass
class ObjectAudit:
    erid: str
    tag: str
    name: str
    file_name: str
    field_erid: str
    mode: str
    point_count: int
    record_count: int
    mbr_match: bool
    status: str
    note: str = ""


@dataclass
class AuditReport:
    generated_at: str
    eic_root: str
    crs: str
    catalog_files: int
    objects_declared: int
    objects_decoded: int
    objects_failed: int
    objects_unknown_type: int
    uuids_preserved: int
    mbr_exact_matches: int
    delta_mode_objects: int
    shifted_objects: int
    total_vertices: int
    shapes_without_catalog: list[str]
    objects: list[ObjectAudit]

    @property
    def safe_to_export(self) -> bool:
        """So liberamos escrita de volta se tudo foi interpretado e conferido."""
        return (
            self.objects_failed == 0
            and self.objects_unknown_type == 0
            and not self.shapes_without_catalog
            and self.objects_decoded == self.mbr_exact_matches
        )

    def summary_lines(self) -> list[str]:
        lines = [
            f"Objetos declarados no catalogo: {self.objects_declared}",
            f"Objetos decodificados: {self.objects_decoded}",
            f"Objetos com falha: {self.objects_failed}",
            f"Tipos nao reconhecidos: {self.objects_unknown_type}",
            f"UUIDs preservados: {self.uuids_preserved}/{self.objects_declared}",
            f"Bbox conferindo com o MBR: {self.mbr_exact_matches}/{self.objects_decoded}",
            f"Objetos em modo delta (curvas): {self.delta_mode_objects}",
            f"Vertices lidos: {self.total_vertices}",
            f"CRS: {self.crs}",
        ]
        if self.shifted_objects:
            lines.append(f"ATENCAO: {self.shifted_objects} objetos com deslocamento RTK nao nulo")
        if self.shapes_without_catalog:
            lines.append(f"ATENCAO: {len(self.shapes_without_catalog)} fdShape sem entrada no catalogo")
        lines.append(
            "Seguro para exportar de volta ao John Deere: "
            + ("sim" if self.safe_to_export else "NAO - revise os avisos acima")
        )
        return lines


def index_spatial_catalogs(eic_root: Path) -> dict[str, CatalogItem]:
    """Mapeia ``fileName`` -> metadados declarados no SpatialCatalog."""
    index: dict[str, CatalogItem] = {}
    for catalog in sorted(eic_root.rglob("*.SpatialCatalog")):
        try:
            root = ET.parse(catalog).getroot()
        except (ET.ParseError, OSError):
            continue

        field_erid = ""
        for el in root.iter():
            if _local(el.tag) == "SpatialItems":
                field_erid = _norm_guid(el.attrib.get("eridFieldRef", ""))
                break

        for el in root.iter():
            file_name = el.attrib.get("fileName", "")
            if not file_name:
                continue
            item = CatalogItem(
                tag=_local(el.tag),
                erid=_norm_guid(el.attrib.get("erid", "")),
                name=el.attrib.get("name", ""),
                file_name=file_name,
                field_erid=field_erid,
                catalog_file=str(catalog),
                geometry_type=el.attrib.get("spatialGeometryType", ""),
            )
            for key, value in el.attrib.items():
                if key.endswith("Ref") or key == "session":
                    item.extra_refs[key] = _norm_guid(value)

            for child in el.iter():
                ctag = _local(child.tag)
                value = child.attrib.get("value")
                if ctag == "MBR":
                    try:
                        item.mbr = (
                            float(child.attrib["west"]),
                            float(child.attrib["south"]),
                            float(child.attrib["east"]),
                            float(child.attrib["north"]),
                        )
                    except (KeyError, TypeError, ValueError):
                        item.mbr = None
                elif not value:
                    continue
                elif ctag == "vrReferenceLongitude":
                    ref = item.reference or (0.0, 0.0)
                    item.reference = (float(value), ref[1])
                elif ctag == "vrReferenceLatitude":
                    ref = item.reference or (0.0, 0.0)
                    item.reference = (ref[0], float(value))
                elif ctag == "vrEastShiftComponent":
                    item.east_shift_mm = float(value)
                elif ctag == "vrNorthShiftComponent":
                    item.north_shift_mm = float(value)

            # Mantem a entrada mais completa quando o mesmo shape aparece em
            # varios catalogos (ImportExport repete os itens da sessao).
            previous = index.get(file_name)
            if previous is None or (previous.mbr is None and item.mbr is not None):
                index[file_name] = item
    return index


def audit_eic(eic_root: Path) -> AuditReport:
    """Le e confere todos os objetos espaciais sob ``EIC``."""
    catalog_index = index_spatial_catalogs(eic_root)
    catalog_files = len(list(eic_root.rglob("*.SpatialCatalog")))

    shapes_on_disk = {p.stem: p for p in eic_root.rglob("*.fdShape")}
    without_catalog = sorted(set(shapes_on_disk) - set(catalog_index))

    objects: list[ObjectAudit] = []
    decoded = failed = unknown = uuids = mbr_ok = delta = shifted = vertices = 0

    for file_name, item in sorted(catalog_index.items()):
        shape_path = shapes_on_disk.get(file_name)
        if item.erid:
            uuids += 1
        if item.tag not in KNOWN_ITEM_TAGS:
            unknown += 1
        if item.east_shift_mm or item.north_shift_mm:
            shifted += 1

        if shape_path is None:
            failed += 1
            objects.append(
                ObjectAudit(
                    erid=item.erid,
                    tag=item.tag,
                    name=item.name,
                    file_name=file_name,
                    field_erid=item.field_erid,
                    mode="ausente",
                    point_count=0,
                    record_count=0,
                    mbr_match=False,
                    status="falha",
                    note="fdShape declarado no catalogo mas ausente no disco",
                )
            )
            continue

        result = decode_fdshape(shape_path, mbr=item.mbr, reference=item.reference)
        status = "ok" if result.ok else "falha"
        note = ""
        if not result.ok:
            failed += 1
            note = "nao foi possivel determinar o modo de coordenada"
        else:
            decoded += 1
            vertices += len(result.points)
            if result.mode == MODE_DELTA:
                delta += 1
            if result.mbr_match:
                mbr_ok += 1
            else:
                note = "bbox nao coincide com o MBR do catalogo"
                status = "atencao"

        objects.append(
            ObjectAudit(
                erid=item.erid,
                tag=item.tag,
                name=item.name,
                file_name=file_name,
                field_erid=item.field_erid,
                mode=result.mode,
                point_count=len(result.points),
                record_count=result.record_count,
                mbr_match=result.mbr_match,
                status=status,
                note=note,
            )
        )

    return AuditReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        eic_root=str(eic_root),
        crs=JD_CRS,
        catalog_files=catalog_files,
        objects_declared=len(catalog_index),
        objects_decoded=decoded,
        objects_failed=failed,
        objects_unknown_type=unknown,
        uuids_preserved=uuids,
        mbr_exact_matches=mbr_ok,
        delta_mode_objects=delta,
        shifted_objects=shifted,
        total_vertices=vertices,
        shapes_without_catalog=without_catalog,
        objects=objects,
    )


def write_report(report: AuditReport, out_dir: Path) -> tuple[Path, Path]:
    """Grava ``conversion_report.json`` e ``spatial_catalog.json``."""
    out_dir.mkdir(parents=True, exist_ok=True)

    report_path = out_dir / "conversion_report.json"
    payload = asdict(report)
    payload["safe_to_export"] = report.safe_to_export
    report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    catalog_path = out_dir / "spatial_catalog.json"
    catalog_payload = [
        {
            "erid": o.erid,
            "tag": o.tag,
            "name": o.name,
            "file_name": o.file_name,
            "field_erid": o.field_erid,
            "mode": o.mode,
            "points": o.point_count,
        }
        for o in report.objects
    ]
    catalog_path.write_text(
        json.dumps(catalog_payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report_path, catalog_path
