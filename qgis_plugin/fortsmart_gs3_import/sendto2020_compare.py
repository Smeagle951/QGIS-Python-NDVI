"""Structural comparison utilities for SendTo2020 packages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .sendto2020_preflight import REQUIRED_EXTENSIONS


def inspect_sendto2020_package(root: Path) -> dict[str, Any]:
    try:
        import fiona
    except ImportError as exc:
        raise RuntimeError("Fiona e obrigatorio para inspecionar SendTo2020.") from exc

    root = Path(root)
    fields: list[dict[str, Any]] = []
    for shp in sorted(root.glob("*.shp"), key=lambda path: path.name.lower()):
        base = shp.with_suffix("")
        mcd_path = base.with_suffix(".mcd")
        mcd_text = mcd_path.read_text(encoding="utf-8-sig") if mcd_path.is_file() else ""
        with fiona.open(shp, "r") as source:
            records = list(source)
            geometry_types = sorted(
                {
                    (record.get("geometry") or {}).get("type", "")
                    for record in records
                }
            )
            vertices = 0
            for record in records:
                geometry = record.get("geometry") or {}
                for ring in geometry.get("coordinates") or []:
                    vertices += len(ring)
            fields.append(
                {
                    "name": base.name,
                    "files": [
                        extension
                        for extension in REQUIRED_EXTENSIONS
                        if base.with_suffix(extension).is_file()
                    ],
                    "feature_count": len(records),
                    "geometry_types": geometry_types,
                    "schema": list(source.schema.get("properties", {}).keys()),
                    "crs_epsg": (
                        source.crs.to_epsg()
                        if hasattr(source.crs, "to_epsg")
                        else None
                    ),
                    "vertex_count": vertices,
                    "mcd_present": mcd_path.is_file(),
                    "mcd_columns": [
                        field
                        for field in ("Business", "Farm", "Field")
                        if f"Key='{field}'" in mcd_text
                    ],
                }
            )
    return {"root": str(root), "field_count": len(fields), "fields": fields}


def compare_sendto2020_packages(generated: Path, reference: Path) -> dict[str, Any]:
    generated_info = inspect_sendto2020_package(generated)
    reference_info = inspect_sendto2020_package(reference)

    required_shape = {
        "files": list(REQUIRED_EXTENSIONS),
        "feature_count": 1,
        "geometry_types": ["Polygon"],
        "schema": ["Business", "Farm", "Field", "CODE"],
        "crs_epsg": 4326,
        "mcd_present": True,
        "mcd_columns": ["Business", "Farm", "Field"],
    }
    deviations = []
    for field in generated_info["fields"]:
        for key, expected in required_shape.items():
            if field.get(key) != expected:
                deviations.append(
                    {"field": field["name"], "property": key, "expected": expected, "actual": field.get(key)}
                )

    reference_vertices = [field["vertex_count"] for field in reference_info["fields"]]
    generated_vertices = [field["vertex_count"] for field in generated_info["fields"]]
    return {
        "generated": generated_info,
        "reference": reference_info,
        "structural_requirements": required_shape,
        "deviations": deviations,
        "compatible_structure": not deviations,
        "vertex_count": {
            "generated": generated_vertices,
            "reference_min": min(reference_vertices) if reference_vertices else None,
            "reference_max": max(reference_vertices) if reference_vertices else None,
        },
    }
