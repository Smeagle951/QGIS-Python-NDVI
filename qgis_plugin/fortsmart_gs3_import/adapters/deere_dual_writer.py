"""FortSmart — escritor John Deere dual (Gen4 + GS3_2630, regra dual Gen4+GS3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..deere_dual_export import dual_field_erids, export_john_deere_dual
from ..fortsmart_model import FortSmartFieldBundle
from ..jd_guidance_plan import GEN4, GS3, guidance_plan_for, require_single_external_boundary
from ..jd_guidance_report import run_guidance_export
from ..profiles import PROFILES
from .base import MonitorWriter, filter_canonical_bundles
from .deere_gs3_writer import guidance_options


DEERE_DUAL_ROLES = {
    "EXTERNAL_BOUNDARY",
    "OPERATIONAL_BOUNDARY",
    "INTERNAL_BOUNDARY",
    "HEADLAND",
    "AB_LINE",
    "AB_CURVE",
    "GUIDANCE_TRACK",
    "OBSTACLE",
}


class DeereDualWriter(MonitorWriter):
    def __init__(self) -> None:
        self.profile = PROFILES["john_deere_dual"]

    def write(
        self,
        bundles: list[FortSmartFieldBundle],
        output_root: Path,
        **options: Any,
    ) -> Path:
        filtered = filter_canonical_bundles(
            bundles,
            allowed_roles=DEERE_DUAL_ROLES,
            legacy_kinds=set(self.profile.supported_kinds),
        )
        if not filtered:
            raise ValueError("Nenhuma feicao canonica compativel com John Deere Dual.")
        require_single_external_boundary(filtered)
        plan = guidance_plan_for(bundles, guidance_options(options), [GS3, GEN4])

        def export() -> Path:
            return export_john_deere_dual(
                filtered,
                output_root,
                template_gs3=options.get("template_gs3"),
                merge_setup=options.get("merge_setup", True),
                guidance_plan=plan,
            )

        if plan is None:
            return export()
        gen4_fields, gs3_fields = dual_field_erids(filtered)
        return run_guidance_export(
            output_root,
            plan,
            {GS3: gs3_fields, GEN4: gen4_fields},
            export,
        )
