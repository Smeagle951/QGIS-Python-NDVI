"""FortSmart — importacao USB Jacto OMNI (shapefile)."""

from __future__ import annotations

from pathlib import Path

from .gs3_rcd_core import unify_field_bundles
from .gs3_spatial_types import Gs3FieldBundle, Gs3FieldImportResult, SpatialKind
from .shapefile_reader import line_features, polygon_features, read_shapefile_lines, read_shapefile_polygons


def find_jacto_omni_root(path: Path) -> Path:
    p = Path(path)
    if p.is_file():
        p = p.parent
    candidates = [
        p,
        p / "OMNI" / "Data",
        p / "OMNI",
        p / "Data",
    ]
    for candidate in candidates:
        if candidate.exists() and list(candidate.rglob("_perimeter.shp")):
            return candidate
    raise FileNotFoundError(f"Estrutura Jacto OMNI nao encontrada em {path}")


def detect_jacto(path: Path) -> bool:
    try:
        find_jacto_omni_root(path)
        return True
    except FileNotFoundError:
        return False


def collect_jacto_fields(path: Path, *, unify_farms: bool = True) -> Gs3FieldImportResult:
    root = find_jacto_omni_root(path)
    bundles_by_key: dict[str, Gs3FieldBundle] = {}

    for shp in sorted(root.rglob("_perimeter.shp")):
        rel = shp.relative_to(root)
        parts = rel.parts
        if len(parts) >= 3:
            fazenda = parts[-3]
            talhao = parts[-2]
        elif len(parts) == 2:
            fazenda = "Fazenda"
            talhao = parts[-2]
        else:
            fazenda = "Fazenda"
            talhao = shp.parent.name

        field_erid = f"jacto-{fazenda}-{talhao}".casefold()
        bundle = bundles_by_key.get(field_erid)
        if bundle is None:
            bundle = Gs3FieldBundle(
                cliente="Cliente",
                fazenda=fazenda,
                talhao=talhao,
                field_erid=field_erid,
            )
            bundles_by_key[field_erid] = bundle

        rings = read_shapefile_polygons(shp)
        bundle.features.extend(
            polygon_features(
                rings,
                kind=SpatialKind.LIMITE,
                cliente=bundle.cliente,
                fazenda=fazenda,
                talhao=talhao,
                field_erid=field_erid,
                name=talhao,
                source=str(shp),
            )
        )

        guidance = shp.parent / "_guidance.shp"
        if guidance.exists():
            lines = read_shapefile_lines(guidance)
            bundle.features.extend(
                line_features(
                    lines,
                    kind=SpatialKind.CURVA_AB,
                    cliente=bundle.cliente,
                    fazenda=fazenda,
                    talhao=talhao,
                    field_erid=field_erid,
                    name="Guia",
                    source=str(guidance),
                )
            )

    bundles = sorted(bundles_by_key.values(), key=lambda b: (b.fazenda.casefold(), b.talhao.casefold()))
    farms_before = len({b.fazenda for b in bundles})
    farms_after = farms_before
    if unify_farms:
        bundles, farms_before, farms_after = unify_field_bundles(bundles)
    return Gs3FieldImportResult(
        eic_root=str(root),
        catalog_count=len(list(root.rglob("_perimeter.shp"))),
        field_count=len(bundles),
        bundles=bundles,
        farms_before_merge=farms_before,
        farms_after_merge=farms_after,
        format_source="jacto_omni",
    )
