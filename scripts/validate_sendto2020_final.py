"""Generate the final SendTo2020 stress package and structural audit report."""

from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "qgis_plugin"))

from fortsmart_gs3_import.adapters.sendto2020_writer import SendTo2020Writer  # noqa: E402
from fortsmart_gs3_import.gs3_spatial_types import (  # noqa: E402
    Gs3FieldBundle,
    Gs3SpatialFeature,
    SpatialKind,
)
from fortsmart_gs3_import.sendto2020_compare import compare_sendto2020_packages  # noqa: E402


VERTEX_COUNT = 461_569
OUTPUT_ROOT = PROJECT_ROOT / "data" / "output" / "sendto2020_final_validation_20260928"
REFERENCE_ROOT = (
    PROJECT_ROOT
    / "data"
    / "temp"
    / "monitor_zip_analysis_20260926"
    / "SendTo2020_1"
    / "SendTo2020"
)


def dense_ring() -> list[tuple[float, float]]:
    unique_vertices = VERTEX_COUNT - 1
    ring = []
    for index in range(unique_vertices):
        angle = 2.0 * math.pi * index / unique_vertices
        radius = 0.012 + 0.000015 * math.sin(angle * 80.0)
        ring.append(
            (
                -55.0 + radius * math.cos(angle),
                -12.0 + radius * math.sin(angle),
            )
        )
    ring.append(ring[0])
    return ring


def main() -> int:
    if OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    ring = dense_ring()
    original_snapshot = (len(ring), ring[0], ring[-1])
    feature = Gs3SpatialFeature(
        kind=SpatialKind.LIMITE,
        cliente="VALIDACAO FORTSMART",
        fazenda="FAZENDA TESTE",
        talhao="TL-12",
        field_erid="sendto2020-validation-field",
        item_erid="sendto2020-validation-boundary",
        name="Limite externo",
        source_file="synthetic-validation",
        catalog_file="canonical",
        geometry="polygon",
        rings=[ring],
        extra={"fortsmart_role": "EXTERNAL_BOUNDARY"},
    )
    bundle = Gs3FieldBundle(
        cliente=feature.cliente,
        fazenda=feature.fazenda,
        talhao=feature.talhao,
        field_erid=feature.field_erid,
        features=[feature],
    )
    package = SendTo2020Writer().write([bundle], OUTPUT_ROOT)
    if original_snapshot != (len(ring), ring[0], ring[-1]):
        raise RuntimeError("A geometria canonica foi modificada durante a exportacao.")

    comparison = compare_sendto2020_packages(package, REFERENCE_ROOT)
    comparison["stress_test"] = {
        "input_vertices": VERTEX_COUNT,
        "canonical_geometry_unchanged": True,
    }
    report = json.loads(
        (OUTPUT_ROOT / "SENDTO2020_DIAGNOSTIC_EXPORT" / "SENDTO2020_EXPORT_REPORT.json").read_text(
            encoding="utf-8"
        )
    )
    comparison["stress_test"].update(
        {
            "export_vertices": report["fields"][0]["export_vertices"],
            "area_difference_percent": report["fields"][0]["area_difference_percent"],
            "status": report["status"],
            "verification_state": report["verification_state"],
        }
    )
    audit_path = OUTPUT_ROOT / "SENDTO2020_GOLDEN_COMPARISON.json"
    audit_path.write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(comparison["stress_test"], ensure_ascii=False, indent=2))
    print(f"compatible_structure={comparison['compatible_structure']}")
    print(f"package={package}")
    print(f"audit={audit_path}")
    return 0 if comparison["compatible_structure"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
