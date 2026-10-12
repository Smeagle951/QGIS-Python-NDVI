"""FortSmart — escritor ISOXML por perfil de monitor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..fortsmart_model import FortSmartFieldBundle, SpatialKind
from ..isoxml_export import export_isoxml_taskdata
from .base import MonitorProfile, MonitorWriter, filter_canonical_bundles


ISOXML_ROLES = {
    "EXTERNAL_BOUNDARY",
    "OPERATIONAL_BOUNDARY",
    "INTERNAL_BOUNDARY",
    "HEADLAND",
    "AB_LINE",
    "AB_CURVE",
    "GUIDANCE_TRACK",
    "OBSTACLE",
    "SAFETY_ZONE",
    "WORK_PATH",
    "FINAL_WORK_PATH",
    "FIELD_HEADLAND_PATH",
    "OBSTACLE_DETOUR",
    "SHUTOFF",
}


class ISOXMLWriter(MonitorWriter):
    def __init__(self, profile: MonitorProfile) -> None:
        self.profile = profile

    def write(
        self,
        bundles: list[FortSmartFieldBundle],
        output_root: Path,
        **options: Any,
    ) -> Path:
        filtered = self._filter_bundles(bundles)
        if not filtered:
            raise ValueError("Nenhuma feicao compativel com este perfil ISOXML.")
        return export_isoxml_taskdata(
            filtered,
            output_root,
            version_major=self.profile.isoxml_version_major,
            version_minor=self.profile.isoxml_version_minor,
            software_version="2.1.0",
        )

    def _filter_bundles(self, bundles: list[FortSmartFieldBundle]) -> list[FortSmartFieldBundle]:
        if not self.profile.supported_kinds:
            return bundles
        return filter_canonical_bundles(
            bundles,
            allowed_roles=ISOXML_ROLES,
            legacy_kinds=set(self.profile.supported_kinds),
        )
