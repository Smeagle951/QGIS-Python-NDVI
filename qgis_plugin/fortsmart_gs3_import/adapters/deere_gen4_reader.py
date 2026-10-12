"""FortSmart — leitor John Deere Gen4."""

from __future__ import annotations

from pathlib import Path

from ..gen4_import import collect_gen4_fields, detect_gen4
from ..profiles import PROFILES
from .base import MonitorReader


class DeereGen4Reader(MonitorReader):
    def __init__(self) -> None:
        self.profile = PROFILES["john_deere_gen4"]

    def detect(self, path: Path) -> bool:
        return detect_gen4(path)

    def read(self, path: Path, *, unify_farms: bool = True):
        return collect_gen4_fields(path, unify_farms=unify_farms)
