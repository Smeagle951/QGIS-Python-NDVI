"""FortSmart — leitor ISOXML ISO 11783-10."""

from __future__ import annotations

from pathlib import Path

from ..isoxml_import import collect_isoxml_fields, detect_import_format, find_taskdata_xml
from ..profiles import PROFILES
from .base import MonitorReader


class ISOXMLReader(MonitorReader):
    def __init__(self) -> None:
        self.profile = PROFILES["isoxml_v4"]

    def detect(self, path: Path) -> bool:
        try:
            find_taskdata_xml(Path(path))
            return True
        except (ValueError, OSError):
            return detect_import_format(path) == "isoxml"

    def read(self, path: Path, *, unify_farms: bool = True):
        return collect_isoxml_fields(path, unify_farms=unify_farms)
