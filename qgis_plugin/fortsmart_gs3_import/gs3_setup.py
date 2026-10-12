"""Native setup.fds merge for the John Deere GS3 adapter."""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path


SETUP_NS = "urn:schemas-johndeere-com:RCD:Setup"
BT_NS = "urn:schemas-johndeere-com:BasicTypes"
NAMESPACES = {
    "": SETUP_NS,
    "bt": BT_NS,
    "rep": "urn:schemas-johndeere-com:Representation",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "spatial": "urn:schemas-johndeere-com:SpatialTypes",
    "unit": "urn:schemas-johndeere-com:UnitSystem",
}
for prefix, uri in NAMESPACES.items():
    ET.register_namespace(prefix, uri)


def _tag(local: str) -> str:
    return f"{{{SETUP_NS}}}{local}"


def _local(element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _braced(value: str) -> str:
    return "{" + str(uuid.UUID(str(value).strip("{}"))) + "}"


def validate_setup_template(path: Path) -> ET.ElementTree:
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError) as exc:
        raise ValueError(f"GS3_TEMPLATE_INVALID: {path}: {exc}") from exc
    root = tree.getroot()
    if _local(root) != "SetupFile":
        raise ValueError("GS3_TEMPLATE_INVALID: raiz SetupFile ausente.")
    setup = root.find(_tag("Setup"))
    if setup is None or setup.find(_tag("FileSchemaVersion")) is None:
        raise ValueError("GS3_TEMPLATE_INVALID: Setup/FileSchemaVersion ausente.")
    sync = setup.find(f"{{{BT_NS}}}Synchronization")
    nodes = sync.find(f"{{{BT_NS}}}NodeVersions") if sync is not None else None
    if sync is None or nodes is None:
        raise ValueError("GS3_TEMPLATE_INVALID: Synchronization/NodeVersions ausente.")
    return tree


def write_merged_setup(
    template_path: Path,
    destination: Path,
    *,
    client_name: str,
    client_erid: str,
    farm_name: str,
    farm_erid: str,
    fields: list[tuple[str, str, float]],
) -> None:
    tree = validate_setup_template(template_path)
    root = tree.getroot()
    setup = root.find(_tag("Setup"))
    assert setup is not None

    # Remove agricultural identities from the template while preserving its
    # schema, synchronization and generic machine/implement infrastructure.
    for child in list(setup):
        if _local(child) in {"Participant", "Farm", "Field", "Equipment"}:
            setup.remove(child)

    source_app = root.find(_tag("SourceApp"))
    if source_app is not None:
        source_app.set("uuidSession", _braced(str(uuid.uuid4())))

    common = {"lastModified": _now(), "sourceNode": "{00000000-0000-0000-0000-000000000000}"}
    participant = ET.Element(_tag("Participant"))
    ET.SubElement(
        participant,
        _tag("Client"),
        {**common, "erid": _braced(client_erid), "name": client_name},
    )
    farm = ET.Element(
        _tag("Farm"),
        {
            **common,
            "erid": _braced(farm_erid),
            "name": farm_name,
            "clientRef": _braced(client_erid),
        },
    )
    field_nodes = []
    for field_erid, field_name, area_ha in fields:
        if area_ha <= 0:
            raise ValueError(f"FIELD_AREA_INVALID: {field_name} = {area_ha}")
        field = ET.Element(
            _tag("Field"),
            {
                **common,
                "erid": _braced(field_erid),
                "name": field_name,
                "farmRef": _braced(farm_erid),
            },
        )
        ET.SubElement(
            field,
            _tag("Area"),
            {
                "value": f"{area_ha:.12f}",
                "sourceUOM": "ha",
                "variableRepresentation": "vrReportedFieldArea",
            },
        )
        field_nodes.append(field)

    # Agricultural nodes belong after schema/synchronization and before the
    # generic Products/MachineType infrastructure.
    insertion = 0
    for index, child in enumerate(list(setup)):
        if _local(child) in {"FileSchemaVersion", "Synchronization"}:
            insertion = index + 1
    for node in [participant, farm, *field_nodes]:
        setup.insert(insertion, deepcopy(node))
        insertion += 1

    ET.indent(tree, space="  ")
    destination.parent.mkdir(parents=True, exist_ok=True)
    tree.write(destination, encoding="utf-8", xml_declaration=True)
    validate_setup_output(destination, expected_fields=len(fields))


def validate_setup_output(path: Path, *, expected_fields: int) -> None:
    tree = validate_setup_template(path)
    setup = tree.getroot().find(_tag("Setup"))
    assert setup is not None
    clients = list(setup.iter(_tag("Client")))
    farms = list(setup.iter(_tag("Farm")))
    fields = list(setup.iter(_tag("Field")))
    if len(clients) != 1 or len(farms) != 1 or len(fields) != expected_fields:
        raise ValueError(
            "GS3_TEMPLATE_INVALID: identidade duplicada/incompleta "
            f"(clients={len(clients)}, farms={len(farms)}, fields={len(fields)})."
        )
    for field in fields:
        uuid.UUID(field.attrib["erid"].strip("{}"))
        uuid.UUID(field.attrib["farmRef"].strip("{}"))
        area = field.find(_tag("Area"))
        if area is None or area.attrib.get("sourceUOM") != "ha" or float(area.attrib.get("value", "0")) <= 0:
            raise ValueError("FIELD_AREA_INVALID no setup.fds.")

