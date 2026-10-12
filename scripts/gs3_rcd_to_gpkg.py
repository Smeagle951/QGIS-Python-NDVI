"""
FortSmart — CLI para importar talhoes GS3 2630 (usa gs3_rcd_core).

Uso:
  python gs3_rcd_to_gpkg.py --input E:/GS3_2630 --output data/output/talhoes_gs3.gpkg

No QGIS use: scripts/qgis_import_gs3.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from gs3_rcd_core import collect_gs3_boundaries


def export_geopackage(records, output_path: Path) -> None:
    try:
        import geopandas as gpd
        from shapely.geometry import Polygon
    except ImportError as exc:
        raise SystemExit(
            "Instale dependencias: python -m pip install geopandas shapely pyogrio\n"
            "Ou use qgis_import_gs3.py dentro do QGIS (nao precisa de geopandas)."
        ) from exc

    rows = []
    for rec in records:
        poly = Polygon(rec.points)
        if not poly.is_valid:
            poly = poly.buffer(0)
        rows.append(
            {
                "cliente": rec.cliente,
                "fazenda": rec.fazenda,
                "talhao": rec.talhao,
                "field_erid": rec.field_erid,
                "boundary_erid": rec.boundary_erid,
                "arquivo": rec.source_file,
                "geometry": poly,
            }
        )

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(output_path, driver="GPKG", layer="talhoes")
    print(f"OK: {len(gdf)} limite(s) -> {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="FortSmart — GS3 2630 RCD para GeoPackage")
    parser.add_argument("--input", required=True, help="Pasta GS3_2630")
    parser.add_argument(
        "--output",
        default=str(SCRIPT_DIR.parent / "data" / "output" / "talhoes_gs3.gpkg"),
        help="GeoPackage de saida",
    )
    args = parser.parse_args()

    result = collect_gs3_boundaries(Path(args.input))
    print(f"Pasta EIC: {result.eic_root}")
    print(f"Catalogos: {result.catalog_count} | Limites: {len(result.boundaries)}")
    if not result.boundaries:
        raise SystemExit("Nenhum limite valido encontrado.")
    export_geopackage(result.boundaries, Path(args.output))


if __name__ == "__main__":
    main()
