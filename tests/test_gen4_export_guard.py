from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "qgis_plugin"))

from fortsmart_gs3_import.gen4_export import export_gen4  # noqa: E402
from fortsmart_gs3_import.gs3_spatial_types import (  # noqa: E402
    Gs3FieldBundle,
    Gs3SpatialFeature,
    SpatialKind,
)


def polygon_feature(kind: SpatialKind, role: str, ring, name: str):
    return Gs3SpatialFeature(
        kind=kind,
        cliente="Cliente",
        fazenda="Fazenda",
        talhao="TL-12",
        field_erid=str(uuid.uuid4()),
        item_erid=str(uuid.uuid4()),
        name=name,
        source_file="unit-test",
        catalog_file="qgis",
        geometry="polygon",
        rings=[ring],
        extra={"canonical_role": role},
    )


class Gen4ExportGuardTest(unittest.TestCase):
    def test_only_simplified_external_boundary_is_exported(self):
        vertices = 12_500
        ring = [
            (
                -54.47 + 0.01 * math.cos(2 * math.pi * index / vertices),
                -15.38 + 0.01 * math.sin(2 * math.pi * index / vertices),
            )
            for index in range(vertices)
        ]
        ring.append(ring[0])
        internal = [
            (-54.4705, -15.3805),
            (-54.4705, -15.3802),
            (-54.4702, -15.3802),
            (-54.4702, -15.3805),
            (-54.4705, -15.3805),
        ]
        bundle = Gs3FieldBundle(
            cliente="Cliente",
            fazenda="Fazenda",
            talhao="TL-12",
            field_erid=str(uuid.uuid4()),
            features=[
                polygon_feature(SpatialKind.LIMITE, "EXTERNAL_BOUNDARY", ring, "Limite"),
                polygon_feature(
                    SpatialKind.LIMITE_INTERNO,
                    "INTERNAL_BOUNDARY",
                    internal,
                    "Limite interno",
                ),
                polygon_feature(SpatialKind.BORDADURA, "HEADLAND", ring, "Bordadura"),
            ],
        )

        with tempfile.TemporaryDirectory(prefix="fortsmart-gen4-") as temp:
            root = Path(temp)
            stale = root / "Gen4" / "SpatialFiles"
            stale.mkdir(parents=True)
            (stale / "Boundary-stale.gjson").write_text("{}", encoding="utf-8")

            output = export_gen4([bundle], root)
            files = list((output / "SpatialFiles").glob("Boundary*.gjson"))
            self.assertEqual(len(files), 1)
            self.assertFalse((output / "SpatialFiles" / "Boundary-stale.gjson").exists())
            self.assertLess(files[0].stat().st_size, 5 * 1024 * 1024)

            payload = json.loads(files[0].read_text(encoding="utf-8"))
            exported_ring = payload["features"][0]["geometry"]["coordinates"][0]
            self.assertLess(len(exported_ring), vertices)

            master = ET.parse(output / "MasterData.xml").getroot()
            tags = [element.tag.rsplit("}", 1)[-1] for element in master.iter()]
            self.assertEqual(tags.count("OperationalBoundary"), 1)


if __name__ == "__main__":
    unittest.main()
