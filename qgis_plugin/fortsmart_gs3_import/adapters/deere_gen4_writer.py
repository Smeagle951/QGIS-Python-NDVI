"""FortSmart — escritor John Deere Gen4."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..fortsmart_model import FortSmartFieldBundle
from ..gen4_export import export_gen4
from ..gen4_pass_guidance import Gen4GuidanceRouter
from ..jd_guidance_plan import GEN4, guidance_plan_for, require_single_external_boundary
from ..jd_guidance_report import run_guidance_export
from ..profiles import PROFILES
from .base import MonitorWriter, filter_canonical_bundles


DEERE_GEN4_ROLES = {
    "EXTERNAL_BOUNDARY",
    "OPERATIONAL_BOUNDARY",
    "AB_LINE",
    "AB_CURVE",
    "GUIDANCE_TRACK",
    "OBSTACLE",
}


class DeereGen4Writer(MonitorWriter):
    def __init__(self) -> None:
        self.profile = PROFILES["john_deere_gen4"]

    def write(
        self,
        bundles: list[FortSmartFieldBundle],
        output_root: Path,
        **options: Any,
    ) -> Path:
        filtered = self._filter_bundles(bundles)
        if not filtered:
            raise ValueError("Nenhuma feicao compativel com Gen4.")
        require_single_external_boundary(filtered)
        plan = guidance_plan_for(
            bundles,
            options.get("guidance_options") or options.get("gs3_pass_options"),
            [GEN4],
        )
        if plan is None:
            return export_gen4(filtered, output_root)

        def export() -> Path:
            gen4_dir = export_gen4(filtered, output_root)
            Gen4GuidanceRouter(plan).apply(gen4_dir)
            return gen4_dir

        return run_guidance_export(
            output_root,
            plan,
            {GEN4: [bundle.field_erid for bundle in filtered]},
            export,
        )

    def _filter_bundles(self, bundles: list[FortSmartFieldBundle]) -> list[FortSmartFieldBundle]:
        supported = set(self.profile.supported_kinds)
        return filter_canonical_bundles(
            bundles,
            allowed_roles=DEERE_GEN4_ROLES,
            legacy_kinds=supported,
        )
