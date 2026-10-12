"""FortSmart — LINKLIST.XML ISO 11783-10 v4."""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LinkEntry:
    object_id: str
    link_uuid: str
    designator: str = ""


@dataclass
class LinkListBuilder:
    version_major: str = "4"
    version_minor: str = "2"
    manufacturer: str = "FortSmart"
    software_version: str = "2.1.0"
    entries: list[LinkEntry] = field(default_factory=list)

    def add(self, object_id: str, designator: str = "") -> str:
        link_uuid = str(uuid.uuid4())
        self.entries.append(LinkEntry(object_id=object_id, link_uuid=link_uuid, designator=designator))
        return link_uuid

    def write(self, path: Path) -> None:
        root = ET.Element(
            "ISO11783LinkList",
            {
                "VersionMajor": self.version_major,
                "VersionMinor": self.version_minor,
                "ManagementSoftwareManufacturer": self.manufacturer,
                "ManagementSoftwareVersion": self.software_version,
                "DataTransferOrigin": "1",
            },
        )
        lgp = ET.SubElement(
            root,
            "LGP",
            {
                "A": "LGP1",
                "B": "1",
                "C": "",
                "D": "FortSmart",
                "E": "FortSmartPrecisionAg",
            },
        )
        for entry in self.entries:
            ET.SubElement(
                lgp,
                "LNK",
                {
                    "A": entry.object_id,
                    "B": entry.link_uuid,
                    "C": entry.designator or "urn:epc:id:sgln:0000000.00000.1",
                },
            )
        tree = ET.ElementTree(root)
        ET.indent(tree, space="  ")
        tree.write(path, encoding="utf-8", xml_declaration=True)
