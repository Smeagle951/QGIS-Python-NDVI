from __future__ import annotations

import json
import math
import re
import struct
import sys
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "qgis_plugin"))

from fortsmart_gs3_import import gs3_pass_guidance as guidance  # noqa: E402
from fortsmart_gs3_import import jd_guidance_plan as jd_plan  # noqa: E402
from fortsmart_gs3_import.adapters.deere_dual_writer import DeereDualWriter  # noqa: E402
from fortsmart_gs3_import.adapters.deere_gs3_writer import DeereGS3Writer  # noqa: E402
from fortsmart_gs3_import.gs3_pass_guidance import (  # noqa: E402
    BORDER_PASS,
    EMPTY_PACKAGE_MESSAGE,
    GROUPING_PER_PASS,
    IGNORED_NAME_MATCH,
    PLANTING_PASS,
    GS3PassGuidanceWriter,
    GS3PassOptions,
    build_gs3_export_plan,
    decode_curvetrack_fdshape,
    is_effectively_straight,
    prepare_gs3_guidance_geometry,
    write_curvetrack_fdshape,
)
from fortsmart_gs3_import.gs3_spatial_types import (  # noqa: E402
    Gs3FieldBundle,
    Gs3SpatialFeature,
    SpatialKind,
)
from fortsmart_gs3_import.monitor_registry import export_fields  # noqa: E402


CLIENTE = "Cliente Teste"
FAZENDA = "Fazenda Teste"
FIELD_A = "6f1d2c3b-4a59-4e6f-8a7b-9c0d1e2f3a4b"
FIELD_B = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
GOLDEN_GS3 = PROJECT_ROOT / "data" / "analysis" / "PL-38L" / "GS3_2630"


def square(lon0: float, lat0: float, size: float = 0.01):
    return [
        (lon0, lat0),
        (lon0, lat0 - size),
        (lon0 + size, lat0 - size),
        (lon0 + size, lat0),
        (lon0, lat0),
    ]


def feature(
    field_erid: str,
    talhao: str,
    kind: SpatialKind,
    role: str,
    geometry: str,
    coordinates,
    name: str,
    **extra: str,
) -> Gs3SpatialFeature:
    return Gs3SpatialFeature(
        kind=kind,
        cliente=CLIENTE,
        fazenda=FAZENDA,
        talhao=talhao,
        field_erid=field_erid,
        item_erid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{field_erid}|{role}|{name}|{list(coordinates)!r}")),
        name=name,
        source_file=extra.pop("source_file", "12_Passadas_Plantio"),
        catalog_file="qgis",
        geometry=geometry,
        points=list(coordinates) if geometry == "line" else [],
        rings=[list(coordinates)] if geometry == "polygon" else [],
        extra={"canonical_role": role, "role_resolution": "fortsmart_role", **extra},
    )


def planting_passes(field_erid: str, talhao: str, lon0: float, lat0: float, count: int = 88):
    passes = []
    for index in range(count):
        lon = lon0 + 0.0002 + index * (0.0096 / max(1, count - 1))
        passes.append(
            feature(
                field_erid,
                talhao,
                SpatialKind.TRABALHO,
                "WORK_PATH",
                "line",
                [(lon, lat0 - 0.0003), (lon, lat0 - 0.0097)],
                "12_Passadas_Plantio",
                layer_role="WORK_PATH",
                pass_number=str(index + 1),
                pass_label=f"P{index + 1}",
            )
        )
    return passes


def border_passes(field_erid: str, talhao: str, lon0: float, lat0: float, count: int = 2):
    passes = []
    for index in range(count):
        inset = 0.00005 * (index + 1)
        ring = [
            (lon0 + inset, lat0 - inset),
            (lon0 + inset, lat0 - 0.01 + inset),
            (lon0 + 0.01 - inset, lat0 - 0.01 + inset),
            (lon0 + 0.01 - inset, lat0 - inset),
            (lon0 + inset, lat0 - inset),
        ]
        passes.append(
            feature(
                field_erid,
                talhao,
                SpatialKind.TRABALHO,
                "FIELD_HEADLAND_PATH",
                "line",
                ring,
                "15_Passadas_Bordadura",
                source_file="15_Passadas_Bordadura",
                layer_role="FIELD_HEADLAND_PATH",
                pass_number=str(index + 1),
                pass_label=f"B{index + 1}",
            )
        )
    return passes


def headland(field_erid: str, talhao: str, lon0: float, lat0: float) -> Gs3SpatialFeature:
    band = feature(field_erid, talhao, SpatialKind.BORDADURA, "HEADLAND", "polygon", square(lon0, lat0), "Bordadura")
    inner = square(lon0 + 0.001, lat0 - 0.001, 0.008)
    band.rings = [square(lon0, lat0), list(reversed(inner))]
    return band


def field_bundle(
    field_erid: str = FIELD_A,
    talhao: str = "TL-12",
    lon0: float = -55.0,
    lat0: float = -12.0,
    *,
    planting: int = 88,
    border: int = 2,
    operational_noise: bool = True,
) -> Gs3FieldBundle:
    features = [
        feature(field_erid, talhao, SpatialKind.LIMITE, "EXTERNAL_BOUNDARY", "polygon", square(lon0, lat0), "Limite"),
        headland(field_erid, talhao, lon0, lat0),
    ]
    features += planting_passes(field_erid, talhao, lon0, lat0, planting)
    features += border_passes(field_erid, talhao, lon0, lat0, border)
    if operational_noise:
        segment = [(lon0 + 0.003, lat0 - 0.003), (lon0 + 0.004, lat0 - 0.004)]
        for role in ("FINAL_WORK_PATH", "WORK_OFF", "HEADLAND_TURN", "TRACTOR_PATH"):
            features.append(feature(field_erid, talhao, SpatialKind.TRABALHO, role, "line", segment, role))
    return Gs3FieldBundle(cliente=CLIENTE, fazenda=FAZENDA, talhao=talhao, field_erid=field_erid, features=features)


def without_passes(bundle: Gs3FieldBundle) -> Gs3FieldBundle:
    keep = [
        item for item in bundle.features
        if item.extra.get("canonical_role") not in ("WORK_PATH", "FIELD_HEADLAND_PATH", "FINAL_WORK_PATH")
    ]
    return Gs3FieldBundle(
        cliente=bundle.cliente,
        fazenda=bundle.fazenda,
        talhao=bundle.talhao,
        field_erid=bundle.field_erid,
        features=keep,
    )


def eic_root(output: Path) -> Path:
    return output / FAZENDA / "RCD" / "EIC"


def curved_track_lines(eic: Path) -> list[tuple[Path, ET.Element]]:
    found = []
    for catalog in eic.rglob("ImportExport.SpatialCatalog"):
        for element in ET.parse(catalog).getroot().iter():
            if element.tag.rsplit("}", 1)[-1] == "CurvedTrackLine":
                found.append((catalog.parent, element))
    return found


def reference_of(element: ET.Element) -> tuple[float, float]:
    values = {
        child.tag.rsplit("}", 1)[-1]: float(child.attrib["value"])
        for child in element
        if child.tag.rsplit("}", 1)[-1].startswith("vrReference")
    }
    return values["vrReferenceLongitude"], values["vrReferenceLatitude"]


def decoded_tracks(folder: Path, element: ET.Element):
    payload = (folder / f"{element.attrib['fileName']}.fdShape").read_bytes()
    return decode_curvetrack_fdshape(payload, reference_of(element))


def normalized_tree(gs3_root: Path) -> dict[str, bytes]:
    tree = {}
    for path in sorted(gs3_root.rglob("*")):
        if not path.is_file():
            continue
        payload = path.read_bytes()
        if path.suffix in (".SpatialCatalog", ".fds"):
            text = payload.decode("utf-8")
            text = re.sub(r'(lastModified|uuidSession)="[^"]*"', r'\1="X"', text)
            text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z?", "T", text)
            payload = text.encode("utf-8")
        tree[str(path.relative_to(gs3_root))] = payload
    return tree


def export(bundles, root: Path, options: GS3PassOptions | None = None) -> Path:
    return DeereGS3Writer().write(bundles, root, gs3_pass_options=options)


def debug_report(root: Path) -> dict:
    return json.loads((root / "fortsmart_gs3_export_debug.json").read_text(encoding="utf-8"))


class GeometryTest(unittest.TestCase):
    def test_densified_straight_line_with_50_vertices_is_straight(self):
        start, end = (-55.0, -12.0), (-55.0, -12.009)
        points = []
        for index in range(50):
            t = index / 49
            wobble = (0.00000005 if index % 2 else -0.00000005) if 0 < index < 49 else 0.0
            points.append((start[0] + wobble, start[1] + (end[1] - start[1]) * t))
        self.assertTrue(is_effectively_straight(points, 0.10))
        prepared = prepare_gs3_guidance_geometry(points, straightness_tolerance_m=0.10)
        self.assertTrue(prepared.straight)
        self.assertEqual(prepared.original_vertices, 50)

    def test_curve_with_few_vertices_is_not_straight(self):
        arc = [(-55.0, -12.0), (-54.999, -12.0045), (-55.0, -12.009)]
        self.assertFalse(is_effectively_straight(arc, 0.10))
        self.assertTrue(is_effectively_straight(arc, 200.0))

    def test_prepare_removes_consecutive_duplicates_without_simplifying(self):
        points = [(-55.0, -12.0), (-55.0, -12.0), (-55.0, -12.001), (-54.9995, -12.002), (-55.0, -12.003)]
        prepared = prepare_gs3_guidance_geometry(points)
        self.assertEqual(prepared.removed_duplicates, 1)
        self.assertEqual(prepared.tracks, [[points[0], points[2], points[3], points[4]]])

    def test_prepare_rejects_nan_and_projected_coordinates(self):
        with self.assertRaisesRegex(ValueError, "GUIDANCE_INVALID_COORDINATE"):
            prepare_gs3_guidance_geometry([(-55.0, -12.0), (math.nan, -12.001)])
        with self.assertRaisesRegex(ValueError, "GUIDANCE_NOT_WGS84"):
            prepare_gs3_guidance_geometry([(712345.0, 8671234.0), (712345.0, 8672234.0)])
        with self.assertRaisesRegex(ValueError, "GUIDANCE_DEGENERATE"):
            prepare_gs3_guidance_geometry([(-55.0, -12.0), (-55.0, -12.0)])

    def test_multipart_pass_keeps_parts_as_separate_tracks(self):
        points = [(-55.0, -12.0), (-55.0, -12.002), (-55.0, -12.004), (-55.0, -12.006)]
        prepared = prepare_gs3_guidance_geometry(points, part_breaks=[2])
        self.assertEqual(len(prepared.tracks), 2)

    def test_densification_keeps_original_vertices(self):
        points = [(-55.0, -12.0), (-55.0, -12.009)]
        prepared = prepare_gs3_guidance_geometry(points, max_segment_m=20.0)
        track = prepared.tracks[0]
        self.assertEqual(track[0], points[0])
        self.assertEqual(track[-1], points[-1])
        self.assertGreaterEqual(len(track), 50)
        self.assertTrue(all(lon == -55.0 for lon, _ in track))

    def test_over_dense_border_pass_is_thinned_within_tolerance_without_clipping(self):
        center, radius_deg = (-55.0, -12.0), 0.0005
        loop = [
            (center[0] + radius_deg * math.cos(t), center[1] + radius_deg * math.sin(t))
            for t in (2 * math.pi * i / 120000 for i in range(120001))
        ]
        prepared = prepare_gs3_guidance_geometry(loop, thinning_tolerance_m=0.02)
        track = prepared.tracks[0]
        self.assertEqual((track[0], track[-1]), (loop[0], loop[-1]))
        self.assertLess(len(track), 1000)
        self.assertEqual(prepared.thinned_vertices, len(loop) - len(track))
        self.assertLessEqual(prepared.thinning_deviation_m, 0.02)
        thinned_length = sum(jd_plan._distance_m(a, b) for a, b in zip(track, track[1:]))
        self.assertAlmostEqual(thinned_length, prepared.length_m, delta=0.05)

    def test_passes_that_already_exported_are_unchanged_with_thinning_enabled(self):
        normal = [(-55.0 + (0.000001 if i % 2 else 0.0), -12.0 - i * 0.00001) for i in range(100)]
        dense_but_small = [(-55.0 + (0.0000001 if i % 2 else 0.0), -12.0 - i * 0.0000004) for i in range(70000)]
        for points in (normal, dense_but_small):
            baseline = prepare_gs3_guidance_geometry(points, max_segment_m=20.0)
            prepared = prepare_gs3_guidance_geometry(points, max_segment_m=20.0, thinning_tolerance_m=0.02)
            self.assertEqual(prepared.thinned_vertices, 0)
            self.assertEqual(prepared.tracks, baseline.tracks)

    def test_field_with_two_external_boundaries_is_blocked_with_layer_names(self):
        bundle = field_bundle(planting=1, border=0, operational_noise=False)
        other = feature(FIELD_A, "TL-12", SpatialKind.LIMITE, "EXTERNAL_BOUNDARY", "polygon",
                        square(-54.98, -12.0), "Limite", source_file="01_limite_externo")
        bundle.features.append(other)
        errors = jd_plan.multiple_external_boundary_errors([bundle])
        self.assertEqual(len(errors), 1)
        self.assertIn("JOHN_DEERE_FIELD_MULTIPLE_EXTERNAL_BOUNDARIES", errors[0])
        self.assertIn("01_limite_externo", errors[0])
        self.assertEqual(jd_plan.multiple_external_boundary_errors([field_bundle(planting=1, border=0)]), [])
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "MULTIPLE_EXTERNAL_BOUNDARIES"):
                DeereDualWriter().write([bundle], Path(temp_dir))


class CurveTrackFormatTest(unittest.TestCase):
    def test_delta_sentinel_layout(self):
        tracks = [
            [(-55.0, -12.0), (-55.0, -12.001), (-55.0, -12.002)],
            [(-54.999, -12.0), (-54.999, -12.002)],
        ]
        payload, reference = write_curvetrack_fdshape(tracks)
        self.assertEqual(reference, (-55.0, -12.0))
        self.assertEqual(payload[16:18], b"\x01\x02")
        self.assertEqual(struct.unpack_from("<QQQQ", payload, 32), (1, 0, 1, 7))
        records = [struct.unpack_from("<ddf", payload, 64 + 20 * i) for i in range(7)]
        self.assertEqual(records[0], (0.0, 0.0, struct.unpack("<f", struct.pack("<f", 999.9))[0]))
        self.assertEqual(records[1][:2], (0.0, 0.0))
        self.assertEqual(records[4][2], records[0][2])
        self.assertEqual(records[4][:2], records[3][:2])
        self.assertEqual({round(r[2], 3) for r in records}, {1.0, 999.9})
        self.assertEqual(decode_curvetrack_fdshape(payload, reference), tracks)

    @unittest.skipUnless(GOLDEN_GS3.is_dir(), "pacote golden ausente")
    def test_layout_matches_accepted_deere_curvetrack(self):
        golden = next(GOLDEN_GS3.rglob("CurveTrack*.fdShape"))
        catalog = golden.parent / "ImportExport.SpatialCatalog"
        element = next(
            item for item in ET.parse(catalog).getroot().iter()
            if item.tag.endswith("CurvedTrackLine") and item.attrib["fileName"] == golden.stem
        )
        reference = reference_of(element)
        payload = golden.read_bytes()
        tracks = decode_curvetrack_fdshape(payload, reference)
        rebuilt, rebuilt_reference = write_curvetrack_fdshape(tracks)
        self.assertEqual(rebuilt[:56], payload[:56])
        self.assertEqual(len(rebuilt), len(payload))
        self.assertAlmostEqual(rebuilt_reference[0], reference[0], places=12)
        count = struct.unpack_from("<Q", payload, 56)[0]
        for index in range(count):
            got = struct.unpack_from("<ddf", rebuilt, 64 + 20 * index)
            want = struct.unpack_from("<ddf", payload, 64 + 20 * index)
            self.assertEqual(got[2], want[2])
            self.assertAlmostEqual(got[0], want[0], places=12)
            self.assertAlmostEqual(got[1], want[1], places=12)


class ExportPlanTest(unittest.TestCase):
    def test_plan_includes_and_skips_by_role(self):
        plan = build_gs3_export_plan(
            [field_bundle()],
            GS3PassOptions(include_planting_passes=True, include_border_passes=True),
        )
        self.assertTrue({"WORK_PATH", "FIELD_HEADLAND_PATH", "EXTERNAL_BOUNDARY", "HEADLAND"} <= plan.include_roles)
        self.assertTrue({"FINAL_WORK_PATH", "WORK_OFF", "HEADLAND_TURN", "WORK_ON", "SHUTOFF"} <= plan.skip_roles)
        field_plan = plan.for_field(FIELD_A)
        self.assertEqual(field_plan.included(PLANTING_PASS), 88)
        self.assertEqual(field_plan.included(BORDER_PASS), 2)
        self.assertEqual(field_plan.skipped_roles["work_paths"], 2)
        self.assertEqual(field_plan.skipped_roles["headland_turns"], 1)
        self.assertEqual(field_plan.skipped_roles["maneuvers"], 1)
        self.assertEqual([item.name for item in field_plan.items[BORDER_PASS]], ["B1", "B2"])
        self.assertEqual(field_plan.items[PLANTING_PASS][55].name, "P56")

    def test_renamed_layer_still_exports_by_role(self):
        bundle = field_bundle(planting=3, border=0)
        for item in bundle.features:
            if item.extra["canonical_role"] == "WORK_PATH":
                item.source_file = "Linhas do operador (renomeada)"
                item.name = "camada qualquer"
        plan = build_gs3_export_plan([bundle], GS3PassOptions(include_planting_passes=True))
        self.assertEqual(plan.for_field(FIELD_A).included(PLANTING_PASS), 3)

    def test_layer_name_makes_a_pass_but_loose_feature_name_does_not(self):
        bundle = field_bundle(planting=0, border=0, operational_noise=False)
        bundle.features.append(
            feature(
                FIELD_A, "TL-12", SpatialKind.TRABALHO, "WORK_PATH", "line",
                [(-54.995, -12.001), (-54.995, -12.009)], "linha plantio",
                role_resolution="legacy_alias",
            )
        )
        bundle.features.append(
            feature(
                FIELD_A, "TL-12", SpatialKind.CURVA_AB, "", "line",
                [(-54.994, -12.001), (-54.994, -12.009)], "Passadas Plantio Bordadura",
                role_resolution="legacy_name",
            )
        )
        plan = build_gs3_export_plan([bundle], GS3PassOptions(include_planting_passes=True))
        field_plan = plan.for_field(FIELD_A)
        self.assertEqual(field_plan.detected[PLANTING_PASS], 1)
        self.assertEqual(field_plan.skipped[IGNORED_NAME_MATCH], 1)

    def test_final_lines_are_planting_passes_when_field_has_no_work_path(self):
        bundle = field_bundle(planting=0, border=1, operational_noise=False)
        for index in range(3):
            lon = -54.997 + index * 0.002
            bundle.features.append(
                feature(
                    FIELD_A, "TL-12", SpatialKind.TRABALHO, "FINAL_WORK_PATH", "line",
                    [(lon, -12.001), (lon, -12.009)], f"31_linhas_finais {index}",
                    source_file="31_linhas_finais", role_resolution="legacy_alias",
                )
            )
        plan = build_gs3_export_plan(
            [bundle], GS3PassOptions(include_planting_passes=True, include_border_passes=True)
        )
        field_plan = plan.for_field(FIELD_A)
        self.assertEqual(field_plan.planting_source_role, "FINAL_WORK_PATH")
        self.assertEqual(field_plan.included(PLANTING_PASS), 3)
        self.assertEqual(field_plan.included(BORDER_PASS), 1)
        self.assertEqual(field_plan.skipped_by_role["FINAL_WORK_PATH"], 0)
        self.assertEqual(plan.preflight_errors(jd_plan.GS3, [FIELD_A]), [])
        self.assertEqual(jd_plan.john_deere_preview_counts([bundle])[PLANTING_PASS], 3)
        self.assertIn("31_linhas_finais", "\n".join(jd_plan.final_lines_source_lines([bundle])))

    def test_final_lines_stay_out_when_field_has_work_path(self):
        bundle = field_bundle(planting=4, border=0, operational_noise=True)
        plan = build_gs3_export_plan([bundle], GS3PassOptions(include_planting_passes=True))
        field_plan = plan.for_field(FIELD_A)
        self.assertEqual(field_plan.planting_source_role, "WORK_PATH")
        self.assertEqual(field_plan.included(PLANTING_PASS), 4)
        self.assertEqual(field_plan.skipped_by_role["FINAL_WORK_PATH"], 1)
        self.assertEqual(jd_plan.final_lines_source_lines([bundle]), [])

    def test_feature_role_traffic_falls_back_to_layer_role(self):
        bundle = field_bundle(planting=0, border=1, operational_noise=False)
        border = bundle.features[-1]
        border.extra["canonical_role"] = "TRAFFIC"
        plan = build_gs3_export_plan([bundle], GS3PassOptions(include_border_passes=True))
        self.assertEqual(plan.for_field(FIELD_A).included(BORDER_PASS), 1)

    def test_device_test_limit_keeps_first_passes(self):
        plan = build_gs3_export_plan(
            [field_bundle()],
            GS3PassOptions(include_planting_passes=True, max_planting_passes=5),
        )
        field_plan = plan.for_field(FIELD_A)
        self.assertEqual([item.name for item in field_plan.items[PLANTING_PASS]], ["P1", "P2", "P3", "P4", "P5"])
        self.assertEqual(field_plan.skipped[f"{PLANTING_PASS}_limite_teste"], 83)

    def test_repeated_item_erids_get_distinct_stable_uuids(self):
        bundle = field_bundle(planting=3, border=0, operational_noise=False)
        for item in bundle.features:
            if item.extra["canonical_role"] == "WORK_PATH":
                item.item_erid = FIELD_B
        options = GS3PassOptions(include_planting_passes=True)
        first = build_gs3_export_plan([bundle], options).for_field(FIELD_A)
        second = build_gs3_export_plan([bundle], options).for_field(FIELD_A)
        erids = [item.item_erid for item in first.items[PLANTING_PASS]]
        self.assertEqual(len(set(erids)), 3)
        self.assertEqual(erids, [item.item_erid for item in second.items[PLANTING_PASS]])
        self.assertEqual(first.uuid_errors, 2)

    def test_feature_flags_disable_requests(self):
        with mock.patch.object(jd_plan, "ENABLE_GS3_PLANTING_PASSES", False):
            options = GS3PassOptions(include_planting_passes=True)
            self.assertFalse(options.any_requested)
            self.assertTrue(options.any_gen4_requested)
        with mock.patch.object(jd_plan, "ENABLE_GEN4_BORDER_PASSES", False):
            options = GS3PassOptions(include_border_passes=True)
            self.assertTrue(options.any_requested)
            self.assertFalse(options.any_gen4_requested)


class GS3PassExportTest(unittest.TestCase):
    def test_01_baseline_unchanged_when_options_are_off(self):
        with tempfile.TemporaryDirectory(prefix="fs-gs3-base-") as temp:
            root = Path(temp)
            reference = export([without_passes(field_bundle())], root / "ref")
            default = export([field_bundle()], root / "default")
            unchecked = export([field_bundle()], root / "unchecked", GS3PassOptions())
            expected = normalized_tree(reference)
            self.assertEqual(normalized_tree(default), expected)
            self.assertEqual(normalized_tree(unchecked), expected)
            self.assertNotIn("guidance_passes", debug_report(root / "default"))
            self.assertNotIn("GS3 Guidance Report", (root / "default" / "GS3_EXPORT_REPORT.txt").read_text())

    def test_02_88_planting_passes_grouped_like_deere_package(self):
        with tempfile.TemporaryDirectory(prefix="fs-gs3-88-") as temp:
            root = Path(temp)
            output = export([field_bundle()], root, GS3PassOptions(include_planting_passes=True))
            lines = curved_track_lines(eic_root(output))
            self.assertEqual(len(lines), 1)
            folder, element = lines[0]
            self.assertEqual(element.attrib["name"], "PLANTIO P1-P88")
            self.assertEqual(len(decoded_tracks(folder, element)), 88)
            report = debug_report(root)["guidance_passes"]
            self.assertEqual(report["totals"][PLANTING_PASS]["detected"], 88)
            self.assertEqual(report["totals"][PLANTING_PASS]["included"], 88)
            self.assertEqual(report["totals"][PLANTING_PASS]["serialized"], 88)
            self.assertEqual(report["totals"][BORDER_PASS]["serialized"], 0)
            self.assertEqual(report["status"], "READY_FOR_DEVICE_TEST")
            text = (root / "GS3_EXPORT_REPORT.txt").read_text(encoding="utf-8")
            self.assertIn("GS3 Guidance Report", text)
            self.assertIn("Planting passes serialized: 88", text)
            self.assertTrue(text.rstrip().endswith("Status: READY_FOR_DEVICE_TEST"))

    def test_02b_one_curved_track_line_per_pass(self):
        with tempfile.TemporaryDirectory(prefix="fs-gs3-pp-") as temp:
            output = export(
                [field_bundle()],
                Path(temp),
                GS3PassOptions(include_planting_passes=True, grouping=GROUPING_PER_PASS),
            )
            lines = curved_track_lines(eic_root(output))
            self.assertEqual(len(lines), 88)
            names = [element.attrib["name"] for _, element in lines]
            self.assertEqual(names[:3], ["P1", "P2", "P3"])
            self.assertIn("P56", names)

    def test_03_two_border_passes(self):
        with tempfile.TemporaryDirectory(prefix="fs-gs3-b-") as temp:
            output = export(
                [field_bundle()],
                Path(temp),
                GS3PassOptions(include_border_passes=True, grouping=GROUPING_PER_PASS),
            )
            lines = curved_track_lines(eic_root(output))
            self.assertEqual([element.attrib["name"] for _, element in lines], ["B1", "B2"])
            for folder, element in lines:
                track = decoded_tracks(folder, element)[0]
                self.assertEqual(track[0], track[-1])

    def test_04_both_categories_serialize_90(self):
        with tempfile.TemporaryDirectory(prefix="fs-gs3-90-") as temp:
            root = Path(temp)
            output = export(
                [field_bundle()],
                root,
                GS3PassOptions(include_planting_passes=True, include_border_passes=True),
            )
            lines = curved_track_lines(eic_root(output))
            self.assertEqual(sorted(element.attrib["name"] for _, element in lines), ["BORDADURA B1-B2", "PLANTIO P1-P88"])
            self.assertEqual(sum(len(decoded_tracks(folder, element)) for folder, element in lines), 90)
            totals = debug_report(root)["guidance_passes"]["totals"]
            self.assertEqual(totals[PLANTING_PASS]["serialized"] + totals[BORDER_PASS]["serialized"], 90)

    def test_05_headland_and_boundary_are_not_duplicated(self):
        with tempfile.TemporaryDirectory(prefix="fs-gs3-hl-") as temp:
            output = export(
                [field_bundle()],
                Path(temp),
                GS3PassOptions(include_planting_passes=True, include_border_passes=True),
            )
            eic = eic_root(output)
            text = "\n".join(path.read_text(encoding="utf-8") for path in eic.rglob("*.SpatialCatalog"))
            self.assertEqual(text.count("<HeadLand "), 1)
            self.assertEqual(text.count("<Boundary "), 1)
            self.assertEqual(len(list(eic.rglob("Boundary*.fdShape"))), 1)
            self.assertLess(text.index("<CurvedTrackLine "), text.index("<HeadLand "))

    def test_06_renamed_layer_exports(self):
        bundle = field_bundle(planting=4, border=0)
        for item in bundle.features:
            item.source_file = "Camada renomeada pelo usuario"
        with tempfile.TemporaryDirectory(prefix="fs-gs3-ren-") as temp:
            output = export([bundle], Path(temp), GS3PassOptions(include_planting_passes=True))
            folder, element = curved_track_lines(eic_root(output))[0]
            self.assertEqual(len(decoded_tracks(folder, element)), 4)

    def test_07_field_isolation(self):
        tl12 = field_bundle(FIELD_A, "TL-12", -55.0, -12.0, planting=10, border=0)
        tl13 = field_bundle(FIELD_B, "TL-13", -54.95, -12.05, planting=5, border=0)
        with tempfile.TemporaryDirectory(prefix="fs-gs3-iso-") as temp:
            root = Path(temp)
            output = export([tl12, tl13], root, GS3PassOptions(include_planting_passes=True))
            by_field = {}
            for folder, element in curved_track_lines(eic_root(output)):
                by_field[folder.name] = decoded_tracks(folder, element)
            self.assertEqual(len(by_field[FIELD_A]), 10)
            self.assertEqual(len(by_field[FIELD_B]), 5)
            self.assertTrue(all(lon < -54.98 for track in by_field[FIELD_A] for lon, _ in track))
            self.assertTrue(all(lon > -54.96 for track in by_field[FIELD_B] for lon, _ in track))

    def test_07b_pass_from_other_field_region_aborts(self):
        tl12 = field_bundle(FIELD_A, "TL-12", -55.0, -12.0, planting=10, border=0)
        tl13 = field_bundle(FIELD_B, "TL-13", -54.95, -12.05, planting=5, border=0)
        stray = planting_passes(FIELD_B, "TL-13", -55.0, -12.0, 1)[0]
        stray.item_erid = str(uuid.uuid4())
        tl13.features.append(stray)
        with tempfile.TemporaryDirectory(prefix="fs-gs3-iso-") as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, "JOHN_DEERE_GUIDANCE_OUTSIDE_FIELD_REGION: Field TL-13"):
                export([tl12, tl13], root, GS3PassOptions(include_planting_passes=True))
            self.assertFalse((root / "GS3_2630").exists())

    def test_08_curved_pass_geometry_preserved(self):
        bundle = field_bundle(planting=0, border=0, operational_noise=False)
        arc = [
            (-55.0 + 0.004 + 0.002 * math.sin(math.pi * t / 30), -12.001 - 0.008 * t / 30)
            for t in range(31)
        ]
        bundle.features.append(
            feature(FIELD_A, "TL-12", SpatialKind.TRABALHO, "WORK_PATH", "line", arc, "curva", pass_number="7")
        )
        with tempfile.TemporaryDirectory(prefix="fs-gs3-arc-") as temp:
            root = Path(temp)
            output = export([bundle], root, GS3PassOptions(include_planting_passes=True, max_segment_m=0.0))
            folder, element = curved_track_lines(eic_root(output))[0]
            self.assertEqual(element.attrib["name"], "P7")
            track = decoded_tracks(folder, element)[0]
            self.assertEqual(len(track), len(arc))
            for got, want in zip(track, arc):
                self.assertAlmostEqual(got[0], want[0], places=12)
                self.assertAlmostEqual(got[1], want[1], places=12)
            totals = debug_report(root)["guidance_passes"]["totals"][PLANTING_PASS]
            self.assertEqual((totals["straight"], totals["curved"]), (0, 1))

    def test_09_densified_straight_pass_counts_as_straight(self):
        bundle = field_bundle(planting=0, border=0, operational_noise=False)
        line = [(-54.995, -12.001 - 0.008 * index / 49) for index in range(50)]
        bundle.features.append(
            feature(FIELD_A, "TL-12", SpatialKind.TRABALHO, "WORK_PATH", "line", line, "reta", pass_number="1")
        )
        with tempfile.TemporaryDirectory(prefix="fs-gs3-st-") as temp:
            root = Path(temp)
            export([bundle], root, GS3PassOptions(include_planting_passes=True))
            totals = debug_report(root)["guidance_passes"]["totals"][PLANTING_PASS]
            self.assertEqual((totals["straight"], totals["curved"], totals["serialized"]), (1, 0, 1))

    def test_10_invalid_geometry_warns_per_feature(self):
        bundle = field_bundle(planting=5, border=0, operational_noise=False)
        bundle.features.append(
            feature(FIELD_A, "TL-12", SpatialKind.TRABALHO, "WORK_PATH", "line",
                    [(-55.0, -12.0), (math.inf, -12.001)], "x", pass_label="P90")
        )
        bundle.features.append(
            feature(FIELD_A, "TL-12", SpatialKind.TRABALHO, "WORK_PATH", "line",
                    [(-54.995, -12.003), (-54.995, -12.003)], "y", pass_label="P91")
        )
        with tempfile.TemporaryDirectory(prefix="fs-gs3-inv-") as temp:
            root = Path(temp)
            export([bundle], root, GS3PassOptions(include_planting_passes=True, invalid_geometry_policy="skip"))
            row = debug_report(root)["guidance_passes"]["fields"][0]
            self.assertEqual(row["invalid_geometries"], 2)
            self.assertEqual(row["planting"]["detected"], 7)
            self.assertEqual(row["planting"]["serialized"], 5)
            self.assertTrue(any("P90" in warning for warning in row["warnings"]))
            self.assertTrue(any("P91" in warning for warning in row["warnings"]))

    def test_10b_invalid_geometry_aborts_by_default(self):
        bundle = field_bundle(planting=5, border=0, operational_noise=False)
        bundle.features.append(
            feature(FIELD_A, "TL-12", SpatialKind.TRABALHO, "WORK_PATH", "line",
                    [(-55.0, -12.0), (math.inf, -12.001)], "x", pass_label="P37")
        )
        with tempfile.TemporaryDirectory(prefix="fs-gs3-invab-") as temp:
            root = Path(temp)
            with self.assertRaises(ValueError) as ctx:
                export([bundle], root, GS3PassOptions(include_planting_passes=True))
            self.assertIn("pass=P37 status=invalid_geometry", str(ctx.exception))
            self.assertFalse((root / "GS3_2630").exists())
            text = (root / "JOHN_DEERE_GUIDANCE_REPORT.txt").read_text(encoding="utf-8")
            self.assertIn("pass = P37 status = invalid_geometry", text)
            self.assertTrue(text.rstrip().endswith("Status: ABORTED"))

    def test_11_serialization_empty_aborts(self):
        bundle = field_bundle(planting=0, border=0, operational_noise=False)
        bundle.features.append(
            feature(FIELD_A, "TL-12", SpatialKind.TRABALHO, "WORK_PATH", "line",
                    [(-55.0, -12.0), (math.nan, -12.001)], "x")
        )
        with tempfile.TemporaryDirectory(prefix="fs-gs3-empty-") as temp:
            root = Path(temp)
            with self.assertRaises(ValueError) as ctx:
                export([bundle], root, GS3PassOptions(include_planting_passes=True))
            self.assertIn("GS3_GUIDANCE_SERIALIZATION_EMPTY", str(ctx.exception))
            self.assertIn(EMPTY_PACKAGE_MESSAGE, str(ctx.exception))
            self.assertFalse((root / "GS3_2630").exists())
            report = json.loads((root / "john_deere_guidance_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ABORTED")

    def test_11b_writer_dropping_passes_is_reported_as_incomplete(self):
        def drop_all(self, field_plan, field_dir):
            return guidance.GS3FieldGuidanceResult(field_erid=field_plan.field_erid, field_dir=field_dir)

        with tempfile.TemporaryDirectory(prefix="fs-gs3-drop-") as temp:
            root = Path(temp)
            with mock.patch.object(GS3PassGuidanceWriter, "write_field", drop_all):
                with self.assertRaises(RuntimeError) as ctx:
                    export([field_bundle()], root, GS3PassOptions(include_planting_passes=True))
            message = str(ctx.exception)
            self.assertIn(EMPTY_PACKAGE_MESSAGE, message)
            self.assertIn("Foram detectadas 88 passadas, mas nenhuma foi adicionada ao pacote GS3.", message)
            self.assertFalse((root / "GS3_2630").exists())

    def test_12_catalog_count_matches_serialized(self):
        with tempfile.TemporaryDirectory(prefix="fs-gs3-cat-") as temp:
            root = Path(temp)
            output = export(
                [field_bundle()],
                root,
                GS3PassOptions(include_planting_passes=True, include_border_passes=True, grouping=GROUPING_PER_PASS),
            )
            lines = curved_track_lines(eic_root(output))
            report = debug_report(root)["guidance_passes"]
            self.assertEqual(len(lines), 90)
            self.assertEqual(report["fields"][0]["catalog_objects"], 90)
            self.assertEqual(len(list(eic_root(output).rglob("CurveTrack*.fdShape"))), 90)

    def test_13_file_references_and_orphans(self):
        with tempfile.TemporaryDirectory(prefix="fs-gs3-ref-") as temp:
            output = export(
                [field_bundle()],
                Path(temp),
                GS3PassOptions(include_planting_passes=True, include_border_passes=True, grouping=GROUPING_PER_PASS),
            )
            for folder, element in curved_track_lines(eic_root(output)):
                self.assertEqual(element.attrib["fileName"], f"CurveTrack{element.attrib['erid'].strip('{}')}")
                self.assertTrue((folder / f"{element.attrib['fileName']}.fdShape").is_file())

        original = GS3PassGuidanceWriter.write_field

        def with_orphan(self, field_plan, field_dir):
            result = original(self, field_plan, field_dir)
            payload, _ = write_curvetrack_fdshape([[(-55.0, -12.0), (-55.0, -12.001)]])
            (field_dir / f"CurveTrack{uuid.uuid4()}.fdShape").write_bytes(payload)
            return result

        with tempfile.TemporaryDirectory(prefix="fs-gs3-orf-") as temp:
            root = Path(temp)
            with mock.patch.object(GS3PassGuidanceWriter, "write_field", with_orphan):
                with self.assertRaisesRegex(RuntimeError, "arquivo orfao"):
                    export([field_bundle()], root, GS3PassOptions(include_planting_passes=True))
            self.assertFalse((root / "GS3_2630").exists())

    def test_14_uuids_valid_and_stable(self):
        erids = []
        for run in range(2):
            with tempfile.TemporaryDirectory(prefix=f"fs-gs3-uuid{run}-") as temp:
                output = export(
                    [field_bundle()],
                    Path(temp),
                    GS3PassOptions(include_planting_passes=True, include_border_passes=True),
                )
                current = []
                for folder, element in curved_track_lines(eic_root(output)):
                    current.append(str(uuid.UUID(element.attrib["erid"].strip("{}"))))
                    catalog = (folder / "ImportExport.SpatialCatalog").read_text(encoding="utf-8")
                    self.assertIn(f'eridFieldRef="{{{FIELD_A}}}"', catalog)
                erids.append(sorted(current))
        self.assertEqual(erids[0], erids[1])

        bundle = field_bundle(planting=2, border=0, operational_noise=False)
        expected = {item.item_erid for item in bundle.features if item.extra["canonical_role"] == "WORK_PATH"}
        with tempfile.TemporaryDirectory(prefix="fs-gs3-uuidpp-") as temp:
            output = export([bundle], Path(temp), GS3PassOptions(include_planting_passes=True, grouping=GROUPING_PER_PASS))
            got = {element.attrib["erid"].strip("{}") for _, element in curved_track_lines(eic_root(output))}
        self.assertEqual(got, expected)

    def test_monitor_registry_forwards_options(self):
        with tempfile.TemporaryDirectory(prefix="fs-gs3-reg-") as temp:
            output = export_fields(
                [field_bundle(planting=3, border=0)],
                Path(temp),
                profile_id="john_deere_gs3",
                gs3_pass_options=GS3PassOptions(include_planting_passes=True),
            )
            self.assertEqual(len(curved_track_lines(eic_root(output))), 1)

    def test_dual_gen4_side_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="fs-dual-") as temp:
            root = Path(temp)
            DeereDualWriter().write([field_bundle()], root / "off")
            DeereDualWriter().write(
                [field_bundle()],
                root / "on",
                gs3_pass_options=GS3PassOptions(include_planting_passes=True, export_gen4=False),
            )
            off = sorted(p.relative_to(root / "off" / "Gen4") for p in (root / "off" / "Gen4").rglob("*") if p.is_file())
            on = sorted(p.relative_to(root / "on" / "Gen4") for p in (root / "on" / "Gen4").rglob("*") if p.is_file())
            self.assertEqual(off, on)
            for rel in off:
                if rel.suffix == ".gjson":
                    self.assertEqual((root / "off" / "Gen4" / rel).read_bytes(), (root / "on" / "Gen4" / rel).read_bytes())
            self.assertEqual(len(curved_track_lines(root / "on" / "GS3_2630" / FAZENDA / "RCD" / "EIC")), 1)
            self.assertEqual(len(curved_track_lines(root / "off" / "GS3_2630" / FAZENDA / "RCD" / "EIC")), 0)


if __name__ == "__main__":
    unittest.main()
