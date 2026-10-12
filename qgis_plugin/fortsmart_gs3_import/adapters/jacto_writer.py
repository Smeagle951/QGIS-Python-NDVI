"""FortSmart — escritor Jacto OMNI USB."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..fortsmart_model import FortSmartFieldBundle, SpatialKind
from ..jacto_export import export_jacto_omni
from ..profiles import PROFILES
from .base import MonitorWriter, filter_canonical_bundles


JACTO_ROLES = {
    "EXTERNAL_BOUNDARY",
    "OPERATIONAL_BOUNDARY",
    "HEADLAND",
    "AB_LINE",
    "AB_CURVE",
    "GUIDANCE_TRACK",
}


class JactoWriter(MonitorWriter):
    def __init__(self) -> None:
        self.profile = PROFILES["jacto"]

    def write(
        self,
        bundles: list[FortSmartFieldBundle],
        output_root: Path,
        **options: Any,
    ) -> Path:
        filtered = self._filter_bundles(bundles)
        if not filtered:
            raise ValueError("Nenhuma feicao compativel com Jacto OMNI.")
        return export_jacto_omni(filtered, output_root)

    def _filter_bundles(self, bundles: list[FortSmartFieldBundle]) -> list[FortSmartFieldBundle]:
        supported = set(self.profile.supported_kinds)
        return filter_canonical_bundles(
            bundles,
            allowed_roles=JACTO_ROLES,
            legacy_kinds=supported,
        )
