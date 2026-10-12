from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "qgis_plugin"))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from fortsmart_gs3_import.gs3_pass_guidance import (  # noqa: E402
    GROUPING_PER_PASS,
    GS3PassOptions,
    write_curvetrack_fdshape,
)
from fortsmart_gs3_import.gs3_rcd_core import collect_gs3_fields  # noqa: E402
from fortsmart_gs3_import.gs3_spatial_types import SpatialKind  # noqa: E402
from fortsmart_gs3_import.jd_fdshape import (  # noqa: E402
    decode_fdshape,
    decode_fdshape_tracks,
    is_sentinel_altitude,
)
from test_gs3_pass_guidance import export, field_bundle, planting_passes  # noqa: E402


def mbr_of(tracks):
    lons = [p[0] for track in tracks for p in track]
    lats = [p[1] for track in tracks for p in track]
    return (min(lons), min(lats), max(lons), max(lats))


def work_lines(output: Path, kind: SpatialKind | None = SpatialKind.TRABALHO):
    result = collect_gs3_fields(output, unify_farms=False)
    return [
        feature
        for bundle in result.bundles
        for feature in bundle.features
        if feature.geometry == "line" and (kind is None or feature.kind == kind)
    ]


class FdShapeTracksTest(unittest.TestCase):
    def test_float32_sentinel_is_detected(self):
        self.assertTrue(is_sentinel_altitude(999.9000244140625))
        self.assertFalse(is_sentinel_altitude(1.0))

    def test_tracks_are_split_at_sentinels(self):
        tracks = [
            [(-55.0, -12.0), (-55.0, -12.001), (-55.0, -12.002)],
            [(-54.999, -12.0), (-54.999, -12.002)],
            [(-54.998, -12.0), (-54.998, -12.002)],
        ]
        payload, reference = write_curvetrack_fdshape(tracks)
        with tempfile.TemporaryDirectory(prefix="fs-fdshape-") as temp:
            path = Path(temp) / "CurveTrack.fdShape"
            path.write_bytes(payload)
            decoded = decode_fdshape_tracks(path, mbr=mbr_of(tracks), reference=reference)
            self.assertEqual(decoded.sentinel_count, 3)
            self.assertEqual(len(decoded.tracks), 3)
            for got, expected in zip(decoded.tracks, tracks):
                self.assertEqual(len(got), len(expected))
                for (lon, lat), (elon, elat) in zip(got, expected):
                    self.assertAlmostEqual(lon, elon, places=12)
                    self.assertAlmostEqual(lat, elat, places=12)

    def test_single_track_matches_legacy_decoder(self):
        tracks = [[(-55.0, -12.0), (-55.0, -12.001), (-54.9995, -12.002)]]
        payload, reference = write_curvetrack_fdshape(tracks)
        with tempfile.TemporaryDirectory(prefix="fs-fdshape-") as temp:
            path = Path(temp) / "CurveTrack.fdShape"
            path.write_bytes(payload)
            mbr = mbr_of(tracks)
            decoded = decode_fdshape_tracks(path, mbr=mbr, reference=reference)
            legacy = decode_fdshape(path, mbr=mbr, reference=reference)
            self.assertEqual(decoded.tracks, [legacy.points])


class CurveTrackImportTest(unittest.TestCase):
    def test_grouped_planting_passes_import_as_separate_lines(self):
        with tempfile.TemporaryDirectory(prefix="fs-gs3-import-") as temp:
            output = export([field_bundle()], Path(temp), GS3PassOptions(include_planting_passes=True))
            lines = work_lines(output)
            self.assertEqual(len(lines), 88)
            self.assertEqual(len({line.item_erid for line in lines}), 88)
            self.assertEqual(lines[0].name, "PLANTIO P1-P88 1/88")
            self.assertEqual(lines[-1].extra["track_index"], "88")
            expected = planting_passes(lines[0].field_erid, lines[0].talhao, -55.0, -12.0)
            for line, source in zip(lines, expected):
                self.assertTrue(all(abs(lon - source.points[0][0]) < 1e-9 for lon, _ in line.points))
                self.assertAlmostEqual(line.points[0][1], source.points[0][1], places=9)
                self.assertAlmostEqual(line.points[-1][1], source.points[-1][1], places=9)

    def test_one_track_per_curved_track_line_keeps_name_and_erid(self):
        with tempfile.TemporaryDirectory(prefix="fs-gs3-import-pp-") as temp:
            output = export(
                [field_bundle(planting=3, border=0)],
                Path(temp),
                GS3PassOptions(include_planting_passes=True, grouping=GROUPING_PER_PASS),
            )
            lines = work_lines(output, kind=None)
            self.assertEqual([line.name for line in lines], ["P1", "P2", "P3"])
            self.assertTrue(all("track_index" not in line.extra for line in lines))

    def test_reimport_is_deterministic(self):
        with tempfile.TemporaryDirectory(prefix="fs-gs3-import-det-") as temp:
            output = export([field_bundle(planting=5)], Path(temp), GS3PassOptions(include_planting_passes=True))
            first = [line.item_erid for line in work_lines(output)]
            second = [line.item_erid for line in work_lines(output)]
            self.assertEqual(first, second)
            self.assertEqual(len(set(first)), 5)


if __name__ == "__main__":
    unittest.main()
