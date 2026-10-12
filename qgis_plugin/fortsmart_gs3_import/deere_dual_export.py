"""FortSmart — pacote dual John Deere (Gen4 + GS3_2630), regra dual Gen4+GS3.

Gera na mesma pasta de saida:
  Gen4/MasterData.xml + SpatialFiles/*.gjson
  GS3_2630/{Fazenda}/RCD/EIC/...

Assim o pendrive serve display Gen4/G5 e GS3 2630 sem conversao extra.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .fortsmart_model import FortSmartFieldBundle
from .gen4_export import export_gen4
from .gen4_pass_guidance import Gen4GuidanceRouter
from .gs3_rcd_export import export_gs3_from_bundles, sanitize_gs3_machine_folder
from .gs3_spatial_types import SpatialKind


DUAL_GEN4_KINDS = {
    SpatialKind.LIMITE,
    SpatialKind.LINHA_AB,
    SpatialKind.CURVA_AB,
    SpatialKind.CURVA_ADAPTADA,
    SpatialKind.TRABALHO,
    SpatialKind.MARCAÇAO,
    SpatialKind.OBSTACULO_PONTO,
}
DUAL_GS3_KINDS = {
    SpatialKind.LIMITE,
    SpatialKind.LIMITE_INTERNO,
    SpatialKind.LINHA_AB,
    SpatialKind.CURVA_AB,
    SpatialKind.CURVA_ADAPTADA,
    SpatialKind.BORDADURA,
    SpatialKind.TRABALHO,
}


def dual_field_erids(bundles: list[FortSmartFieldBundle]) -> tuple[set[str], set[str]]:
    """Talhoes que cada lado do pacote dual vai exportar (Gen4, GS3)."""
    return (
        {bundle.field_erid for bundle in _filter_kinds(bundles, DUAL_GEN4_KINDS)},
        {bundle.field_erid for bundle in _filter_kinds(bundles, DUAL_GS3_KINDS)},
    )


def export_john_deere_dual(
    bundles: list[FortSmartFieldBundle],
    output_root: Path,
    *,
    template_gs3: Path | None = None,
    merge_setup: bool = True,
    guidance_plan: Any = None,
) -> Path:
    """Exporta Gen4 + GS3 no mesmo destino (layout dual Gen4+GS3)."""
    if not bundles:
        raise ValueError("Nenhum talhao para exportar John Deere.")

    out = Path(output_root)
    out.mkdir(parents=True, exist_ok=True)

    gen4_bundles = _filter_kinds(bundles, DUAL_GEN4_KINDS)
    gs3_bundles = _filter_kinds(bundles, DUAL_GS3_KINDS)
    if not gen4_bundles and not gs3_bundles:
        raise ValueError("Nenhuma feicao compativel com Gen4/GS3.")

    errors: list[str] = []
    gen4_ok = gs3_ok = False

    if gen4_bundles:
        try:
            gen4_dir = export_gen4(gen4_bundles, out)
            if guidance_plan is not None and guidance_plan.options.any_gen4_requested:
                Gen4GuidanceRouter(guidance_plan).apply(gen4_dir)
            gen4_ok = True
        except Exception as exc:
            errors.append(f"Gen4: {exc}")

    if gs3_bundles:
        try:
            fazenda = gs3_bundles[0].fazenda or "Fazenda"
            export_gs3_from_bundles(
                gs3_bundles,
                out,
                template_gs3=template_gs3,
                merge_setup=merge_setup,
                machine_folder=sanitize_gs3_machine_folder(fazenda),
                guidance_plan=guidance_plan,
            )
            gs3_ok = True
        except Exception as exc:
            errors.append(f"GS3: {exc}")

    if not gen4_ok and not gs3_ok:
        raise RuntimeError("Falha no export dual John Deere.\n" + "\n".join(errors))

    if errors:
        # Um dos lados falhou — ainda assim devolvemos a pasta se o outro ok,
        # mas sinalizamos claramente.
        raise RuntimeError(
            "Export dual parcial (um formato falhou).\n"
            + "\n".join(errors)
            + f"\nPasta: {out}"
        )

    return out


def _filter_kinds(
    bundles: list[FortSmartFieldBundle],
    allowed: set[SpatialKind],
) -> list[FortSmartFieldBundle]:
    out: list[FortSmartFieldBundle] = []
    for bundle in bundles:
        feats = [f for f in bundle.features if f.kind in allowed]
        if feats:
            out.append(
                FortSmartFieldBundle(
                    cliente=bundle.cliente,
                    fazenda=bundle.fazenda,
                    talhao=bundle.talhao,
                    field_erid=bundle.field_erid,
                    features=feats,
                )
            )
    return out
