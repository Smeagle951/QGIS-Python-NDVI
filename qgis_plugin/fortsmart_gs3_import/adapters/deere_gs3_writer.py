"""FortSmart — escritor John Deere GS3 2630."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..fortsmart_model import FortSmartFieldBundle, SpatialKind
from ..gs3_rcd_export import export_gs3_from_bundles, export_gs3_usb
from ..gs3_rcd_export import ExportField
from ..jd_guidance_plan import (
    GS3,
    JohnDeereGuidanceExportPlan,
    JohnDeereGuidanceOptions,
    guidance_plan_for,
    require_single_external_boundary,
)
from ..jd_guidance_report import run_guidance_export
from ..profiles import PROFILES
from .base import MonitorWriter, filter_canonical_bundles


DEERE_GS3_ROLES = {
    "EXTERNAL_BOUNDARY",
    "OPERATIONAL_BOUNDARY",
    "INTERNAL_BOUNDARY",
    "HEADLAND",
    "AB_LINE",
    "AB_CURVE",
    "GUIDANCE_TRACK",
}


def guidance_options(options: dict[str, Any]) -> JohnDeereGuidanceOptions | None:
    return options.get("guidance_options") or options.get("gs3_pass_options")


def gs3_guidance_plan(
    bundles: list[FortSmartFieldBundle],
    pass_options: JohnDeereGuidanceOptions | None,
) -> JohnDeereGuidanceExportPlan | None:
    """Plano de passadas sobre os bundles nao filtrados; None mantem o pacote atual."""
    return guidance_plan_for(bundles, pass_options, [GS3])


class DeereGS3Writer(MonitorWriter):
    def __init__(self) -> None:
        self.profile = PROFILES["john_deere_gs3"]

    def write(
        self,
        bundles: list[FortSmartFieldBundle],
        output_root: Path,
        **options: Any,
    ) -> Path:
        filtered = self._filter_bundles(bundles)
        if not filtered:
            raise ValueError("Nenhuma feicao compativel com GS3 2630.")
        require_single_external_boundary(filtered)
        plan = gs3_guidance_plan(bundles, guidance_options(options))

        def export() -> Path:
            return export_gs3_from_bundles(
                filtered,
                output_root,
                template_gs3=options.get("template_gs3"),
                merge_setup=options.get("merge_setup", True),
                guidance_plan=plan,
            )

        if plan is None:
            return export()
        return run_guidance_export(
            output_root,
            plan,
            {GS3: [bundle.field_erid for bundle in filtered]},
            export,
        )

    def _filter_bundles(self, bundles: list[FortSmartFieldBundle]) -> list[FortSmartFieldBundle]:
        supported = set(self.profile.supported_kinds)
        return filter_canonical_bundles(
            bundles,
            allowed_roles=DEERE_GS3_ROLES,
            legacy_kinds=supported,
        )

    @staticmethod
    def write_legacy_fields(
        fields: list[ExportField],
        output_root: Path,
        *,
        template_gs3: Path | None = None,
        merge_setup: bool = True,
    ) -> Path:
        """Modo legado: apenas poligonos/limites."""
        return export_gs3_usb(
            fields,
            output_root,
            template_gs3=template_gs3,
            merge_setup=merge_setup,
        )
