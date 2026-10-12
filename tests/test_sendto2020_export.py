from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "qgis_plugin"))

from fortsmart_gs3_import.adapters.sendto2020_writer import SendTo2020Writer  # noqa: E402
from fortsmart_gs3_import.gs3_spatial_types import (  # noqa: E402
    Gs3FieldBundle,
    Gs3SpatialFeature,
    SpatialKind,
)
from fortsmart_gs3_import.sendto2020_compare import (  # noqa: E402
    compare_sendto2020_packages,
)
from fortsmart_gs3_import.sendto2020_export import _field_code  # noqa: E402
from fortsmart_gs3_import.sendto2020_names import (  # noqa: E402
    MonitorHierarchyEntry,
    load_monitor_hierarchy,
    monitor_name,
    resolve_monitor_names,
)
from fortsmart_gs3_import.usb_copy import (  # noqa: E402
    copy_payload_to_usb,
    detect_payload,
    verify_copy,
)


def polygon_feature(role: str, item: str, offset: float = 0.0) -> Gs3SpatialFeature:
    ring = [
        (-55.0 + offset, -12.0),
        (-55.0 + offset, -12.01),
        (-54.99 + offset, -12.01),
        (-54.99 + offset, -12.0),
        (-55.0 + offset, -12.0),
    ]
    kind = {
        "EXTERNAL_BOUNDARY": SpatialKind.LIMITE,
        "INTERNAL_BOUNDARY": SpatialKind.LIMITE_INTERNO,
        "HEADLAND": SpatialKind.BORDADURA,
    }.get(role, SpatialKind.TRABALHO)
    return Gs3SpatialFeature(
        kind=kind,
        cliente="PROPRIETARIO TESTE",
        fazenda="FAZENDA TESTE",
        talhao="TL-12",
        field_erid="field-12",
        item_erid=item,
        name=role,
        source_file="unit-test",
        catalog_file="qgis",
        geometry="polygon",
        rings=[ring],
        extra={"fortsmart_role": role},
    )


def work_feature(item: str) -> Gs3SpatialFeature:
    return Gs3SpatialFeature(
        kind=SpatialKind.TRABALHO,
        cliente="PROPRIETARIO TESTE",
        fazenda="FAZENDA TESTE",
        talhao="TL-12",
        field_erid="field-12",
        item_erid=item,
        name="Linha operacional",
        source_file="unit-test",
        catalog_file="qgis",
        geometry="line",
        points=[(-55.0, -12.0), (-54.99, -12.01)],
        extra={"fortsmart_role": "FINAL_WORK_PATH"},
    )


def bundle(name: str = "TL-12", offset: float = 0.0) -> Gs3FieldBundle:
    external = polygon_feature("EXTERNAL_BOUNDARY", f"external-{name}", offset)
    external.talhao = name
    external.field_erid = f"field-{name}"
    return Gs3FieldBundle(
        cliente="PROPRIETARIO TESTE",
        fazenda="FAZENDA TESTE",
        talhao=name,
        field_erid=f"field-{name}",
        features=[external],
    )


class SendTo2020ExportTest(unittest.TestCase):
    def test_field_filename_removes_spaces_around_hyphen(self):
        self.assertEqual(_field_code("TL - 12", 1), "TL-12")

    def test_exports_only_external_boundary_and_roundtrip_validates(self):
        source = bundle()
        original_ring = list(source.features[0].rings[0])
        source.features.extend(
            [
                polygon_feature("INTERNAL_BOUNDARY", "internal"),
                polygon_feature("HEADLAND", "headland"),
                *[work_feature(f"work-{index}") for index in range(112)],
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = SendTo2020Writer().write([source], Path(temporary))
            base = result / "TL-12"
            for extension in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".mcd"):
                self.assertTrue(base.with_suffix(extension).is_file(), extension)

            import fiona

            with fiona.open(base.with_suffix(".shp"), "r") as shapefile:
                records = list(shapefile)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["geometry"]["type"], "Polygon")
                self.assertEqual(shapefile.crs.to_epsg(), 4326)
                self.assertEqual(
                    list(shapefile.schema["properties"]),
                    ["Business", "Farm", "Field", "CODE"],
                )
                self.assertEqual(records[0]["properties"]["Field"], "TL-12")

            self.assertEqual(base.with_suffix(".cpg").read_bytes(), b"\xef\xbb\xbf65001")
            self.assertIn("Key='Business'", base.with_suffix(".mcd").read_text(encoding="utf-8-sig"))
            self.assertEqual(len(base.with_suffix(".mcd").read_bytes()), 343)
            self.assertFalse(base.with_suffix(".mcd").read_bytes().endswith(b"\r\n"))
            self.assertEqual(source.features[0].rings[0], original_ring)

            diagnostics = Path(temporary) / "SENDTO2020_DIAGNOSTIC_EXPORT"
            report = json.loads((diagnostics / "SENDTO2020_EXPORT_REPORT.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "READY_FOR_DEVICE_TEST")
            self.assertEqual(report["verification_state"], "ROUNDTRIP_TESTED")
            plan = json.loads((diagnostics / "SENDTO2020_EXPORT_PLAN.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["totals"]["external_boundaries_included"], 1)
            self.assertEqual(plan["totals"]["skipped_by_role"]["FINAL_WORK_PATH"], 112)
            self.assertEqual(plan["totals"]["skipped_by_role"]["HEADLAND"], 1)
            self.assertEqual(plan["totals"]["skipped_by_role"]["INTERNAL_BOUNDARY"], 1)

    def test_multiple_external_boundaries_abort_without_final_package(self):
        source = bundle()
        source.features.append(polygon_feature("EXTERNAL_BOUNDARY", "external-2", 0.002))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "SENDTO2020_BOUNDARY_MULTIPLE_FEATURES"):
                SendTo2020Writer().write([source], root)
            self.assertFalse((root / "SendTo2020").exists())
            self.assertTrue(
                (root / "SENDTO2020_DIAGNOSTIC_EXPORT" / "SENDTO2020_EXPORT_REPORT.json").is_file()
            )

    def test_two_fields_remain_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = SendTo2020Writer().write(
                [bundle("TL-11"), bundle("TL-12", 0.02)],
                Path(temporary),
            )
            import fiona

            for name in ("TL-11", "TL-12"):
                with fiona.open(result / f"{name}.shp", "r") as shapefile:
                    records = list(shapefile)
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0]["properties"]["Field"], name)

            comparison = compare_sendto2020_packages(result, result)
            self.assertTrue(comparison["compatible_structure"])
            self.assertEqual(comparison["generated"]["field_count"], 2)

    def test_non_polygon_external_boundary_is_blocked(self):
        source = bundle()
        source.features[0].geometry = "line"
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "PRECISION2020_BOUNDARY_INVALID_GEOMETRY_TYPE"):
                SendTo2020Writer().write([source], Path(temporary))
            self.assertFalse((Path(temporary) / "SendTo2020").exists())

    def test_empty_external_boundary_is_blocked(self):
        source = bundle()
        source.features[0].rings = []
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "PRECISION2020_BOUNDARY_EMPTY"):
                SendTo2020Writer().write([source], Path(temporary))
            self.assertFalse((Path(temporary) / "SendTo2020").exists())

    def test_safe_boundary_disabled_keeps_previous_flow(self):
        from unittest import mock

        from fortsmart_gs3_import import precision2020_boundary

        with mock.patch.object(precision2020_boundary, "ENABLE_PRECISION2020_SAFE_BOUNDARY", False):
            for mutate in (
                lambda feature: setattr(feature, "geometry", "line"),
                lambda feature: setattr(feature, "rings", []),
            ):
                source = bundle()
                mutate(source.features[0])
                with tempfile.TemporaryDirectory() as temporary:
                    with self.assertRaisesRegex(ValueError, "SENDTO2020_GEOMETRY_TYPE_INVALID"):
                        SendTo2020Writer().write([source], Path(temporary))

    def test_fortsmart_role_precedes_misleading_legacy_kind(self):
        source = bundle()
        misleading = polygon_feature("HEADLAND", "misleading")
        misleading.kind = SpatialKind.LIMITE
        source.features.append(misleading)
        filtered = SendTo2020Writer()._filter_bundles([source])
        self.assertEqual(len(filtered[0].features), 1)
        self.assertEqual(
            filtered[0].features[0].extra["fortsmart_role"],
            "EXTERNAL_BOUNDARY",
        )

    def test_monitor_names_are_normalized(self):
        self.assertEqual(monitor_name("FAZ. FERRADURA"), "FAZ FERRADURA")
        self.assertEqual(monitor_name("TL - 7,8,9"), "TL - 7-8-9")
        self.assertEqual(monitor_name("  Fazenda São João  "), "Fazenda São João")
        self.assertLessEqual(len(monitor_name("X" * 50)), 32)

    def test_names_snap_to_hierarchy_already_on_monitor(self):
        def named(owner: str, farm: str, field: str, offset: float) -> Gs3FieldBundle:
            item = bundle(field, offset)
            item.cliente = owner
            item.fazenda = farm
            return item

        with tempfile.TemporaryDirectory() as reference, tempfile.TemporaryDirectory() as output:
            SendTo2020Writer().write(
                [
                    named("SILVANDRO BARCHET", "FAZ FERRADURA", f"PV-0{index}40L", index * 0.02)
                    for index in range(1, 4)
                ]
                + [named("SILVANDRO BARCHETT", "FAZ. FERRADURA", "TL-OLD", 0.2)],
                Path(reference),
            )
            existing = load_monitor_hierarchy([Path(reference)])
            self.assertEqual(len(existing), 4)

            source = named("SILVANDRO BARCHETT", "FAZ. FERRADURA", "TL - 7,8,9", 0.5)
            result = SendTo2020Writer().write(
                [source],
                Path(output),
                sendto2020_reference=Path(reference) / "SendTo2020",
            )
            import fiona

            with fiona.open(next(result.glob("*.shp")), "r") as shapefile:
                properties = dict(next(iter(shapefile))["properties"])
            self.assertEqual(properties["Business"], "SILVANDRO BARCHET")
            self.assertEqual(properties["Farm"], "FAZ FERRADURA")
            self.assertEqual(properties["Field"], "TL - 7-8-9")
            self.assertEqual(properties["CODE"], "TL - 7-8-9")

    def test_unrelated_owner_is_not_merged(self):
        existing = [MonitorHierarchyEntry("SILVANDRO BARCHET", "FAZ FERRADURA", "PV-01")]
        names = resolve_monitor_names("JOAO PEREIRA", "FAZ FERRADURA", "TL-1", existing)
        self.assertEqual(names.business, "JOAO PEREIRA")
        names = resolve_monitor_names("SILVANDRO BARCHET", "FAZ FERRADURA", "PV-02", existing)
        self.assertEqual(names.field, "PV-02")

    def test_sendto2020_is_detected_copied_to_usb_root_and_verified(self):
        with tempfile.TemporaryDirectory() as export_directory, tempfile.TemporaryDirectory() as usb_directory:
            export_root = Path(export_directory)
            package = SendTo2020Writer().write([bundle()], export_root)
            profile, detected, _hint = detect_payload(export_root)
            self.assertEqual(profile, "sendto2020")
            self.assertEqual(detected, package)

            destination = copy_payload_to_usb(export_root, Path(usb_directory))
            self.assertEqual(destination, Path(usb_directory) / "SendTo2020")
            self.assertTrue((destination / "TL-12.shp").is_file())
            self.assertEqual(verify_copy(destination), [])


if __name__ == "__main__":
    unittest.main()
