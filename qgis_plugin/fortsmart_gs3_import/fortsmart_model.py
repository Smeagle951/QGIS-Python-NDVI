"""FortSmart — modelo interno unificado (hub de agricultura de precisao)."""

from __future__ import annotations

# Aliases publicos FortSmart (compativel com nomes GS3 legados)
from .gs3_spatial_types import (
    Gs3FieldBundle,
    Gs3FieldImportResult,
    Gs3SpatialFeature,
    LAYER_LABELS,
    LAYER_ORDER,
    SpatialKind,
)

FortSmartFeature = Gs3SpatialFeature
FortSmartFieldBundle = Gs3FieldBundle
FortSmartImportResult = Gs3FieldImportResult

__all__ = [
    "FortSmartFeature",
    "FortSmartFieldBundle",
    "FortSmartImportResult",
    "Gs3FieldBundle",
    "Gs3FieldImportResult",
    "Gs3SpatialFeature",
    "LAYER_LABELS",
    "LAYER_ORDER",
    "SpatialKind",
]
