"""Round-trip validation for Precision Planting SendTo2020 exports."""

from __future__ import annotations

from pathlib import Path
from typing import Any


DBF_FIELDS = ("Business", "Farm", "Field", "CODE")
REQUIRED_EXTENSIONS = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".mcd")


class SendTo2020PreflightValidator:
    """Reopen generated files and reject incomplete or ambiguous packages."""

    def validate(
        self,
        base_path: Path,
        expected: dict[str, str],
        *,
        geometry_types: tuple[str, ...] = ("Polygon",),
    ) -> dict[str, Any]:
        try:
            import fiona
        except ImportError as exc:
            raise RuntimeError(
                "SENDTO2020_DEPENDENCY_MISSING: Fiona e obrigatorio para validar o pacote."
            ) from exc

        missing = [
            extension
            for extension in REQUIRED_EXTENSIONS
            if not base_path.with_suffix(extension).is_file()
        ]
        if missing:
            raise ValueError(
                "SENDTO2020_REQUIRED_FILE_MISSING: " + ", ".join(missing)
            )

        try:
            source_context = fiona.open(base_path.with_suffix(".shp"), "r")
        except Exception as exc:
            raise ValueError(
                "SENDTO2020_SHAPEFILE_REOPEN_FAILED: nao foi possivel reabrir o SHP."
            ) from exc

        with source_context as source:
            records = list(source)
            if len(records) != 1:
                raise ValueError(
                    "SENDTO2020_BOUNDARY_MULTIPLE_FEATURES: "
                    "o shapefile deve conter exatamente um poligono; "
                    f"encontrados {len(records)}."
                )

            geometry = records[0].get("geometry")
            geometry_type = geometry.get("type") if geometry else ""
            if geometry_type not in geometry_types:
                raise ValueError(
                    "SENDTO2020_GEOMETRY_TYPE_INVALID: "
                    f"esperado {'/'.join(geometry_types)}, encontrado {geometry_type or 'vazio'}."
                )

            schema_fields = tuple(source.schema.get("properties", {}).keys())
            if schema_fields != DBF_FIELDS:
                raise ValueError(
                    "SENDTO2020_DBF_SCHEMA_INVALID: "
                    f"esperado {DBF_FIELDS}, encontrado {schema_fields}."
                )

            properties = dict(records[0].get("properties") or {})
            for field in DBF_FIELDS:
                actual = str(properties.get(field, "")).strip()
                wanted = str(expected.get(field, "")).strip()
                if actual != wanted:
                    raise ValueError(
                        "SENDTO2020_DBF_VALUE_INVALID: "
                        f"{field} esperado={wanted!r}, encontrado={actual!r}."
                    )

            crs_text = (source.crs_wkt or "").upper()
            crs_mapping = source.crs or {}
            epsg = source.crs.to_epsg() if hasattr(source.crs, "to_epsg") else None
            if epsg != 4326 and "WGS 84" not in crs_text and "EPSG:4326" not in str(crs_mapping):
                raise ValueError(
                    "SENDTO2020_CRS_INVALID: o shapefile reaberto nao esta em EPSG:4326."
                )

            coordinates = geometry.get("coordinates") or []
            polygons = coordinates if geometry_type == "MultiPolygon" else [coordinates]
            vertex_count = sum(len(ring) for polygon in polygons for ring in polygon)
            bounds = tuple(float(value) for value in source.bounds)
            try:
                from shapely.geometry import shape

                reopened_geometry = shape(geometry)
            except Exception as exc:
                raise ValueError(
                    "SENDTO2020_SHAPEFILE_REOPEN_FAILED: geometria nao pode ser interpretada."
                ) from exc
            if (
                reopened_geometry.is_empty
                or not reopened_geometry.is_valid
                or reopened_geometry.area <= 0
            ):
                raise ValueError(
                    "SENDTO2020_GEOMETRY_TYPE_INVALID: poligono reaberto vazio, invalido ou sem area."
                )

        mcd_text = base_path.with_suffix(".mcd").read_text(encoding="utf-8-sig")
        for field in ("Business", "Farm", "Field"):
            if f"Key='{field}'" not in mcd_text:
                raise ValueError(
                    "SENDTO2020_MCD_INVALID: " f"coluna {field} ausente."
                )

        return {
            "feature_count": 1,
            "geometry_type": geometry_type,
            "crs": "EPSG:4326",
            "schema": list(DBF_FIELDS),
            "attributes": {field: expected[field] for field in DBF_FIELDS},
            "vertex_count": vertex_count,
            "bbox": list(bounds),
            "files": [base_path.with_suffix(ext).name for ext in REQUIRED_EXTENSIONS],
            "roundtrip_valid": True,
        }
