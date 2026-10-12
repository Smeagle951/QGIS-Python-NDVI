"""FortSmart — leitor Stara USB DADOS/."""

from __future__ import annotations

from pathlib import Path

from ..profiles import PROFILES
from ..stara_import import collect_stara_fields, detect_stara
from .base import MonitorReader


class StaraReader(MonitorReader):
    def __init__(self) -> None:
        self.profile = PROFILES["stara"]

    def detect(self, path: Path) -> bool:
        return detect_stara(path)

    def read(self, path: Path, *, unify_farms: bool = True):
        return collect_stara_fields(path, unify_farms=unify_farms)
