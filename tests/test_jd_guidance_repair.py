"""Guidance John Deere (GS3 2630 + Gen4/G5): roteamento, CRS, regiao e pacote."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "qgis_plugin"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fortsmart_gs3_import import gs3_guidance_router as router  # noqa: E402
from fortsmart_gs3_import import jd_guidance_plan as jd_plan  # noqa: E402
from fortsmart_gs3_import.gs3_pass_guidance import (  # noqa: E402
    GS3PassGuidanceWriter,
    write_curvetrack_fdshape,
)
from fortsmart_gs3_import.gs3_spatial_types import SpatialKind  # noqa: E402
from fortsmart_gs3_import.jd_guidance_plan import (  # noqa: E402
    BORDER_PASS,
    GUIDANCE_CATEGORY,
    PLANTING_PASS,
    JohnDeereGuidanceOptions,
    build_john_deere_guidance_plan,
    layer_not_collected_errors,
)
from fortsmart_gs3_import.jd_guidance_validators import (  # noqa: E402
    COORDINATES_INVALID,
    GS3_CATALOG_MISSING,
    GS3_FILE_MISSING,
    OUTSIDE_FIELD_REGION,
    SOURCE_CRS_UNDEFINED,
    GuidanceCoordinateValidator,
    validate_guidance_against_boundary,
)
from test_gen4_pass_guidance import (  # noqa: E402
    NS,
    export_gen4,
    gjson_lines,
    guidance_curves,
    package_bytes,
)
from test_gs3_pass_guidance import (  # noqa: E402
    FIELD_A,
    FIELD_B,
    curved_track_lines,
    decoded_tracks,
    eic_root,
    export,
    feature,
    field_bundle,
    without_passes,
)
from test_jd_guidance_collection import dual_export, tl13_bundle  # noqa: E402

UTM_31981 = {"crs": "EPSG:31981", "valid": True, "geographic": False, "transform": "OK"}
NO_CRS = {"crs": "", "valid": False, "geographic": False, "transform": "UNDEFINED"}


def report_text(root: Path) -> str:
    return (root / "JOHN_DEERE_GUIDANCE_REPORT.txt").read_text(encoding="utf-8")


def report_json(root: Path) -> dict:
    return json.loads((root / "john_deere_guidance_report.json").read_text(encoding="utf-8"))


def section(text: str, title: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index(title) + 1
    end = lines.index("", start)
    return lines[start:end]


def with_source_crs(bundle, meta):
    for item in bundle.features:
        if item.extra.get("canonical_role") in ("WORK_PATH", "FIELD_HEADLAND_PATH"):
            item.extra["source_crs"] = dict(meta)
    return bundle


def all_options(**extra) -> JohnDeereGuidanceOptions:
    return JohnDeereGuidanceOptions(include_planting_passes=True, include_border_passes=True, **extra)


class CanonicalPlanTest(unittest.TestCase):
    # 01 — coleta canonica: WORK_PATH=88, FIELD_HEADLAND_PATH=2
    def test_01_canonical_counts_in_report(self):
        with tempfile.TemporaryDirectory(prefix="fs-jdr-01-") as temp:
            root = Path(temp)
            dual_export([tl13_bundle()], root, all_options())
            text = report_text(root)
            self.assertEqual(section(text, "FIELD"), ["TL 13"])
            self.assertEqual(section(text, "CANONICAL"), ["WORK_PATH = 88", "FIELD_HEADLAND_PATH = 2"])

    # 02 — plano: PLANTING_GUIDANCE / BORDER_GUIDANCE
    def test_02_export_plan_categories(self):
        plan = build_john_deere_guidance_plan([tl13_bundle()], all_options())
        field_plan = plan.for_field(FIELD_A)
        self.assertEqual(field_plan.included(PLANTING_PASS), 88)
        self.assertEqual(field_plan.included(BORDER_PASS), 2)
        self.assertEqual(GUIDANCE_CATEGORY, {PLANTING_PASS: "PLANTING_GUIDANCE", BORDER_PASS: "BORDER_GUIDANCE"})
        with tempfile.TemporaryDirectory(prefix="fs-jdr-02-") as temp:
            root = Path(temp)
            dual_export([tl13_bundle()], root, all_options())
            self.assertEqual(
                section(report_text(root), "EXPORT PLAN"),
                ["PLANTING_GUIDANCE = 88", "BORDER_GUIDANCE = 2"],
            )

    def test_02b_guidance_flags_roll_back_each_platform(self):
        options = all_options()
        with mock.patch.object(jd_plan, "ENABLE_GS3_PLANTING_GUIDANCE", False):
            self.assertFalse(options.platform_requested(jd_plan.GS3, PLANTING_PASS))
            self.assertTrue(options.platform_requested(jd_plan.GEN4, PLANTING_PASS))
        with mock.patch.object(jd_plan, "ENABLE_GEN4_BORDER_GUIDANCE", False):
            self.assertFalse(options.platform_requested(jd_plan.GEN4, BORDER_PASS))
            self.assertTrue(options.platform_requested(jd_plan.GS3, BORDER_PASS))


class GS3RoutingTest(unittest.TestCase):
    def test_classification_by_role_not_vertex_count(self):
        two_vertices = feature(FIELD_A, "TL-12", SpatialKind.TRABALHO, "WORK_PATH", "line",
                               [(-55.0, -12.001), (-55.0, -12.009)], "p")
        border = feature(FIELD_A, "TL-12", SpatialKind.TRABALHO, "FIELD_HEADLAND_PATH", "line",
                         [(-55.0, -12.001), (-54.999, -12.002), (-54.998, -12.009)], "b")
        ab_curve = feature(FIELD_A, "TL-12", SpatialKind.CURVA_AB, "AB_CURVE", "line",
                           [(-55.0, -12.001), (-54.999, -12.002), (-54.998, -12.009)], "ab")
        self.assertEqual(router.classify_gs3_guidance(two_vertices), router.CURVE_TRACK)
        self.assertEqual(router.classify_gs3_guidance(border), router.CURVE_TRACK)
        self.assertEqual(router.classify_gs3_guidance(ab_curve), router.AB_CURVE)
        self.assertFalse(router.GS3_ABCURVE_GUIDANCE_PROVEN)

    # 03 — GS3 com 5 passadas
    def test_03_gs3_five_passes(self):
        with tempfile.TemporaryDirectory(prefix="fs-jdr-03-") as temp:
            root = Path(temp)
            output = export([field_bundle()], root, JohnDeereGuidanceOptions(
                include_planting_passes=True, max_planting_passes=5))
            lines = curved_track_lines(eic_root(output))
            self.assertEqual(len(lines), 1)
            folder, element = lines[0]
            self.assertEqual(len(decoded_tracks(folder, element)), 5)
            self.assertFalse(list(eic_root(output).rglob("ABCurve*.fdShape")))
            self.assertEqual(
                section(report_text(root), "GS3"),
                [
                    "requested = 5",
                    "serialized = 5",
                    "ABCurve files = 0",
                    "CurveTrack files = 1",
                    "catalog refs = 1",
                    "coordinates = OK",
                ],
            )
            self.assertEqual(section(report_text(root), "PREFLIGHT"), ["READY_TO_PACKAGE"])
            self.assertEqual(section(report_text(root), "STATUS"), ["READY_FOR_DEVICE_TEST"])

    def test_03b_unproven_abcurve_aborts_instead_of_converting(self):
        with mock.patch.dict(router.GS3_GUIDANCE_TYPE_BY_CATEGORY, {PLANTING_PASS: router.AB_CURVE}):
            with tempfile.TemporaryDirectory(prefix="fs-jdr-03b-") as temp:
                root = Path(temp)
                with self.assertRaisesRegex(ValueError, router.ABCURVE_NOT_PROVEN):
                    export([field_bundle()], root, JohnDeereGuidanceOptions(include_planting_passes=True))
                self.assertFalse((root / "GS3_2630").exists())
                self.assertEqual(report_json(root)["status"], "ABORTED")


class Gen4GuidanceTest(unittest.TestCase):
    # 04 — Gen4 com 5 passadas
    def test_04_gen4_five_passes_masterdata(self):
        with tempfile.TemporaryDirectory(prefix="fs-jdr-04-") as temp:
            root = Path(temp)
            gen4_dir = export_gen4([field_bundle()], root, JohnDeereGuidanceOptions(
                include_planting_passes=True, max_planting_passes=5))
            curves = guidance_curves(gen4_dir)
            self.assertEqual(len(curves), 1)
            curve = curves[0]
            self.assertEqual(curve.get("Name"), "PLANTIO P1-P5")
            self.assertTrue(curve.get("StringGuid"))
            self.assertEqual(curve.get("TaggedEntity"), FIELD_A)
            self.assertTrue(curve.findtext(f"{NS}Geometry/{NS}FilenameWithExtension").startswith("AdaptiveCurve"))
            self.assertEqual(curve.findtext(f"{NS}Geometry/{NS}Path"), "./SpatialFiles/")
            self.assertEqual(len(gjson_lines(gen4_dir, curve)), 5)
            self.assertEqual(
                section(report_text(root), "GEN4"),
                [
                    "requested = 5",
                    "serialized = 5",
                    "AdaptiveCurve files = 1",
                    "MasterData refs = 1",
                    "coordinates = OK",
                ],
            )

    # 05 — bordadura (HEADLAND) nao duplicada pelas passadas de bordadura
    def test_05_border_guidance_does_not_duplicate_headland(self):
        with tempfile.TemporaryDirectory(prefix="fs-jdr-05-") as temp:
            root = Path(temp)
            dual_export([field_bundle()], root, JohnDeereGuidanceOptions(include_border_passes=True))
            eic = root / "GS3_2630" / "Fazenda Teste" / "RCD" / "EIC"
            catalogs = "".join(p.read_text(encoding="utf-8") for p in eic.rglob("*.SpatialCatalog"))
            self.assertEqual(catalogs.count("<HeadLand "), 1)
            self.assertEqual(catalogs.count("<Boundary "), 1)
            names = [element.attrib["name"] for _, element in curved_track_lines(eic)]
            self.assertEqual(names, ["BORDADURA B1-B2"])
            self.assertEqual([c.get("Name") for c in guidance_curves(root / "Gen4")], ["BORDADURA B1-B2"])


class FieldScopeTest(unittest.TestCase):
    # 06 — TL-12 + TL-13 no projeto, exporta so TL-13: nenhuma guia de TL-12
    def test_06_only_selected_field_guidance(self):
        tl13 = field_bundle(FIELD_B, "TL-13", -54.95, -12.05, planting=5, border=0)
        audit = (
            {"layer_name": "12_Passadas_Plantio", "fortsmart_role": "WORK_PATH", "field_id": "TL-12",
             "field_scope": False, "features": 10, "collected": 0, "reason": "field_group_not_selected"},
            {"layer_name": "12_Passadas_Plantio", "fortsmart_role": "WORK_PATH", "field_id": "TL-13",
             "field_scope": True, "features": 5, "collected": 5, "reason": ""},
        )
        options = JohnDeereGuidanceOptions(include_planting_passes=True, collection_audit=audit)
        with tempfile.TemporaryDirectory(prefix="fs-jdr-06-") as temp:
            root = Path(temp)
            dual_export([tl13], root, options)
            curves = guidance_curves(root / "Gen4")
            self.assertEqual({c.get("TaggedEntity") for c in curves}, {FIELD_B})
            eic = root / "GS3_2630" / "Fazenda Teste" / "RCD" / "EIC"
            self.assertEqual({folder.name for folder, _ in curved_track_lines(eic)}, {FIELD_B})
            self.assertTrue(all(
                lon > -54.96 for folder, element in curved_track_lines(eic)
                for track in decoded_tracks(folder, element) for lon, _ in track
            ))

    def test_06b_layer_with_passes_not_collected_aborts(self):
        audit = [{"layer_name": "12_Passadas_Plantio", "fortsmart_role": "WORK_PATH", "field_scope": True,
                  "features": 88, "collected": 0, "reason": "hidden_in_layer_panel"}]
        errors = layer_not_collected_errors(audit, [PLANTING_PASS])
        self.assertEqual(len(errors), 1)
        self.assertIn("JOHN_DEERE_WORK_PATH_NOT_COLLECTED: camada '12_Passadas_Plantio'", errors[0])
        self.assertEqual(layer_not_collected_errors(audit, [BORDER_PASS]), [])
        self.assertEqual(layer_not_collected_errors([dict(audit[0], field_scope=False)], [PLANTING_PASS]), [])
        options = JohnDeereGuidanceOptions(include_planting_passes=True, collection_audit=tuple(audit))
        with tempfile.TemporaryDirectory(prefix="fs-jdr-06b-") as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, "JOHN_DEERE_WORK_PATH_NOT_COLLECTED"):
                dual_export([field_bundle()], root, options)
            self.assertFalse((root / "GS3_2630").exists())
            self.assertFalse((root / "Gen4").exists())
            self.assertEqual(section(report_text(root), "PREFLIGHT"), ["BLOCKED"])

    # 07 — camada renomeada: identidade pela role
    def test_07_renamed_layer_still_exports(self):
        bundle = field_bundle(planting=4, border=0)
        for item in bundle.features:
            item.source_file = "Linhas da plantadeira (renomeada)"
            item.extra.pop("pass_label", None)
        with tempfile.TemporaryDirectory(prefix="fs-jdr-07-") as temp:
            root = Path(temp)
            dual_export([bundle], root, JohnDeereGuidanceOptions(include_planting_passes=True))
            self.assertEqual([c.get("Name") for c in guidance_curves(root / "Gen4")], ["PLANTIO P1-P4"])


class CoordinateTest(unittest.TestCase):
    # 08 — origem EPSG:31981 transformada: Gen4 em coordenadas geograficas
    def test_08_projected_source_exports_geographic(self):
        bundle = with_source_crs(field_bundle(planting=3, border=0), UTM_31981)
        with tempfile.TemporaryDirectory(prefix="fs-jdr-08-") as temp:
            root = Path(temp)
            gen4_dir = export_gen4([bundle], root, JohnDeereGuidanceOptions(include_planting_passes=True))
            for line in gjson_lines(gen4_dir, guidance_curves(gen4_dir)[0]):
                for lon, lat, *_ in line:
                    self.assertTrue(-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0)
                    self.assertAlmostEqual(lon, -55.0, delta=0.02)

    # 09 — CRS indefinido / coordenadas projetadas como lon/lat: aborta
    def test_09_undefined_crs_aborts(self):
        bundle = with_source_crs(field_bundle(planting=3, border=0), NO_CRS)
        with tempfile.TemporaryDirectory(prefix="fs-jdr-09-") as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, SOURCE_CRS_UNDEFINED):
                dual_export([bundle], root, JohnDeereGuidanceOptions(include_planting_passes=True))
            self.assertFalse((root / "GS3_2630").exists())
            self.assertFalse((root / "Gen4").exists())

    def test_09b_projected_coordinates_written_as_lonlat_abort(self):
        bundle = field_bundle(planting=2, border=0)
        bundle.features.append(
            feature(FIELD_A, "TL-12", SpatialKind.TRABALHO, "WORK_PATH", "line",
                    [(712345.0, 8671234.0), (712345.0, 8672234.0)], "utm", pass_number="3")
        )
        with tempfile.TemporaryDirectory(prefix="fs-jdr-09b-") as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, f"{COORDINATES_INVALID}: Field TL-12 pass=P3"):
                export_gen4([bundle], root, JohnDeereGuidanceOptions(include_planting_passes=True))
            self.assertFalse((root / "Gen4").exists())

    # 10 — guia longe do talhao: aborta (nunca recorta, nunca pula)
    def test_10_far_guidance_aborts_outside_field_region(self):
        bundle = field_bundle(planting=3, border=0)
        bundle.features.append(
            feature(FIELD_A, "TL-12", SpatialKind.TRABALHO, "WORK_PATH", "line",
                    [(-54.5, -12.001), (-54.5, -12.009)], "longe", pass_number="9")
        )
        with tempfile.TemporaryDirectory(prefix="fs-jdr-10-") as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, f"{OUTSIDE_FIELD_REGION}: Field TL-12 pass=P9"):
                dual_export([bundle], root, JohnDeereGuidanceOptions(include_planting_passes=True))
            self.assertFalse((root / "GS3_2630").exists())
            self.assertFalse((root / "Gen4").exists())

    def test_10b_validator_units(self):
        bbox = (-55.0, -12.01, -54.99, -12.0)
        inside = [[(-54.995, -12.001), (-54.995, -12.009)]]
        self.assertIsNone(validate_guidance_against_boundary(inside, bbox, 100.0))
        far = [[(-54.5, -12.001), (-54.5, -12.009)]]
        self.assertEqual(validate_guidance_against_boundary(far, bbox, 100.0).code, OUTSIDE_FIELD_REGION)
        validator = GuidanceCoordinateValidator()
        self.assertIsNone(validator.check({"source_crs": UTM_31981}, inside[0]))
        self.assertEqual(validator.check({"source_crs": NO_CRS}, inside[0]).code, SOURCE_CRS_UNDEFINED)
        failed = dict(UTM_31981, transform="FAILED")
        self.assertEqual(validator.check({"source_crs": failed}, inside[0]).code, COORDINATES_INVALID)


class PackageValidatorTest(unittest.TestCase):
    # 11 — AdaptiveCurve orfao: aborta
    def test_11_orphan_adaptive_curve_aborts(self):
        from fortsmart_gs3_import import gen4_pass_guidance as gen4_guidance

        original = gen4_guidance.write_adaptive_curve_tracks_gjson

        def with_orphan(path, tracks):
            original(path, tracks)
            original(path.parent / "AdaptiveCurve00000000-0000-4000-8000-000000000001.gjson", tracks)

        with tempfile.TemporaryDirectory(prefix="fs-jdr-11-") as temp:
            root = Path(temp)
            with mock.patch.object(gen4_guidance, "write_adaptive_curve_tracks_gjson", with_orphan):
                with self.assertRaisesRegex(RuntimeError, "GEN4_GUIDANCE_ORPHAN_FILE"):
                    export_gen4([field_bundle()], root, JohnDeereGuidanceOptions(include_planting_passes=True))
            self.assertFalse((root / "Gen4").exists())

    # 12 — GS3: catalogo sem fdShape e fdShape sem catalogo
    def test_12_gs3_catalog_and_file_mismatch_abort(self):
        original = GS3PassGuidanceWriter.write_field

        def drop_file(self, field_plan, field_dir):
            result = original(self, field_plan, field_dir)
            for obj in result.objects:
                (field_dir / f"{obj.file_name}.fdShape").unlink()
            return result

        def extra_file(self, field_plan, field_dir):
            result = original(self, field_plan, field_dir)
            payload, _ = write_curvetrack_fdshape([[(-55.0, -12.001), (-55.0, -12.002)]])
            (field_dir / "CurveTrack00000000-0000-4000-8000-000000000002.fdShape").write_bytes(payload)
            return result

        for name, patched, code in (("file", drop_file, GS3_FILE_MISSING), ("catalog", extra_file, GS3_CATALOG_MISSING)):
            with tempfile.TemporaryDirectory(prefix=f"fs-jdr-12{name}-") as temp:
                root = Path(temp)
                with mock.patch.object(GS3PassGuidanceWriter, "write_field", patched):
                    with self.assertRaisesRegex(RuntimeError, code):
                        export([field_bundle()], root, JohnDeereGuidanceOptions(include_planting_passes=True))
                self.assertFalse((root / "GS3_2630").exists(), name)


class UnrequestedPassesTest(unittest.TestCase):
    """Passadas no projeto com a opcao desmarcada nunca somem em silencio."""

    def test_preview_states_whether_passes_go_to_package(self):
        counts = jd_plan.john_deere_preview_counts([tl13_bundle()])
        off = jd_plan.guidance_inclusion_lines(counts, None, (jd_plan.GS3, jd_plan.GEN4))
        self.assertEqual(off[0], "Linhas de guia no pacote:")
        self.assertIn("✗ Passadas de plantio: 88 NÃO serão exportadas — marque 'Exportar passadas de plantio'", off[1])
        self.assertIn("✗ Passadas de bordadura: 2 NÃO serão exportadas", off[2])
        on = jd_plan.guidance_inclusion_lines(
            counts, JohnDeereGuidanceOptions(include_planting_passes=True), (jd_plan.GS3, jd_plan.GEN4)
        )
        self.assertEqual(on[1], "  ✓ Passadas de plantio: 88 serão exportadas (GS3 + GEN4)")
        self.assertIn("NÃO serão exportadas", on[2])
        none = jd_plan.john_deere_preview_counts([without_passes(field_bundle())])
        self.assertEqual(jd_plan.guidance_inclusion_lines(none, None, (jd_plan.GS3,)), [])

    def test_unrequested_passes_and_hidden_layers(self):
        counts = jd_plan.john_deere_preview_counts([tl13_bundle()])
        platforms = (jd_plan.GS3, jd_plan.GEN4)
        self.assertEqual(jd_plan.unrequested_passes(counts, None, platforms), [(PLANTING_PASS, 88), (BORDER_PASS, 2)])
        self.assertEqual(jd_plan.unrequested_passes(counts, all_options(), platforms), [])
        hidden = [{"layer_name": "12", "fortsmart_role": "WORK_PATH", "field_scope": True,
                   "features": 115, "collected": 0}]
        detected = jd_plan.detected_pass_counts([without_passes(field_bundle())], hidden)
        self.assertEqual(detected[PLANTING_PASS], 115)
        other_field = [dict(hidden[0], field_scope=False)]
        self.assertEqual(jd_plan.detected_pass_counts([without_passes(field_bundle())], other_field)[PLANTING_PASS], 0)


class BaselineDualTest(unittest.TestCase):
    # 13 — opcoes desligadas ou flag de guidance desligada: pacote identico
    def test_13_baseline_identical(self):
        with tempfile.TemporaryDirectory(prefix="fs-jdr-13-") as temp:
            root = Path(temp)
            reference = package_bytes(export_gen4([without_passes(field_bundle())], root / "ref"))
            got = package_bytes(export_gen4([field_bundle()], root / "off", JohnDeereGuidanceOptions()))
            self.assertEqual(got, reference)
            with mock.patch.object(jd_plan, "ENABLE_GEN4_PLANTING_GUIDANCE", False):
                flag_off = package_bytes(export_gen4(
                    [field_bundle()], root / "flag", JohnDeereGuidanceOptions(include_planting_passes=True)))
            self.assertEqual(flag_off, reference)
            self.assertFalse((root / "flag" / "john_deere_guidance_report.json").exists())

    # 14 — dual: um plano, GS3 e Gen4 no mesmo relatorio
    def test_14_dual_report_sections(self):
        with tempfile.TemporaryDirectory(prefix="fs-jdr-14-") as temp:
            root = Path(temp)
            dual_export([tl13_bundle()], root, all_options(max_planting_passes=5))
            text = report_text(root)
            self.assertEqual(section(text, "GS3")[:2], ["requested = 7", "serialized = 7"])
            self.assertEqual(section(text, "GEN4")[:2], ["requested = 7", "serialized = 7"])
            self.assertEqual(section(text, "STATUS"), ["READY_FOR_DEVICE_TEST"])
            report = report_json(root)
            self.assertEqual(report["preflight_status"], "READY_TO_PACKAGE")
            self.assertEqual(report["gs3"]["gs3_guidance_types"], {PLANTING_PASS: "CURVE_TRACK", BORDER_PASS: "CURVE_TRACK"})
            master = ET.parse(root / "Gen4" / "MasterData.xml").getroot()
            self.assertEqual(len(master.findall(f".//{NS}Tracks/{NS}AdaptiveCurve")), 2)


if __name__ == "__main__":
    unittest.main()
