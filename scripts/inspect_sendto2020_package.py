"""Inspect SendTo2020 shapefiles without changing the package."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def inspect(root: Path) -> None:
    import fiona
    from shapely.geometry import shape

    print(f"=== {root}")
    for shp in sorted(root.glob("*.shp")):
        with fiona.open(shp, "r") as source:
            records = list(source)
            for record in records:
                geometry = shape(record["geometry"])
                exterior = list(geometry.exterior.coords)
                signed = sum(
                    (exterior[index + 1][0] - exterior[index][0])
                    * (exterior[index + 1][1] + exterior[index][1])
                    for index in range(len(exterior) - 1)
                )
                print(
                    json.dumps(
                        {
                            "file": shp.name,
                            "feature_count": len(records),
                            "schema": source.schema,
                            "crs": source.crs.to_epsg(),
                            "bounds": source.bounds,
                            "properties": dict(record["properties"]),
                            "valid": geometry.is_valid,
                            "area_degrees": geometry.area,
                            "rings": 1 + len(geometry.interiors),
                            "vertices": len(exterior)
                            + sum(len(interior.coords) for interior in geometry.interiors),
                            "exterior_orientation": "CW" if signed > 0 else "CCW",
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                )
    for extension in (".cpg", ".mcd", ".prj"):
        print(
            extension,
            [
                {"name": path.name, "size": len(path.read_bytes()), "prefix": path.read_bytes()[:16].hex()}
                for path in sorted(root.glob(f"*{extension}"))
            ],
        )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Uso: inspect_sendto2020_package.py PASTA [PASTA_REFERENCIA]")
    for argument in sys.argv[1:]:
        inspect(Path(argument))
