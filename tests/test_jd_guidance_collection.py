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

from fortsmart_gs3_import.adapters import deere_dual_writer  # noqa: E402
from fortsmart_gs3_import.export_collector import summarize_text  # noqa: E402
from fortsmart_gs3_import.gs3_spatial_types import SpatialKind  # noqa: E402
from fortsmart_gs3_import.jd_guidance_plan import (  # noqa: E402
    BORDER_PASS,
    BOUNDARY,
    GEN4,
    GS3,
    HEADLAND,
    INTERNAL_BOUNDARY,
    NOT_COLLECTED,
    PLANTING_PASS,
    JohnDeereGuidanceOptions,
    format_layer_audit,
    format_preview_counts,
    guidance_block_reasons,
    john_deere_preview_counts,
    source_pass_counts,
)
from fortsmart_gs3_import.monitor_registry import export_fields  # noqa: E402
from test_gen4_pass_guidance import NS, deterministic_gen4, guidance_curves, package_bytes  # noqa: E402
from test_gs3_pass_guidance import (  # noqa: E402
    FAZENDA,
    FIELD_A,
    curved_track_lines,
    feature,
    field_bundle,
    square,
    without_passes,
)

SPEC_MESSAGE = (
    "Foram encontradas passadas de plantio no QGIS, mas nenhuma chegou ao plano de exportação."
)


def tl13_bundle():
    """01_limite_externo=1, 02_limite_interno=1, 03_bordadura=1, 12=88, 15=2."""
    bundle = field_bundle(talhao="TL 13", operational_noise=False)
    bundle.features.append(
        feature(
            FIELD_A, "TL 13", SpatialKind.LIMITE_INTERNO, "INTERNAL_BOUNDARY", "polygon",
            square(-54.996, -12.004, 0.001), "02_Limite_Interno",
        )
    )
    return bundle


def dual_export(bundles, root: Path, options: JohnDeereGuidanceOptions | None) -> Path:
    with deterministic_gen4():
        return export_fields(bundles, root, profile_id="john_deere_dual", guidance_options=options)


class PreviewTest(unittest.TestCase):
    # 13 — preview TL13
    def test_tl13_preview_counts(self):
        counts = john_deere_preview_counts([tl13_bundle()])
        self.assertEqual(
            counts,
            {BOUNDARY: 1, INTERNAL_BOUNDARY: 1, HEADLAND: 1, PLANTING_PASS: 88, BORDER_PASS: 2},
        )
        self.assertEqual(
            format_preview_counts(counts),
            [
                "  • Limites: 1",
                "  • Limites internos: 1",
                "  • Bordaduras: 1",
                "  • Passadas de plantio: 88",
                "  • Passadas de bordadura: 2",
            ],
        )

    def test_generic_preview_names_passes_by_role(self):
        text = summarize_text([tl13_bundle()], "John Deere Dual")
        for line in (
            "• Limites: 1",
            "• Limites internos: 1",
            "• Bordaduras: 1",
            "• Passadas de plantio: 88",
            "• Passadas de bordadura: 2",
        ):
            self.assertIn(line, text)
        self.assertNotIn("Trabalhos / faixas", text)

    def test_border_pass_is_not_headland(self):
        counts = john_deere_preview_counts([tl13_bundle()])
        self.assertEqual(counts[HEADLAND], 1)
        self.assertEqual(counts[BORDER_PASS], 2)

    def test_layer_name_never_makes_a_pass(self):
        bundle = field_bundle(planting=0, border=0, operational_noise=False)
        bundle.features.append(
            feature(FIELD_A, "TL-12", SpatialKind.CURVA_AB, "", "line",
                    [(-54.995, -12.001), (-54.995, -12.009)], "12_passadas_plantio")
        )
        self.assertEqual(john_deere_preview_counts([bundle])[PLANTING_PASS], 0)

    # 5 — sem passadas no preview, exportacao com guidance bloqueada
    def test_block_reasons_when_requested_pass_is_missing(self):
        counts = john_deere_preview_counts([without_passes(field_bundle())])
        options = JohnDeereGuidanceOptions(include_planting_passes=True, source_pass_counts=((PLANTING_PASS, 88),))
        reasons = guidance_block_reasons(counts, options, (GS3, GEN4))
        self.assertEqual(len(reasons), 1)
        self.assertIn(NOT_COLLECTED, reasons[0])
        self.assertIn(SPEC_MESSAGE, reasons[0])
        self.assertEqual(guidance_block_reasons(counts, JohnDeereGuidanceOptions(), (GS3, GEN4)), [])
        self.assertEqual(guidance_block_reasons(counts, None, (GS3, GEN4)), [])
        full = john_deere_preview_counts([tl13_bundle()])
        self.assertEqual(guidance_block_reasons(full, options, (GS3, GEN4)), [])

    # 1 — log obrigatorio por camada
    def test_layer_audit_line(self):
        entry = {
            "layer_name": "12_Passadas_Plantio",
            "internal_key": "06_PASSADAS_PLANTIO",
            "fortsmart_role": "WORK_PATH",
            "resolution": "fortsmart_role",
            "visible": False,
            "selected": True,
            "in_scope": True,
            "features": 88,
            "collected": 88,
            "reason": "",
        }
        self.assertEqual(
            format_layer_audit(entry),
            "12_Passadas_Plantio | internal_key=06_PASSADAS_PLANTIO | role=WORK_PATH "
            "| identity=fortsmart_role | visible=false | selected=true | collected=true (88/88) "
            "| reason_if_skipped=-",
        )
        skipped = dict(entry, collected=0, selected=False, in_scope=False, reason="hidden_in_layer_panel")
        self.assertIn("collected=false (0/88) | reason_if_skipped=hidden_in_layer_panel", format_layer_audit(skipped))
        border = dict(entry, layer_name="15", fortsmart_role="FIELD_HEADLAND_PATH", features=2)
        self.assertEqual(
            source_pass_counts([entry, skipped, border]),
            ((PLANTING_PASS, 88), (BORDER_PASS, 2)),
        )


class NotCollectedAssertTest(unittest.TestCase):
    # 10 — WORK_PATH no QGIS, nenhuma no plano: aborta antes dos writers
    def test_passes_in_qgis_but_not_in_plan_abort(self):
        audit = (
            {"layer_name": "12_Passadas_Plantio", "fortsmart_role": "WORK_PATH", "visible": True,
             "selected": True, "in_scope": True, "features": 88, "collected": 0, "reason": "x"},
        )
        options = JohnDeereGuidanceOptions(
            include_planting_passes=True,
            source_pass_counts=((PLANTING_PASS, 88),),
            collection_audit=audit,
        )
        with tempfile.TemporaryDirectory(prefix="fs-jd-nc-") as temp:
            root = Path(temp)
            with self.assertRaises(ValueError) as ctx:
                dual_export([without_passes(field_bundle())], root, options)
            message = str(ctx.exception)
            self.assertIn(NOT_COLLECTED, message)
            self.assertIn(SPEC_MESSAGE, message)
            self.assertEqual(message.count(NOT_COLLECTED), 1, "GS3 e Gen4 nao duplicam o erro")
            self.assertFalse((root / "GS3_2630").exists())
            self.assertFalse((root / "Gen4").exists())
            report = json.loads((root / "john_deere_guidance_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ABORTED")
            self.assertEqual(report["preflight"]["source_pass_counts"], {PLANTING_PASS: 88})
            text = (root / "JOHN_DEERE_GUIDANCE_REPORT.txt").read_text(encoding="utf-8")
            self.assertIn("QGIS COLLECTION", text)
            self.assertIn("12_Passadas_Plantio | internal_key=- | role=WORK_PATH", text)

    def test_requested_pass_missing_everywhere_aborts(self):
        options = JohnDeereGuidanceOptions(include_border_passes=True)
        with tempfile.TemporaryDirectory(prefix="fs-jd-nc2-") as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, NOT_COLLECTED):
                dual_export([field_bundle(border=0)], root, options)
            self.assertFalse((root / "GS3_2630").exists())

    def test_gs3_only_and_gen4_only_also_abort(self):
        for profile in ("john_deere_gs3", "john_deere_gen4"):
            with tempfile.TemporaryDirectory(prefix="fs-jd-nc3-") as temp:
                with self.assertRaisesRegex(ValueError, NOT_COLLECTED):
                    with deterministic_gen4():
                        export_fields(
                            [without_passes(field_bundle())], Path(temp), profile_id=profile,
                            guidance_options=JohnDeereGuidanceOptions(include_planting_passes=True),
                        )


class DualFiveCurvesTest(unittest.TestCase):
    # 14, 15, 16 — 5 passadas: CurveTrack no GS3 e AdaptiveCurve no Gen4, mesmo plano
    def test_five_passes_reach_both_serializers_from_one_plan(self):
        options = JohnDeereGuidanceOptions(include_planting_passes=True, max_planting_passes=5)
        calls = []
        original = deere_dual_writer.guidance_plan_for

        def spy(bundles, opts, platforms):
            calls.append(list(platforms))
            return original(bundles, opts, platforms)

        with tempfile.TemporaryDirectory(prefix="fs-jd-5-") as temp:
            root = Path(temp)
            with mock.patch.object(deere_dual_writer, "guidance_plan_for", spy):
                dual_export([tl13_bundle()], root, options)
            self.assertEqual(calls, [[GS3, GEN4]], "um unico plano canonico para os dois adapters")

            eic = root / "GS3_2630" / FAZENDA / "RCD" / "EIC"
            self.assertTrue(list(eic.rglob("CurveTrack*.fdShape")))
            catalogs = "".join(path.read_text(encoding="utf-8") for path in eic.rglob("ImportExport.SpatialCatalog"))
            self.assertIn("CurvedTrackLine", catalogs)
            self.assertEqual(len(curved_track_lines(eic)), 1)

            gen4 = root / "Gen4"
            files = sorted(path.name for path in (gen4 / "SpatialFiles").glob("AdaptiveCurve*.gjson"))
            self.assertTrue(files)
            master = (gen4 / "MasterData.xml").read_text(encoding="utf-8")
            for name in files:
                self.assertIn(name, master)
            curve = guidance_curves(gen4)[0]
            self.assertTrue(curve.findtext(f"{NS}Geometry/{NS}FilenameWithExtension").startswith("AdaptiveCurve"))

            report = json.loads((root / "john_deere_guidance_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["gs3"]["totals"][PLANTING_PASS]["serialized"], 5)
            self.assertEqual(report["gen4"]["totals"][PLANTING_PASS]["serialized"], 5)

    # 17 — opcoes desligadas: pacote igual ao anterior
    def test_options_off_package_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="fs-jd-off-") as temp:
            root = Path(temp)
            dual_export([without_passes(tl13_bundle())], root / "ref", None)
            reference = package_bytes(root / "ref" / "Gen4")
            self.assertIn("MasterData.xml", reference)
            for name, options in (("none", None), ("unchecked", JohnDeereGuidanceOptions())):
                dual_export([tl13_bundle()], root / name, options)
                self.assertEqual(package_bytes(root / name / "Gen4"), reference, name)
                gs3_ref = sorted(p.relative_to(root / "ref").as_posix() for p in (root / "ref" / "GS3_2630").rglob("*"))
                gs3_got = sorted(p.relative_to(root / name).as_posix() for p in (root / name / "GS3_2630").rglob("*"))
                self.assertEqual(gs3_got, gs3_ref, name)
                self.assertFalse((root / name / "john_deere_guidance_report.json").exists())
            self.assertFalse(list((root / "none" / "GS3_2630").rglob("CurveTrack*.fdShape")))
            root_xml = ET.parse(root / "none" / "Gen4" / "MasterData.xml").getroot()
            self.assertFalse(root_xml.findall(f".//{NS}AdaptiveCurve"))


if __name__ == "__main__":
    unittest.main()
