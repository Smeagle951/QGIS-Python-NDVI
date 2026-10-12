from __future__ import annotations

import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "qgis_plugin"))

from fortsmart_gs3_import import gs3_rcd_core as core  # noqa: E402
from fortsmart_gs3_import.gs3_spatial_types import SpatialKind  # noqa: E402
from fortsmart_gs3_import.jd_fdshape import decode_fdshape  # noqa: E402

REFERENCE = (-54.43, -15.33)
META = ("Cliente", "Fazenda", "Talhao", "6f1d2c3b-4a59-4e6f-8a7b-9c0d1e2f3a4b")


def curvetrack_payload(tracks, reference=REFERENCE) -> bytes:
    """CurveTrack como o monitor grava: origem no centro do talhao, fora das trilhas."""
    records = bytearray()
    count = 0
    previous = (0.0, 0.0)
    for track in tracks:
        records += struct.pack("<ddf", previous[0], previous[1], 999.9)
        count += 1
        for lon, lat in track:
            previous = (lon - reference[0], lat - reference[1])
            records += struct.pack("<ddf", previous[0], previous[1], 1.0)
            count += 1
    header = bytearray(64)
    header[16:18] = b"\x01\x02"
    struct.pack_into("<Q", header, 56, count)
    return bytes(header) + bytes(records)


def tracks_out_of_order():
    """Passadas gravadas alternando em torno da central (0, +1, -1, +2, -2)."""
    order = (0, 1, -1, 2, -2)
    return [
        [(REFERENCE[0] + 0.001 * k, REFERENCE[1] + 0.004), (REFERENCE[0] + 0.001 * k, REFERENCE[1] - 0.004)]
        for k in order
    ]


def mbr(tracks):
    lons = [p[0] for t in tracks for p in t]
    lats = [p[1] for t in tracks for p in t]
    return (min(lons), min(lats), max(lons), max(lats))


class CurveTrackImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fortsmart-import-")
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _shape(self, tracks) -> Path:
        path = self.dir / "CurveTrackTest.fdShape"
        path.write_bytes(curvetrack_payload(tracks))
        return path

    def test_sentinel_origin_is_never_a_vertex(self):
        tracks = tracks_out_of_order()
        wide = (REFERENCE[0] - 0.01, REFERENCE[1] - 0.01, REFERENCE[0] + 0.01, REFERENCE[1] + 0.01)
        result = decode_fdshape(self._shape(tracks), mbr=wide, reference=REFERENCE)
        self.assertNotIn(REFERENCE, result.points)
        self.assertEqual(len(result.points), 10)

    def test_multi_track_file_imports_one_line_per_pass(self):
        tracks = tracks_out_of_order()
        imported = core._curved_track_tracks(self._shape(tracks), mbr(tracks), REFERENCE)
        self.assertEqual(imported, tracks)

    def test_multi_track_file_with_single_valid_track_is_not_joined(self):
        tracks = tracks_out_of_order()
        imported = core._curved_track_tracks(self._shape(tracks), mbr(tracks[:1]), REFERENCE)
        self.assertEqual(imported, [tracks[0]])

    def test_curvetrack_roles_follow_name(self):
        tracks = tracks_out_of_order()
        self._shape(tracks)
        for name, kind, role in (
            ("BORDADURA B1-B1.4", SpatialKind.BORDADURA, "FIELD_HEADLAND_PATH"),
            ("PLANTIO P1-P5", SpatialKind.TRABALHO, "WORK_PATH"),
            ("Curva 3", SpatialKind.CURVA_ADAPTADA, "GUIDANCE_TRACK"),
        ):
            element = ET.fromstring(
                f'<CurvedTrackLine name="{name}" fileName="CurveTrackTest" erid="{{{META[3]}}}">'
                f'<MBR west="{mbr(tracks)[0]}" south="{mbr(tracks)[1]}" east="{mbr(tracks)[2]}" north="{mbr(tracks)[3]}"/>'
                f'<vrReferenceLongitude value="{REFERENCE[0]}"/><vrReferenceLatitude value="{REFERENCE[1]}"/>'
                "</CurvedTrackLine>"
            )
            features = core._parse_curved_track_feature(element, self.dir, META, "catalog")
            with self.subTest(name=name):
                self.assertEqual(len(features), 5)
                self.assertEqual({f.kind for f in features}, {kind})
                self.assertEqual({f.extra["canonical_role"] for f in features}, {role})


class ABLineImportTest(unittest.TestCase):
    def _abline(self, a_lon: str, b_lon: str):
        element = ET.fromstring(
            f'<ABLine name="milho" erid="{{3ccff1f3-5a7e-4ee5-902d-bf6329c861f8}}" active="false">'
            f'<APoint latitude="-15.35737704" longitude="{a_lon}"/>'
            f'<BPoint latitude="-15.357377066" longitude="{b_lon}"/>'
            "</ABLine>"
        )
        return core._parse_abline_feature(element, META, "catalog")

    def test_longitude_without_west_sign_is_fixed_and_flagged(self):
        feature = self._abline("54.44631424", "54.446342637")
        self.assertIsNotNone(feature)
        self.assertEqual(feature.points, [(-54.44631424, -15.35737704), (-54.446342637, -15.357377066)])
        self.assertEqual(feature.extra["longitude_sign_fixed"], "true")

    def test_regular_abline_keeps_monitor_points(self):
        feature = self._abline("-54.44631424", "-54.446342637")
        self.assertEqual(feature.points[0], (-54.44631424, -15.35737704))
        self.assertNotIn("longitude_sign_fixed", feature.extra)


if __name__ == "__main__":
    unittest.main()
