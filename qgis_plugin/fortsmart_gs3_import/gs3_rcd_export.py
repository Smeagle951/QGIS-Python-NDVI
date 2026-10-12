"""FortSmart — exportar talhoes QGIS para pendrive GS3 2630 (RCD/fdShape)."""

from __future__ import annotations

import json
import shutil
import struct
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .fortsmart_coords import sanitize_ring
from .gs3_geometry import inward_offset_ring, prepare_gs3_boundary_geometry, ring_area_ha
from .gs3_guidance_router import GS3GuidanceRouter
from .gs3_pass_guidance import (
    GS3ExportPlan,
    format_gs3_guidance_report,
    guidance_failure_message,
    validate_gs3_pass_guidance,
)
from .gs3_preflight import validate_gs3_package
from .gs3_setup import write_merged_setup
from .gs3_rcd_core import _clean_label
from .gs3_spatial_types import Gs3FieldBundle, Gs3SpatialFeature, SpatialKind


@dataclass
class ExportField:
    cliente: str
    fazenda: str
    talhao: str
    points: list[tuple[float, float]]
    field_erid: str = ""
    boundary_erid: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _field_bucket(field_guid: str) -> str:
    return field_guid.split("-")[0][-2:].upper()


def sanitize_gs3_machine_folder(name: str) -> str:
    """Nome da pasta sob GS3_2630 (ex.: nome da fazenda)."""
    text = " ".join((name or "").split()).strip(" .")
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in bad else ch for ch in text)
    return cleaned[:60] if cleaned else "JD4600"


def _mbr_from_points(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return min(lats), max(lats), min(lons), max(lons)


def _fdshape_points(
    points: list[tuple[float, float]],
    *,
    close_ring: bool,
) -> list[tuple[float, float]]:
    clean = sanitize_ring(list(points), min_points=2 if not close_ring else 4)
    if close_ring and len(clean) >= 2 and clean[0] != clean[-1]:
        clean.append(clean[0])
    return clean


def _pack_fdshape_records(points: list[tuple[float, float]]) -> bytes:
    body = bytearray()
    for lon, lat in points:
        body.extend(struct.pack("<ddf", lon, lat, 0.0))
    return bytes(body)


def write_fdshape(
    points: list[tuple[float, float]],
    *,
    close_ring: bool = True,
    additional_rings: list[list[tuple[float, float]]] | None = None,
) -> bytes:
    """Serialize the native Deere fdShape container.

    Header uint64 values at 0x20/0x28/0x30 are respectively section count,
    reserved zero, and version one.  Polygon sections after the exterior ring
    are encoded as ``<name-id:uint64><count:uint64><records>``.  This is the
    structure used by accepted Deere packages for internal boundaries and the
    driven headland ring.
    """
    primary = _fdshape_points(points, close_ring=close_ring)
    extras = [
        _fdshape_points(ring, close_ring=True)
        for ring in (additional_rings or [])
        if len(ring) >= 4
    ]
    header = bytearray(64)
    header[16:18] = b"\x01\x02"
    struct.pack_into("<Q", header, 32, 1 + len(extras))
    struct.pack_into("<Q", header, 40, 0)
    struct.pack_into("<Q", header, 48, 1)
    struct.pack_into("<Q", header, 56, len(primary))

    body = bytearray(_pack_fdshape_records(primary))
    for ring in extras:
        # Deere fixtures use NameID 1 for every associated polygon section.
        body.extend(struct.pack("<QQ", 1, len(ring)))
        body.extend(_pack_fdshape_records(ring))
    return bytes(header) + bytes(body)


def default_fddata_template() -> bytes:
    return bytes.fromhex(
        "000000000100000000000000000000000102000000000000000000000000000005"
        "00000000000000000000000000000007000000426f756e6461727954797065"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "0006"
    )


def build_spatial_catalog_xml(
    *,
    client_name: str,
    client_erid: str,
    farm_name: str,
    farm_erid: str,
    field_name: str,
    field_erid: str,
    boundary_erid: str,
    boundary_prefix: str,
    mbr: tuple[float, float, float, float],
) -> str:
    south, north, west, east = mbr
    now = _utc_now()
    session = uuid.uuid4()
    esc = lambda s: s.replace("&", "&amp;").replace('"', "&quot;")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<rcdscfldie:SpatialCatalog xmlns:rcdsetup="urn:schemas-johndeere-com:RCD:Setup" xmlns:bt="urn:schemas-johndeere-com:BasicTypes" xmlns:rcdscbase="urn:schemas-johndeere-com:RCD:SpatialCatalog:Base" xmlns:unit="urn:schemas-johndeere-com:UnitSystem" xmlns:rep="urn:schemas-johndeere-com:Representation" xmlns:spatial="urn:schemas-johndeere-com:SpatialTypes" xmlns:rcdscfldie="urn:schemas-johndeere-com:RCD:SpatialCatalog:FieldImportExport">
  <FileSchemaVersion nonProductionCode="0">
    <bt:FileSchemaContentVersion major="1" minor="11" />
    <bt:UnitOfMeasureVersion major="1" minor="43" />
    <bt:RepresentationSystemVersion major="4" minor="161" />
  </FileSchemaVersion>
  <SourceApp major="2" minor="0" build="0" revision="135" nameSourceApp="FortSmart GS3 Export" uuidSourceApp="{{b050528e-f328-4dd8-9e9c-71fe8153692e}}" uuidSourceAppNode="{{c4598e47-8d1d-4caf-90c6-f6cfedf0e94f}}" uuidSession="{{{session}}}" />
  <Setup>
    <rcdsetup:FileSchemaVersion nonProductionCode="0">
      <bt:FileSchemaContentVersion major="3" minor="28" />
      <bt:UnitOfMeasureVersion major="1" minor="43" />
      <bt:RepresentationSystemVersion major="4" minor="161" />
    </rcdsetup:FileSchemaVersion>
    <rcdsetup:Participant>
      <rcdsetup:Client lastModified="{now}" sourceNode="{{00000000-0000-0000-0000-000000000000}}" erid="{{{client_erid}}}" name="{esc(client_name)}" />
    </rcdsetup:Participant>
    <rcdsetup:Farm lastModified="{now}" sourceNode="{{00000000-0000-0000-0000-000000000000}}" erid="{{{farm_erid}}}" name="{esc(farm_name)}" clientRef="{{{client_erid}}}" />
    <rcdsetup:Field lastModified="{now}" sourceNode="{{00000000-0000-0000-0000-000000000000}}" erid="{{{field_erid}}}" name="{esc(field_name)}" farmRef="{{{farm_erid}}}">
      <rcdsetup:Area value="0" sourceUOM="ac" variableRepresentation="vrReportedFieldArea" />
    </rcdsetup:Field>
    <rcdsetup:Products />
  </Setup>
  <SpatialItems eridFieldRef="{{{field_erid}}}">
    <Boundary lastModified="{now}" sourceNode="{{00000000-0000-0000-0000-000000000000}}" erid="{{{boundary_erid}}}" spatialGeometryType="polygon" fileName="{boundary_prefix}">
      <spatial:MBR uomSource="arcdeg" uomTarget="arcdeg" north="{north}" south="{south}" east="{east}" west="{west}" />
      <spatial:dtSignalType value="dtiSignalTypeUnknown" definedTypeRepresentation="dtSignalType" />
    </Boundary>
  </SpatialItems>
</rcdscfldie:SpatialCatalog>
"""


def build_setup_fds_xml(
    client_name: str,
    client_erid: str,
    farm_name: str,
    farm_erid: str,
    fields: list[tuple[str, str, float]],
) -> str:
    now = _utc_now()
    session = uuid.uuid4()
    esc = lambda s: s.replace("&", "&amp;").replace('"', "&quot;")
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<SetupFile xmlns:spatial="urn:schemas-johndeere-com:SpatialTypes" xmlns:unit="urn:schemas-johndeere-com:UnitSystem" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:rep="urn:schemas-johndeere-com:Representation" xmlns:bt="urn:schemas-johndeere-com:BasicTypes" xmlns="urn:schemas-johndeere-com:RCD:Setup">',
        f'  <SourceApp major="10" minor="29" build="342294" revision="0" nameSourceApp="FortSmart GS3 Export" uuidSourceApp="{{b050528e-f328-4dd8-9e9c-71fe8153692e}}" uuidSourceAppNode="{{c4598e47-8d1d-4caf-90c6-f6cfedf0e94f}}" uuidSession="{{{session}}}" />',
        "  <Setup>",
        '    <FileSchemaVersion nonProductionCode="0">',
        '      <bt:FileSchemaContentVersion major="3" minor="28" />',
        '      <bt:UnitOfMeasureVersion major="1" minor="43" />',
        '      <bt:RepresentationSystemVersion major="4" minor="161" />',
        "    </FileSchemaVersion>",
        "    <Participant>",
        f'      <Client lastModified="{now}" sourceNode="{{00000000-0000-0000-0000-000000000000}}" erid="{{{client_erid}}}" name="{esc(client_name)}" />',
        "    </Participant>",
        f'    <Farm lastModified="{now}" sourceNode="{{00000000-0000-0000-0000-000000000000}}" erid="{{{farm_erid}}}" name="{esc(farm_name)}" clientRef="{{{client_erid}}}" />',
    ]
    for field_erid, field_name, area_ha in fields:
        lines.append(
            f'    <Field lastModified="{now}" sourceNode="{{00000000-0000-0000-0000-000000000000}}" erid="{{{field_erid}}}" name="{esc(field_name)}" farmRef="{{{farm_erid}}}">'
        )
        lines.append(
            f'      <Area value="{area_ha:.12f}" sourceUOM="ha" '
            'variableRepresentation="vrReportedFieldArea" />'
        )
        lines.append("    </Field>")
    lines.extend(["  </Setup>", "</SetupFile>"])
    return "\n".join(lines)


def copy_support_files(template_eic: Path | None, eic_root: Path) -> None:
    eic_root.mkdir(parents=True, exist_ok=True)
    for name in ("host", "global.ver"):
        src = template_eic / name if template_eic else None
        if src and src.exists():
            shutil.copy2(src, eic_root / name)
        else:
            raise RuntimeError(
                f"Recurso GS3 nativo ausente: {name}. "
                "O exportador nao cria mais arquivos de suporte preenchidos com zeros."
            )


def _resolve_template_eic(template_gs3: Path | None) -> Path | None:
    if template_gs3:
        root = Path(template_gs3)
        for candidate in (
            root / "JD4600" / "RCD" / "EIC",
            root / "GS3_2630" / "JD4600" / "RCD" / "EIC",
        ):
            if (candidate / "setup.fds").exists():
                return candidate
        matches = list(root.rglob("setup.fds")) if root.exists() else []
        if matches:
            return matches[0].parent

    # Native support resources verified against the user's accepted GS3
    # package. They contain no farm/field identity and are safe to reuse.
    bundled = Path(__file__).resolve().parent / "resources" / "gs3_native"
    if all(
        (bundled / name).is_file()
        for name in ("host", "global.ver", "Boundary.fdData", "setup_template.fds")
    ):
        return bundled
    return None


def export_gs3_usb(
    fields: list[ExportField],
    output_root: Path,
    *,
    template_gs3: Path | None = None,
    merge_setup: bool = True,
    machine_folder: str | None = None,
) -> Path:
    """Compatibility entry point routed through the canonical GS3 pipeline."""
    if not fields:
        raise ValueError("Nenhum talhao para exportar.")
    namespace = uuid.UUID("2345eeb4-e436-4d22-9474-3b858699f48a")
    bundles: list[Gs3FieldBundle] = []
    for item in fields:
        if len(item.points) < 4:
            continue
        try:
            field_erid = str(uuid.UUID(str(item.field_erid).strip("{}")))
        except (ValueError, AttributeError):
            field_erid = str(
                uuid.uuid5(namespace, f"field|{item.cliente}|{item.fazenda}|{item.talhao}")
            )
        try:
            boundary_erid = str(uuid.UUID(str(item.boundary_erid).strip("{}")))
        except (ValueError, AttributeError):
            boundary_erid = str(uuid.uuid5(namespace, f"boundary|{field_erid}"))
        talhao = _clean_label(item.talhao, "Talhao")
        feature = Gs3SpatialFeature(
            kind=SpatialKind.LIMITE,
            cliente=item.cliente,
            fazenda=item.fazenda,
            talhao=talhao,
            field_erid=field_erid,
            item_erid=boundary_erid,
            name="Limite externo",
            source_file="legacy",
            catalog_file="legacy",
            geometry="polygon",
            rings=[list(item.points)],
            format_source="qgis",
            extra={"canonical_role": "EXTERNAL_BOUNDARY"},
        )
        bundles.append(
            Gs3FieldBundle(
                cliente=item.cliente,
                fazenda=item.fazenda,
                talhao=talhao,
                field_erid=field_erid,
                features=[feature],
            )
        )
    if not bundles:
        raise ValueError("Nenhuma geometria valida para exportar.")
    return export_gs3_from_bundles(
        bundles,
        output_root,
        template_gs3=template_gs3,
        merge_setup=merge_setup,
        machine_folder=machine_folder,
    )


def _norm_erid(erid: str) -> str:
    clean = erid.strip("{}")
    return f"{{{clean}}}"


def _guid_clean(erid: str) -> str:
    return erid.strip("{}")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace('"', "&quot;")


def _feature_points(feature: Gs3SpatialFeature) -> list[tuple[float, float]]:
    if feature.rings and feature.rings[0]:
        return list(feature.rings[0])
    return list(feature.points)


def _xml_header(session: uuid.UUID | None = None) -> str:
    now = _utc_now()
    sid = session or uuid.uuid4()
    return f"""  <FileSchemaVersion nonProductionCode="0">
    <bt:FileSchemaContentVersion major="1" minor="11" />
    <bt:UnitOfMeasureVersion major="1" minor="43" />
    <bt:RepresentationSystemVersion major="4" minor="161" />
  </FileSchemaVersion>
  <SourceApp major="2" minor="0" build="0" revision="135" nameSourceApp="FortSmart GS3 Export" uuidSourceApp="{{b050528e-f328-4dd8-9e9c-71fe8153692e}}" uuidSourceAppNode="{{c4598e47-8d1d-4caf-90c6-f6cfedf0e94f}}" uuidSession="{{{sid}}}" />"""


def _xml_setup_block(
    cliente: str,
    client_erid: str,
    fazenda: str,
    farm_erid: str,
    field_name: str,
    field_erid: str,
    area_ha: float,
) -> str:
    now = _utc_now()
    return f"""  <Setup>
    <rcdsetup:FileSchemaVersion nonProductionCode="0">
      <bt:FileSchemaContentVersion major="3" minor="28" />
      <bt:UnitOfMeasureVersion major="1" minor="43" />
      <bt:RepresentationSystemVersion major="4" minor="161" />
    </rcdsetup:FileSchemaVersion>
    <rcdsetup:Participant>
      <rcdsetup:Client lastModified="{now}" sourceNode="{{00000000-0000-0000-0000-000000000000}}" erid="{{{client_erid}}}" name="{_esc(cliente)}" />
    </rcdsetup:Participant>
    <rcdsetup:Farm lastModified="{now}" sourceNode="{{00000000-0000-0000-0000-000000000000}}" erid="{{{farm_erid}}}" name="{_esc(fazenda)}" clientRef="{{{client_erid}}}" />
    <rcdsetup:Field lastModified="{now}" sourceNode="{{00000000-0000-0000-0000-000000000000}}" erid="{{{field_erid}}}" name="{_esc(field_name)}" farmRef="{{{farm_erid}}}">
      <rcdsetup:Area value="{area_ha:.12f}" sourceUOM="ha" variableRepresentation="vrReportedFieldArea" />
    </rcdsetup:Field>
    <rcdsetup:Products />
  </Setup>"""


def _xml_mbr(points: list[tuple[float, float]]) -> str:
    if not points:
        return ""
    south, north, west, east = _mbr_from_points(points)
    return (
        f'      <spatial:MBR uomSource="arcdeg" uomTarget="arcdeg" '
        f'north="{north}" south="{south}" east="{east}" west="{west}" />'
    )


def _xml_abline(feature: Gs3SpatialFeature) -> str:
    pts = _feature_points(feature)
    if len(pts) < 2:
        return ""
    erid = feature.item_erid or str(uuid.uuid4())
    a_lon, a_lat = pts[0]
    b_lon, b_lat = pts[-1]
    now = _utc_now()
    active = "true" if feature.active else "false"
    return f"""    <ABLine lastModified="{now}" sourceNode="{{00000000-0000-0000-0000-000000000000}}" erid="{{{erid}}}" name="{_esc(feature.name or 'Linha AB')}" active="{active}">
      <rcdscbase:APoint latitude="{a_lat}" longitude="{a_lon}" />
      <rcdscbase:BPoint latitude="{b_lat}" longitude="{b_lon}" />
    </ABLine>"""


def _xml_abcurve(feature: Gs3SpatialFeature, prefix: str) -> str:
    pts = _feature_points(feature)
    if len(pts) < 2:
        return ""
    erid = feature.item_erid or str(uuid.uuid4())
    a_lon, a_lat = pts[0]
    b_lon, b_lat = pts[-1]
    now = _utc_now()
    segs = max(2, len(pts) - 1)
    return f"""    <ABCurve lastModified="{now}" sourceNode="{{00000000-0000-0000-0000-000000000000}}" erid="{{{erid}}}" spatialGeometryType="point" fileName="{prefix}" name="{_esc(feature.name or 'Curva AB')}" numberOfSegments="{segs}">
{_xml_mbr(pts)}
      <rcdscbase:APoint>
        <spatial:vrLatitude value="{a_lat}" sourceUOM="arcdeg" variableRepresentation="vrLatitude" />
        <spatial:vrLongitude value="{a_lon}" sourceUOM="arcdeg" variableRepresentation="vrLongitude" />
      </rcdscbase:APoint>
      <rcdscbase:BPoint>
        <spatial:vrLatitude value="{b_lat}" sourceUOM="arcdeg" variableRepresentation="vrLatitude" />
        <spatial:vrLongitude value="{b_lon}" sourceUOM="arcdeg" variableRepresentation="vrLongitude" />
      </rcdscbase:BPoint>
    </ABCurve>"""


def _xml_curved_track(feature: Gs3SpatialFeature, prefix: str) -> str:
    pts = _feature_points(feature)
    if len(pts) < 2:
        return ""
    erid = feature.item_erid or str(uuid.uuid4())
    now = _utc_now()
    ref_lat = pts[0][1]
    ref_lon = pts[0][0]
    return f"""    <CurvedTrackLine lastModified="{now}" sourceNode="{{00000000-0000-0000-0000-000000000000}}" erid="{{{erid}}}" spatialGeometryType="point" fileName="{prefix}" name="{_esc(feature.name or 'Curva adaptada')}">
{_xml_mbr(pts)}
      <rcdscbase:vrReferenceLatitude value="{ref_lat}" sourceUOM="arcdeg" variableRepresentation="vrLatitude" />
      <rcdscbase:vrReferenceLongitude value="{ref_lon}" sourceUOM="arcdeg" variableRepresentation="vrLongitude" />
    </CurvedTrackLine>"""


def _xml_boundary(
    feature: Gs3SpatialFeature,
    prefix: str,
    *,
    headland_erid: str = "",
    internal_boundaries: list[Gs3SpatialFeature] | None = None,
) -> str:
    pts = _feature_points(feature)
    if len(pts) < 4:
        return ""
    erid = feature.item_erid or str(uuid.uuid4())
    now = _utc_now()
    headland_xml = ""
    if headland_erid:
        headland_xml = f'\n      <HeadlandLookup ID="0" eridRef="{{{headland_erid}}}" />'
    name_xml = "".join(
        f'\n      <NameIDPair Name="{_esc(item.name or "LIMITE INTERNO")}" ID="{index}" />'
        for index, item in enumerate(internal_boundaries or [], start=1)
    )
    return f"""    <Boundary lastModified="{now}" sourceNode="{{00000000-0000-0000-0000-000000000000}}" erid="{{{erid}}}" spatialGeometryType="polygon" fileName="{prefix}">
{_xml_mbr(pts)}
      <spatial:dtSignalType value="dtiSignalTypeUnknown" definedTypeRepresentation="dtSignalType" />{name_xml}{headland_xml}
    </Boundary>"""


def _xml_headland(feature: Gs3SpatialFeature) -> str:
    erid = feature.item_erid or str(uuid.uuid4())
    now = _utc_now()
    name = _esc(feature.name or "Bordadura")
    if _is_driven_headland(feature) and len(_feature_points(feature)) >= 4:
        return f"""    <HeadLand lastModified="{now}" sourceNode="{{00000000-0000-0000-0000-000000000000}}" erid="{{{erid}}}" name="{name}" useAsTurnPredictor="false" useDrivenExteriorHeadland="true">
      <vrHeadlandOffset value="0" sourceUOM="ft" variableRepresentation="vrHeadlandOffset" />
    </HeadLand>"""
    offset = feature.extra.get("offset_m", "9")
    return f"""    <HeadLand lastModified="{now}" sourceNode="{{00000000-0000-0000-0000-000000000000}}" erid="{{{erid}}}" name="{name}" useAsTurnPredictor="false">
      <vrHeadlandOffset value="{offset}" sourceUOM="m" variableRepresentation="vrHeadlandOffset" />
    </HeadLand>"""


def _write_field_catalogs(
    bundle: Gs3FieldBundle,
    field_dir: Path,
    *,
    client_erid: str,
    farm_erid: str,
    cliente: str,
    fazenda: str,
    fddata_templates: dict[int, bytes],
    area_ha: float,
    guidance_items: list[str] | None = None,
) -> int:
    field_erid = bundle.field_erid or str(uuid.uuid4())
    talhao = _clean_label(bundle.talhao, "Talhao")
    session = uuid.uuid4()
    field_dir.mkdir(parents=True, exist_ok=True)

    # Accepted Deere catalogs list CurvedTrackLine before HeadLand/Boundary.
    import_items: list[str] = list(guidance_items or [])
    curve_items: list[str] = []
    written = len(import_items)

    headlands: dict[str, Gs3SpatialFeature] = {}
    boundaries: list[Gs3SpatialFeature] = []
    internal_boundaries = [
        feature for feature in bundle.features
        if feature.kind == SpatialKind.LIMITE_INTERNO
        and not _is_headland_edge(feature)
        and len(_feature_points(feature)) >= 4
    ]

    for feature in bundle.features:
        if feature.kind == SpatialKind.BORDADURA:
            erid = feature.item_erid or str(uuid.uuid4())
            feature.item_erid = erid
            headlands[erid] = feature

    # A FortSmart HEADLAND is GS3 HeadLand metadata linked from Boundary.
    # Its polygon is not a guidance CurveTrack and must never be serialized as
    # one merely because it contains coordinates.
    for feature in headlands.values():
        import_items.append(_xml_headland(feature))
        written += 1

    for feature in bundle.features:
        kind = feature.kind
        pts = _feature_points(feature)

        if kind == SpatialKind.LIMITE:
            erid = feature.item_erid or str(uuid.uuid4())
            prefix = f"Boundary{erid.replace('-', '')}" if "-" in erid else f"Boundary{erid.strip('{}')}"
            if not prefix.startswith("Boundary"):
                prefix = f"Boundary{erid.strip('{}')}"
            # Normalizar prefix como no GS3 real: Boundary + guid sem chaves
            guid_clean = erid.strip("{}")
            prefix = f"Boundary{guid_clean}"
            feature.item_erid = erid.strip("{}")

            headland_erid = ""
            geometric_headlands = [
                hl_feat
                for hl_feat in headlands.values()
                if len(_feature_points(hl_feat)) >= 4
            ]
            for hl_erid, hl_feat in headlands.items():
                if len(_feature_points(hl_feat)) < 4:
                    continue
                if feature.extra.get("headland_erid") == hl_erid or hl_feat.name in feature.name:
                    headland_erid = hl_erid
                    break
            if not headland_erid and geometric_headlands:
                headland_erid = geometric_headlands[0].item_erid

            import_items.append(
                _xml_boundary(
                    feature,
                    prefix,
                    headland_erid=headland_erid,
                    internal_boundaries=internal_boundaries,
                )
            )
            if len(pts) >= 4:
                internal_count = len(internal_boundaries)
                if internal_count not in fddata_templates:
                    obstacles = "; ".join(
                        f"{item.source_file or item.name or '-'} "
                        f"{ring_area_ha(_feature_points(item)) * 10_000.0:.0f} m2"
                        for item in internal_boundaries
                    )
                    raise ValueError(
                        "GS3_INTERNAL_BOUNDARY_UNSUPPORTED: "
                        f"talhao '{talhao}' tem {internal_count} limites internos tratados como "
                        f"obstaculo ({obstacles}); o GS3 so tem fixture fdData confirmada para "
                        f"{', '.join(str(count) for count in sorted(fddata_templates))} obstaculo(s)."
                    )
                associated_rings = [
                    _clockwise(_feature_points(item))
                    for item in internal_boundaries
                ]
                associated_rings.extend(
                    _clockwise(_feature_points(item))
                    for item in geometric_headlands
                    if item.item_erid == headland_erid
                )
                field_dir.mkdir(parents=True, exist_ok=True)
                (field_dir / f"{prefix}.fdShape").write_bytes(
                    write_fdshape(
                        pts,
                        close_ring=True,
                        additional_rings=associated_rings,
                    )
                )
                (field_dir / f"{prefix}.fdData").write_bytes(fddata_templates[internal_count])
                written += 1
            boundaries.append(feature)

        elif kind == SpatialKind.LIMITE_INTERNO:
            # Represented by NameIDPair entries of the principal Boundary.
            # No second external Boundary/fdShape is emitted.
            continue

        elif kind == SpatialKind.LINHA_AB and len(pts) >= 2:
            import_items.append(_xml_abline(feature))
            written += 1

        elif kind == SpatialKind.BORDADURA:
            # Serialized once above as HeadLand metadata.
            continue

        elif kind in (SpatialKind.CURVA_ADAPTADA, SpatialKind.TRABALHO) and len(pts) >= 2:
            erid = feature.item_erid or str(uuid.uuid4())
            guid_clean = erid.strip("{}")
            prefix = f"CurveTrack{guid_clean}"
            import_items.append(_xml_curved_track(feature, prefix))
            (field_dir / f"{prefix}.fdShape").write_bytes(write_fdshape(pts, close_ring=False))
            written += 1

        elif kind == SpatialKind.CURVA_AB and len(pts) >= 2:
            erid = feature.item_erid or str(uuid.uuid4())
            guid_clean = erid.strip("{}")
            # The accepted reference package represents curved guidance as a
            # CurvedTrackLine/CurveTrack, not the rejected ABCurve object.
            prefix = f"CurveTrack{guid_clean}"
            import_items.append(_xml_curved_track(feature, prefix))
            (field_dir / f"{prefix}.fdShape").write_bytes(write_fdshape(pts, close_ring=False))
            written += 1

    if import_items:
        catalog = f"""<?xml version="1.0" encoding="utf-8"?>
<rcdscfldie:SpatialCatalog xmlns:rcdsetup="urn:schemas-johndeere-com:RCD:Setup" xmlns:bt="urn:schemas-johndeere-com:BasicTypes" xmlns:rcdscbase="urn:schemas-johndeere-com:RCD:SpatialCatalog:Base" xmlns:unit="urn:schemas-johndeere-com:UnitSystem" xmlns:rep="urn:schemas-johndeere-com:Representation" xmlns:spatial="urn:schemas-johndeere-com:SpatialTypes" xmlns:rcdscfldie="urn:schemas-johndeere-com:RCD:SpatialCatalog:FieldImportExport">
{_xml_header(session)}
{_xml_setup_block(cliente, client_erid, fazenda, farm_erid, talhao, field_erid, area_ha)}
  <SpatialItems eridFieldRef="{{{field_erid.strip('{}')}}}">
{chr(10).join(import_items)}
  </SpatialItems>
</rcdscfldie:SpatialCatalog>
"""
        field_dir.mkdir(parents=True, exist_ok=True)
        (field_dir / "ImportExport.SpatialCatalog").write_text(catalog, encoding="utf-8")

    if curve_items:
        catalog = f"""<?xml version="1.0" encoding="utf-8"?>
<rcdscfldie:SpatialCatalog xmlns:rcdsetup="urn:schemas-johndeere-com:RCD:Setup" xmlns:bt="urn:schemas-johndeere-com:BasicTypes" xmlns:unit="urn:schemas-johndeere-com:UnitSystem" xmlns:rcdscbase="urn:schemas-johndeere-com:RCD:SpatialCatalog:Base" xmlns:rep="urn:schemas-johndeere-com:Representation" xmlns:spatial="urn:schemas-johndeere-com:SpatialTypes" xmlns:rcdscfldie="urn:schemas-johndeere-com:RCD:SpatialCatalog:FieldImportExport">
{_xml_header(uuid.uuid4())}
{_xml_setup_block(cliente, client_erid, fazenda, farm_erid, talhao, field_erid, area_ha)}
  <SpatialItems eridFieldRef="{{{field_erid.strip('{}')}}}">
{chr(10).join(curve_items)}
  </SpatialItems>
</rcdscfldie:SpatialCatalog>
"""
        (field_dir / "ABCurves.SpatialCatalog").write_text(catalog, encoding="utf-8")

    if written > 0:
        _write_stub_catalogs(
            field_dir,
            field_erid=field_erid.strip("{}"),
            cliente=cliente,
            client_erid=client_erid,
            fazenda=fazenda,
            farm_erid=farm_erid,
            talhao=talhao,
            area_ha=area_ha,
        )

    return written


def _write_stub_catalogs(
    field_dir: Path,
    *,
    field_erid: str,
    cliente: str,
    client_erid: str,
    fazenda: str,
    farm_erid: str,
    talhao: str,
    area_ha: float,
) -> None:
    """ExportOnly + WaterManagement vazios — presentes no pacote Deere real."""
    session_a = uuid.uuid4()
    session_b = uuid.uuid4()
    export_only = f"""<?xml version="1.0" encoding="utf-8"?>
<rcdscfld:SpatialCatalog xmlns:spatial="urn:schemas-johndeere-com:SpatialTypes" xmlns:bt="urn:schemas-johndeere-com:BasicTypes" xmlns:rep="urn:schemas-johndeere-com:Representation" xmlns:unit="urn:schemas-johndeere-com:UnitSystem" xmlns:rcdscbase="urn:schemas-johndeere-com:RCD:SpatialCatalog:Base" xmlns:rcdsetup="urn:schemas-johndeere-com:RCD:Setup" xmlns:rcdscfld="urn:schemas-johndeere-com:RCD:SpatialCatalog:Field">
  <rcdscfld:FileSchemaVersion nonProductionCode="0">
    <bt:FileSchemaContentVersion major="1" minor="3" />
    <bt:UnitOfMeasureVersion major="1" minor="43" />
    <bt:RepresentationSystemVersion major="4" minor="161" />
  </rcdscfld:FileSchemaVersion>
  <rcdscfld:SourceApp major="2" minor="0" build="0" revision="135" nameSourceApp="FortSmart GS3 Export" uuidSourceApp="{{b050528e-f328-4dd8-9e9c-71fe8153692e}}" uuidSourceAppNode="{{c4598e47-8d1d-4caf-90c6-f6cfedf0e94f}}" uuidSession="{{{session_a}}}" />
  <rcdscfld:SpatialItems eridFieldRef="{{{field_erid}}}" />
</rcdscfld:SpatialCatalog>
"""
    water = f"""<?xml version="1.0" encoding="utf-8"?>
<rcdscfldiewm:SpatialCatalog xmlns:spatial="urn:schemas-johndeere-com:SpatialTypes" xmlns:bt="urn:schemas-johndeere-com:BasicTypes" xmlns:rep="urn:schemas-johndeere-com:Representation" xmlns:unit="urn:schemas-johndeere-com:UnitSystem" xmlns:rcdscbase="urn:schemas-johndeere-com:RCD:SpatialCatalog:Base" xmlns:rcdsetup="urn:schemas-johndeere-com:RCD:Setup" xmlns:rcdscfldiewm="urn:schemas-johndeere-com:RCD:SpatialCatalog:FieldImportExportWaterManagment">
{_xml_header(session_b)}
{_xml_setup_block(cliente, client_erid, fazenda, farm_erid, talhao, field_erid, area_ha)}
  <SpatialItems eridFieldRef="{{{field_erid}}}" />
</rcdscfldiewm:SpatialCatalog>
"""
    (field_dir / "ExportOnly.SpatialCatalog").write_text(export_only, encoding="utf-8")
    (field_dir / "WaterManagement.SpatialCatalog").write_text(water, encoding="utf-8")


GS3_DEBUG_KEYS = (
    "FIELD",
    "EXTERNAL_BOUNDARY",
    "INTERNAL_BOUNDARY",
    "HEADLAND",
    "AB_LINE",
    "AB_CURVE",
    "GUIDANCE_TRACK",
)


def _gs3_debug_role(feature: Gs3SpatialFeature) -> str:
    role = str(feature.extra.get("canonical_role", "") or "").strip().upper()
    if role == "OPERATIONAL_BOUNDARY":
        return "EXTERNAL_BOUNDARY"
    if role in GS3_DEBUG_KEYS:
        return role
    return {
        SpatialKind.LIMITE: "EXTERNAL_BOUNDARY",
        SpatialKind.LIMITE_INTERNO: "INTERNAL_BOUNDARY",
        SpatialKind.BORDADURA: "HEADLAND",
        SpatialKind.LINHA_AB: "AB_LINE",
        SpatialKind.CURVA_AB: "AB_CURVE",
        SpatialKind.CURVA_ADAPTADA: "GUIDANCE_TRACK",
    }.get(feature.kind, "UNSUPPORTED")


def _feature_is_serializable(feature: Gs3SpatialFeature) -> bool:
    points = _feature_points(feature)
    if feature.kind in (SpatialKind.LIMITE, SpatialKind.LIMITE_INTERNO):
        return len(points) >= 4
    if feature.kind == SpatialKind.BORDADURA:
        return True
    if feature.kind in (
        SpatialKind.LINHA_AB,
        SpatialKind.CURVA_AB,
        SpatialKind.CURVA_ADAPTADA,
    ):
        return len(points) >= 2
    return False


def build_gs3_export_debug(bundles: list[Gs3FieldBundle]) -> dict:
    detected = Counter({key: 0 for key in GS3_DEBUG_KEYS})
    included = Counter({key: 0 for key in GS3_DEBUG_KEYS})
    fields: list[dict] = []
    detected["FIELD"] = len(bundles)
    for bundle in bundles:
        field_detected = Counter()
        field_included = Counter()
        for feature in bundle.features:
            role = _gs3_debug_role(feature)
            if role not in detected:
                continue
            detected[role] += 1
            field_detected[role] += 1
            if _feature_is_serializable(feature):
                included[role] += 1
                field_included[role] += 1
        has_boundary = field_included["EXTERNAL_BOUNDARY"] > 0
        if has_boundary:
            included["FIELD"] += 1
        fields.append(
            {
                "name": bundle.talhao,
                "field_erid": bundle.field_erid,
                "detected": dict(field_detected),
                "included": dict(field_included),
                "has_external_boundary": has_boundary,
            }
        )
    return {
        "status": "planned",
        "detected": {key: detected[key] for key in GS3_DEBUG_KEYS},
        "included": {key: included[key] for key in GS3_DEBUG_KEYS},
        "serialized": {
            "setup_fds_generated": False,
            "field_folders_generated": 0,
            "boundary_files_generated": 0,
            "headland_items_generated": 0,
            "guidance_files_generated": 0,
        },
        "fields": fields,
        "errors": [],
    }


def _write_gs3_debug(output_root: Path, report: dict) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / "fortsmart_gs3_export_debug.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _write_gs3_text_report(output_root: Path, report: dict) -> Path:
    lines = ["GS3 EXPORT REPORT", ""]
    for field in report.get("fields", []):
        lines.extend(
            [
                f"Field: {field.get('name', '-')}",
                f"Area: {field.get('area_ha', 0):.4f} ha",
            ]
        )
        for boundary in field.get("boundaries", []):
            lines.extend(
                [
                    f"Boundary original vertices: {boundary['original_vertices']}",
                    f"Boundary export vertices: {boundary['export_vertices']}",
                    f"Boundary area difference: {boundary['area_difference_percent']:.6f}%",
                    f"Boundary tolerance: {boundary['tolerance_m']:.2f} m",
                ]
            )
            lines.extend(
                f"Internal boundary ignored (sliver < {INTERNAL_BOUNDARY_MIN_AREA_M2:.0f} m2): "
                f"{item['name']} {item['area_m2']:.2f} m2"
                for item in boundary.get("ignored_internal_boundaries", [])
            )
        lines.append("")
    serialized = report.get("serialized", {})
    lines.extend(
        [
            f"HeadLand: {serialized.get('headland_items_generated', 0)}",
            f"Internal Boundaries: {serialized.get('internal_boundary_associations', 0)}",
            f"Guidance: {serialized.get('guidance_files_generated', 0)}",
            "Template: VALID" if report.get("preflight", {}).get("setup_valid") else "Template: INVALID",
            "UUIDs: VALID" if report.get("preflight", {}).get("uuid_count", 0) else "UUIDs: INVALID",
            f"Status: {report.get('verification_state', report.get('status', 'UNKNOWN'))}",
        ]
    )
    if "guidance_passes" in report:
        lines.extend(format_gs3_guidance_report(report["guidance_passes"]))
    path = output_root / "GS3_EXPORT_REPORT.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def gs3_minimal_test_bundles(
    bundles: list[Gs3FieldBundle],
    mode: str,
) -> list[Gs3FieldBundle]:
    """Build diagnostic A/B/C bundles without operational work paths."""
    mode = str(mode or "").strip().upper()
    if mode not in {"A", "B", "C"}:
        raise ValueError("GS3_MINIMAL_TEST deve ser A, B ou C.")
    allowed = {"EXTERNAL_BOUNDARY", "OPERATIONAL_BOUNDARY"}
    if mode in {"B", "C"}:
        allowed.add("HEADLAND")
    if mode == "C":
        allowed.update({"AB_LINE", "AB_CURVE", "GUIDANCE_TRACK"})
    result = []
    for bundle in bundles:
        features = []
        guidance_added = False
        for feature in bundle.features:
            role = str(feature.extra.get("canonical_role", "") or "").upper()
            if role not in allowed:
                continue
            if role in {"AB_LINE", "AB_CURVE", "GUIDANCE_TRACK"}:
                if guidance_added:
                    continue
                guidance_added = True
            features.append(feature)
        if features:
            result.append(replace(bundle, features=features))
    return result


def _inspect_serialized_gs3(eic_root: Path) -> dict[str, int | bool]:
    catalogs = list(eic_root.rglob("*.SpatialCatalog"))
    headlands = 0
    ab_lines = 0
    internal_pairs = 0
    for catalog in catalogs:
        text = catalog.read_text(encoding="utf-8", errors="ignore")
        headlands += text.count("<HeadLand ")
        ab_lines += text.count("<ABLine ")
        internal_pairs += text.count("<NameIDPair ")
    field_folders = {
        catalog.parent
        for catalog in catalogs
        if "Fields" in catalog.parts
    }
    return {
        "setup_fds_generated": (eic_root / "setup.fds").is_file(),
        "field_folders_generated": len(field_folders),
        "boundary_files_generated": len(list(eic_root.rglob("Boundary*.fdShape"))),
        "headland_items_generated": headlands,
        "internal_boundary_associations": internal_pairs,
        "guidance_files_generated": len(list(eic_root.rglob("CurveTrack*.fdShape"))) + ab_lines,
    }


def _load_fddata_templates() -> dict[int, bytes]:
    resources = Path(__file__).resolve().parent / "resources" / "gs3_native"
    required_tokens = (
        b"BoundaryType",
        b"BoundaryNameID",
        b"ParentBoundaryID",
        b"HeadlandLookupID",
        b"SignalType",
    )
    templates: dict[int, bytes] = {}
    for count in (0, 1, 4):
        path = resources / f"Boundary_{count}.fdData"
        if not path.is_file():
            continue
        payload = path.read_bytes()
        missing = [token.decode("ascii") for token in required_tokens if token not in payload]
        if missing:
            raise ValueError(
                f"GS3_FDDATA_INVALID: {path.name} sem {', '.join(missing)}."
            )
        templates[count] = payload
    return templates


# GS3 treats every NameIDPair ring as an exclusion hole and computes the field
# area as exterior minus interiors. A FortSmart INTERNAL_BOUNDARY covering most
# of the field is the inner edge of the headland (working area), which Deere
# stores as the driven headland ring instead.
HEADLAND_EDGE_MIN_RATIO = 0.5
HEADLAND_EDGE_MAX_RATIO = 0.995
HEADLAND_EDGE_ROLE = "HEADLAND_EDGE"
# Smaller INTERNAL_BOUNDARY polygons are digitizing leftovers, not obstacles a
# machine can drive around. They are dropped only when the obstacle count has
# no confirmed fdData fixture, so packages that already exported are unchanged.
INTERNAL_BOUNDARY_MIN_AREA_M2 = 100.0
INTERNAL_SLIVER_ROLE = "IGNORED_SLIVER"


def _is_headland_edge(feature: Gs3SpatialFeature) -> bool:
    return feature.extra.get("gs3_role") == HEADLAND_EDGE_ROLE


def _is_driven_headland(feature: Gs3SpatialFeature) -> bool:
    return feature.extra.get("gs3_driven_headland") == "1"


def _clockwise(ring: list[tuple[float, float]]) -> list[tuple[float, float]]:
    signed = sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1])
    )
    return list(reversed(ring)) if signed > 0 else list(ring)


def _offset_m(feature: Gs3SpatialFeature) -> float:
    try:
        return float(str(feature.extra.get("offset_m", "0")).replace(",", "."))
    except ValueError:
        return 0.0


def _resolve_driven_headland(
    features: list[tuple[Gs3SpatialFeature, list[list[tuple[float, float]]]]],
    exterior_ring: list[tuple[float, float]],
) -> tuple[list[tuple[float, float]], set[int]]:
    """Return the Deere driven headland ring and ids of INTERNAL_BOUNDARY edges."""
    exterior_area = ring_area_ha(exterior_ring)
    if exterior_area <= 0:
        return [], set()

    def is_edge_area(area: float) -> bool:
        return HEADLAND_EDGE_MIN_RATIO * exterior_area <= area < HEADLAND_EDGE_MAX_RATIO * exterior_area

    driven: list[tuple[float, float]] = []
    for feature, rings in features:
        if feature.kind != SpatialKind.BORDADURA or not rings:
            continue
        holes = [ring for ring in rings[1:] if len(ring) >= 4]
        if holes:
            candidate = max(holes, key=ring_area_ha)
            if is_edge_area(ring_area_ha(candidate)):
                driven = candidate
                break
        if is_edge_area(ring_area_ha(rings[0])):
            driven = rings[0]
            break

    edge_ids: set[int] = set()
    for feature, rings in features:
        if feature.kind != SpatialKind.LIMITE_INTERNO or not rings:
            continue
        if is_edge_area(ring_area_ha(rings[0])):
            edge_ids.add(id(feature))
            if not driven:
                driven = rings[0]

    if not driven:
        for feature, _rings in features:
            if feature.kind == SpatialKind.BORDADURA and _offset_m(feature) > 0:
                driven = inward_offset_ring(exterior_ring, _offset_m(feature))
                if driven:
                    break
    return driven, edge_ids


def _prepare_bundle_boundaries(
    bundle: Gs3FieldBundle,
    supported_obstacle_counts: Iterable[int] = (0, 1, 4),
) -> tuple[Gs3FieldBundle, float, list[dict]]:
    supported_obstacle_counts = set(supported_obstacle_counts)
    prepared_features: list[Gs3SpatialFeature] = []
    polygon_rings: list[tuple[Gs3SpatialFeature, list[list[tuple[float, float]]]]] = []
    boundary_reports: list[dict] = []
    ignored_internal: list[dict] = []
    field_area_ha = 0.0
    for feature in bundle.features:
        if feature.kind not in (
            SpatialKind.LIMITE,
            SpatialKind.LIMITE_INTERNO,
            SpatialKind.BORDADURA,
        ):
            prepared_features.append(feature)
            continue
        if not feature.rings:
            if feature.kind == SpatialKind.LIMITE:
                raise ValueError(f"FIELD_AREA_INVALID: talhao '{bundle.talhao}' sem anel externo.")
            prepared_features.append(feature)
            continue
        prepared = prepare_gs3_boundary_geometry(feature.rings)
        if feature.kind == SpatialKind.LIMITE and len(prepared.rings) > 1:
            raise ValueError(
                "GS3_BOUNDARY_HOLES_UNSUPPORTED: anel interno embutido sem contrato fdShape confirmado; "
                "use INTERNAL_BOUNDARY canonico separado."
            )
        canonical_area = 0.0
        if feature.kind == SpatialKind.LIMITE and field_area_ha <= 0:
            canonical_area = feature.extra.get("area_ha") or feature.extra.get("canonical_area_ha")
            try:
                canonical_area = float(canonical_area)
            except (TypeError, ValueError):
                canonical_area = 0.0
            field_area_ha = canonical_area if canonical_area > 0 else prepared.original_area_ha
        prepared_features.append(feature)
        polygon_rings.append((feature, prepared.rings))
        if feature.kind == SpatialKind.LIMITE:
            boundary_reports.append(
                {
                    "name": feature.name,
                    "original_vertices": prepared.original_vertices,
                    "export_vertices": prepared.export_vertices,
                    "original_area_ha": prepared.original_area_ha,
                    "export_area_ha": prepared.export_area_ha,
                    "area_difference_percent": prepared.area_difference_percent,
                    "tolerance_m": prepared.tolerance_m,
                    "metric_crs": prepared.metric_crs,
                    "field_area_source": "canonical" if canonical_area > 0 else "external_boundary_metric",
                }
            )
    if field_area_ha <= 0:
        raise ValueError(f"FIELD_AREA_INVALID: talhao '{bundle.talhao}' com area <= 0.")

    exterior_ring = next(
        (rings[0] for feature, rings in polygon_rings if feature.kind == SpatialKind.LIMITE),
        [],
    )
    driven_ring, edge_ids = _resolve_driven_headland(polygon_rings, exterior_ring)
    rings_by_id = {id(feature): rings for feature, rings in polygon_rings}
    has_headland = any(f.kind == SpatialKind.BORDADURA for f in bundle.features)
    driven_assigned = False
    # Each associated fdShape section is one ring: the exterior, each obstacle
    # (NameIDPair) and at most one driven headland ring (HeadlandLookup).
    for index, feature in enumerate(prepared_features):
        rings = rings_by_id.get(id(feature))
        if feature.kind == SpatialKind.BORDADURA:
            if driven_ring and not driven_assigned:
                driven_assigned = True
                prepared_features[index] = replace(
                    feature,
                    rings=[driven_ring],
                    extra={**feature.extra, "gs3_driven_headland": "1"},
                )
            else:
                prepared_features[index] = replace(feature, rings=[], points=[])
        elif rings is None:
            continue
        elif id(feature) in edge_ids:
            prepared_features[index] = replace(
                feature,
                rings=[rings[0]],
                extra={**feature.extra, "gs3_role": HEADLAND_EDGE_ROLE},
            )
        else:
            prepared_features[index] = replace(feature, rings=[rings[0]])

    obstacle_indexes = [
        index for index, feature in enumerate(prepared_features)
        if feature.kind == SpatialKind.LIMITE_INTERNO
        and not _is_headland_edge(feature)
        and len(_feature_points(feature)) >= 4
    ]
    if len(obstacle_indexes) not in supported_obstacle_counts:
        for index in obstacle_indexes:
            feature = prepared_features[index]
            area_m2 = ring_area_ha(_feature_points(feature)) * 10_000.0
            if area_m2 >= INTERNAL_BOUNDARY_MIN_AREA_M2:
                continue
            ignored_internal.append({"name": feature.name or feature.source_file, "area_m2": round(area_m2, 2)})
            prepared_features[index] = replace(
                feature,
                rings=[],
                points=[],
                extra={**feature.extra, "gs3_role": INTERNAL_SLIVER_ROLE},
            )

    if driven_ring and not has_headland:
        field_key = (bundle.field_erid or bundle.talhao or "").strip("{}")
        prepared_features.append(
            Gs3SpatialFeature(
                kind=SpatialKind.BORDADURA,
                cliente=bundle.cliente,
                fazenda=bundle.fazenda,
                talhao=bundle.talhao,
                field_erid=bundle.field_erid,
                item_erid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"fortsmart-gs3-headland|{field_key}")),
                name="CABECEIRA",
                source_file="",
                catalog_file="",
                geometry="polygon",
                rings=[driven_ring],
                extra={"gs3_driven_headland": "1"},
            )
        )
    for report in boundary_reports:
        report["headland_ring_area_ha"] = ring_area_ha(driven_ring) if driven_ring else 0.0
        report["obstacle_count"] = sum(
            1
            for f in prepared_features
            if f.kind == SpatialKind.LIMITE_INTERNO
            and not _is_headland_edge(f)
            and f.extra.get("gs3_role") != INTERNAL_SLIVER_ROLE
        )
        report["headland_edges_from_internal_boundary"] = len(edge_ids)
        report["ignored_internal_boundaries"] = ignored_internal
    return (
        Gs3FieldBundle(
            cliente=bundle.cliente,
            fazenda=bundle.fazenda,
            talhao=bundle.talhao,
            field_erid=bundle.field_erid,
            features=prepared_features,
        ),
        field_area_ha,
        boundary_reports,
    )


def export_gs3_from_bundles(
    bundles: list[Gs3FieldBundle],
    output_root: Path,
    *,
    template_gs3: Path | None = None,
    merge_setup: bool = True,
    machine_folder: str | None = None,
    guidance_plan: GS3ExportPlan | None = None,
) -> Path:
    """Export completo GS3: limites, linhas AB, curvas, bordaduras (layout dual Gen4+GS3)."""
    if not bundles:
        raise ValueError("Nenhum talhao para exportar.")

    output_root = Path(output_root).resolve()
    report = build_gs3_export_debug(bundles)
    if report["included"]["FIELD"] == 0:
        report["status"] = "aborted"
        report["errors"].append("Nenhum Field contem objeto GS3 serializavel.")
    if report["included"]["EXTERNAL_BOUNDARY"] == 0:
        report["status"] = "aborted"
        report["errors"].append(
            "Nenhum EXTERNAL_BOUNDARY/OPERATIONAL_BOUNDARY valido foi incluido."
        )
    for field in report["fields"]:
        if not field["has_external_boundary"]:
            report["status"] = "aborted"
            report["errors"].append(
                f"Talhao '{field['name']}' sem EXTERNAL_BOUNDARY serializavel."
            )
    if report["errors"]:
        debug_path = _write_gs3_debug(output_root, report)
        raise ValueError(
            "Exportacao GS3 abortada antes de criar o pacote. "
            f"Diagnostico: {debug_path}\n" + "\n".join(report["errors"])
        )

    template_eic = _resolve_template_eic(template_gs3)
    if template_eic is None:
        report["status"] = "aborted"
        report["errors"].append("Recursos GS3 nativos host/global.ver/fdData indisponiveis.")
        debug_path = _write_gs3_debug(output_root, report)
        raise RuntimeError(
            "Exportacao GS3 abortada: recursos nativos ausentes. "
            f"Diagnostico: {debug_path}"
        )

    cliente = _clean_label(bundles[0].cliente, "Cliente")
    fazenda = _clean_label(bundles[0].fazenda, "Fazenda")
    folder = sanitize_gs3_machine_folder(machine_folder or fazenda)
    identity_namespace = uuid.UUID("2345eeb4-e436-4d22-9474-3b858699f48a")
    client_erid = str(uuid.uuid5(identity_namespace, f"client|{cliente.casefold()}"))
    farm_erid = str(
        uuid.uuid5(identity_namespace, f"farm|{cliente.casefold()}|{fazenda.casefold()}")
    )

    # Keep the staging path short. Farm/project destinations on Windows are
    # often deeply nested and fdShape filenames are long UUID-based names.
    stage_root = Path(tempfile.mkdtemp(prefix="fsgs3_")).resolve()
    gs3_root = stage_root / "GS3_2630"
    eic_root = gs3_root / folder / "RCD" / "EIC"
    fields_root = eic_root / "Fields"
    fields_root.mkdir(parents=True, exist_ok=True)
    copy_support_files(template_eic, eic_root)

    fddata_templates = _load_fddata_templates()

    setup_fields: list[tuple[str, str, float]] = []
    total_written = 0
    expected_obstacles = 0
    guidance_writer = (
        GS3GuidanceRouter(guidance_plan.options)
        if guidance_plan is not None and guidance_plan.options.any_requested
        else None
    )
    guidance_results = {}

    try:
        for bundle_index, bundle in enumerate(bundles):
            prepared_bundle, area_ha, boundary_reports = _prepare_bundle_boundaries(
                bundle, fddata_templates.keys()
            )
            expected_obstacles += sum(
                1
                for f in prepared_bundle.features
                if f.kind == SpatialKind.LIMITE_INTERNO
                and not _is_headland_edge(f)
                and len(_feature_points(f)) >= 4
            )
            report["fields"][bundle_index]["area_ha"] = area_ha
            report["fields"][bundle_index]["boundaries"] = boundary_reports
            field_erid = bundle.field_erid or str(uuid.uuid4())
            field_erid_clean = field_erid.strip("{}")
            # Fail early instead of creating Fields/IS from qgis-field:...
            field_erid_clean = str(uuid.UUID(field_erid_clean))
            field_dir = fields_root / _field_bucket(field_erid_clean) / field_erid_clean
            guidance_items = None
            if guidance_writer is not None:
                guidance_result = guidance_writer.write_field(
                    guidance_plan.for_field(field_erid_clean),
                    field_dir,
                )
                guidance_results[field_erid_clean] = guidance_result
                guidance_items = guidance_result.xml_items
            count = _write_field_catalogs(
                prepared_bundle,
                field_dir,
                client_erid=client_erid,
                farm_erid=farm_erid,
                cliente=cliente,
                fazenda=fazenda,
                fddata_templates=fddata_templates,
                area_ha=area_ha,
                guidance_items=guidance_items,
            )
            if count > 0:
                setup_fields.append((field_erid_clean, _clean_label(bundle.talhao, "Talhao"), area_ha))
                total_written += count

        if total_written == 0:
            raise ValueError("Nenhuma geometria valida foi serializada para GS3.")

        setup_template = (
            template_eic / "setup.fds"
            if (template_eic / "setup.fds").is_file()
            else template_eic / "setup_template.fds"
        )
        write_merged_setup(
            setup_template,
            eic_root / "setup.fds",
            client_name=cliente,
            client_erid=client_erid,
            farm_name=fazenda,
            farm_erid=farm_erid,
            fields=setup_fields,
        )

        report["serialized"] = _inspect_serialized_gs3(eic_root)
        serialized = report["serialized"]
        if not serialized["setup_fds_generated"]:
            raise RuntimeError("setup.fds nao foi gerado.")
        if serialized["field_folders_generated"] == 0:
            raise RuntimeError("Nenhuma pasta Field funcional foi gerada.")
        if serialized["boundary_files_generated"] == 0:
            raise RuntimeError("Boundary obrigatorio nao foi serializado.")
        if report["included"]["HEADLAND"] and not serialized["headland_items_generated"]:
            raise RuntimeError("HEADLAND detectado, mas nenhum HeadLand foi serializado.")
        if (
            report["included"]["AB_LINE"]
            + report["included"]["AB_CURVE"]
            + report["included"]["GUIDANCE_TRACK"]
            and not serialized["guidance_files_generated"]
        ):
            raise RuntimeError("Guidance detectado, mas nenhum arquivo de orientacao foi serializado.")

        report["preflight"] = validate_gs3_package(
            eic_root,
            expected_fields=len(setup_fields),
            expected_internal_boundaries=expected_obstacles,
        )
        if guidance_writer is not None:
            report["guidance_passes"] = validate_gs3_pass_guidance(
                guidance_plan,
                guidance_results,
                eic_root,
            )
            if report["guidance_passes"]["errors"]:
                raise RuntimeError(guidance_failure_message(report["guidance_passes"]["errors"]))
        from .gs3_rcd_core import collect_gs3_fields

        roundtrip = collect_gs3_fields(gs3_root, unify_farms=False)
        if roundtrip.field_count != len(setup_fields):
            raise RuntimeError(
                "GS3_ROUNDTRIP_FAILED: "
                f"fields lidos={roundtrip.field_count}, esperados={len(setup_fields)}."
            )
        report["roundtrip"] = {
            "field_count": roundtrip.field_count,
            "catalog_count": roundtrip.catalog_count,
        }
        report["verification_state"] = "ROUNDTRIP_TESTED"

        final_root = output_root / "GS3_2630"
        if final_root.exists():
            backup = output_root / f"GS3_2630_anterior_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            final_root.rename(backup)
        shutil.move(str(gs3_root), str(final_root))
        if stage_root.exists():
            stage_root.rmdir()
        report["status"] = "success"
        _write_gs3_debug(output_root, report)
        _write_gs3_text_report(output_root, report)
        return final_root
    except Exception as exc:
        report["status"] = "aborted"
        report["errors"].append(str(exc))
        _write_gs3_debug(output_root, report)
        if stage_root.exists():
            shutil.rmtree(stage_root)
        raise
