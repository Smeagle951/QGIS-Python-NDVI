"""FortSmart — escritor Stara USB DADOS/."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..fortsmart_model import FortSmartFieldBundle, SpatialKind
from ..profiles import PROFILES
from ..stara_export import export_stara_dados
from .base import MonitorWriter, filter_canonical_bundles


STARA_ROLES = {
    "EXTERNAL_BOUNDARY",
    "OPERATIONAL_BOUNDARY",
    "HEADLAND",
    "AB_LINE",
    "AB_CURVE",
    "GUIDANCE_TRACK",
}


class StaraWriter(MonitorWriter):
    def __init__(self) -> None:
        self.profile = PROFILES["stara"]

    def write(
        self,
        bundles: list[FortSmartFieldBundle],
        output_root: Path,
        **options: Any,
    ) -> Path:
        filtered = self._filter_bundles(bundles)
        if not filtered:
            raise ValueError("Nenhuma feicao compativel com Stara DADOS.")
        return export_stara_dados(filtered, output_root)

    def _filter_bundles(self, bundles: list[FortSmartFieldBundle]) -> list[FortSmartFieldBundle]:
        supported = set(self.profile.supported_kinds)
        return filter_canonical_bundles(
            bundles,
            allowed_roles=STARA_ROLES,
            legacy_kinds=supported,
        )
