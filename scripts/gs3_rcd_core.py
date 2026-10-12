"""Core GS3 — reexporta modulo do plugin FortSmart."""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_CORE = Path(__file__).resolve().parent.parent / "qgis_plugin" / "fortsmart_gs3_import" / "gs3_rcd_core.py"
if PLUGIN_CORE.exists():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gs3_rcd_core", PLUGIN_CORE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    collect_gs3_boundaries = mod.collect_gs3_boundaries
    Gs3Boundary = mod.Gs3Boundary
    Gs3ImportResult = mod.Gs3ImportResult
else:
    from gs3_rcd_core import Gs3Boundary, Gs3ImportResult, collect_gs3_boundaries  # type: ignore

__all__ = ["Gs3Boundary", "Gs3ImportResult", "collect_gs3_boundaries"]
