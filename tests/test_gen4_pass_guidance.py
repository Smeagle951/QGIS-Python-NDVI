from __future__ import annotations

import contextlib
import itertools
import json
import sys
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "qgis_plugin"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fortsmart_gs3_import import gen4_export  # noqa: E402
from fortsmart_gs3_import import gen4_pass_guidance as gen4_guidance  # noqa: E402
from fortsmart_gs3_import.adapters.deere_dual_writer import DeereDualWriter  # noqa: E402
from fortsmart_gs3_import.adapters.deere_gen4_writer import DeereGen4Writer  # noqa: E402
from fortsmart_gs3_import.adapters.deere_gs3_writer import DeereGS3Writer  # noqa: E402
from fortsmart_gs3_import.gen4_pass_guidance import Gen4PassGuidanceRouter  # noqa: E402
from fortsmart_gs3_import.gs3_pass_guidance import GS3PassGuidanceWriter  # noqa: E402
from fortsmart_gs3_import.gs3_spatial_types import SpatialKind  # noqa: E402
from fortsmart_gs3_import.jd_guidance_plan import (  # noqa: E402
    BORDER_PASS,
    EMPTY_PACKAGE_MESSAGE,
    IGNORED_NAME_MATCH,
    PLANTING_PASS,
    JohnDeereGuidanceOptions,
)
from fortsmart_gs3_import.jd_guidance_report import success_summary  # noqa: E402
from fortsmart_gs3_import.monitor_registry import export_fields  # noqa: E402
from test_gs3_pass_guidance import (  # noqa: E402
    FAZENDA,
    FIELD_A,
    FIELD_B,
    curved_track_lines,
    decoded_tracks,
    feature,
    field_bundle,
    without_passes,
)


NS = f"{{{gen4_export.NS}}}"
GOLDEN_GEN4 = PROJECT_ROOT / "data" / "analysis" / "PL-38L" / "Gen4" / "MasterData.xml"


@contextlib.contextmanager
def deterministic_gen4():
    counter = itertools.count(1)
    with mock.patch.object(gen4_export, "_guid", lambda: str(uuid.UUID(int=next(counter)))), mock.patch.object(
        gen4_export, "_utc_now", lambda: "2026-01-01T00:00:00.000Z"
    ):
        yield


def export_gen4(bundles, root: Path, options: JohnDeereGuidanceOptions | None = None) -> Path:
    with deterministic_gen4():
        return DeereGen4Writer().write(bundles, root, guidance_options=options)


def package_bytes(gen4_dir: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(gen4_dir)): path.read_bytes()
        for path in sorted(gen4_dir.rglob("*"))
        if path.is_file()
    }


def guidance_curves(gen4_dir: Path) -> list[ET.Element]:
    root = ET.parse(gen4_dir / "MasterData.xml").getroot()
    return root.findall(f"{NS}Setup/{NS}Guidance/{NS}Tracks/{NS}AdaptiveCurve")


def gjson_lines(gen4_dir: Path, element: ET.Element) -> list:
    name = element.findtext(f"{NS}Geometry/{NS}FilenameWithExtension")
    data = json.loads((gen4_dir / "SpatialFiles" / name).read_text(encoding="utf-8"))
    return data["geometry"]["coordinates"]


def combined_report(root: Path) -> dict:
    return json.loads((root / "john_deere_guidance_report.json").read_text(encoding="utf-8"))


def fake_name_bundle():
    bundle = field_bundle(planting=0, border=0, operational_noise=False)
    for index in range(3):
        lon = -54.995 + index * 0.001
        bundle.features.append(
            feature(FIELD_A, "TL-12", SpatialKind.CURVA_AB, "", "line",
                    [(lon, -12.001), (lon, -12.009)], "12_Passadas_Plantio")
        )
    return bundle


class Gen4PassExportTest(unittest.TestCase):
    def test_38_gen4_baseline_byte_identical_when_options_off(self):
        with tempfile.TemporaryDirectory(prefix="fs-g4-base-") as temp:
            root = Path(temp)
            reference = package_bytes(export_gen4([without_passes(field_bundle())], root / "ref"))
            for name, options in (
                ("default", None),
                ("unchecked", JohnDeereGuidanceOptions()),
                ("gs3_only", JohnDeereGuidanceOptions(include_planting_passes=True, export_gen4=False)),
            ):
                got = package_bytes(export_gen4([field_bundle()], root / name, options))
                self.assertEqual(got, reference, name)
                self.assertFalse((root / name / "john_deere_guidance_report.json").exists())

    def test_42_gen4_five_planting_passes(self):
        with tempfile.TemporaryDirectory(prefix="fs-g4-5-") as temp:
            root = Path(temp)
            options = JohnDeereGuidanceOptions(include_planting_passes=True, max_planting_passes=5)
            gen4_dir = export_gen4([field_bundle()], root, options)
            curves = guidance_curves(gen4_dir)
            self.assertEqual(len(curves), 1)
            curve = curves[0]
            self.assertEqual(curve.get("Name"), "PLANTIO P1-P5")
            self.assertEqual(curve.get("TaggedEntity"), FIELD_A)
            self.assertEqual(curve.get("Archived"), "false")
            filename = curve.findtext(f"{NS}Geometry/{NS}FilenameWithExtension")
            self.assertEqual(filename, f"AdaptiveCurve{curve.get('StringGuid')}.gjson")
            self.assertEqual(curve.findtext(f"{NS}Geometry/{NS}Path"), "./SpatialFiles/")
            lines = gjson_lines(gen4_dir, curve)
            self.assertEqual(len(lines), 5)
            self.assertTrue(all(point[2:] == [0, 1] for line in lines for point in line))
            self.assertEqual(float(curve.find(f"{NS}ReferenceLongitude").get("Value")), lines[0][0][0])
            self.assertEqual(float(curve.find(f"{NS}ReferenceLatitude").get("Value")), lines[0][0][1])
            self.assertFalse(list(gen4_dir.rglob("*.SpatialCatalog")))
            self.assertFalse(list(gen4_dir.rglob("*.fdShape")))
            report = combined_report(root)
            self.assertEqual(report["status"], "READY_FOR_DEVICE_TEST")
            totals = report["gen4"]["totals"][PLANTING_PASS]
            self.assertEqual((totals["detected"], totals["requested"], totals["serialized"]), (88, 5, 5))
            self.assertEqual(totals["adaptive_curve_files"], 1)
            self.assertNotIn("gs3", report)

    def test_43_gen4_all_planting_and_border_passes(self):
        with tempfile.TemporaryDirectory(prefix="fs-g4-all-") as temp:
            root = Path(temp)
            options = JohnDeereGuidanceOptions(include_planting_passes=True, include_border_passes=True)
            gen4_dir = export_gen4([field_bundle()], root, options)
            curves = {curve.get("Name"): curve for curve in guidance_curves(gen4_dir)}
            self.assertEqual(sorted(curves), ["BORDADURA B1-B2", "PLANTIO P1-P88"])
            self.assertEqual(len(gjson_lines(gen4_dir, curves["PLANTIO P1-P88"])), 88)
            border = gjson_lines(gen4_dir, curves["BORDADURA B1-B2"])
            self.assertEqual(len(border), 2)
            self.assertTrue(all(line[0] == line[-1] for line in border))
            summary = "\n".join(success_summary(combined_report(root)))
            self.assertIn("Gen4\nBoundary: 1\nPlanting passes: 88/88\nBorder passes: 2/2", summary)
            self.assertIn("AdaptiveCurve files: 2\nStatus: READY_FOR_DEVICE_TEST", summary)
            text = (root / "JOHN_DEERE_GUIDANCE_REPORT.txt").read_text(encoding="utf-8")
            self.assertIn("Gen4 Guidance Report", text)
            self.assertIn("adaptive_curve_files = 1", text)

    def test_43b_existing_gen4_objects_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="fs-g4-keep-") as temp:
            root = Path(temp)
            off = export_gen4([field_bundle()], root / "off")
            on = export_gen4(
                [field_bundle()],
                root / "on",
                JohnDeereGuidanceOptions(include_planting_passes=True, include_border_passes=True),
            )
            off_files = package_bytes(off)
            on_files = package_bytes(on)
            new_files = sorted(set(on_files) - set(off_files))
            self.assertEqual(len(new_files), 2)
            self.assertTrue(all(name.startswith("SpatialFiles") and "AdaptiveCurve" in name for name in new_files))
            for name, payload in off_files.items():
                if name != "MasterData.xml":
                    self.assertEqual(on_files[name], payload, name)
            off_root = ET.parse(off / "MasterData.xml").getroot()
            on_root = ET.parse(on / "MasterData.xml").getroot()
            setup = on_root.find(f"{NS}Setup")
            setup.remove(setup.find(f"{NS}Guidance"))
            ET.indent(off_root)
            ET.indent(on_root)
            self.assertEqual(ET.tostring(on_root), ET.tostring(off_root))

    @unittest.skipUnless(GOLDEN_GEN4.is_file(), "MasterData Gen4 golden ausente")
    def test_44_structure_matches_accepted_deere_gen4(self):
        def shape(element):
            return [(child.tag, sorted(child.attrib), shape(child)) for child in element]

        golden_root = ET.parse(GOLDEN_GEN4).getroot()
        golden = next(golden_root.iter(f"{NS}AdaptiveCurve"))
        golden_setup = [child.tag for child in golden_root.find(f"{NS}Setup")]
        with tempfile.TemporaryDirectory(prefix="fs-g4-gold-") as temp:
            gen4_dir = export_gen4(
                [field_bundle()], Path(temp), JohnDeereGuidanceOptions(include_planting_passes=True)
            )
            curve = guidance_curves(gen4_dir)[0]
            self.assertEqual(sorted(curve.attrib), sorted(golden.attrib))
            self.assertEqual(shape(curve), shape(golden))
            for child in ("SignalType", "SpatialProjection/ProjectionType"):
                path = "/".join(f"{NS}{part}" for part in child.split("/"))
                self.assertEqual(curve.find(path).attrib, golden.find(path).attrib)
            setup = [child.tag for child in ET.parse(gen4_dir / "MasterData.xml").getroot().find(f"{NS}Setup")]
            for tags in (golden_setup, setup):
                self.assertLess(tags.index(f"{NS}Guidance"), tags.index(f"{NS}OperationalBoundary"))

    def test_45_dual_same_plan_on_both_platforms(self):
        with tempfile.TemporaryDirectory(prefix="fs-g4-dual-") as temp:
            root = Path(temp)
            options = JohnDeereGuidanceOptions(include_planting_passes=True, max_planting_passes=5)
            with deterministic_gen4():
                DeereDualWriter().write([field_bundle()], root, guidance_options=options)
            gen4_dir = root / "Gen4"
            curve = guidance_curves(gen4_dir)[0]
            gen4_lines = [[(point[0], point[1]) for point in line] for line in gjson_lines(gen4_dir, curve)]
            (folder, element), = curved_track_lines(root / "GS3_2630" / FAZENDA / "RCD" / "EIC")
            gs3_lines = decoded_tracks(folder, element)
            self.assertEqual(len(gen4_lines), 5)
            self.assertEqual(len(gs3_lines), 5)
            for got, want in zip(gs3_lines, gen4_lines):
                self.assertEqual(len(got), len(want))
                for a, b in zip(got, want):
                    self.assertAlmostEqual(a[0], b[0], places=12)
                    self.assertAlmostEqual(a[1], b[1], places=12)
            self.assertFalse(list(gen4_dir.rglob("*.SpatialCatalog")) + list(gen4_dir.rglob("*.fdShape")))
            self.assertFalse(list((root / "GS3_2630").rglob("*.gjson")))
            report = combined_report(root)
            self.assertEqual(report["status"], "READY_FOR_DEVICE_TEST")
            self.assertEqual(report["gs3"]["totals"][PLANTING_PASS]["serialized"], 5)
            self.assertEqual(report["gen4"]["totals"][PLANTING_PASS]["serialized"], 5)
            summary = "\n".join(success_summary(report))
            self.assertIn("GS3\nBoundary: 1\nHeadLand: 1\nPlanting passes: 5/5", summary)
            self.assertIn("CurveTrack files: 1", summary)

    def test_45b_dual_destination_checkboxes(self):
        with tempfile.TemporaryDirectory(prefix="fs-g4-dual-dst-") as temp:
            root = Path(temp)
            options = JohnDeereGuidanceOptions(include_border_passes=True, export_gs3=False)
            with deterministic_gen4():
                DeereDualWriter().write([field_bundle()], root, guidance_options=options)
            self.assertEqual([c.get("Name") for c in guidance_curves(root / "Gen4")], ["BORDADURA B1-B2"])
            self.assertEqual(curved_track_lines(root / "GS3_2630" / FAZENDA / "RCD" / "EIC"), [])
            self.assertEqual(combined_report(root)["platforms"], ["gen4"])

    def test_46_adapters_never_call_each_other(self):
        explode = mock.Mock(side_effect=AssertionError("writer da outra plataforma chamado"))
        with tempfile.TemporaryDirectory(prefix="fs-g4-iso-") as temp:
            root = Path(temp)
            options = JohnDeereGuidanceOptions(include_planting_passes=True, max_planting_passes=5)
            with mock.patch.object(Gen4PassGuidanceRouter, "apply", explode):
                DeereGS3Writer().write([field_bundle()], root / "gs3", guidance_options=options)
            with mock.patch.object(GS3PassGuidanceWriter, "write_field", explode):
                export_gen4([field_bundle()], root / "gen4", options)
            explode.assert_not_called()

    def test_47_field_isolation_multi_field(self):
        tl12 = field_bundle(FIELD_A, "TL-12", -55.0, -12.0, planting=10, border=0)
        tl13 = field_bundle(FIELD_B, "TL-13", -54.95, -12.05, planting=5, border=0)
        with tempfile.TemporaryDirectory(prefix="fs-g4-mf-") as temp:
            gen4_dir = export_gen4(
                [tl12, tl13], Path(temp), JohnDeereGuidanceOptions(include_planting_passes=True)
            )
            by_field = {curve.get("TaggedEntity"): gjson_lines(gen4_dir, curve) for curve in guidance_curves(gen4_dir)}
            self.assertEqual(len(by_field[FIELD_A]), 10)
            self.assertEqual(len(by_field[FIELD_B]), 5)
            self.assertTrue(all(point[0] < -54.98 for line in by_field[FIELD_A] for point in line))
            self.assertTrue(all(point[0] > -54.96 for line in by_field[FIELD_B] for point in line))

    def test_47b_passes_without_exported_field_abort(self):
        tl12 = field_bundle(FIELD_A, "TL-12", planting=3, border=0)
        tl13 = field_bundle(FIELD_B, "TL-13", -54.95, -12.05, planting=3, border=0)
        tl13.features = [item for item in tl13.features if item.extra["canonical_role"] == "WORK_PATH"]
        with tempfile.TemporaryDirectory(prefix="fs-g4-fm-") as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, "JOHN_DEERE_GUIDANCE_FIELD_MISMATCH"):
                export_gen4([tl12, tl13], root, JohnDeereGuidanceOptions(include_planting_passes=True))
            self.assertFalse((root / "Gen4").exists())

    def test_48_fake_role_layer_is_ignored_and_aborts(self):
        options = JohnDeereGuidanceOptions(include_planting_passes=True)
        for name, writer in (("gs3", DeereGS3Writer()), ("gen4", DeereGen4Writer())):
            with tempfile.TemporaryDirectory(prefix=f"fs-g4-fake-{name}-") as temp:
                root = Path(temp)
                with self.assertRaisesRegex(ValueError, IGNORED_NAME_MATCH):
                    writer.write([fake_name_bundle()], root, guidance_options=options)
                self.assertFalse((root / "Gen4").exists())
                self.assertFalse((root / "GS3_2630").exists())
                report = combined_report(root)
                self.assertEqual(report["status"], "ABORTED")
                self.assertEqual(report["preflight"]["fields"][0]["skipped"][IGNORED_NAME_MATCH], 3)

    def test_49_writer_dropping_curves_reports_empty(self):
        with tempfile.TemporaryDirectory(prefix="fs-g4-empty-") as temp:
            root = Path(temp)
            with mock.patch.object(Gen4PassGuidanceRouter, "_write", lambda self, gen4_dir, errors: []):
                with self.assertRaises(RuntimeError) as ctx:
                    export_gen4([field_bundle()], root, JohnDeereGuidanceOptions(include_planting_passes=True))
            message = str(ctx.exception)
            self.assertIn(EMPTY_PACKAGE_MESSAGE, message)
            self.assertIn("GEN4_GUIDANCE_SERIALIZATION_EMPTY", message)
            self.assertFalse((root / "Gen4").exists())
            self.assertEqual(combined_report(root)["status"], "ABORTED")

    def test_49b_orphan_file_and_orphan_reference_abort(self):
        original = gen4_guidance.write_adaptive_curve_tracks_gjson

        def with_orphan(path, tracks):
            original(path, tracks)
            original(path.parent / f"AdaptiveCurve{uuid.uuid4()}.gjson", tracks)

        options = JohnDeereGuidanceOptions(include_planting_passes=True, max_planting_passes=5)
        cases = (
            ("orphan", with_orphan, "GEN4_GUIDANCE_ORPHAN_FILE"),
            ("missing", lambda path, tracks: None, "GEN4_GUIDANCE_ORPHAN_REFERENCE"),
        )
        for name, replacement, code in cases:
            with tempfile.TemporaryDirectory(prefix=f"fs-g4-{name}-") as temp:
                root = Path(temp)
                with mock.patch.object(gen4_guidance, "write_adaptive_curve_tracks_gjson", replacement):
                    with self.assertRaisesRegex(RuntimeError, code):
                        export_gen4([field_bundle()], root, options)
                self.assertFalse((root / "Gen4").exists())

    def test_50_monitor_registry_forwards_options_to_gen4(self):
        with tempfile.TemporaryDirectory(prefix="fs-g4-reg-") as temp:
            with deterministic_gen4():
                gen4_dir = export_fields(
                    [field_bundle(planting=3, border=2)],
                    Path(temp),
                    profile_id="john_deere_gen4",
                    guidance_options=JohnDeereGuidanceOptions(include_border_passes=True),
                )
            self.assertEqual([c.get("Name") for c in guidance_curves(gen4_dir)], ["BORDADURA B1-B2"])
            totals = combined_report(Path(temp))["gen4"]["totals"]
            self.assertEqual(totals[BORDER_PASS]["serialized"], 2)
            self.assertEqual(totals[PLANTING_PASS]["requested"], 0)


if __name__ == "__main__":
    unittest.main()
