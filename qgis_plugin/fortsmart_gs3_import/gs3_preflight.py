"""Strict package gate for John Deere GS3/RCD exports."""

from __future__ import annotations

import re
import struct
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path


FDDATA_TOKENS = (
    b"BoundaryType",
    b"BoundaryNameID",
    b"ParentBoundaryID",
    b"HeadlandLookupID",
    b"SignalType",
)
UUID_ATTRIBUTES = {
    "erid", "clientRef", "farmRef", "eridFieldRef", "eridRef",
    "sourceNode", "uuid", "uuidSourceApp", "uuidSourceAppNode", "uuidSession",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _fdshape_layout(path: Path) -> dict:
    payload = path.read_bytes()
    if len(payload) < 64:
        raise ValueError(f"fdShape truncado: {path.name}")
    if payload[16:18] != b"\x01\x02":
        raise ValueError(f"fdShape marcador invalido: {path.name}")

    sections, reserved, version, primary_count = struct.unpack_from("<QQQQ", payload, 32)
    if sections < 1 or reserved != 0 or version != 1:
        raise ValueError(
            f"fdShape cabecalho incompativel {path.name}: "
            f"sections/reserved/version={sections}/{reserved}/{version}"
        )

    cursor = 64 + primary_count * 20
    if cursor > len(payload):
        raise ValueError(f"fdShape registros truncados: {path.name}")
    counts = [int(primary_count)]
    ids: list[int] = []
    for _ in range(int(sections) - 1):
        if cursor + 16 > len(payload):
            raise ValueError(f"fdShape secao adicional truncada: {path.name}")
        name_id, count = struct.unpack_from("<QQ", payload, cursor)
        cursor += 16
        if name_id < 1:
            raise ValueError(f"fdShape NameID invalido: {path.name}")
        end = cursor + count * 20
        if end > len(payload):
            raise ValueError(f"fdShape pontos adicionais truncados: {path.name}")
        ids.append(int(name_id))
        counts.append(int(count))
        cursor = end
    if cursor != len(payload):
        raise ValueError(
            f"fdShape tamanho incompativel {path.name}: lido={cursor}, bytes={len(payload)}"
        )
    return {
        "sections": int(sections),
        "reserved": int(reserved),
        "version": int(version),
        "vertices": int(primary_count),
        "section_vertices": counts,
        "name_ids": ids,
        "bytes": len(payload),
    }


def _fdshape_vertices(path: Path) -> int:
    return int(_fdshape_layout(path)["vertices"])


def validate_gs3_package(
    eic_root: Path,
    *,
    expected_fields: int,
    expected_internal_boundaries: int,
    boundary_max_vertices: int = 20_000,
) -> dict:
    errors: list[str] = []
    setup_path = eic_root / "setup.fds"
    if not setup_path.is_file():
        errors.append("setup.fds ausente")
    if not (eic_root / "host").is_file() or (eic_root / "host").stat().st_size != 32:
        errors.append("host ausente ou tamanho invalido")
    if not (eic_root / "global.ver").is_file() or (eic_root / "global.ver").stat().st_size != 1024:
        errors.append("global.ver ausente ou tamanho invalido")

    xml_paths = ([setup_path] if setup_path.is_file() else []) + list(eic_root.rglob("*.SpatialCatalog"))
    declared: set[str] = set()
    references: list[tuple[str, str]] = []
    roots = []
    for path in xml_paths:
        text = path.read_text(encoding="utf-8", errors="strict")
        if "{{" in text or "}}" in text or re.search(r"\b(?:fld|itm)-", text, re.IGNORECASE):
            errors.append(f"identificador textual/chaves duplicadas em {path.name}")
        try:
            root = ET.fromstring(text)
            roots.append((path, root))
        except ET.ParseError as exc:
            errors.append(f"XML invalido em {path.name}: {exc}")
            continue
        for element in root.iter():
            for name, value in element.attrib.items():
                local_name = _local(name)
                if local_name not in UUID_ATTRIBUTES:
                    continue
                clean = value.strip("{}")
                try:
                    normalized = str(uuid.UUID(clean))
                except ValueError:
                    errors.append(f"UUID invalido {local_name}={value} em {path.name}")
                    continue
                if local_name == "erid":
                    declared.add(normalized)
                elif local_name.endswith("Ref") or local_name == "eridFieldRef":
                    references.append((normalized, path.name))

    setup_root = next((root for path, root in roots if path == setup_path), None)
    setup_fields = []
    if setup_root is not None:
        names = [_local(element.tag) for element in setup_root.iter()]
        for required in ("Synchronization", "NodeVersions", "Client", "Farm", "Field"):
            if required not in names:
                errors.append(f"setup.fds sem {required}")
        setup_fields = [element for element in setup_root.iter() if _local(element.tag) == "Field"]
        if len(setup_fields) != expected_fields:
            errors.append(f"setup.fds fields={len(setup_fields)}, esperado={expected_fields}")
        for field in setup_fields:
            area = next((item for item in field if _local(item.tag) == "Area"), None)
            if area is None or area.attrib.get("sourceUOM") != "ha" or float(area.attrib.get("value", "0")) <= 0:
                errors.append(f"FIELD_AREA_INVALID: {field.attrib.get('name', '?')}")

    # Generic template references and agricultural references must resolve in
    # the complete package. Zero source-node UUID is a permitted sentinel.
    zero = str(uuid.UUID(int=0))
    for reference, source in references:
        if reference != zero and reference not in declared:
            errors.append(f"referencia UUID orfa {reference} em {source}")

    boundary_shapes = list(eic_root.rglob("Boundary*.fdShape"))
    if len(boundary_shapes) < expected_fields:
        errors.append(f"Boundary files={len(boundary_shapes)}, esperado>={expected_fields}")
    expected_sections: dict[Path, int] = {}
    for catalog_path, root in roots:
        if catalog_path.suffix != ".SpatialCatalog":
            continue
        for element in root.iter():
            if _local(element.tag) != "Boundary":
                continue
            file_name = element.attrib.get("fileName", "")
            if not file_name:
                continue
            associated = sum(
                1
                for child in element
                if _local(child.tag) in {"NameIDPair", "HeadlandLookup"}
            )
            expected_sections[(catalog_path.parent / f"{file_name}.fdShape").resolve()] = 1 + associated

    boundary_stats = []
    for shape in boundary_shapes:
        try:
            layout = _fdshape_layout(shape)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        vertices = int(layout["vertices"])
        if vertices > boundary_max_vertices:
            errors.append(f"Boundary excessivo: {shape.name} com {vertices} vertices")
        expected = expected_sections.get(shape.resolve())
        if expected is not None and layout["sections"] != expected:
            errors.append(
                f"Boundary secoes={layout['sections']}, esperado={expected}: {shape.name}"
            )
        data = shape.with_suffix(".fdData")
        if not data.is_file():
            errors.append(f"fdData ausente para {shape.name}")
            continue
        payload = data.read_bytes()
        missing = [token.decode("ascii") for token in FDDATA_TOKENS if token not in payload]
        if missing:
            errors.append(f"fdData incompleto {data.name}: {', '.join(missing)}")
        boundary_stats.append(
            {
                "file": shape.name,
                "vertices": vertices,
                "sections": layout["sections"],
                "section_vertices": layout["section_vertices"],
                "header": [
                    layout["sections"],
                    layout["reserved"],
                    layout["version"],
                ],
                "bytes": layout["bytes"],
                "fddata_bytes": len(payload),
            }
        )

    for shape in eic_root.rglob("*.fdShape"):
        if shape.name.startswith("Boundary"):
            continue
        try:
            layout = _fdshape_layout(shape)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if layout["sections"] != 1:
            errors.append(f"fdShape linear com secoes extras: {shape.name}")

    catalogs_text = "\n".join(path.read_text(encoding="utf-8") for path in eic_root.rglob("*.SpatialCatalog"))
    internal_pairs = catalogs_text.count("<NameIDPair ")
    if internal_pairs != expected_internal_boundaries:
        errors.append(
            f"internal boundary associations={internal_pairs}, esperado={expected_internal_boundaries}"
        )
    if expected_internal_boundaries and len(boundary_shapes) != expected_fields:
        errors.append("INTERNAL_BOUNDARY foi serializado como Boundary independente")

    report = {
        "status": "READY_FOR_DEVICE_TEST" if not errors else "GS3_PACKAGE_NOT_READY",
        "setup_valid": setup_root is not None,
        "fields": len(setup_fields),
        "boundaries": boundary_stats,
        "internal_boundary_associations": internal_pairs,
        "uuid_count": len(declared),
        "references_checked": len(references),
        "errors": errors,
    }
    if errors:
        raise ValueError("GS3_PACKAGE_NOT_READY:\n" + "\n".join(errors))
    return report

