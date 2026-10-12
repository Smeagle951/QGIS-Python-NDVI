"""Behavioral comparison for generated and golden GS3 packages."""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def inspect_gs3_package(root: Path) -> dict:
    root = Path(root)
    setup = next(iter(root.rglob("setup.fds")), None)
    if setup is None:
        raise ValueError(f"Pacote sem setup.fds: {root}")
    eic = setup.parent
    tags = Counter()
    invalid_uuids = []
    for path in [setup, *eic.rglob("*.SpatialCatalog")]:
        xml_root = ET.parse(path).getroot()
        for element in xml_root.iter():
            tags[_local(element.tag)] += 1
            for name, value in element.attrib.items():
                if _local(name) in {"erid", "eridRef", "eridFieldRef", "farmRef", "clientRef"}:
                    try:
                        uuid.UUID(value.strip("{}"))
                    except ValueError:
                        invalid_uuids.append({"file": path.name, "attribute": name, "value": value})
    return {
        "field_directories": len({path.parent for path in eic.rglob("ImportExport.SpatialCatalog")}),
        "boundary_shapes": len(list(eic.rglob("Boundary*.fdShape"))),
        "curve_tracks": len(list(eic.rglob("CurveTrack*.fdShape"))),
        "tags": dict(tags),
        "invalid_uuids": invalid_uuids,
        "has_setup": setup.is_file(),
        "has_host": (eic / "host").is_file(),
        "has_global_ver": (eic / "global.ver").is_file(),
    }


def compare_gs3_packages(generated: Path, golden: Path) -> dict:
    actual = inspect_gs3_package(generated)
    reference = inspect_gs3_package(golden)
    important_tags = (
        "Client", "Farm", "Field", "Area", "Boundary", "HeadLand",
        "ABLine", "CurvedTrackLine", "Synchronization", "NodeVersions",
    )
    return {
        "generated": actual,
        "golden": reference,
        "tag_comparison": {
            tag: {
                "generated": actual["tags"].get(tag, 0),
                "golden": reference["tags"].get(tag, 0),
            }
            for tag in important_tags
        },
        "generated_uuid_valid": not actual["invalid_uuids"],
        "structural_markers_present": all(
            actual[key] for key in ("has_setup", "has_host", "has_global_ver")
        ),
    }

