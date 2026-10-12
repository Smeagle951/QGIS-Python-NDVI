from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "qgis_plugin"))

from fortsmart_gs3_import.gs3_geometry import (  # noqa: E402
    Gs3BoundaryConfig,
    prepare_gs3_boundary_geometry,
)


class Gs3GeometryTest(unittest.TestCase):
    def test_area_is_metric_and_source_ring_is_unchanged(self):
        ring = [(-55.0, -12.0), (-55.0, -12.01), (-54.99, -12.01), (-54.99, -12.0), (-55.0, -12.0)]
        original = list(ring)
        result = prepare_gs3_boundary_geometry([ring])
        self.assertEqual(ring, original)
        self.assertGreater(result.original_area_ha, 100.0)
        self.assertLess(result.original_area_ha, 130.0)
        self.assertEqual(result.tolerance_m, 0.0)

    def test_dense_boundary_is_simplified_with_area_preserved(self):
        count = 50_000
        ring = []
        for index in range(count):
            angle = 2.0 * math.pi * index / count
            radius = 0.01 + 0.00002 * math.sin(angle * 80)
            ring.append((-55.0 + radius * math.cos(angle), -12.0 + radius * math.sin(angle)))
        ring.append(ring[0])
        source_count = len(ring)
        result = prepare_gs3_boundary_geometry(
            [ring],
            config=Gs3BoundaryConfig(warning_vertices=10_000, target_vertices=5_000, max_vertices=20_000),
        )
        self.assertEqual(len(ring), source_count)
        self.assertLess(result.export_vertices, 5_000)
        self.assertLessEqual(result.area_difference_percent, 0.5)
        self.assertGreater(result.tolerance_m, 0.0)


if __name__ == "__main__":
    unittest.main()
