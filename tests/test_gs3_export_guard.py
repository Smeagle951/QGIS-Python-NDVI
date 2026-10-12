from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "qgis_plugin"))

from fortsmart_gs3_import.gs3_rcd_export import export_gs3_from_bundles  # noqa: E402
from fortsmart_gs3_import.gs3_compare import compare_gs3_packages  # noqa: E402
from fortsmart_gs3_import.gs3_spatial_types import (  # noqa: E402
    Gs3FieldBundle,
    Gs3SpatialFeature,
    SpatialKind,
)


def feature(kind: SpatialKind, role: str, geometry: str, coordinates, name: str):
    points = coordinates if geometry == "line" else []
    rings = [coordinates] if geometry == "polygon" else []
    return Gs3SpatialFeature(
        kind=kind,
        cliente="SILVANDRO BARCHET",
        fazenda="FAZ. FERRADURA",
        talhao="TL-12",
        field_erid=FIELD_ID,
        item_erid=str(uuid.uuid5(uuid.NAMESPACE_DNS, role + name)),
        name=name,
        source_file="unit-test",
        catalog_file="qgis",
        geometry=geometry,
        points=points,
        rings=rings,
        extra={"canonical_role": role},
    )


FIELD_ID = "e5146132-fc03-4aac-b6f0-ee7e17b98c2f"
RING = [(-55.0, -12.0), (-55.0, -12.01), (-54.99, -12.01), (-54.99, -12.0), (-55.0, -12.0)]
WORK_AREA = [
    (-54.999, -12.001), (-54.999, -12.009), (-54.991, -12.009), (-54.991, -12.001), (-54.999, -12.001),
]
OBSTACLE = [
    (-54.9952, -12.0048), (-54.9952, -12.0052), (-54.9948, -12.0052), (-54.9948, -12.0048),
    (-54.9952, -12.0048),
]


def headland_band(name: str = "Bordadura"):
    band = feature(SpatialKind.BORDADURA, "HEADLAND", "polygon", RING, name)
    band.rings = [list(RING), list(reversed(WORK_AREA))]
    return band


def signed_area(ring):
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1])) / 2.0


def read_boundary_sections(payload: bytes):
    sections = struct.unpack_from("<Q", payload, 32)[0]
    primary_count = struct.unpack_from("<Q", payload, 56)[0]
    cursor = 64 + primary_count * 20
    extras = []
    while cursor < len(payload):
        name_id, count = struct.unpack_from("<QQ", payload, cursor)
        cursor += 16
        ring = [struct.unpack_from("<ddf", payload, cursor + i * 20)[:2] for i in range(count)]
        cursor += count * 20
        extras.append((name_id, ring))
    return sections, extras


class Gs3ExportGuardTest(unittest.TestCase):
    def test_complete_package_is_published_only_after_validation(self):
        bundle = Gs3FieldBundle(
            cliente="SILVANDRO BARCHET",
            fazenda="FAZ. FERRADURA",
            talhao="TL-12",
            field_erid=FIELD_ID,
            features=[
                feature(SpatialKind.LIMITE, "EXTERNAL_BOUNDARY", "polygon", RING, "Limite"),
                feature(SpatialKind.LIMITE_INTERNO, "INTERNAL_BOUNDARY", "polygon", OBSTACLE, "LIMITE INTERNO"),
                headland_band(),
                feature(
                    SpatialKind.LINHA_AB,
                    "AB_LINE",
                    "line",
                    [(-55.0, -12.0), (-54.997, -12.003), (-54.99, -12.01)],
                    "Reta A",
                ),
                feature(
                    SpatialKind.CURVA_AB,
                    "AB_CURVE",
                    "line",
                    [(-55.0, -12.0), (-54.995, -12.005), (-54.99, -12.01)],
                    "Curva",
                ),
            ],
        )
        with tempfile.TemporaryDirectory(prefix="fortsmart-gs3-") as temp:
            root = Path(temp)
            output = export_gs3_from_bundles([bundle], root)
            eic = output / "FAZ. FERRADURA" / "RCD" / "EIC"
            self.assertTrue((eic / "setup.fds").is_file())
            self.assertEqual((eic / "host").stat().st_size, 32)
            self.assertNotEqual(set((eic / "host").read_bytes()), {0})
            self.assertEqual((eic / "global.ver").stat().st_size, 1024)
            boundary_shapes = list(eic.rglob("Boundary*.fdShape"))
            self.assertTrue(boundary_shapes)
            boundary_payload = boundary_shapes[0].read_bytes()
            self.assertEqual(
                tuple(struct.unpack_from("<Q", boundary_payload, offset)[0] for offset in (32, 40, 48)),
                (3, 0, 1),
            )
            primary_count = struct.unpack_from("<Q", boundary_payload, 56)[0]
            cursor = 64 + primary_count * 20
            extra_counts = []
            for _ in range(2):
                name_id, count = struct.unpack_from("<QQ", boundary_payload, cursor)
                self.assertEqual(name_id, 1)
                extra_counts.append(count)
                cursor += 16 + count * 20
            self.assertEqual(cursor, len(boundary_payload))
            self.assertEqual(extra_counts, [5, 5])
            self.assertTrue(list(eic.rglob("CurveTrack*.fdShape")))
            catalog_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in eic.rglob("*.SpatialCatalog")
            )
            self.assertIn("<HeadLand ", catalog_text)
            self.assertIn('useDrivenExteriorHeadland="true"', catalog_text)
            self.assertIn('<NameIDPair Name="LIMITE INTERNO" ID="1"', catalog_text)
            self.assertLess(
                catalog_text.index("<NameIDPair "),
                catalog_text.index("<HeadlandLookup "),
            )
            self.assertIn("<ABLine ", catalog_text)
            self.assertIn("<CurvedTrackLine ", catalog_text)
            self.assertNotIn("<ABCurve ", catalog_text)
            self.assertNotIn('erid="{{', catalog_text)

            report = json.loads((root / "fortsmart_gs3_export_debug.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "success")
            self.assertTrue(report["serialized"]["setup_fds_generated"])
            self.assertEqual(report["serialized"]["boundary_files_generated"], 1)
            self.assertEqual(report["serialized"]["headland_items_generated"], 1)
            self.assertEqual(report["serialized"]["internal_boundary_associations"], 1)
            self.assertEqual(report["serialized"]["guidance_files_generated"], 2)
            self.assertTrue(report["fields"][0]["has_external_boundary"])
            self.assertEqual(report["verification_state"], "ROUNDTRIP_TESTED")
            setup_text = (eic / "setup.fds").read_text(encoding="utf-8")
            self.assertIn('sourceUOM="ha"', setup_text)
            self.assertNotIn('value="0" sourceUOM="ac"', setup_text)
            self.assertIn("Synchronization", setup_text)
            self.assertIn("NodeVersions", setup_text)
            comparison = compare_gs3_packages(output, output)
            self.assertTrue(comparison["generated_uuid_valid"])
            self.assertTrue(comparison["structural_markers_present"])
            self.assertTrue((root / "GS3_EXPORT_REPORT.txt").is_file())

    def test_internal_work_area_becomes_driven_headland_not_hole(self):
        bundle = Gs3FieldBundle(
            cliente="SILVANDRO BARCHET",
            fazenda="FAZ. FERRADURA",
            talhao="TL-12",
            field_erid=FIELD_ID,
            features=[
                feature(SpatialKind.LIMITE, "EXTERNAL_BOUNDARY", "polygon", RING, "Limite"),
                feature(
                    SpatialKind.LIMITE_INTERNO, "INTERNAL_BOUNDARY", "polygon", WORK_AREA, "02_limite_interno"
                ),
                headland_band("03_bordadura"),
            ],
        )
        with tempfile.TemporaryDirectory(prefix="fortsmart-gs3-area-") as temp:
            output = export_gs3_from_bundles([bundle], Path(temp))
            eic = output / "FAZ. FERRADURA" / "RCD" / "EIC"
            payload = next(eic.rglob("Boundary*.fdShape")).read_bytes()
            sections, extras = read_boundary_sections(payload)
            self.assertEqual(sections, 2)
            self.assertEqual(len(extras), 1)
            name_id, headland_ring = extras[0]
            self.assertEqual(name_id, 1)
            inner_area = abs(signed_area(headland_ring))
            self.assertAlmostEqual(inner_area, abs(signed_area(WORK_AREA)), places=9)
            self.assertLess(signed_area(headland_ring), 0)

            catalog_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in eic.rglob("*.SpatialCatalog")
            )
            self.assertNotIn("<NameIDPair ", catalog_text)
            self.assertIn("<HeadlandLookup ", catalog_text)
            self.assertIn('useDrivenExteriorHeadland="true"', catalog_text)
            self.assertIn('<vrHeadlandOffset value="0" sourceUOM="ft"', catalog_text)
            fddata = next(eic.rglob("Boundary*.fdData")).read_bytes()
            template = (
                PROJECT_ROOT / "qgis_plugin" / "fortsmart_gs3_import" / "resources" / "gs3_native"
                / "Boundary_0.fdData"
            ).read_bytes()
            self.assertEqual(fddata, template)

    def test_tiny_sliver_is_dropped_only_when_obstacle_count_is_unsupported(self):
        sliver = [
            (-54.9930, -12.0030), (-54.9930, -12.00303), (-54.99297, -12.00303), (-54.99297, -12.0030),
            (-54.9930, -12.0030),
        ]
        bundle = Gs3FieldBundle(
            cliente="SILVANDRO BARCHET",
            fazenda="FAZ. FERRADURA",
            talhao="TL-12",
            field_erid=FIELD_ID,
            features=[
                feature(SpatialKind.LIMITE, "EXTERNAL_BOUNDARY", "polygon", RING, "Limite"),
                feature(SpatialKind.LIMITE_INTERNO, "INTERNAL_BOUNDARY", "polygon", WORK_AREA, "borda"),
                feature(SpatialKind.LIMITE_INTERNO, "INTERNAL_BOUNDARY", "polygon", OBSTACLE, "obstaculo"),
                feature(SpatialKind.LIMITE_INTERNO, "INTERNAL_BOUNDARY", "polygon", sliver, "resto"),
                headland_band("03_bordadura"),
            ],
        )
        with tempfile.TemporaryDirectory(prefix="fortsmart-gs3-sliver-") as temp:
            root = Path(temp)
            output = export_gs3_from_bundles([bundle], root)
            eic = output / "FAZ. FERRADURA" / "RCD" / "EIC"
            catalog_text = "\n".join(path.read_text(encoding="utf-8") for path in eic.rglob("*.SpatialCatalog"))
            self.assertEqual(catalog_text.count("<NameIDPair "), 1)
            self.assertIn('Name="obstaculo"', catalog_text)
            report = json.loads((root / "fortsmart_gs3_export_debug.json").read_text(encoding="utf-8"))
            boundary = report["fields"][0]["boundaries"][0]
            self.assertEqual(boundary["obstacle_count"], 1)
            self.assertEqual([item["name"] for item in boundary["ignored_internal_boundaries"]], ["resto"])
            self.assertLess(boundary["ignored_internal_boundaries"][0]["area_m2"], 100)
            text = (root / "GS3_EXPORT_REPORT.txt").read_text(encoding="utf-8")
            self.assertIn("Internal boundary ignored", text)

        bundle.features = [item for item in bundle.features if item.name != "obstaculo"]
        with tempfile.TemporaryDirectory(prefix="fortsmart-gs3-sliver-kept-") as temp:
            root = Path(temp)
            output = export_gs3_from_bundles([bundle], root)
            eic = output / "FAZ. FERRADURA" / "RCD" / "EIC"
            catalog_text = "\n".join(path.read_text(encoding="utf-8") for path in eic.rglob("*.SpatialCatalog"))
            self.assertIn('<NameIDPair Name="resto"', catalog_text)
            report = json.loads((root / "fortsmart_gs3_export_debug.json").read_text(encoding="utf-8"))
            self.assertEqual(report["fields"][0]["boundaries"][0]["ignored_internal_boundaries"], [])

    def test_missing_boundary_aborts_without_skeleton(self):
        bundle = Gs3FieldBundle(
            cliente="Cliente",
            fazenda="Fazenda",
            talhao="TL-12",
            field_erid=FIELD_ID,
            features=[
                feature(
                    SpatialKind.CURVA_AB,
                    "AB_CURVE",
                    "line",
                    [(-55.0, -12.0), (-54.99, -12.01)],
                    "Curva",
                )
            ],
        )
        with tempfile.TemporaryDirectory(prefix="fortsmart-gs3-empty-") as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, "EXTERNAL_BOUNDARY"):
                export_gs3_from_bundles([bundle], root)
            self.assertFalse((root / "GS3_2630").exists())
            report = json.loads((root / "fortsmart_gs3_export_debug.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "aborted")


if __name__ == "__main__":
    unittest.main()
