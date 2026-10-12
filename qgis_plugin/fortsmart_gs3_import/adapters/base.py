"""FortSmart — interfaces base para adaptadores de monitor."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..fortsmart_model import FortSmartFieldBundle, FortSmartImportResult, SpatialKind
from ..canonical_project import canonical_feature_allowed


def filter_canonical_bundles(
    bundles: list[FortSmartFieldBundle],
    *,
    allowed_roles: set[str],
    legacy_kinds: set[str],
) -> list[FortSmartFieldBundle]:
    """Filter by semantic role, retaining a kind fallback for legacy imports."""
    out: list[FortSmartFieldBundle] = []
    for bundle in bundles:
        features = [
            feature
            for feature in bundle.features
            if canonical_feature_allowed(
                feature,
                allowed_roles=allowed_roles,
                legacy_kinds=legacy_kinds,
            )
        ]
        if features:
            out.append(
                FortSmartFieldBundle(
                    cliente=bundle.cliente,
                    fazenda=bundle.fazenda,
                    talhao=bundle.talhao,
                    field_erid=bundle.field_erid,
                    features=features,
                )
            )
    return out


@dataclass
class MonitorProfile:
    """Perfil de compatibilidade de um monitor/marca."""

    id: str
    name: str
    brand: str
    format_key: str  # gs3 | isoxml_v3 | isoxml_v4
    import_patterns: list[str] = field(default_factory=list)
    export_root: str = ""
    fat32_required: bool = False
    supported_kinds: list[str] = field(default_factory=list)
    unsupported_kinds: list[str] = field(default_factory=list)
    notes: str = ""
    isoxml_version_major: str = "4"
    isoxml_version_minor: str = "3"
    extra: dict[str, Any] = field(default_factory=dict)

    def supports(self, kind: SpatialKind) -> bool:
        if not self.supported_kinds:
            return True
        return kind.value in self.supported_kinds


class MonitorReader(ABC):
    profile: MonitorProfile

    @abstractmethod
    def detect(self, path: Path) -> bool:
        ...

    @abstractmethod
    def read(self, path: Path, *, unify_farms: bool = True) -> FortSmartImportResult:
        ...


class MonitorWriter(ABC):
    profile: MonitorProfile

    @abstractmethod
    def write(
        self,
        bundles: list[FortSmartFieldBundle],
        output_root: Path,
        **options: Any,
    ) -> Path:
        ...
