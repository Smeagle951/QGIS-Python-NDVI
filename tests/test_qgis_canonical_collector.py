from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "qgis_plugin"))

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsFeature,
    QgsGeometry,
    QgsProject,
    QgsVectorLayer,
)

from fortsmart_gs3_import.adapters.deere_gen4_writer import DeereGen4Writer  # noqa: E402
from fortsmart_gs3_import.export_collector import (  # noqa: E402
    _valid_or_stable_uuid,
    apply_monitor_hierarchy,
    collect_export_bundles,
)
from fortsmart_gs3_import.field_layers import _build_typed_layer  # noqa: E402
from fortsmart_gs3_import.field_group_export import export_name_map, group_export_layers  # noqa: E402
from fortsmart_gs3_import.gs3_spatial_types import Gs3SpatialFeature, SpatialKind  # noqa: E402
from fortsmart_gs3_import.project_batch_exporter import _with_export_field, validate_project  # noqa: E402
from fortsmart_gs3_import.qgis_layer_source import LayerSourceMode  # noqa: E402
from fortsmart_gs3_import.qgis_layer_source import find_layer_by_exact_spec  # noqa: E402


def memory_layer(uri: str, name: str, role: str, wkt: str) -> QgsVectorLayer:
    layer = QgsVectorLayer(uri, name, "memory")
    layer.setCustomProperty("fortsmart_role", role)
    feature = QgsFeature(layer.fields())
    feature.setGeometry(QgsGeometry.fromWkt(wkt))
    layer.dataProvider().addFeature(feature)
    layer.updateExtents()
    return layer


class QgisCanonicalCollectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QgsApplication([], False)
        cls.app.initQgis()

    @classmethod
    def tearDownClass(cls):
        QgsProject.instance().clear()
        cls.app.exitQgis()

    def setUp(self):
        QgsProject.instance().clear()

    def test_renamed_layers_are_collected_by_role_and_filtered_by_adapter(self):
        boundary = memory_layer(
            "Polygon?crs=EPSG:4326",
            "MEU LIMITE RENOMEADO",
            "EXTERNAL_BOUNDARY",
            "POLYGON((0 0, 0 1, 1 1, 1 0, 0 0))",
        )
        ab_line = memory_layer(
            "LineString?crs=EPSG:4326",
            "GUIA PRINCIPAL DO OPERADOR",
            "AB_LINE",
            "LINESTRING(0.1 0.1, 0.9 0.9)",
        )
        work_path = memory_layer(
            "LineString?crs=EPSG:4326",
            "PLANTIO RENOMEADO",
            "WORK_PATH",
            "LINESTRING(0.1 0.2, 0.9 0.8)",
        )
        project = QgsProject.instance()
        project.addMapLayers([boundary, ab_line, work_path])

        bundles = collect_export_bundles(include_loose_layers=True)
        self.assertEqual(len(bundles), 1)
        roles = {feature.extra["canonical_role"] for feature in bundles[0].features}
        self.assertEqual(roles, {"EXTERNAL_BOUNDARY", "AB_LINE", "WORK_PATH"})

        filtered = DeereGen4Writer()._filter_bundles(bundles)
        filtered_roles = {feature.extra["canonical_role"] for feature in filtered[0].features}
        self.assertEqual(filtered_roles, {"EXTERNAL_BOUNDARY", "AB_LINE"})

        monitor_bundles = apply_monitor_hierarchy(
            bundles,
            proprietario="Joao da Silva",
            fazenda="Fazenda Santa Luzia",
        )
        self.assertEqual(monitor_bundles[0].cliente, "Joao da Silva")
        self.assertEqual(monitor_bundles[0].fazenda, "Fazenda Santa Luzia")
        self.assertEqual(monitor_bundles[0].talhao, bundles[0].talhao)
        self.assertTrue(all(f.cliente == "Joao da Silva" for f in monitor_bundles[0].features))

        with tempfile.TemporaryDirectory(prefix="fortsmart-canonical-") as temp_dir:
            output = DeereGen4Writer().write(monitor_bundles, Path(temp_dir))
            self.assertTrue((output / "MasterData.xml").is_file())
            master_data = (output / "MasterData.xml").read_text(encoding="utf-8")
            self.assertIn("Joao da Silva", master_data)
            self.assertIn("Fazenda Santa Luzia", master_data)
            spatial_names = {path.name for path in (output / "SpatialFiles").glob("*.gjson")}
            self.assertTrue(any(name.startswith("Boundary") for name in spatial_names))
            self.assertTrue(any(name.startswith("ABLine") for name in spatial_names))
            self.assertFalse(any(name.startswith("AdaptiveCurve") for name in spatial_names))

    def test_imported_layer_receives_canonical_metadata(self):
        item = Gs3SpatialFeature(
            kind=SpatialKind.LIMITE,
            cliente="Cliente",
            fazenda="Fazenda",
            talhao="Talhao 1",
            field_erid="field-1",
            item_erid="item-1",
            name="Limite",
            source_file="Boundary.gjson",
            catalog_file="MasterData.xml",
            geometry="polygon",
            rings=[[(0, 0), (0, 1), (1, 1), (1, 0), (0, 0)]],
        )
        layer = _build_typed_layer(
            [item],
            SpatialKind.LIMITE,
            "Nome livre",
            boundary_crs(),
        )
        self.assertEqual(layer.customProperty("fortsmart_role"), "EXTERNAL_BOUNDARY")
        self.assertEqual(layer.customProperty("fortsmart_schema_version"), "1")
        feature = next(layer.getFeatures())
        self.assertEqual(feature["fortsmart_role"], "EXTERNAL_BOUNDARY")

    def test_generic_project_exporter_prefers_role_over_display_name(self):
        wrong_by_name = memory_layer(
            "Polygon?crs=EPSG:4326",
            "01_Limite_Externo",
            "HEADLAND",
            "POLYGON((2 2, 2 3, 3 3, 3 2, 2 2))",
        )
        correct_by_role = memory_layer(
            "Polygon?crs=EPSG:4326",
            "Nome alterado pelo usuario",
            "EXTERNAL_BOUNDARY",
            "POLYGON((0 0, 0 1, 1 1, 1 0, 0 0))",
        )
        spec = {
            "order": 1,
            "qgis_name": "01_Limite_Externo",
            "filename": "01_limite_externo",
            "role": "EXTERNAL_BOUNDARY",
        }
        found = find_layer_by_exact_spec(spec, [wrong_by_name, correct_by_role])
        self.assertIs(found, correct_by_role)

    def test_group_names_partition_all_monitor_bundles(self):
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        layers = []
        for name, offset in (("TL - 12", 0), ("TL - 11", 2)):
            group = root.addGroup(name)
            references = group.addGroup("00_REFERENCIAS")
            planning = group.addGroup("30_OPERACAO")
            individual_lines = planning.addGroup("Linhas individuais")
            boundary = memory_layer(
                "Polygon?crs=EPSG:4326", "01_Limite_Externo",
                "EXTERNAL_BOUNDARY",
                f"POLYGON(({offset} 0, {offset} 1, {offset + 1} 1, {offset + 1} 0, {offset} 0))",
            )
            line = memory_layer(
                "LineString?crs=EPSG:4326", "31_Linhas_Finais",
                "FINAL_WORK_PATH",
                f"LINESTRING({offset + 0.1} 0.1, {offset + 0.9} 0.9)",
            )
            for layer, parent in ((boundary, references), (line, individual_lines)):
                project.addMapLayer(layer, False)
                parent.addLayer(layer)
                layers.append(layer)

        groups = group_export_layers(layers, root)
        self.assertEqual([group.label for group in groups], ["TL - 11", "TL - 12"])
        names = export_name_map(groups, {group.key: group.label for group in groups})
        bundles = collect_export_bundles(
            only_selected_layers=True,
            selected_layer_ids=set(names),
            field_names_by_layer_id=names,
        )
        self.assertEqual(len(bundles), 2)
        for bundle in bundles:
            self.assertEqual(len(bundle.features), 2)
            self.assertTrue(all(feature.talhao == bundle.talhao for feature in bundle.features))
            # Display labels never become Windows paths. Every field/item ID
            # passed to a monitor writer is a valid bare UUID.
            import uuid
            uuid.UUID(bundle.field_erid)
            for feature in bundle.features:
                uuid.UUID(feature.item_erid)

        for group in groups:
            validation = validate_project(
                [1, 12], source_mode=LayerSourceMode.AUTO_MAP,
                scope_mode=LayerSourceMode.VISIBLE,
                selected_layer_ids=group.layer_ids,
            )
            self.assertEqual(validation.exportable_count, 2)
            for item in validation.layers:
                self.assertTrue(set(item.matched_layer_ids) <= group.layer_ids)

        clone = _with_export_field(layers[0], "Pivo 12")
        self.assertEqual(next(clone.getFeatures())["talhao"], "Pivo 12")
        self.assertEqual(len(list(layers[0].fields())), 0)

    def test_precision2020_boundary_from_utm_layer_is_verified(self):
        import json

        from fortsmart_gs3_import.adapters.sendto2020_writer import SendTo2020Writer

        boundary = memory_layer(
            "Polygon?crs=EPSG:32721",
            "Limite UTM",
            "EXTERNAL_BOUNDARY",
            "POLYGON((500000 8670000, 500000 8669000, 501000 8669000, 501000 8670000, 500000 8670000))",
        )
        original_wkt = next(boundary.getFeatures()).geometry().asWkt()
        QgsProject.instance().addMapLayer(boundary)
        bundles = collect_export_bundles(include_loose_layers=True)
        feature = bundles[0].features[0]
        source = feature.extra["source_geometry"]
        self.assertEqual(source["crs"], "EPSG:32721")
        self.assertEqual(source["transform"], "OK")
        self.assertLess(source["roundtrip_centroid_error_m"], 0.01)
        self.assertEqual(len(feature.extra["polygon_parts"]), 1)
        self.assertEqual(next(boundary.getFeatures()).geometry().asWkt(), original_wkt)

        with tempfile.TemporaryDirectory(prefix="fortsmart-pp2020-") as temp_dir:
            package = SendTo2020Writer().write(bundles, Path(temp_dir))
            self.assertEqual(len(list(package.glob("*.shp"))), 1)
            report = json.loads(
                (Path(temp_dir) / "SENDTO2020_DIAGNOSTIC_EXPORT" / "PRECISION2020_EXPORT_REPORT.json").read_text(
                    encoding="utf-8"
                )
            )
            crs = report["fields"][0]["crs"]
            self.assertEqual((crs["source"], crs["transform"], crs["verified"]), ("EPSG:32721", "OK", "YES"))
            self.assertLess(crs["centroid_error_m"], 1.0)

    def test_precision2020_layer_without_crs_aborts(self):
        from qgis.core import QgsCoordinateReferenceSystem

        from fortsmart_gs3_import.adapters.sendto2020_writer import SendTo2020Writer

        wkt = "POLYGON((500000 8670000, 500000 8669000, 501000 8669000, 501000 8670000, 500000 8670000))"
        for invalid_crs, code in (
            (True, "PRECISION2020_SOURCE_CRS_UNDEFINED"),
            (False, "PRECISION2020_BOUNDARY_COORDINATES_INVALID"),
        ):
            QgsProject.instance().clear()
            boundary = memory_layer("Polygon", "Limite sem CRS", "EXTERNAL_BOUNDARY", wkt)
            if invalid_crs:
                boundary.setCrs(QgsCoordinateReferenceSystem())
            QgsProject.instance().addMapLayer(boundary)
            bundles = collect_export_bundles(include_loose_layers=True)
            if invalid_crs:
                self.assertEqual(bundles[0].features[0].extra["source_geometry"]["transform"], "UNDEFINED")
            with tempfile.TemporaryDirectory(prefix="fortsmart-pp2020-") as temp_dir:
                with self.assertRaisesRegex(ValueError, code):
                    SendTo2020Writer().write(bundles, Path(temp_dir))
                self.assertFalse((Path(temp_dir) / "SendTo2020").exists())

    def _tl13_project(self):
        from qgis.core import QgsField
        from qgis.PyQt.QtCore import QVariant

        project = QgsProject.instance()
        root = project.layerTreeRoot()
        field_group = root.addGroup("TL 13")
        references = field_group.addGroup("00_REFERENCIAS")
        planning = field_group.addGroup("10_PLANEJAMENTO")

        def add(layer, parent, visible=True):
            project.addMapLayer(layer, False)
            parent.addLayer(layer).setItemVisibilityChecked(visible)
            return layer

        add(memory_layer("Polygon?crs=EPSG:4326", "01_Limite_Externo", "EXTERNAL_BOUNDARY",
                         "POLYGON((-55 -12, -55 -12.01, -54.99 -12.01, -54.99 -12, -55 -12))"), references)
        add(memory_layer("Polygon?crs=EPSG:4326", "02_Limite_Interno", "INTERNAL_BOUNDARY",
                         "POLYGON((-54.996 -12.004, -54.996 -12.005, -54.995 -12.005, -54.995 -12.004, -54.996 -12.004))"),
            references)
        add(memory_layer("Polygon?crs=EPSG:4326", "03_Bordadura", "HEADLAND",
                         "POLYGON((-55 -12, -55 -12.01, -54.99 -12.01, -54.99 -12, -55 -12),"
                         "(-54.999 -12.001, -54.991 -12.001, -54.991 -12.009, -54.999 -12.009, -54.999 -12.001))"),
            references)

        planting = QgsVectorLayer("LineString?crs=EPSG:4326&field=line_no:integer", "12_Passadas_Plantio", "memory")
        planting.setCustomProperty("fortsmart_role", "WORK_PATH")
        planting.setCustomProperty("fortsmart_internal_key", "06_Passadas_Plantio")
        rows = []
        for index in range(88):
            lon = -54.9998 + index * 0.0001
            item = QgsFeature(planting.fields())
            item.setGeometry(QgsGeometry.fromWkt(f"LINESTRING({lon} -12.0003, {lon} -12.0097)"))
            item.setAttributes([index + 1])
            rows.append(item)
        planting.dataProvider().addFeatures(rows)
        add(planting, planning, visible=False)

        border = QgsVectorLayer("LineString?crs=EPSG:4326", "15_Passadas_Bordadura", "memory")
        border.dataProvider().addAttributes(
            [QgsField("pass_no", QVariant.Int), QgsField("role", QVariant.String), QgsField("fortsmart_role", QVariant.String)]
        )
        border.updateFields()
        border.setCustomProperty("fortsmart_role", "FIELD_HEADLAND_PATH")
        border.setCustomProperty("fortsmart_internal_key", "15_Passadas_Bordadura")
        rows = []
        for index in range(2):
            inset = 0.00005 * (index + 1)
            item = QgsFeature(border.fields())
            item.setGeometry(QgsGeometry.fromWkt(
                f"LINESTRING({-55 + inset} {-12 - inset}, {-55 + inset} {-12.01 + inset}, "
                f"{-54.99 - inset} {-12.01 + inset}, {-54.99 - inset} {-12 - inset}, {-55 + inset} {-12 - inset})"
            ))
            item.setAttributes([index + 1, "traffic", None])
            rows.append(item)
        border.dataProvider().addFeatures(rows)
        add(border, planning)
        return project, root, planting

    def _collect_like_export_dialog(self, options, force_passes: bool):
        from fortsmart_gs3_import.export_ui import pass_layer_ids, requested_pass_roles
        from fortsmart_gs3_import.jd_guidance_plan import GEN4, GS3

        project = QgsProject.instance()
        root = project.layerTreeRoot()
        layers = [layer for layer in project.mapLayers().values() if isinstance(layer, QgsVectorLayer)]
        forced = pass_layer_ids(layers, requested_pass_roles(options, (GS3, GEN4))) if force_passes else set()
        candidates = [layer for layer in layers if root.findLayer(layer.id()).isVisible() or layer.id() in forced]
        groups = group_export_layers(candidates, root)
        names = export_name_map(groups, {group.key: group.label for group in groups})
        audit: list[dict] = []
        bundles = collect_export_bundles(
            only_selected_layers=True,
            selected_layer_ids=set(names),
            field_names_by_layer_id=names,
            audit=audit,
        )
        return bundles, audit

    def test_tl13_passes_reach_john_deere_dual_plan(self):
        from dataclasses import replace

        from fortsmart_gs3_import.export_ui import john_deere_preview_text
        from fortsmart_gs3_import.jd_guidance_plan import (
            BORDER_PASS, GEN4, GS3, PLANTING_PASS, JohnDeereGuidanceOptions,
            john_deere_preview_counts, pass_layer_audit, source_pass_counts,
        )
        from fortsmart_gs3_import.monitor_registry import export_fields

        self._tl13_project()
        options = JohnDeereGuidanceOptions(
            include_planting_passes=True, include_border_passes=True, max_planting_passes=5
        )
        bundles, audit = self._collect_like_export_dialog(options, force_passes=True)
        self.assertEqual(len(bundles), 1)
        self.assertEqual(
            john_deere_preview_counts(bundles),
            {"BOUNDARY": 1, "INTERNAL_BOUNDARY": 1, "HEADLAND": 1, PLANTING_PASS: 88, BORDER_PASS: 2},
        )
        by_name = {entry["layer_name"]: entry for entry in audit}
        planting = by_name["12_Passadas_Plantio"]
        self.assertEqual((planting["fortsmart_role"], planting["visible"], planting["collected"]), ("WORK_PATH", False, 88))
        border = by_name["15_Passadas_Bordadura"]
        self.assertEqual((border["fortsmart_role"], border["visible"], border["collected"]), ("FIELD_HEADLAND_PATH", True, 2))
        roles = {f.extra["canonical_role"] for f in bundles[0].features if f.source_file == "15_Passadas_Bordadura"}
        self.assertEqual(roles, {"FIELD_HEADLAND_PATH"})
        preview = john_deere_preview_text(bundles, audit, options, (GS3, GEN4))
        self.assertIn("Passadas de plantio: 88", preview)
        self.assertIn("Passadas de bordadura: 2", preview)
        self.assertIn("✓ Passadas de plantio: 88 serão exportadas (GS3 + GEN4)", preview)
        self.assertNotIn("BLOQUEADA", preview)
        unchecked = john_deere_preview_text(bundles, audit, None, (GS3, GEN4))
        self.assertIn("✗ Passadas de plantio: 88 NÃO serão exportadas", unchecked)

        bundles = apply_monitor_hierarchy(bundles, proprietario="Cliente", fazenda="Fazenda Teste")
        options = replace(options, source_pass_counts=source_pass_counts(audit),
                          collection_audit=tuple(pass_layer_audit(audit)))
        with tempfile.TemporaryDirectory(prefix="fortsmart-jd-tl13-") as temp_dir:
            root = Path(temp_dir)
            export_fields(bundles, root, profile_id="john_deere_dual", guidance_options=options)
            self.assertTrue(list((root / "GS3_2630").rglob("CurveTrack*.fdShape")))
            self.assertTrue(list((root / "Gen4" / "SpatialFiles").glob("AdaptiveCurve*.gjson")))
            report = (root / "JOHN_DEERE_GUIDANCE_REPORT.txt").read_text(encoding="utf-8")
            self.assertIn("12_Passadas_Plantio | internal_key=06_PASSADAS_PLANTIO | role=WORK_PATH", report)

    def test_tl13_hidden_planting_layer_without_request_is_reported(self):
        from fortsmart_gs3_import.export_ui import john_deere_preview_text
        from fortsmart_gs3_import.jd_guidance_plan import GEN4, GS3, PLANTING_PASS, JohnDeereGuidanceOptions
        from fortsmart_gs3_import.jd_guidance_plan import john_deere_preview_counts

        self._tl13_project()
        options = JohnDeereGuidanceOptions(include_planting_passes=True)
        bundles, audit = self._collect_like_export_dialog(options, force_passes=False)
        self.assertEqual(john_deere_preview_counts(bundles)[PLANTING_PASS], 0)
        entry = next(e for e in audit if e["layer_name"] == "12_Passadas_Plantio")
        self.assertEqual(entry["reason"], "hidden_in_layer_panel")
        self.assertEqual((entry["field_id"], entry["field_scope"], entry["feature_count"]), ("TL 13", True, 88))
        preview = john_deere_preview_text(bundles, audit, options, (GS3, GEN4))
        self.assertIn("reason_if_skipped=hidden_in_layer_panel", preview)
        self.assertIn("JOHN_DEERE_GUIDANCE_NOT_COLLECTED", preview)
        self.assertIn("JOHN_DEERE_WORK_PATH_NOT_COLLECTED: camada '12_Passadas_Plantio'", preview)

    def _guidance_field(self, name: str, lon0: float, passes: int, crs: str = "EPSG:4326", invalid_crs=False):
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform

        project = QgsProject.instance()
        group = project.layerTreeRoot().addGroup(name)
        references = group.addGroup("00_REFERENCIAS")
        planning = group.addGroup("10_PLANEJAMENTO")
        boundary = memory_layer(
            "Polygon?crs=EPSG:4326", "01_Limite_Externo", "EXTERNAL_BOUNDARY",
            f"POLYGON(({lon0} -12, {lon0} -12.01, {lon0 + 0.01} -12.01, {lon0 + 0.01} -12, {lon0} -12))",
        )
        project.addMapLayer(boundary, False)
        references.addLayer(boundary)
        planting = QgsVectorLayer(f"LineString?crs={crs}&field=line_no:integer", "12_Passadas_Plantio", "memory")
        planting.setCustomProperty("fortsmart_role", "WORK_PATH")
        to_layer = QgsCoordinateTransform(
            QgsCoordinateReferenceSystem("EPSG:4326"), QgsCoordinateReferenceSystem(crs), project
        )
        originals, rows = [], []
        for index in range(passes):
            lon = lon0 + 0.001 + index * 0.001
            geometry = QgsGeometry.fromWkt(f"LINESTRING({lon} -12.0005, {lon} -12.0095)")
            originals.append([(lon, -12.0005), (lon, -12.0095)])
            geometry.transform(to_layer)
            item = QgsFeature(planting.fields())
            item.setGeometry(geometry)
            item.setAttributes([index + 1])
            rows.append(item)
        planting.dataProvider().addFeatures(rows)
        if invalid_crs:
            planting.setCrs(QgsCoordinateReferenceSystem())
        project.addMapLayer(planting, False)
        planning.addLayer(planting)
        return planting, originals

    def _collect_field(self, label: str):
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        layers = [layer for layer in project.mapLayers().values() if isinstance(layer, QgsVectorLayer)]
        groups = [group for group in group_export_layers(layers, root) if group.label == label]
        names = export_name_map(groups, {group.key: group.label for group in groups})
        audit: list[dict] = []
        bundles = collect_export_bundles(
            only_selected_layers=True,
            selected_layer_ids=set(names),
            field_names_by_layer_id=names,
            audit=audit,
        )
        return apply_monitor_hierarchy(bundles, proprietario="Cliente", fazenda="Fazenda Teste"), audit

    def test_guidance_from_utm_31981_layer_exports_geographic(self):
        import json

        from fortsmart_gs3_import.jd_guidance_plan import JohnDeereGuidanceOptions
        from fortsmart_gs3_import.monitor_registry import export_fields

        planting, originals = self._guidance_field("TL 13", -55.0, 3, crs="EPSG:31981")
        source_wkt = [f.geometry().asWkt() for f in planting.getFeatures()]
        bundles, _ = self._collect_field("TL 13")
        passes = [f for f in bundles[0].features if f.extra.get("canonical_role") == "WORK_PATH"]
        self.assertEqual(passes[0].extra["source_crs"]["crs"], "EPSG:31981")
        self.assertEqual(passes[0].extra["source_crs"]["transform"], "OK")
        with tempfile.TemporaryDirectory(prefix="fortsmart-jd-utm-") as temp_dir:
            root = Path(temp_dir)
            export_fields(bundles, root, profile_id="john_deere_dual",
                          guidance_options=JohnDeereGuidanceOptions(include_planting_passes=True))
            gjson = next((root / "Gen4" / "SpatialFiles").glob("AdaptiveCurve*.gjson"))
            lines = json.loads(gjson.read_text(encoding="utf-8"))["geometry"]["coordinates"]
            self.assertEqual(len(lines), 3)
            for line, original in zip(lines, originals):
                self.assertAlmostEqual(line[0][0], original[0][0], places=6)
                self.assertAlmostEqual(line[0][1], original[0][1], places=6)
                self.assertAlmostEqual(line[-1][1], original[-1][1], places=6)
            text = (root / "JOHN_DEERE_GUIDANCE_REPORT.txt").read_text(encoding="utf-8")
            self.assertIn("coordinates = OK", text)
        self.assertEqual([f.geometry().asWkt() for f in planting.getFeatures()], source_wkt)

    def test_guidance_layer_without_crs_aborts(self):
        from fortsmart_gs3_import.jd_guidance_plan import JohnDeereGuidanceOptions
        from fortsmart_gs3_import.monitor_registry import export_fields

        self._guidance_field("TL 13", -55.0, 3, crs="EPSG:31981", invalid_crs=True)
        bundles, _ = self._collect_field("TL 13")
        passes = [f for f in bundles[0].features if f.extra.get("canonical_role") == "WORK_PATH"]
        self.assertEqual(passes[0].extra["source_crs"]["transform"], "UNDEFINED")
        with tempfile.TemporaryDirectory(prefix="fortsmart-jd-nocrs-") as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(ValueError, "JOHN_DEERE_GUIDANCE_SOURCE_CRS_UNDEFINED"):
                export_fields(bundles, root, profile_id="john_deere_dual",
                              guidance_options=JohnDeereGuidanceOptions(include_planting_passes=True))
            self.assertFalse((root / "Gen4").exists())
            self.assertFalse((root / "GS3_2630").exists())

    def test_multi_field_project_exports_only_selected_field_guidance(self):
        import xml.etree.ElementTree as ET

        from fortsmart_gs3_import.jd_guidance_plan import JohnDeereGuidanceOptions, pass_layer_audit
        from fortsmart_gs3_import.monitor_registry import export_fields

        self._guidance_field("TL 12", -55.0, 4)
        self._guidance_field("TL 13", -54.95, 3)
        bundles, audit = self._collect_field("TL 13")
        self.assertEqual([bundle.talhao for bundle in bundles], ["TL 13"])
        scopes = sorted((e["field_id"], e["field_scope"], e["collected"]) for e in pass_layer_audit(audit))
        self.assertEqual(scopes, [("", False, 0), ("TL 13", True, 3)])
        options = JohnDeereGuidanceOptions(include_planting_passes=True, collection_audit=tuple(pass_layer_audit(audit)))
        with tempfile.TemporaryDirectory(prefix="fortsmart-jd-multi-") as temp_dir:
            root = Path(temp_dir)
            export_fields(bundles, root, profile_id="john_deere_dual", guidance_options=options)
            master = ET.parse(root / "Gen4" / "MasterData.xml").getroot()
            curves = [e for e in master.iter() if e.tag.endswith("}AdaptiveCurve")]
            self.assertEqual(len(curves), 1)
            self.assertEqual(curves[0].get("TaggedEntity"), bundles[0].field_erid)
            self.assertEqual(curves[0].get("Name"), "PLANTIO P1-P3")

    def _named_layers_field(self, name: str, lon0: float, final_lines: int):
        """Projeto do usuario: camadas so com nome (sem fortsmart_role), em UTM 31981."""
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform

        project = QgsProject.instance()
        group = project.layerTreeRoot().addGroup(name)
        to_utm = QgsCoordinateTransform(
            QgsCoordinateReferenceSystem("EPSG:4326"), QgsCoordinateReferenceSystem("EPSG:31981"), project
        )

        def add(geometry_type: str, layer_name: str, wkts: list[str]):
            layer = QgsVectorLayer(f"{geometry_type}?crs=EPSG:31981", layer_name, "memory")
            rows = []
            for wkt in wkts:
                geometry = QgsGeometry.fromWkt(wkt)
                geometry.transform(to_utm)
                item = QgsFeature(layer.fields())
                item.setGeometry(geometry)
                rows.append(item)
            layer.dataProvider().addFeatures(rows)
            layer.updateExtents()
            project.addMapLayer(layer, False)
            group.addLayer(layer)
            return layer

        def ring(inset: float) -> str:
            a, b = lon0 + inset, lon0 + 0.01 - inset
            return f"{a} {-12 - inset}, {a} {-12.01 + inset}, {b} {-12.01 + inset}, {b} {-12 - inset}, {a} {-12 - inset}"

        add("Polygon", "03_bordadura", [f"POLYGON(({ring(0)}), ({ring(0.0008)}))"])
        add("Polygon", "01_limite_externo", [f"POLYGON(({ring(0)}))"])
        add("LineString", "31_linhas_finais", [
            f"LINESTRING({lon0 + 0.001 + i * 0.001} -12.0009, {lon0 + 0.001 + i * 0.001} -12.0091)"
            for i in range(final_lines)
        ])
        add("LineString", "15_passadas_bordadura", [f"LINESTRING({ring(0.0004)})"])
        add("LineString", "marcacao_10_epsg_31981", [f"LINESTRING({lon0 + 0.002} -12.002, {lon0 + 0.003} -12.002)"])
        add("LineString", "pivo_marcacao", [f"LINESTRING({ring(0.0002)})"])
        add("LineString", "limite", [f"LINESTRING({ring(0.0001)})"])
        add("Polygon", "02_limite_interno", [
            f"POLYGON(({lon0 + 0.004} -12.004, {lon0 + 0.004} -12.005, {lon0 + 0.005} -12.005, "
            f"{lon0 + 0.005} -12.004, {lon0 + 0.004} -12.004))"
        ])

    def test_layers_named_freely_export_final_lines_as_planting_guidance(self):
        import xml.etree.ElementTree as ET
        from dataclasses import replace

        from fortsmart_gs3_import.export_ui import john_deere_preview_text
        from fortsmart_gs3_import.jd_guidance_plan import (
            BORDER_PASS, GEN4, GS3, PLANTING_PASS, JohnDeereGuidanceOptions,
            john_deere_preview_counts, pass_layer_audit, source_pass_counts,
        )
        from fortsmart_gs3_import.monitor_registry import export_fields

        self._named_layers_field("TH-11-40L", -55.0, 6)
        bundles, audit = self._collect_field("TH-11-40L")
        roles = {entry["layer_name"]: (entry["fortsmart_role"], entry["resolution"]) for entry in audit}
        self.assertEqual(roles["31_linhas_finais"], ("FINAL_WORK_PATH", "legacy_alias"))
        self.assertEqual(roles["15_passadas_bordadura"], ("FIELD_HEADLAND_PATH", "legacy_alias"))
        self.assertEqual(roles["marcacao_10_epsg_31981"], ("MARKER", "legacy_alias"))
        self.assertEqual(roles["pivo_marcacao"], ("MARKER", "legacy_alias"))
        line_limit = next(entry for entry in audit if entry["layer_name"] == "limite")
        self.assertIn("geometry_conflict", line_limit["reason"])
        boundaries = [f for f in bundles[0].features if f.kind == SpatialKind.LIMITE]
        self.assertEqual([f.geometry for f in boundaries], ["polygon"])
        self.assertEqual(roles["03_bordadura"][0], "HEADLAND")
        self.assertEqual(roles["02_limite_interno"][0], "INTERNAL_BOUNDARY")
        counts = john_deere_preview_counts(bundles)
        self.assertEqual((counts[PLANTING_PASS], counts[BORDER_PASS]), (6, 1))

        options = JohnDeereGuidanceOptions(include_planting_passes=True, include_border_passes=True)
        preview = john_deere_preview_text(bundles, audit, options, (GS3, GEN4))
        self.assertNotIn("BLOQUEADA", preview)
        self.assertIn("TH-11-40L: 6 linha(s) finais usadas como passadas de plantio", preview)

        options = replace(options, source_pass_counts=source_pass_counts(audit),
                          collection_audit=tuple(pass_layer_audit(audit)))
        with tempfile.TemporaryDirectory(prefix="fortsmart-jd-names-") as temp_dir:
            root = Path(temp_dir)
            export_fields(bundles, root, profile_id="john_deere_dual", guidance_options=options)
            master = ET.parse(root / "Gen4" / "MasterData.xml").getroot()
            names = sorted(e.get("Name") for e in master.iter() if e.tag.endswith("}AdaptiveCurve"))
            self.assertEqual(names, ["B1", "PLANTIO P1-P6"])
            self.assertTrue(list((root / "GS3_2630").rglob("CurveTrack*.fdShape")))

    def test_import_splits_headland_area_from_border_passes_and_draws_ab_points(self):
        from qgis.core import QgsCoordinateReferenceSystem

        from fortsmart_gs3_import.field_layers import add_organized_field_layers
        from fortsmart_gs3_import.gs3_spatial_types import Gs3FieldBundle

        def item(kind, geometry, points, role="", name="item"):
            return Gs3SpatialFeature(
                kind=kind, cliente="C", fazenda="FAZ", talhao="TH-14", field_erid="f", item_erid=name,
                name=name, source_file="s", catalog_file="c", geometry=geometry,
                points=points, extra={"canonical_role": role} if role else {},
            )

        square = [(-54.0, -15.0), (-54.0, -15.01), (-53.99, -15.01), (-53.99, -15.0), (-54.0, -15.0)]
        bundle = Gs3FieldBundle(cliente="C", fazenda="FAZ", talhao="TH-14", field_erid="f", features=[
            item(SpatialKind.BORDADURA, "polygon", square, name="03_bordadura"),
            item(SpatialKind.BORDADURA, "line", square[:3], "FIELD_HEADLAND_PATH", "B1 1/2"),
            item(SpatialKind.BORDADURA, "line", square[2:], "FIELD_HEADLAND_PATH", "B1 2/2"),
            item(SpatialKind.LINHA_AB, "line", [(-53.995, -15.005), (-53.9949, -15.005)], name="Reta AB"),
        ])
        add_organized_field_layers([bundle], QgsCoordinateReferenceSystem("EPSG:4326"))
        group = QgsProject.instance().layerTreeRoot().findGroup("FortSmart GS3").findGroup("FAZ").findGroup("TH-14")
        layers = {node.name(): node.layer() for node in group.findLayers()}
        self.assertEqual(sorted(layers), ["Bordadura", "Linha AB", "Passadas de bordadura (2)"])
        self.assertEqual(layers["Bordadura"].featureCount(), 1)
        border = layers["Passadas de bordadura (2)"]
        self.assertEqual((border.featureCount(), border.customProperty("fortsmart_role")), (2, "FIELD_HEADLAND_PATH"))
        ab = layers["Linha AB"]
        self.assertEqual(ab.featureCount(), 1)
        symbol_layers = ab.renderer().symbol().symbolLayers()
        self.assertEqual([s.layerType() for s in symbol_layers],
                         ["GeometryGenerator", "SimpleLine", "MarkerLine", "MarkerLine"])
        points = [p for p in ab.getFeature(next(ab.getFeatures()).id()).geometry().asPolyline()]
        self.assertEqual(len(points), 2)

    def test_legacy_textual_erids_become_stable_valid_uuids(self):
        import uuid

        first = _valid_or_stable_uuid("qgis-field:bordadura", "qgis-field", "TL - 12")
        second = _valid_or_stable_uuid("qgis-field:bordadura", "qgis-field", "TL - 12")
        self.assertEqual(first, second)
        self.assertEqual(str(uuid.UUID(first)), first)
        self.assertNotIn(":", first)



def boundary_crs():
    from qgis.core import QgsCoordinateReferenceSystem

    return QgsCoordinateReferenceSystem("EPSG:4326")


if __name__ == "__main__":
    unittest.main()
