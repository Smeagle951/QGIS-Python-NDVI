"""FortSmart — importacao USB Stara (shapefile DADOS/)."""

from __future__ import annotations

from pathlib import Path

from .gs3_rcd_core import unify_field_bundles
from .gs3_spatial_types import Gs3FieldBundle, Gs3FieldImportResult, SpatialKind
from .shapefile_reader import line_features, polygon_features, read_shapefile_lines, read_shapefile_polygons


def find_stara_dados_root(path: Path) -> Path:
    p = Path(path)
    if p.is_file():
        p = p.parent
    candidates = [
        p / "DADOS",
        p,
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        if list(candidate.rglob("LIMITE.shp")) or list(candidate.rglob("LINHAS.shp")):
            return candidate
    raise FileNotFoundError(f"Estrutura Stara DADOS nao encontrada em {path}")


def detect_stara(path: Path) -> bool:
    try:
        find_stara_dados_root(path)
        return True
    except FileNotFoundError:
        return False


def _talhao_from_shp(shp: Path, root: Path) -> str:
    rel = shp.relative_to(root)
    if len(rel.parts) >= 2:
        return rel.parts[-2]
    return shp.parent.name


def collect_stara_fields(path: Path, *, unify_farms: bool = True) -> Gs3FieldImportResult:
    root = find_stara_dados_root(path)
    bundles_by_key: dict[str, Gs3FieldBundle] = {}

    limite_paths = sorted(root.rglob("LIMITE.shp"))
    for shp in limite_paths:
        talhao = _talhao_from_shp(shp, root)
        fazenda = "Fazenda"
        field_erid = f"stara-{fazenda}-{talhao}".casefold()
        bundle = bundles_by_key.get(field_erid)
        if bundle is None:
            bundle = Gs3FieldBundle(cliente="Cliente", fazenda=fazenda, talhao=talhao, field_erid=field_erid)
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

    for shp in sorted(root.rglob("LINHAS.shp")):
        talhao = _talhao_from_shp(shp, root)
        fazenda = "Fazenda"
        field_erid = f"stara-{fazenda}-{talhao}".casefold()
        bundle = bundles_by_key.get(field_erid)
        if bundle is None:
            bundle = Gs3FieldBundle(cliente="Cliente", fazenda=fazenda, talhao=talhao, field_erid=field_erid)
            bundles_by_key[field_erid] = bundle
        lines = read_shapefile_lines(shp)
        bundle.features.extend(
            line_features(
                lines,
                kind=SpatialKind.CURVA_AB,
                cliente=bundle.cliente,
                fazenda=fazenda,
                talhao=talhao,
                field_erid=field_erid,
                name="Guia",
                source=str(shp),
            )
        )

    bundles = sorted(bundles_by_key.values(), key=lambda b: (b.fazenda.casefold(), b.talhao.casefold()))
    farms_before = len({b.fazenda for b in bundles})
    farms_after = farms_before
    if unify_farms:
        bundles, farms_before, farms_after = unify_field_bundles(bundles)
    return Gs3FieldImportResult(
        eic_root=str(root),
        catalog_count=len(limite_paths) + len(list(root.rglob("LINHAS.shp"))),
        field_count=len(bundles),
        bundles=bundles,
        farms_before_merge=farms_before,
        farms_after_merge=farms_after,
        format_source="stara_dados",
    )
