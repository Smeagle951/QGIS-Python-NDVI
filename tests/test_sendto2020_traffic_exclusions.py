"""SendTo2020 - modo opcional boundary + exclusoes dos rastros dos pneus.

Nomes de talhao, proprietario e fazenda sao ficticios.
"""

from __future__ import annotations

import copy
import gc
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "qgis_plugin"))

from fortsmart_gs3_import import sendto2020_traffic_exclusions as tx  # noqa: E402
from fortsmart_gs3_import.adapters.sendto2020_writer import SendTo2020Writer  # noqa: E402
from fortsmart_gs3_import.gs3_spatial_types import (  # noqa: E402
    Gs3FieldBundle,
    Gs3SpatialFeature,
    SpatialKind,
)
from fortsmart_gs3_import.sendto2020_preflight import DBF_FIELDS  # noqa: E402

PLANNER_ROOT = Path(os.environ.get("FORTSMART_PRECISION_ROOT", PROJECT_ROOT.parent / "PYTHON"))
WORKING_CRS = "EPSG:32721"
ORIGIN = (500_000.0, 8_670_000.0)
PACKAGE_EXTENSIONS = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".mcd")
DIAGNOSTICS = "SENDTO2020_DIAGNOSTIC_EXPORT"

SCHEMA = """
CREATE TABLE fortsmart_operation_manifest (
    generation_id TEXT PRIMARY KEY, field_id TEXT NOT NULL, field_name TEXT NOT NULL,
    operation_type TEXT NOT NULL, operation_id TEXT NOT NULL, created_at TEXT NOT NULL,
    algorithm_version TEXT NOT NULL, status TEXT NOT NULL, source_mode TEXT NOT NULL,
    source_crs TEXT NOT NULL, working_crs TEXT NOT NULL, sprayer_name TEXT NOT NULL,
    planting_layer TEXT NOT NULL, spray_layer TEXT NOT NULL, selected_spray_count INTEGER NOT NULL,
    analyzed_planting_rows INTEGER NOT NULL, affected_rows_count INTEGER NOT NULL,
    intersection_count INTEGER NOT NULL, excluded_length_m REAL NOT NULL,
    sprayer_snapshot_json TEXT NOT NULL, cross_snapshot_json TEXT NOT NULL);
CREATE TABLE fortsmart_operation_planting_rows (
    generation_id TEXT NOT NULL, source_row_id TEXT NOT NULL, source_feature_id TEXT NOT NULL,
    pass_id INTEGER NOT NULL, row_id INTEGER NOT NULL, geometry_json TEXT NOT NULL, length_m REAL NOT NULL);
CREATE TABLE fortsmart_operation_tire_tracks (
    generation_id TEXT NOT NULL, track_id TEXT NOT NULL, spray_pass_id TEXT NOT NULL,
    source_feature_id TEXT NOT NULL, side TEXT NOT NULL, center_offset_m REAL NOT NULL,
    track_width_m REAL NOT NULL, physical_tire_width_m REAL NOT NULL, safety_margin_m REAL NOT NULL,
    effective_exclusion_width_m REAL NOT NULL, geometry_json TEXT NOT NULL);
CREATE TABLE fortsmart_operation_segments (
    generation_id TEXT NOT NULL, segment_id TEXT NOT NULL, field_id TEXT NOT NULL,
    operation_id TEXT NOT NULL, operation_type TEXT NOT NULL, source_layer_role TEXT NOT NULL,
    source_feature_id TEXT NOT NULL, source_row_id TEXT NOT NULL, source_mode TEXT NOT NULL,
    geometry_json TEXT NOT NULL, station_start_m REAL NOT NULL, station_end_m REAL NOT NULL,
    length_m REAL NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
    algorithm_version TEXT NOT NULL, spray_pass_id TEXT NOT NULL, spray_source_feature_id TEXT NOT NULL,
    tire_track_id TEXT NOT NULL, tire_side TEXT NOT NULL, physical_tire_width_m REAL NOT NULL,
    safety_margin_m REAL NOT NULL, effective_exclusion_width_m REAL NOT NULL,
    override_status TEXT NOT NULL, user_override INTEGER NOT NULL);
"""


# --------------------------------------------------------------------------
# Fixtures geometricas (metros relativos a ORIGIN no working_crs)
# --------------------------------------------------------------------------

def m(x: float, y: float) -> tuple[float, float]:
    return (ORIGIN[0] + x, ORIGIN[1] + y)


def rect(x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
    return [m(x0, y0), m(x1, y0), m(x1, y1), m(x0, y1), m(x0, y0)]


def to_wgs84(points):
    from pyproj import Transformer

    transformer = Transformer.from_crs(WORKING_CRS, "EPSG:4326", always_xy=True)
    return [tuple(transformer.transform(x, y)) for x, y in points]


def to_metric_geometry(geometry):
    from pyproj import Transformer
    from shapely.ops import transform

    transformer = Transformer.from_crs("EPSG:4326", WORKING_CRS, always_xy=True)
    return transform(transformer.transform, geometry)


def boundary_feature(name: str, ring_metric, *, field_erid: str | None = None) -> Gs3SpatialFeature:
    from shapely.geometry import Polygon

    projected = Polygon(ring_metric)
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
        rings=[to_wgs84(ring_metric)],
        extra={
            "fortsmart_role": "EXTERNAL_BOUNDARY",
            "source_geometry": {
                "crs": WORKING_CRS, "crs_wkt": "", "crs_valid": True, "crs_geographic": False,
                "transform": "OK", "bbox": list(projected.bounds),
                "centroid": [projected.centroid.x, projected.centroid.y],
                "has_z": False, "has_m": False, "wkb_type": "Polygon", "parts": 1, "valid": True,
            },
        },
    )


def extra_feature(name: str, role: str, geometry: str, ring_or_line_metric) -> Gs3SpatialFeature:
    coords = to_wgs84(ring_or_line_metric)
    return Gs3SpatialFeature(
        kind=SpatialKind.LIMITE if geometry == "polygon" else SpatialKind.CURVA_AB,
        cliente="PROPRIETARIO TESTE",
        fazenda="FAZENDA TESTE",
        talhao=name,
        field_erid=f"field-{name}",
        item_erid=f"{role}-{name}",
        name=role,
        source_file="unit-test",
        catalog_file="qgis",
        geometry=geometry,
        rings=[coords] if geometry == "polygon" else [],
        points=coords if geometry == "line" else [],
        extra={"fortsmart_role": role},
    )


def bundle_for(name: str, ring_metric=None, extras=()) -> Gs3FieldBundle:
    ring_metric = ring_metric or rect(0, 0, 200, 100)
    return Gs3FieldBundle(
        cliente="PROPRIETARIO TESTE",
        fazenda="FAZENDA TESTE",
        talhao=name,
        field_erid=f"field-{name}",
        features=[boundary_feature(name, ring_metric), *extras],
    )


def track(track_id: str, points, *, side="LEFT", pass_id="1", tire=0.40, margin=0.10, width=None):
    return {
        "track_id": track_id, "spray_pass_id": pass_id, "source_feature_id": f"SP{pass_id}",
        "side": side, "center_offset_m": -1.0 if side == "LEFT" else 1.0, "track_width_m": 2.0,
        "physical_tire_width_m": tire, "safety_margin_m": margin,
        "effective_exclusion_width_m": width if width is not None else tire + 2 * margin,
        "points": [m(x, y) for x, y in points],
    }


def vertical_rows(x_values, y0=5.0, y1=95.0):
    return [{"source_row_id": f"R{index:03d}", "points": [m(x, y0), m(x, y1)]}
            for index, x in enumerate(x_values)]


def work_off_for(rows, tracks):
    """WORK_OFF de referencia: linha x faixa de cada pneu (como o Planner persiste)."""
    from shapely.geometry import LineString, Point

    segments = []
    for row in rows:
        line = LineString(row["points"])
        for item in tracks:
            strip = LineString(item["points"]).buffer(item["effective_exclusion_width_m"] / 2.0)
            piece = line.intersection(strip)
            if piece.is_empty or piece.length <= 0:
                continue
            coords = list(piece.coords)
            start = line.project(Point(coords[0]))
            segments.append({
                "segment_id": f"SH_{row['source_row_id']}_{item['track_id']}",
                "source_row_id": row["source_row_id"], "points": coords,
                "station_start_m": start, "station_end_m": start + piece.length, "length_m": piece.length,
                "tire_track_id": item["track_id"], "spray_pass_id": item["spray_pass_id"],
                "tire_side": item["side"], "reason": "SPRAYER_TIRE_TRACK",
                "physical": item["physical_tire_width_m"], "margin": item["safety_margin_m"],
                "width": item["effective_exclusion_width_m"],
            })
    return segments


def write_store(path: Path, *, field_id="TL-01", status="ACTIVE", generation_id="GEN-0001",
                tracks=(), rows=(), segments=None, working_crs=WORKING_CRS, extra_manifests=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    segments = work_off_for(rows, tracks) if segments is None else segments
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(SCHEMA)
        manifests = [(generation_id, field_id, status), *extra_manifests]
        for gen, field, state in manifests:
            connection.execute(
                "INSERT INTO fortsmart_operation_manifest VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (gen, field, field, "PLANTING_SHUTOFF", f"PLANTING_SHUTOFF:{field}", "2026-01-01T00:00:00+00:00",
                 "test", state, "EXISTING_PASSES", working_crs, working_crs, "PULV TESTE", "linhas", "pulv",
                 1, len(rows), len(rows), len(segments), 0.0, "{}", "{}"),
            )
        for row in rows:
            connection.execute(
                "INSERT INTO fortsmart_operation_planting_rows VALUES (?,?,?,?,?,?,?)",
                (generation_id, row["source_row_id"], row["source_row_id"], 1, 1, json.dumps(row["points"]), 90.0),
            )
        for item in tracks:
            connection.execute(
                "INSERT INTO fortsmart_operation_tire_tracks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (generation_id, item["track_id"], item["spray_pass_id"], item["source_feature_id"], item["side"],
                 item["center_offset_m"], item["track_width_m"], item["physical_tire_width_m"],
                 item["safety_margin_m"], item["effective_exclusion_width_m"], json.dumps(item["points"])),
            )
        for seg in segments:
            connection.execute(
                "INSERT INTO fortsmart_operation_segments VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (generation_id, seg["segment_id"], field_id, f"PLANTING_SHUTOFF:{field_id}", "PLANTING_SHUTOFF",
                 "PLANTING_ROW", seg["source_row_id"], seg["source_row_id"], "EXISTING_PASSES",
                 json.dumps(seg["points"]), seg["station_start_m"], seg["station_end_m"], seg["length_m"],
                 seg.get("state", "WORK_OFF"), seg["reason"], "2026-01-01T00:00:00+00:00", "test",
                 seg["spray_pass_id"], f"SP{seg['spray_pass_id']}", seg["tire_track_id"], seg["tire_side"],
                 seg["physical"], seg["margin"], seg["width"], "VALID", 0),
            )
    return path


def read_shape(folder: Path):
    import fiona
    from shapely.geometry import shape

    shp = next(Path(folder).glob("*.shp"))
    with fiona.open(shp) as source:
        records = list(source)
        schema = list(source.schema["properties"])
        epsg = source.crs.to_epsg()
    return [shape(record["geometry"]) for record in records], schema, epsg


def holes_of(geometry):
    from shapely.geometry import Polygon

    polygons = [geometry] if geometry.geom_type == "Polygon" else list(geometry.geoms)
    return [Polygon(ring) for polygon in polygons for ring in polygon.interiors]


def short_side(polygon) -> float:
    rectangle = polygon.minimum_rotated_rectangle
    coords = list(rectangle.exterior.coords)
    return min(math.dist(a, b) for a, b in zip(coords, coords[1:]))


STRAIGHT_PAIR = (
    track("TT_1_LEFT", [(20, 40), (180, 40)], side="LEFT"),
    track("TT_1_RIGHT", [(20, 42), (180, 42)], side="RIGHT"),
)
ROWS = vertical_rows([10 + 5 * i for i in range(37)])


class TrafficExportCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fs2020_tx_test_"))
        self.out = self.tmp / "out"

    def tearDown(self) -> None:
        gc.collect()
        shutil.rmtree(self.tmp)

    def store(self, name="TL-01", **kwargs) -> Path:
        return write_store(self.tmp / "fortsmart_data" / name / f"fortsmart_{name}.gpkg", field_id=name, **kwargs)

    def export_traffic(self, bundles, sources, out=None, **options):
        return SendTo2020Writer().write(
            bundles, out or self.out,
            sendto2020_boundary_mode=tx.BOUNDARY_WITH_TRAFFIC_EXCLUSIONS,
            sendto2020_traffic_sources=sources, **options,
        )

    def source(self, path: Path, field_id="TL-01"):
        return tx.TrafficGenerationSource(store_path=path, field_id=field_id)

    def traffic_report(self, out=None) -> dict:
        return json.loads(((out or self.out) / DIAGNOSTICS / tx.TRAFFIC_REPORT_JSON).read_text(encoding="utf-8"))

    def assert_blocked(self, code: str, call):
        with self.assertRaises(tx.TrafficExclusionError) as ctx:
            call()
        self.assertEqual(ctx.exception.code, code)
        self.assertFalse((self.out / "SendTo2020").exists(), "pacote parcial publicado")
        return ctx.exception

    def export_ok(self, tracks, rows=ROWS, segments=None, bundle=None, name="TL-01"):
        path = self.store(name=name, tracks=tracks, rows=rows, segments=segments)
        folder = self.export_traffic([bundle or bundle_for(name)], {name: self.source(path, name)})
        geometries, schema, epsg = read_shape(folder)
        return folder, geometries, schema, epsg, self.traffic_report()


class NormalModeTests(TrafficExportCase):
    def test_2020_normal_mode_unchanged(self) -> None:
        bundle = bundle_for("TL-01")
        default = SendTo2020Writer().write([bundle], self.tmp / "a")
        explicit = SendTo2020Writer().write(
            [bundle], self.tmp / "b", sendto2020_boundary_mode=tx.BOUNDARY_ONLY
        )
        with mock.patch.object(tx.SendTo2020TrafficExclusionMode, "apply", side_effect=AssertionError):
            patched = SendTo2020Writer().write([bundle], self.tmp / "c")
        for ext in PACKAGE_EXTENSIONS:
            reference = next(default.glob(f"*{ext}")).read_bytes()
            self.assertEqual(reference, next(explicit.glob(f"*{ext}")).read_bytes(), ext)
            self.assertEqual(reference, next(patched.glob(f"*{ext}")).read_bytes(), ext)
        report = json.loads((self.tmp / "a" / DIAGNOSTICS / "SENDTO2020_EXPORT_REPORT.json").read_text("utf-8"))
        self.assertNotIn("boundary_mode", report)
        self.assertNotIn("traffic_exclusions", report)
        self.assertFalse((self.tmp / "a" / DIAGNOSTICS / tx.TRAFFIC_REPORT_JSON).exists())
        geometries, _, _ = read_shape(default)
        self.assertEqual(holes_of(geometries[0]), [])

    def test_2020_unknown_boundary_mode_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            SendTo2020Writer().write([bundle_for("TL-01")], self.out, sendto2020_boundary_mode="OUTRO")
        self.assertIn(tx.BOUNDARY_MODE_INVALID, str(ctx.exception))

    def test_2020_failed_traffic_mode_keeps_previous_package_and_normal_mode(self) -> None:
        SendTo2020Writer().write([bundle_for("TL-01")], self.out)
        marker = self.out / "SendTo2020" / "marker.txt"
        marker.write_text("pacote anterior", encoding="utf-8")
        with self.assertRaises(tx.TrafficExclusionError):
            self.export_traffic([bundle_for("TL-01")], {"TL-01": self.source(self.tmp / "nao_existe.gpkg")})
        self.assertTrue(marker.is_file())
        again = SendTo2020Writer().write([bundle_for("TL-01")], self.tmp / "depois")
        self.assertEqual(len(list(again.glob("*.shp"))), 1)


class GenerationStateTests(TrafficExportCase):
    def test_2020_requires_active_generation_for_exclusions(self) -> None:
        error = self.assert_blocked(
            tx.NO_ACTIVE_GENERATION,
            lambda: self.export_traffic([bundle_for("TL-01")], {"TL-01": self.source(self.tmp / "x.gpkg")}),
        )
        self.assertIn(tx.MSG_NO_ACTIVE, str(error))
        self.assert_blocked(tx.NO_ACTIVE_GENERATION, lambda: self.export_traffic([bundle_for("TL-01")], {}))
        empty = self.tmp / "vazio.gpkg"
        sqlite3.connect(empty).close()
        self.assert_blocked(
            tx.NO_ACTIVE_GENERATION,
            lambda: self.export_traffic([bundle_for("TL-01")], {"TL-01": self.source(empty)}),
        )
        report = self.traffic_report()
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["fields"][-1]["error_code"], tx.NO_ACTIVE_GENERATION)

    def test_2020_stale_generation_rejected(self) -> None:
        path = self.store(status="STALE", tracks=STRAIGHT_PAIR, rows=ROWS)
        error = self.assert_blocked(
            tx.STALE_GENERATION, lambda: self.export_traffic([bundle_for("TL-01")], {"TL-01": self.source(path)})
        )
        self.assertIn(tx.MSG_STALE, str(error))

    def test_2020_draft_generation_rejected(self) -> None:
        path = self.store(status="DRAFT", tracks=STRAIGHT_PAIR, rows=ROWS)
        error = self.assert_blocked(
            tx.DRAFT_GENERATION, lambda: self.export_traffic([bundle_for("TL-01")], {"TL-01": self.source(path)})
        )
        self.assertIn(tx.MSG_DRAFT, str(error))

    def test_2020_superseded_only_is_not_active(self) -> None:
        path = self.store(status="SUPERSEDED", tracks=STRAIGHT_PAIR, rows=ROWS)
        self.assert_blocked(
            tx.NO_ACTIVE_GENERATION,
            lambda: self.export_traffic([bundle_for("TL-01")], {"TL-01": self.source(path)}),
        )

    def test_2020_field_id_mismatch_rejected(self) -> None:
        path = self.store(name="TL-01", tracks=STRAIGHT_PAIR, rows=ROWS)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("UPDATE fortsmart_operation_manifest SET field_id = 'TL-99'")
        self.assert_blocked(
            tx.FIELD_ID_MISMATCH,
            lambda: self.export_traffic([bundle_for("TL-01")], {"TL-01": self.source(path)}),
        )

    def test_2020_generation_store_is_read_only(self) -> None:
        path = self.store(tracks=STRAIGHT_PAIR, rows=ROWS)
        before = path.read_bytes()
        self.export_traffic([bundle_for("TL-01")], {"TL-01": self.source(path)})
        self.assertEqual(before, path.read_bytes())

    def test_2020_work_off_without_track_rejected(self) -> None:
        segments = work_off_for(ROWS, STRAIGHT_PAIR)
        segments[0]["tire_track_id"] = "TT_INEXISTENTE"
        path = self.store(tracks=STRAIGHT_PAIR, rows=ROWS, segments=segments)
        self.assert_blocked(
            tx.GENERATION_INVALID,
            lambda: self.export_traffic([bundle_for("TL-01")], {"TL-01": self.source(path)}),
        )


class GeometryTests(TrafficExportCase):
    def test_2020_inner_exclusion_created(self) -> None:
        tracks = (track("TT_1_LEFT", [(20, 40), (180, 40)]),)
        folder, geometries, _, epsg, report = self.export_ok(tracks)
        self.assertEqual(len(geometries), 1)
        self.assertEqual(len(holes_of(geometries[0])), 1)
        self.assertEqual(epsg, 4326)
        field = report["fields"][0]
        self.assertEqual(field["status"], "EXPORTED")
        self.assertEqual(field["interior_rings"], 1)
        self.assertEqual(field["exterior_rings"], 1)
        self.assertEqual(field["postwrite"]["status"], "PASS")
        main = json.loads((self.out / DIAGNOSTICS / "SENDTO2020_EXPORT_REPORT.json").read_text("utf-8"))
        self.assertEqual(main["boundary_mode"], tx.BOUNDARY_WITH_TRAFFIC_EXCLUSIONS)
        self.assertEqual(main["fields"][0]["boundary_source"], "EXTERNAL_BOUNDARY")
        self.assertEqual(main["fields"][0]["generation_id"], "GEN-0001")

    def test_2020_tire_track_buffer_metric(self) -> None:
        tracks = (track("TT_1_LEFT", [(20, 40), (180, 40)], tire=0.40, margin=0.10),)
        _, geometries, _, _, _ = self.export_ok(tracks)
        hole = to_metric_geometry(holes_of(geometries[0])[0])
        self.assertAlmostEqual(short_side(hole), 0.60, delta=0.005)
        self.assertAlmostEqual(hole.length, 2 * 160 + math.pi * 0.6, delta=0.5)
        with self.assertRaises(tx.TrafficExclusionError) as ctx:
            tx.MetricProjector("EPSG:4326")
        self.assertEqual(ctx.exception.code, tx.WORKING_CRS_INVALID)

    def test_2020_geographic_working_crs_blocked(self) -> None:
        path = self.store(tracks=STRAIGHT_PAIR, rows=ROWS, working_crs="EPSG:4326")
        self.assert_blocked(
            tx.WORKING_CRS_INVALID,
            lambda: self.export_traffic([bundle_for("TL-01")], {"TL-01": self.source(path)}),
        )

    def test_2020_crs_transformed_after_buffer(self) -> None:
        _, geometries, _, epsg, _ = self.export_ok(STRAIGHT_PAIR)
        prj = next((self.out / "SendTo2020").glob("*.prj")).read_text()
        self.assertIn("WGS", prj.upper())
        self.assertEqual(epsg, 4326)
        for hole in holes_of(geometries[0]):
            xmin, ymin, xmax, ymax = hole.bounds
            self.assertTrue(-180 <= xmin <= xmax <= 180 and -90 <= ymin <= ymax <= 90)
            self.assertLess(xmax - xmin, 0.01)
            self.assertAlmostEqual(short_side(to_metric_geometry(hole)), 0.60, delta=0.005)

    def test_2020_left_right_tracks_preserved(self) -> None:
        _, geometries, _, _, report = self.export_ok(STRAIGHT_PAIR)
        self.assertEqual(len(holes_of(geometries[0])), 2)
        field = report["fields"][0]
        by_id = {item["tire_track_id"]: item for item in field["tracks"]}
        self.assertEqual(set(by_id), {"TT_1_LEFT", "TT_1_RIGHT"})
        self.assertEqual(by_id["TT_1_LEFT"]["tire_side"], "LEFT")
        self.assertEqual(by_id["TT_1_RIGHT"]["tire_side"], "RIGHT")
        for item in by_id.values():
            self.assertEqual(item["spray_pass_id"], "1")
            self.assertEqual(item["generation_id"], "GEN-0001")
            self.assertEqual(item["concept"], tx.TRAFFIC_EXCLUSION)
            self.assertEqual(item["classification"], tx.INTERIOR_SAFE)

    def test_2020_effective_exclusion_width_used(self) -> None:
        with_margin = (track("TT_A", [(20, 40), (180, 40)], tire=0.40, margin=0.10),)
        _, geometries, _, _, _ = self.export_ok(with_margin)
        self.assertAlmostEqual(short_side(to_metric_geometry(holes_of(geometries[0])[0])), 0.60, delta=0.005)
        zero_margin = (track("TT_B", [(20, 40), (180, 40)], tire=0.40, margin=0.0),)
        self.out = self.tmp / "zero"
        path = self.store(name="TL-02", tracks=zero_margin, rows=ROWS)
        folder = self.export_traffic([bundle_for("TL-02")], {"TL-02": self.source(path, "TL-02")})
        geometries, _, _ = read_shape(folder)
        self.assertAlmostEqual(short_side(to_metric_geometry(holes_of(geometries[0])[0])), 0.40, delta=0.005)
        persisted_width = (track("TT_C", [(20, 40), (180, 40)], tire=0.40, margin=0.10, width=0.90),)
        self.out = self.tmp / "persisted"
        path = self.store(name="TL-03", tracks=persisted_width, rows=ROWS)
        folder = self.export_traffic([bundle_for("TL-03")], {"TL-03": self.source(path, "TL-03")})
        geometries, _, _ = read_shape(folder)
        self.assertAlmostEqual(short_side(to_metric_geometry(holes_of(geometries[0])[0])), 0.90, delta=0.005)

    def test_2020_external_boundary_not_modified(self) -> None:
        bundle = bundle_for("TL-01")
        snapshot = copy.deepcopy(bundle.features[0].rings)
        normal = SendTo2020Writer().write([bundle], self.tmp / "normal")
        path = self.store(tracks=STRAIGHT_PAIR, rows=ROWS)
        traffic = self.export_traffic([bundle], {"TL-01": self.source(path)})
        self.assertEqual(snapshot, bundle.features[0].rings)
        normal_geometry = read_shape(normal)[0][0]
        traffic_geometry = read_shape(traffic)[0][0]
        self.assertEqual(list(normal_geometry.exterior.coords), list(traffic_geometry.exterior.coords))
        self.assertEqual(normal_geometry.bounds, traffic_geometry.bounds)
        normal_dbf = next(normal.glob("*.dbf")).read_bytes()
        self.assertEqual(normal_dbf, next(traffic.glob("*.dbf")).read_bytes())
        for ext in (".prj", ".cpg", ".mcd"):
            self.assertEqual(next(normal.glob(f"*{ext}")).read_bytes(), next(traffic.glob(f"*{ext}")).read_bytes())

    def test_2020_headland_not_promoted(self) -> None:
        extras = (
            extra_feature("TL-01", "HEADLAND", "polygon", rect(5, 5, 195, 95)),
            extra_feature("TL-01", "INTERNAL_BOUNDARY", "polygon", rect(60, 60, 70, 70)),
            extra_feature("TL-01", "OBSTACLE", "polygon", rect(100, 70, 104, 74)),
        )
        tracks = (track("TT_1_LEFT", [(20, 40), (180, 40)]),)
        _, geometries, _, _, report = self.export_ok(tracks, bundle=bundle_for("TL-01", extras=extras))
        self.assertEqual(len(holes_of(geometries[0])), 1)
        self.assertEqual(report["fields"][0]["exported_exclusions"], 1)

    def test_2020_work_path_not_promoted(self) -> None:
        extras = (
            extra_feature("TL-01", "WORK_PATH", "line", [m(10, 50), m(190, 50)]),
            extra_feature("TL-01", "AB_LINE", "line", [m(10, 60), m(190, 60)]),
            extra_feature("TL-01", "WORK_OFF", "line", [m(10, 70), m(190, 70)]),
        )
        tracks = (track("TT_1_LEFT", [(20, 40), (180, 40)]),)
        _, geometries, _, _, _ = self.export_ok(tracks, bundle=bundle_for("TL-01", extras=extras))
        self.assertEqual(len(holes_of(geometries[0])), 1)

    def test_2020_work_off_not_written_as_field(self) -> None:
        folder, geometries, schema, _, report = self.export_ok(STRAIGHT_PAIR)
        self.assertEqual(len(geometries), 1)
        self.assertEqual(tuple(schema), DBF_FIELDS)
        self.assertEqual(sorted(path.suffix for path in folder.iterdir()), sorted(PACKAGE_EXTENSIONS))
        self.assertGreater(report["fields"][0]["work_off"]["work_off_events"], 0)

    def test_2020_postwrite_validation(self) -> None:
        path = self.store(tracks=STRAIGHT_PAIR, rows=ROWS)
        mode = tx.SendTo2020TrafficExclusionMode({"TL-01": self.source(path)})
        from fortsmart_gs3_import import precision2020_boundary as pb

        exporter = pb.Precision2020BoundaryExporter()
        plan = exporter.prepare_field(bundle_for("TL-01"), lambda f: f.extra.get("fortsmart_role") == "EXTERNAL_BOUNDARY")
        traffic_plan = mode.apply(plan)
        normal = SendTo2020Writer().write([bundle_for("TL-01")], self.tmp / "normal")
        with self.assertRaises(tx.TrafficExclusionError) as ctx:
            mode.validate_written(traffic_plan, next(normal.glob("*.shp")))
        self.assertEqual(ctx.exception.code, tx.POSTWRITE_FAILED)
        self.assertIn("aneis internos", str(ctx.exception))

    def test_2020_payload_built_from_copy(self) -> None:
        path = self.store(tracks=STRAIGHT_PAIR, rows=ROWS)
        mode = tx.SendTo2020TrafficExclusionMode({"TL-01": self.source(path)})
        from fortsmart_gs3_import import precision2020_boundary as pb

        plan = pb.Precision2020BoundaryExporter().prepare_field(
            bundle_for("TL-01"), lambda f: f.extra.get("fortsmart_role") == "EXTERNAL_BOUNDARY"
        )
        original_parts = copy.deepcopy(plan.prepared.parts)
        traffic_plan = mode.apply(plan)
        self.assertEqual(original_parts, plan.prepared.parts)
        payload = mode.payload_for(traffic_plan)
        self.assertEqual(payload.mode, tx.BOUNDARY_WITH_TRAFFIC_EXCLUSIONS)
        self.assertEqual(payload.generation_id, "GEN-0001")
        self.assertEqual(payload.export_crs, "EPSG:4326")
        self.assertEqual(payload.source_crs, WORKING_CRS)
        self.assertEqual(len(payload.inner_exclusion_geometries), 2)
        self.assertEqual(payload.external_geometry["coordinates"][0], original_parts[0][0])


class ScenarioTests(TrafficExportCase):
    def test_scenario_curve(self) -> None:
        arc = [(100 + 60 * math.cos(math.radians(a)), -10 + 60 * math.sin(math.radians(a)))
               for a in range(40, 141, 5)]
        tracks = (track("TT_CURVA", arc),)
        _, geometries, _, _, report = self.export_ok(tracks)
        self.assertEqual(len(holes_of(geometries[0])), 1)
        self.assertEqual(report["fields"][0]["work_off"]["status"], "PASS")

    def test_scenario_multi_segment(self) -> None:
        zigzag = [(20, 30), (60, 45), (100, 30), (140, 45), (180, 30)]
        _, geometries, _, _, report = self.export_ok((track("TT_ZZ", zigzag),))
        self.assertEqual(len(holes_of(geometries[0])), 1)
        self.assertEqual(report["fields"][0]["work_off"]["status"], "PASS")

    def test_scenario_multiple_passes(self) -> None:
        tracks = tuple(
            track(f"TT_{p}_{side}", [(20, y + (0 if side == 'LEFT' else 2)), (180, y + (0 if side == 'LEFT' else 2))],
                  side=side, pass_id=str(p))
            for p, y in ((1, 20), (2, 45), (3, 70)) for side in ("LEFT", "RIGHT")
        )
        _, geometries, _, _, report = self.export_ok(tracks)
        self.assertEqual(len(holes_of(geometries[0])), 6)
        self.assertEqual({item["spray_pass_id"] for item in report["fields"][0]["tracks"]}, {"1", "2", "3"})

    def test_scenario_intersecting_tracks_merged(self) -> None:
        tracks = (
            track("TT_X1", [(40, 20), (160, 80)]),
            track("TT_X2", [(40, 80), (160, 20)], side="RIGHT"),
        )
        _, geometries, _, _, report = self.export_ok(tracks)
        self.assertEqual(len(holes_of(geometries[0])), 1)
        field = report["fields"][0]
        self.assertEqual(field["merged"], 1)
        self.assertEqual(set(field["exclusions"][0]["tire_track_ids"]), {"TT_X1", "TT_X2"})

    def test_scenario_track_outside_discarded_with_warning(self) -> None:
        tracks = (track("TT_IN", [(20, 40), (180, 40)]), track("TT_OUT", [(20, 140), (180, 140)], side="RIGHT"))
        _, geometries, _, _, report = self.export_ok(tracks)
        self.assertEqual(len(holes_of(geometries[0])), 1)
        field = report["fields"][0]
        self.assertEqual(field["outside_discarded"], 1)
        self.assertTrue(any("TT_OUT" in warning for warning in field["warnings"]))

    def test_scenario_all_tracks_outside_blocked(self) -> None:
        path = self.store(tracks=(track("TT_OUT", [(20, 140), (180, 140)]),), rows=ROWS)
        self.assert_blocked(
            tx.TRACKS_OUTSIDE_FIELD,
            lambda: self.export_traffic([bundle_for("TL-01")], {"TL-01": self.source(path)}),
        )

    def test_scenario_partial_track_crosses_blocked(self) -> None:
        path = self.store(tracks=(track("TT_PARCIAL", [(-20, 40), (180, 40)]),), rows=ROWS)
        error = self.assert_blocked(
            tx.EXCLUSION_CROSSES,
            lambda: self.export_traffic([bundle_for("TL-01")], {"TL-01": self.source(path)}),
        )
        self.assertIn("TT_PARCIAL", str(error))
        field = self.traffic_report()["fields"][0]
        self.assertEqual(field["crosses_exterior"], 1)
        self.assertGreater(field["tracks"][0]["clipped_area_m2"], 0)

    def test_scenario_touching_track_blocked_with_message(self) -> None:
        path = self.store(tracks=(track("TT_BORDA", [(20, 0.31), (180, 0.31)]),), rows=ROWS)
        error = self.assert_blocked(
            tx.EXCLUSION_TOUCHES,
            lambda: self.export_traffic([bundle_for("TL-01")], {"TL-01": self.source(path)}),
        )
        self.assertIn(tx.MSG_TOUCHES, str(error))

    def test_scenario_enclosed_island_blocked(self) -> None:
        tracks = (
            track("TT_N", [(40, 70), (160, 70)]), track("TT_S", [(40, 30), (160, 30)]),
            track("TT_W", [(50, 20), (50, 80)]), track("TT_E", [(150, 20), (150, 80)]),
        )
        path = self.store(tracks=tracks, rows=ROWS)
        self.assert_blocked(
            tx.ENCLOSED_ISLAND,
            lambda: self.export_traffic([bundle_for("TL-01")], {"TL-01": self.source(path)}),
        )

    def test_scenario_no_work_off(self) -> None:
        rows = vertical_rows([5.0, 195.0])
        _, geometries, _, _, report = self.export_ok(STRAIGHT_PAIR, rows=rows)
        self.assertEqual(len(holes_of(geometries[0])), 2)
        work_off = report["fields"][0]["work_off"]
        self.assertEqual((work_off["work_off_events"], work_off["simulated_intersections"]), (0, 0))
        self.assertEqual(work_off["status"], "PASS")

    def test_scenario_work_off_missing_blocks(self) -> None:
        path = self.store(tracks=STRAIGHT_PAIR, rows=ROWS, segments=[])
        error = self.assert_blocked(
            tx.WORK_OFF_MISMATCH,
            lambda: self.export_traffic([bundle_for("TL-01")], {"TL-01": self.source(path)}),
        )
        self.assertIn("OFF extra", str(error))
        work_off = self.traffic_report()["fields"][0]["work_off"]
        self.assertGreater(work_off["unexpected_intersections"], 0)

    def test_scenario_unexplained_work_off_blocks(self) -> None:
        segments = work_off_for(ROWS, STRAIGHT_PAIR)
        segments.append({**segments[0], "segment_id": "SH_EXTRA", "points": [m(10, 80), m(10, 85)]})
        path = self.store(tracks=STRAIGHT_PAIR, rows=ROWS, segments=segments)
        self.assert_blocked(
            tx.WORK_OFF_MISMATCH,
            lambda: self.export_traffic([bundle_for("TL-01")], {"TL-01": self.source(path)}),
        )
        work_off = self.traffic_report()["fields"][0]["work_off"]
        self.assertEqual(work_off["unexplained"], 1)
        self.assertEqual(work_off["unexplained_items"][0]["segment_id"], "SH_EXTRA")

    def test_scenario_work_off_comparison_pass(self) -> None:
        _, _, _, _, report = self.export_ok(STRAIGHT_PAIR)
        work_off = report["fields"][0]["work_off"]
        self.assertEqual(work_off["status"], "PASS")
        self.assertEqual(work_off["work_off_events"], work_off["explained"])
        self.assertEqual(work_off["unexpected_intersections"], 0)

    def test_scenario_multiple_fields_separate_generations(self) -> None:
        first = self.store(name="TL-01", generation_id="GEN-A", tracks=STRAIGHT_PAIR, rows=ROWS)
        second_ring = rect(400, 0, 600, 100)
        second_tracks = (track("TT_B", [(420, 50), (580, 50)]),)
        second = self.store(
            name="TL-02", generation_id="GEN-B", tracks=second_tracks,
            rows=vertical_rows([410 + 5 * i for i in range(37)]),
        )
        folder = self.export_traffic(
            [bundle_for("TL-01"), bundle_for("TL-02", second_ring)],
            {"TL-01": self.source(first, "TL-01"), "TL-02": self.source(second, "TL-02")},
        )
        self.assertEqual(len(list(folder.glob("*.shp"))), 2)
        fields = {item["field"]: item for item in self.traffic_report()["fields"]}
        self.assertEqual(fields["TL-01"]["generation_id"], "GEN-A")
        self.assertEqual(fields["TL-02"]["generation_id"], "GEN-B")
        self.assertEqual(fields["TL-01"]["exported_exclusions"], 2)
        self.assertEqual(fields["TL-02"]["exported_exclusions"], 1)

    def test_scenario_diagnostics_written(self) -> None:
        self.export_ok(STRAIGHT_PAIR)
        text = (self.out / DIAGNOSTICS / tx.TRAFFIC_REPORT_TEXT).read_text(encoding="utf-8")
        for label in ("Modo:", "Geracao:", "Rastros de pneu:", "Poligonos de exclusao:", "Mesclados:",
                      "Interior safe:", "Tocando borda:", "Eventos WORK_OFF:", "Area excluida:",
                      "Area plantavel:", "Status:"):
            self.assertIn(label, text)
        preview = self.out / DIAGNOSTICS / tx.TRAFFIC_PREVIEW_DIR / "TL-01.geojson"
        layers = {f["properties"]["layer"] for f in json.loads(preview.read_text("utf-8"))["features"]}
        self.assertEqual(layers, {"tire_track_exclusion", "work_off_audit", "boundary_export", "exported_exclusion"})
        field = self.traffic_report()["fields"][0]
        self.assertAlmostEqual(
            field["external_area_m2"] - field["excluded_area_m2"], field["plantable_area_m2"], places=2
        )


class PlannerLocationTests(unittest.TestCase):
    def test_planner_field_id_and_store_path(self) -> None:
        self.assertEqual(tx.planner_field_id("FORTSMART - TL-01"), "TL-01")
        self.assertEqual(tx.planner_field_id("Boundary \u2014 TL-01 (EPSG:32721)"), "TL-01")
        self.assertEqual(tx.planner_field_id(""), "PROJETO")
        path = tx.planner_field_store_path(Path("proj"), "FORTSMART - TL 01")
        self.assertEqual(path, Path("proj") / "fortsmart_data" / "TL_01" / "fortsmart_TL_01.gpkg")
        sources = tx.planner_traffic_sources(Path("proj"), {"Talhao exportado": "FORTSMART - TL-01"})
        self.assertEqual(sources["Talhao exportado"].field_id, "TL-01")
        with self.assertRaises(tx.TrafficExclusionError):
            tx.planner_traffic_sources("", {"A": "B"})


def _planner_modules():
    package = PLANNER_ROOT / "plugin" / "fortsmart_precision"
    if not package.is_dir():
        raise unittest.SkipTest(f"FortSmart Precision nao encontrado em {PLANNER_ROOT}")
    if str(PLANNER_ROOT) not in sys.path:
        sys.path.append(str(PLANNER_ROOT))
    from plugin.fortsmart_precision.core import layer_roles
    from plugin.fortsmart_precision.core.line_geometry import Segment
    from plugin.fortsmart_precision.core.operational_generation import build_operational_generation
    from plugin.fortsmart_precision.core.operational_persistence import OperationalPersistenceStore
    from plugin.fortsmart_precision.core.row_engine import PlantingRow
    from plugin.fortsmart_precision.core.shutoff_engine import apply_row_shutoff
    from plugin.fortsmart_precision.core.tire_track_engine import TireTrack
    from plugin.fortsmart_precision.domain.operational_contract import CrossSourceMode

    return SimpleNamespace(**locals())


class PlannerContractTests(TrafficExportCase):
    """Grava com o OperationalPersistenceStore real e o motor real de WORK_OFF do Planner."""

    def planner_generation(self, planner, track_points, *, margin=0.10, spacing=4.0):
        rows = [
            planner.PlantingRow(
                fs_id=f"ROW_{index:03d}", row_id=index, pass_id=1, machine_id="PLANTADEIRA",
                spacing_m=5.0, direction="N", offset_m=0.0, azimuth_deg=0.0,
                segment=planner.Segment(m(x, 5), m(x, 95)),
            )
            for index, x in enumerate(10 + 5 * i for i in range(37))
        ]
        tracks = [
            planner.TireTrack(
                pass_id=1, side=side, offset_m=offset, tire_width_m=0.40 + 2 * margin, track_width_m=2.0,
                points=tuple(m(x, y + dy) for x, y in track_points), track_id=f"TT_1_{side.upper()}",
                source_feature_id="SP1", physical_tire_width_m=0.40, safety_margin_m=margin,
            )
            for side, offset, dy in (("left", -spacing / 2, 0.0), ("right", spacing / 2, spacing))
        ]
        off = [
            item for item in planner.apply_row_shutoff(rows, [], shutoff_on_tramlines=True, tire_tracks=tracks)
            if item.shutoff_state == "off"
        ]
        cross = SimpleNamespace(
            planting_rows=rows,
            spraying_passes=[SimpleNamespace(pass_no=1, fs_id="SP1", line_points=lambda: tuple(
                m(x, y + spacing / 2) for x, y in track_points))],
            tire_tracks=tracks,
            tire_shutoff_segments=off,
        )
        sprayer = SimpleNamespace(name="PULVERIZADOR TESTE", to_dict=lambda: {"name": "PULVERIZADOR TESTE"})
        draft = planner.build_operational_generation(
            cross=cross, field_id="TL-01", field_name="TL-01", source_crs=WORKING_CRS,
            working_crs=WORKING_CRS, source_mode=planner.CrossSourceMode.EXISTING_PASSES,
            sprayer=sprayer, planting_layer="linhas", spray_layer="pulverizacao",
        )
        return draft, off

    NAMES = ("FORTSMART - TL-01", "Boundary \u2013 TL 7 (EPSG:31982)", "  TL-03  ", "", "Talhão Ç/1")

    def test_planner_canonical_rules_mirrored(self) -> None:
        planner = _planner_modules()
        for name in self.NAMES:
            self.assertEqual(tx.planner_field_id(name), planner.layer_roles.canonical_field_name(name))

    def test_planner_storage_path_mirrored(self) -> None:
        _planner_modules()
        try:
            from plugin.fortsmart_precision.core import persistence_manager
        except ImportError as exc:
            raise unittest.SkipTest(f"persistence_manager exige QGIS ({exc})")
        for name in self.NAMES:
            field_id = tx.planner_field_id(name)
            self.assertEqual(tx.planner_storage_slug(field_id), persistence_manager._storage_slug(field_id))
        self.assertEqual(persistence_manager.STORAGE_DIR_NAME, tx.PLANNER_STORAGE_DIR)

    def test_reopen_from_planner_gpkg_with_real_work_off(self) -> None:
        """Pneus a 4 m e a 2 m (abaixo da amostragem antiga de 2,5 m) passam igual."""
        planner = _planner_modules()
        for spacing in (4.0, 2.0):
            for label, points in (
                ("reta", [(20, 40), (180, 40)]),
                ("curva", [(100 + 70 * math.cos(math.radians(a)), -20 + 70 * math.sin(math.radians(a)))
                           for a in range(50, 131, 2)]),
            ):
                with self.subTest(label, spacing=spacing):
                    case = self.tmp / f"{label}_{spacing:g}m"
                    path = case / "fortsmart_TL-01.gpkg"
                    draft, off = self.planner_generation(planner, points, spacing=spacing)
                    self.assertTrue(off, "motor do Planner nao gerou WORK_OFF")
                    sides = {item.source_row_id: set() for item in off}
                    for segment in draft.segments:
                        sides[segment.source_row_id].add(segment.tire_side)
                    self.assertTrue(all(found == {"LEFT", "RIGHT"} for found in sides.values()), sides)
                    saved = planner.OperationalPersistenceStore(path).save_generation(draft)
                    out = case / "out"
                    folder = self.export_traffic([bundle_for("TL-01")], {"TL-01": self.source(path)}, out=out)
                    geometries, _, _ = read_shape(folder)
                    self.assertEqual(len(holes_of(geometries[0])), 2)
                    field = self.traffic_report(out)["fields"][0]
                    self.assertEqual(field["generation_id"], saved.manifest.generation_id)
                    self.assertEqual(field["work_off"]["status"], "PASS", field["work_off"])
                    self.assertEqual(field["work_off"]["work_off_events"], len(off))
                    self.assertEqual(field["work_off"]["unexpected_intersections"], 0)
                    self.assertEqual(field["work_off"]["unexplained"], 0)

    def test_planner_stale_generation_rejected(self) -> None:
        planner = _planner_modules()
        path = self.tmp / "stale" / "fortsmart_TL-01.gpkg"
        draft, _ = self.planner_generation(planner, [(20, 40), (180, 40)])
        store = planner.OperationalPersistenceStore(path)
        store.save_generation(draft)
        self.assertEqual(store.mark_active_stale("TL-01"), 1)
        self.assert_blocked(
            tx.STALE_GENERATION,
            lambda: self.export_traffic([bundle_for("TL-01")], {"TL-01": self.source(path)}),
        )


if __name__ == "__main__":
    unittest.main()
