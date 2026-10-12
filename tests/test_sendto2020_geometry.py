from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "qgis_plugin"))

from fortsmart_gs3_import.sendto2020_geometry import prepare_sendto2020_boundary  # noqa: E402


class SendTo2020GeometryTest(unittest.TestCase):
    def test_dense_boundary_is_reduced_without_mutating_source(self):
        count = 50_000
        ring = []
        for index in range(count):
            angle = 2 * math.pi * index / count
            radius = 0.01 + 0.00002 * math.sin(angle * 60)
            ring.append((-55 + radius * math.cos(angle), -12 + radius * math.sin(angle)))
        ring.append(ring[0])
        source_count = len(ring)
        result = prepare_sendto2020_boundary([ring])
        self.assertEqual(len(ring), source_count)
        self.assertLessEqual(result.export_vertices, 2_000)
        self.assertLessEqual(result.area_difference_percent, 0.5)

    def test_detached_component_emits_warning_and_one_principal_polygon(self):
        main = [(-55, -12), (-55, -12.01), (-54.99, -12.01), (-54.99, -12), (-55, -12)]
        island = [(-54.98, -12), (-54.98, -12.001), (-54.979, -12.001), (-54.979, -12), (-54.98, -12)]
        result = prepare_sendto2020_boundary([main, island])
        self.assertEqual(result.source_components, 2)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("MULTIPART_BOUNDARY_WARNING", result.warnings[0])


if __name__ == "__main__":
    unittest.main()
