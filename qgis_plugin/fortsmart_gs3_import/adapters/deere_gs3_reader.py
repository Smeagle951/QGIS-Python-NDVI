"""FortSmart — leitor John Deere GS3 2630."""

from __future__ import annotations

from pathlib import Path

from ..gs3_rcd_core import collect_gs3_fields
from ..profiles import PROFILES
from .base import MonitorReader


class DeereGS3Reader(MonitorReader):
    def __init__(self) -> None:
        self.profile = PROFILES["john_deere_gs3"]

    def detect(self, path: Path) -> bool:
        p = Path(path)
        if p.is_file():
            return p.name.lower() == "setup.fds"
        if (p / "setup.fds").exists():
            return True
        if p.name.upper() == "GS3_2630":
            return any(p.rglob("setup.fds"))
        return bool(list(p.rglob("setup.fds")))

    def read(self, path: Path, *, unify_farms: bool = True):
        return collect_gs3_fields(path, unify_farms=unify_farms)
