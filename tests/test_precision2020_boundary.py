from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "qgis_plugin"))

from fortsmart_gs3_import import precision2020_boundary as pb  # noqa: E402
from fortsmart_gs3_import.adapters.sendto2020_writer import SendTo2020Writer  # noqa: E402
from fortsmart_gs3_import.gs3_spatial_types import (  # noqa: E402
    Gs3FieldBundle,
    Gs3SpatialFeature,
    SpatialKind,
)
from fortsmart_gs3_import.sendto2020_export import _write_boundary_geometry  # noqa: E402

PACKAGE_EXTENSIONS = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".mcd")
DIAGNOSTICS = "SENDTO2020_DIAGNOSTIC_EXPORT"


def square(x0: float, y0: float, size: float = 0.01) -> list[tuple[float, float]]:
    return [(x0, y0), (x0, y0 - size), (x0 + size, y0 - size), (x0 + size, y0), (x0, y0)]


def boundary(
    name: str = "TL-12",
    rings: list | None = None,
    extra: dict | None = None,
    field_erid: str | None = None,
) -> Gs3SpatialFeature:
    return Gs3SpatialFeature(
        kind=SpatialKind.LIMITE,
        cliente="PROPRIETARIO TESTE",
        fazenda="FAZENDA TESTE",
        talhao=name,
        field_erid=field_erid or f"field-{name}",
        item_erid=f"external-{name}",
        name="EXTERNAL_BOUNDARY",
        source_file="unit-test",
        catalog_file="qgis",
        geometry="polygon",
        rings=[square(-55.0, -12.0)] if rings is None else rings,
        extra={"fortsmart_role": "EXTERNAL_BOUNDARY", **(extra or {})},
    )


def field_bundle(feature: Gs3SpatialFeature, name: str | None = None) -> Gs3FieldBundle:
    name = name or feature.talhao
    return Gs3FieldBundle(
        cliente="PROPRIETARIO TESTE",
        fazenda="FAZENDA TESTE",
        talhao=name,
        field_erid=f"field-{name}",
        features=[feature],
    )


def utm_metadata(ring: list[tuple[float, float]], crs: str, *, claimed: str | None = None) -> dict:
    """Metadados que o coletor QGIS anota para uma camada em CRS projetado."""
    from pyproj import Transformer
    from shapely.geometry import Polygon

    transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    projected = Polygon([transformer.transform(x, y) for x, y in ring])
    return {
        "crs": claimed or crs,
        "crs_wkt": "",
        "crs_valid": True,
        "crs_geographic": False,
        "transform": "OK",
        "bbox": list(projected.bounds),
        "centroid": [projected.centroid.x, projected.centroid.y],
        "has_z": False,
        "has_m": False,
        "wkb_type": "Polygon",
        "parts": 1,
        "valid": True,
    }


def read_report(root: Path) -> dict:
    return json.loads((root / DIAGNOSTICS / pb.REPORT_JSON).read_text(encoding="utf-8"))


def read_records(path: Path) -> list[dict]:
    import fiona

    with fiona.open(path, "r") as source:
        return [dict(record) for record in source]


class Precision2020BoundaryExportTest(unittest.TestCase):
    def export(self, *bundles: Gs3FieldBundle, root: Path) -> Path:
        return SendTo2020Writer().write(list(bundles), root)

    def assert_aborts(self, code: str, *bundles: Gs3FieldBundle) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        with self.assertRaisesRegex(ValueError, code):
            self.export(*bundles, root=root)
        self.assertFalse((root / "SendTo2020").exists(), "pacote final nao pode existir")
        return root

    # 38 — baseline: limite simples valido gera exatamente os mesmos bytes do fluxo anterior.
    def test_baseline_safe_flow_is_byte_identical_to_previous_flow(self):
        with tempfile.TemporaryDirectory() as safe_dir, tempfile.TemporaryDirectory() as old_dir:
            safe = self.export(field_bundle(boundary()), root=Path(safe_dir))
            with mock.patch.object(pb, "ENABLE_PRECISION2020_SAFE_BOUNDARY", False):
                old = self.export(field_bundle(boundary()), root=Path(old_dir))
            for extension in PACKAGE_EXTENSIONS:
                self.assertEqual(
                    (safe / f"TL-12{extension}").read_bytes(),
                    (old / f"TL-12{extension}").read_bytes(),
                    extension,
                )
            self.assertEqual(sorted(p.name for p in safe.iterdir()), sorted(p.name for p in old.iterdir()))

    # 39 — poligono simples: preflight completo e todos os eventos.
    def test_simple_polygon_preflight_and_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self.export(field_bundle(boundary()), root=root)
            records = read_records(package / "TL-12.shp")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["geometry"]["type"], "Polygon")
            report = read_report(root)
            self.assertEqual(report["status"], "READY_FOR_DEVICE_TEST")
            row = report["fields"][0]
            self.assertEqual(
                row["events"],
                [
                    pb.EVENT_DETECTED,
                    pb.EVENT_PREPARED,
                    pb.EVENT_TRANSFORMED,
                    pb.EVENT_WRITTEN,
                    pb.EVENT_REOPENED,
                    pb.EVENT_VALIDATED,
                ],
            )
            self.assertEqual(row["boundary"]["identity"], "fortsmart_role")
            self.assertEqual(row["postwrite"]["feature_count"], 1)
            self.assertEqual(row["postwrite"]["crs"], "EPSG:4326")
            self.assertEqual(row["postwrite"]["files"]["TL-12.prj"], "OK")
            text = (root / DIAGNOSTICS / pb.REPORT_TEXT).read_text(encoding="utf-8")
            for section in ("PRECISION 20|20 BOUNDARY PREFLIGHT", "Geometry:", "CRS:", "Export bbox:", "POSTWRITE"):
                self.assertIn(section, text)
            success = "\n".join(pb.precision2020_success_lines(report))
            self.assertIn("✓ shapefile reaberto", success)
            self.assertIn("READY_FOR_DEVICE_TEST", success)
            self.assertNotIn("DEVICE_VERIFIED", success)

    # 40 — MultiPolygon: 1 feicao, N partes, nenhuma parte perdida, buraco preservado.
    def test_multipolygon_keeps_all_parts_and_holes(self):
        hole = [(-54.997, -12.003), (-54.993, -12.003), (-54.993, -12.007), (-54.997, -12.007), (-54.997, -12.003)]
        parts = [[square(-55.0, -12.0), hole], [square(-54.98, -12.0)]]
        feature = boundary(rings=[parts[0][0]], extra={"polygon_parts": copy.deepcopy(parts)})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self.export(field_bundle(feature), root=root)
            records = read_records(package / "TL-12.shp")
            self.assertEqual(len(records), 1)
            geometry = records[0]["geometry"]
            self.assertEqual(geometry["type"], "MultiPolygon")
            self.assertEqual(len(geometry["coordinates"]), 2)
            self.assertEqual(sum(len(polygon) - 1 for polygon in geometry["coordinates"]), 1)
            row = read_report(root)["fields"][0]
            self.assertEqual(row["geometry"]["parts"], 2)
            self.assertEqual(row["geometry"]["holes"], 1)
            self.assertEqual(feature.extra["polygon_parts"], parts, "geometria original alterada")

    # 41 — origem UTM: coletor transformou, exportador confere de forma independente.
    def test_utm_source_is_verified(self):
        ring = square(-55.0, -12.0)
        feature = boundary(extra={"source_geometry": utm_metadata(ring, "EPSG:32721")})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.export(field_bundle(feature), root=root)
            crs = read_report(root)["fields"][0]["crs"]
            self.assertEqual(crs["source"], "EPSG:32721")
            self.assertEqual(crs["target"], "EPSG:4326")
            self.assertEqual(crs["transform"], "OK")
            self.assertEqual(crs["verified"], "YES")
            self.assertLess(crs["centroid_error_m"], 1.0)

    def test_utm_with_wrong_zone_aborts_before_package(self):
        ring = square(-55.0, -12.0)
        metadata = utm_metadata(ring, "EPSG:32721", claimed="EPSG:32722")
        self.assert_aborts(pb.CRS_TRANSFORM_FAILED, field_bundle(boundary(extra={"source_geometry": metadata})))

    def test_collector_transform_failure_aborts(self):
        metadata = utm_metadata(square(-55.0, -12.0), "EPSG:32721")
        metadata["transform"] = "FAILED"
        self.assert_aborts(pb.CRS_TRANSFORM_FAILED, field_bundle(boundary(extra={"source_geometry": metadata})))

    # 42 — mesma origem e destino: IDENTITY.
    def test_wgs84_source_is_identity(self):
        metadata = {
            "crs": "EPSG:4326",
            "crs_valid": True,
            "crs_geographic": True,
            "transform": "IDENTITY",
            "bbox": [-55.0, -12.01, -54.99, -12.0],
            "centroid": [-54.995, -12.005],
            "parts": 1,
            "roundtrip_centroid_error_m": 0.0,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.export(field_bundle(boundary(extra={"source_geometry": metadata})), root=root)
            crs = read_report(root)["fields"][0]["crs"]
            self.assertEqual(crs["transform"], "IDENTITY")
            self.assertEqual(crs["verified"], "YES")

    # 43 — CRS indefinido: nunca atribuir EPSG arbitrario.
    def test_undefined_source_crs_aborts(self):
        metadata = {"crs": "", "crs_valid": False, "transform": "UNDEFINED", "parts": 1}
        root = self.assert_aborts(pb.SOURCE_CRS_UNDEFINED, field_bundle(boundary(extra={"source_geometry": metadata})))
        report = read_report(root)
        self.assertEqual(report["status"], "ABORTED")
        self.assertIn(pb.UNDEFINED_CRS_MESSAGE, report["fields"][0]["error"])

    # 44 — latitude impossivel / metros sem transformacao.
    def test_latitude_150_aborts(self):
        ring = [(-55.0, 150.0), (-55.0, 149.99), (-54.99, 149.99), (-54.99, 150.0), (-55.0, 150.0)]
        self.assert_aborts(pb.BOUNDARY_COORDINATES_INVALID, field_bundle(boundary(rings=[ring])))

    def test_untransformed_utm_meters_abort(self):
        ring = [(500000.0, 8670000.0), (500000.0, 8669000.0), (501000.0, 8669000.0), (501000.0, 8670000.0), (500000.0, 8670000.0)]
        root = self.assert_aborts(pb.BOUNDARY_COORDINATES_INVALID, field_bundle(boundary(rings=[ring])))
        self.assertIn("UTM", read_report(root)["fields"][0]["error"])

    # 45 — vazio / degenerado.
    def test_degenerate_ring_is_empty(self):
        ring = [(-55.0, -12.0), (-54.99, -12.0), (-55.0, -12.0)]
        self.assert_aborts(pb.BOUNDARY_EMPTY, field_bundle(boundary(rings=[ring])))

    def test_collinear_ring_is_empty(self):
        ring = [(-55.0, -12.0), (-54.995, -12.0), (-54.99, -12.0), (-54.995, -12.0), (-55.0, -12.0)]
        self.assert_aborts(pb.BOUNDARY_EMPTY, field_bundle(boundary(rings=[ring])))

    # 46 — auto-intersecao: makeValid, revalidado, aviso no relatorio.
    def test_self_intersection_is_made_valid(self):
        bowtie = [(-55.0, -12.0), (-54.99, -12.01), (-54.99, -12.0), (-55.0, -12.01), (-55.0, -12.0)]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self.export(field_bundle(boundary(rings=[bowtie])), root=root)
            from shapely.geometry import shape

            geometry = shape(read_records(package / "TL-12.shp")[0]["geometry"])
            self.assertTrue(geometry.is_valid)
            row = read_report(root)["fields"][0]
            self.assertTrue(row["geometry"]["made_valid"])
            self.assertEqual(row["geometry"]["valid"], "NO")
            self.assertTrue(any("makeValid" in warning for warning in row["warnings"]))

    def test_touching_parts_are_merged_without_area_loss(self):
        parts = [[square(-55.0, -12.0)], [square(-54.99, -12.0)]]
        feature = boundary(rings=[parts[0][0]], extra={"polygon_parts": parts})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self.export(field_bundle(feature), root=root)
            from shapely.geometry import shape

            geometry = shape(read_records(package / "TL-12.shp")[0]["geometry"])
            self.assertEqual(geometry.geom_type, "Polygon")
            self.assertAlmostEqual(geometry.area, 2 * 0.01 * 0.01, places=12)
            row = read_report(root)["fields"][0]
            self.assertLess(row["geometry"]["make_valid_area_change_percent"], 1e-6)
            self.assertTrue(any("unidas pelo makeValid" in warning for warning in row["warnings"]))

    # 47 — Z/M removidos na exportacao.
    def test_z_coordinates_are_exported_2d(self):
        ring = [(x, y, 412.5) for x, y in square(-55.0, -12.0)]
        with tempfile.TemporaryDirectory() as temporary:
            package = self.export(field_bundle(boundary(rings=[ring])), root=Path(temporary))
            from shapely.geometry import shape

            self.assertFalse(shape(read_records(package / "TL-12.shp")[0]["geometry"]).has_z)
            shape_type = int.from_bytes((package / "TL-12.shp").read_bytes()[32:36], "little")
            self.assertEqual(shape_type, 5)

    # 48 — varios talhoes isolados por field_uuid.
    def test_multiple_fields_stay_isolated(self):
        first = boundary("TL-11")
        second = boundary("TL-12", rings=[square(-54.9, -12.0)])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self.export(field_bundle(first), field_bundle(second), root=root)
            for name, x0 in (("TL-11", -55.0), ("TL-12", -54.9)):
                records = read_records(package / f"{name}.shp")
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["properties"]["Field"], name)
                xs = [x for x, _ in records[0]["geometry"]["coordinates"][0]]
                self.assertAlmostEqual(min(xs), x0, places=9)
            self.assertEqual(len(read_report(root)["fields"]), 2)

    def test_boundary_from_another_field_aborts(self):
        feature = boundary("TL-12", field_erid="field-TL-99")
        self.assert_aborts(pb.BOUNDARY_FIELD_MISMATCH, field_bundle(feature, "TL-12"))

    def test_field_far_from_others_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.export(
                field_bundle(boundary("TL-11")),
                field_bundle(boundary("TL-12", rings=[square(-45.0, -12.0)])),
                root=root,
            )
            report = read_report(root)
            warnings = [w for row in report["fields"] for w in row["warnings"]]
            self.assertTrue(any(pb.REGION_MISMATCH in w for w in warnings))
            self.assertEqual(report["status"], "READY_FOR_DEVICE_TEST")

    # Debug fora do pacote final.
    def test_debug_files_stay_outside_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(pb, "PRECISION2020_BOUNDARY_DEBUG", True):
                package = self.export(field_bundle(boundary()), root=root)
            self.assertEqual(
                sorted(path.name for path in package.iterdir()),
                sorted(f"TL-12{extension}" for extension in PACKAGE_EXTENSIONS),
            )
            debug = root / DIAGNOSTICS / "precision2020_debug" / "TL-12"
            for name in ("boundary_source.geojson", "boundary_export.geojson", "boundary_export.shp"):
                self.assertTrue((debug / name).is_file(), name)


class Precision2020ExporterUnitTest(unittest.TestCase):
    def prepared_plan(self, config: pb.Precision2020ExportConfig | None = None):
        exporter = pb.Precision2020BoundaryExporter(config)
        plan = exporter.prepare_field(field_bundle(boundary()), lambda item: pb._role(item) == "EXTERNAL_BOUNDARY")
        return exporter, plan

    def test_export_crs_is_centralized(self):
        self.assertEqual(pb.PRECISION2020_EXPORT_CRS, "EPSG:4326")
        with self.assertRaises(ValueError):
            pb.Precision2020BoundaryExporter(pb.Precision2020ExportConfig(boundary_crs="EPSG:31982"))

    def test_postwrite_detects_shifted_geometry(self):
        exporter, plan = self.prepared_plan()

        def shifted_writer(base_path, geometry, attrs):
            moved = {
                "type": "Polygon",
                "coordinates": [[(x + 0.5, y) for x, y in ring] for ring in geometry["coordinates"]],
            }
            _write_boundary_geometry(base_path, moved, attrs)

        attrs = {"Business": "B", "Farm": "F", "Field": "TL-12", "CODE": "TL-12"}
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, pb.POSTWRITE_VALIDATION_FAILED):
                exporter.write_field(plan, Path(temporary) / "TL-12", attrs, shifted_writer)
        self.assertEqual(plan.result.status, "ABORTED")

    def test_postwrite_detects_missing_shx(self):
        _exporter, plan = self.prepared_plan()
        attrs = {"Business": "B", "Farm": "F", "Field": "TL-12", "CODE": "TL-12"}
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "TL-12"
            _write_boundary_geometry(base, plan.prepared.geojson_geometry(), attrs)
            base.with_suffix(".shx").unlink()
            with self.assertRaisesRegex(ValueError, pb.SHAPEFILE_INCOMPLETE):
                pb.Precision2020BoundaryPostWriteValidator().validate(base, plan.prepared, pb.Precision2020ExportConfig())

    def test_writer_failure_is_reported(self):
        exporter, plan = self.prepared_plan()

        def failing_writer(base_path, geometry, attrs):
            raise OSError("disco cheio")

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, pb.SHAPEFILE_WRITE_FAILED):
                exporter.write_field(plan, Path(temporary) / "TL-12", {}, failing_writer)

    def test_region_check_warns_or_aborts(self):
        far = (-10.0, 30.0, 0.0, 40.0)
        _exporter, plan = self.prepared_plan(pb.Precision2020ExportConfig(expected_region_bbox=far))
        self.assertEqual(plan.result.region["status"], "WARNING")
        with self.assertRaisesRegex(ValueError, pb.REGION_MISMATCH):
            self.prepared_plan(pb.Precision2020ExportConfig(expected_region_bbox=far, region_policy="abort"))
        _exporter, plan = self.prepared_plan(
            pb.Precision2020ExportConfig(expected_region_bbox=(-60.0, -20.0, -50.0, -5.0))
        )
        self.assertEqual(plan.result.region["status"], "PASS")

    def test_without_collector_metadata_crs_is_not_provided(self):
        _exporter, plan = self.prepared_plan()
        self.assertEqual(plan.result.crs["source"], pb.SOURCE_CRS_NOT_PROVIDED)
        self.assertEqual(plan.result.crs["transform"], pb.TRANSFORM_IDENTITY)

    def test_prepare_never_mutates_source_rings(self):
        feature = boundary(rings=[[(x, y, 1.0) for x, y in square(-55.0, -12.0)] + [(-55.0, -12.0, 1.0)]])
        original = copy.deepcopy(feature.rings)
        pb.Precision2020BoundaryExporter().prepare_field(
            field_bundle(feature), lambda item: pb._role(item) == "EXTERNAL_BOUNDARY"
        )
        self.assertEqual(feature.rings, original)


if __name__ == "__main__":
    unittest.main()
