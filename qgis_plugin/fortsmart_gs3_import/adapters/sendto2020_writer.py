"""FortSmart — escritor SendTo2020 / Precision Planting 20/20."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..fortsmart_model import FortSmartFieldBundle
from ..profiles import PROFILES
from ..sendto2020_export import export_sendto2020
from ..sendto2020_geometry import SendTo2020Config
from ..sendto2020_traffic_exclusions import (
    BOUNDARY_MODE_INVALID,
    BOUNDARY_MODES,
    BOUNDARY_ONLY,
    BOUNDARY_WITH_TRAFFIC_EXCLUSIONS,
    SendTo2020TrafficExclusionMode,
)
from .base import MonitorWriter


SENDTO2020_ROLES = {
    "EXTERNAL_BOUNDARY",
}


class SendTo2020Writer(MonitorWriter):
    def __init__(self) -> None:
        self.profile = PROFILES["sendto2020"]

    def write(
        self,
        bundles: list[FortSmartFieldBundle],
        output_root: Path,
        **options: Any,
    ) -> Path:
        filtered = self._filter_bundles(bundles)
        if not filtered:
            raise ValueError("Nenhum EXTERNAL_BOUNDARY compativel com SendTo2020.")
        config = SendTo2020Config(
            vertex_warning=int(options.get("sendto2020_vertex_warning", 10_000)),
            vertex_target=int(options.get("sendto2020_vertex_target", 2_000)),
            vertex_max=int(options.get("sendto2020_vertex_max", 10_000)),
            max_area_difference_percent=float(
                options.get("sendto2020_max_area_difference_percent", 0.5)
            ),
        )
        mode = options.get("sendto2020_boundary_mode") or BOUNDARY_ONLY
        if mode not in BOUNDARY_MODES:
            raise ValueError(f"{BOUNDARY_MODE_INVALID}: modo de boundary desconhecido '{mode}'.")
        if mode == BOUNDARY_WITH_TRAFFIC_EXCLUSIONS:
            return export_sendto2020(
                filtered,
                output_root,
                source_bundles=bundles,
                config=config,
                reference_roots=self._reference_roots(output_root, options.get("sendto2020_reference")),
                traffic_exclusions=SendTo2020TrafficExclusionMode(
                    options.get("sendto2020_traffic_sources"),
                    options.get("sendto2020_traffic_config"),
                ),
            )
        return export_sendto2020(
            filtered,
            output_root,
            source_bundles=bundles,
            config=config,
            reference_roots=self._reference_roots(output_root, options.get("sendto2020_reference")),
        )

    @staticmethod
    def _reference_roots(output_root: Path, explicit: Any) -> list[Path]:
        roots: list[Path] = []
        if explicit:
            roots.append(Path(explicit))
        try:
            from ..usb_copy import list_usb_drives

            system_drive = os.environ.get("SystemDrive", "C:").rstrip(":\\").upper()
            for drive in list_usb_drives():
                if drive.letter.upper() == system_drive:
                    continue
                candidate = drive.root / "SendTo2020"
                if candidate.is_dir():
                    roots.append(candidate)
        except Exception:
            pass
        output = (Path(output_root) / "SendTo2020").resolve()
        unique: list[Path] = []
        for root in roots:
            resolved = root.resolve()
            if resolved != output and resolved not in unique:
                unique.append(resolved)
        return unique

    def _filter_bundles(self, bundles: list[FortSmartFieldBundle]) -> list[FortSmartFieldBundle]:
        filtered: list[FortSmartFieldBundle] = []
        for bundle in bundles:
            features = []
            for feature in bundle.features:
                role = str(
                    feature.extra.get("fortsmart_role")
                    or feature.extra.get("canonical_role")
                    or ""
                ).strip().upper()
                if role:
                    allowed = role in SENDTO2020_ROLES
                else:
                    allowed = feature.kind.value in self.profile.supported_kinds
                if allowed:
                    features.append(feature)
            if features:
                filtered.append(
                    FortSmartFieldBundle(
                        cliente=bundle.cliente,
                        fazenda=bundle.fazenda,
                        talhao=bundle.talhao,
                        field_erid=bundle.field_erid,
                        features=features,
                    )
                )
        return filtered
