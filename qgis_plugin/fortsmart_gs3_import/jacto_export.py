"""FortSmart — exportacao USB Jacto OMNI (shapefile)."""

from __future__ import annotations

from pathlib import Path

from .gs3_spatial_types import Gs3FieldBundle, SpatialKind
from .shapefile_core import safe_folder_name, write_shapefile_lines, write_shapefile_polygons


def export_jacto_omni(bundles: list[Gs3FieldBundle], output_root: Path) -> Path:
    if not bundles:
        raise ValueError("Nenhum talhao para exportar Jacto OMNI.")

    omni_root = output_root / "OMNI" / "Data"
    omni_root.mkdir(parents=True, exist_ok=True)

    for bundle in bundles:
        farm = safe_folder_name(bundle.fazenda, "Fazenda")
        field = safe_folder_name(bundle.talhao, "Talhao")
        field_dir = omni_root / farm / field
        field_dir.mkdir(parents=True, exist_ok=True)

        boundary_rings: list[list[tuple[float, float]]] = []
        guidance_lines: list[list[tuple[float, float]]] = []

        for feature in bundle.features:
            if feature.kind in (SpatialKind.LIMITE, SpatialKind.LIMITE_INTERNO, SpatialKind.BORDADURA):
                rings = feature.rings or ([feature.points] if feature.points else [])
                boundary_rings.extend(rings)
            elif feature.kind in (
                SpatialKind.LINHA_AB,
                SpatialKind.CURVA_AB,
                SpatialKind.CURVA_ADAPTADA,
                SpatialKind.TRABALHO,
            ) and len(feature.points) >= 2:
                guidance_lines.append(feature.points)

        common = {
            "CLIENT": bundle.cliente,
            "FARM": bundle.fazenda,
            "FIELD": bundle.talhao,
        }

        if boundary_rings:
            write_shapefile_polygons(
                field_dir / "_perimeter",
                boundary_rings,
                field_names=["CLIENT", "FARM", "FIELD", "NAME"],
                attrs=[{**common, "NAME": "LIMITE EXTERNO"} for _ in boundary_rings],
                geometry_type="MultiPolygon",
            )
        if guidance_lines:
            write_shapefile_lines(
                field_dir / "_runline",
                guidance_lines,
                field_names=["CLIENT", "FARM", "FIELD", "NAME"],
                attrs=[{**common, "NAME": "CURVA"} for _ in guidance_lines],
            )

    return output_root / "OMNI"
