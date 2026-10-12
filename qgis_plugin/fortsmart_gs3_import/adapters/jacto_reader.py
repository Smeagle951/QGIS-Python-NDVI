"""FortSmart — leitor Jacto OMNI USB."""

from __future__ import annotations

from pathlib import Path

from ..jacto_import import collect_jacto_fields, detect_jacto
from ..profiles import PROFILES
from .base import MonitorReader


class JactoReader(MonitorReader):
    def __init__(self) -> None:
        self.profile = PROFILES["jacto"]

    def detect(self, path: Path) -> bool:
        return detect_jacto(path)

    def read(self, path: Path, *, unify_farms: bool = True):
        return collect_jacto_fields(path, unify_farms=unify_farms)
