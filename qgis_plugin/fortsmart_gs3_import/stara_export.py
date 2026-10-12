"""FortSmart — exportacao USB Stara (shapefile DADOS/)."""

from __future__ import annotations

from pathlib import Path

from .gs3_spatial_types import Gs3FieldBundle, SpatialKind
from .shapefile_core import safe_folder_name, write_shapefile_lines, write_shapefile_polygons


def export_stara_dados(bundles: list[Gs3FieldBundle], output_root: Path) -> Path:
    if not bundles:
        raise ValueError("Nenhum talhao para exportar Stara DADOS.")

    dados_root = output_root / "DADOS"
    talhoes_root = dados_root / "TALHOES"
    guias_root = dados_root / "GUIAS"
    talhoes_root.mkdir(parents=True, exist_ok=True)
    guias_root.mkdir(parents=True, exist_ok=True)

    for bundle in bundles:
        field = safe_folder_name(bundle.talhao, "Talhao")
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

        if boundary_rings:
            field_dir = talhoes_root / field
            field_dir.mkdir(parents=True, exist_ok=True)
            write_shapefile_polygons(field_dir / "LIMITE", boundary_rings)
        if guidance_lines:
            guia_dir = guias_root / field
            guia_dir.mkdir(parents=True, exist_ok=True)
            write_shapefile_lines(guia_dir / "LINHAS", guidance_lines)

    return dados_root
