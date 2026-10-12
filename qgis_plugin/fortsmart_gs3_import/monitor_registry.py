"""FortSmart — registro central de leitores/escritores por monitor."""

from __future__ import annotations

from pathlib import Path

from .adapters.base import MonitorProfile, MonitorReader, MonitorWriter
from .adapters.deere_gs3_reader import DeereGS3Reader
from .adapters.deere_gs3_writer import DeereGS3Writer
from .adapters.deere_gen4_reader import DeereGen4Reader
from .adapters.deere_gen4_writer import DeereGen4Writer
from .adapters.deere_dual_writer import DeereDualWriter
from .adapters.isoxml_reader import ISOXMLReader
from .adapters.isoxml_writer import ISOXMLWriter
from .adapters.jacto_reader import JactoReader
from .adapters.jacto_writer import JactoWriter
from .adapters.sendto2020_writer import SendTo2020Writer
from .adapters.stara_reader import StaraReader
from .adapters.stara_writer import StaraWriter
from .fortsmart_model import FortSmartFieldBundle, FortSmartImportResult
from .profiles import EXPORT_PROFILES, IMPORT_PROFILES, PROFILES
from .isoxml_import import detect_import_format


def get_profile(profile_id: str) -> MonitorProfile:
    if profile_id not in PROFILES:
        raise KeyError(f"Perfil desconhecido: {profile_id}")
    return PROFILES[profile_id]


def list_export_profiles() -> list[MonitorProfile]:
    return [PROFILES[pid] for pid in EXPORT_PROFILES if pid in PROFILES]


def list_import_profiles() -> list[MonitorProfile]:
    return [PROFILES[pid] for pid in IMPORT_PROFILES if pid in PROFILES]


_READERS: list[MonitorReader] = [
    DeereGS3Reader(),
    DeereGen4Reader(),
    ISOXMLReader(),
    JactoReader(),
    StaraReader(),
]

_WRITERS: dict[str, MonitorWriter] = {
    "john_deere_dual": DeereDualWriter(),
    "john_deere_gs3": DeereGS3Writer(),
    "john_deere_gen4": DeereGen4Writer(),
    "isoxml_v4": ISOXMLWriter(PROFILES["isoxml_v4"]),
    "isoxml_v3": ISOXMLWriter(PROFILES["isoxml_v3"]),
    "cnh": ISOXMLWriter(PROFILES["cnh"]),
    "fendt": ISOXMLWriter(PROFILES["fendt"]),
    "jacto": JactoWriter(),
    "sendto2020": SendTo2020Writer(),
    "stara": StaraWriter(),
    "precision_planting": ISOXMLWriter(PROFILES["precision_planting"]),
}


def detect_reader(path: Path) -> MonitorReader | None:
    for reader in _READERS:
        if reader.detect(path):
            return reader
    return None


def read_fields(path: Path, *, profile_id: str = "auto", unify_farms: bool = True) -> FortSmartImportResult:
    if profile_id != "auto":
        reader = next((r for r in _READERS if r.profile.id == profile_id), None)
        if reader is None:
            raise RuntimeError(f"Leitor nao disponivel para perfil: {profile_id}")
        return reader.read(path, unify_farms=unify_farms)

    detected = detect_import_format(path)
    if detected == "gs3":
        return DeereGS3Reader().read(path, unify_farms=unify_farms)
    if detected == "gen4":
        return DeereGen4Reader().read(path, unify_farms=unify_farms)
    if detected == "isoxml":
        return ISOXMLReader().read(path, unify_farms=unify_farms)
    if detected == "jacto":
        return JactoReader().read(path, unify_farms=unify_farms)
    if detected == "stara":
        return StaraReader().read(path, unify_farms=unify_farms)
    raise RuntimeError(
        f"Formato nao reconhecido em {path}.\n"
        "Selecione GS3_2630, Gen4/MasterData.xml, TASKDATA, OMNI/ ou DADOS/."
    )


def get_writer(profile_id: str) -> MonitorWriter:
    writer = _WRITERS.get(profile_id)
    if writer is None:
        profile = get_profile(profile_id)
        if profile.format_key.startswith("isoxml"):
            return ISOXMLWriter(profile)
        if profile.format_key == "jacto_shp":
            return JactoWriter()
        if profile.format_key == "sendto2020_shp":
            return SendTo2020Writer()
        if profile.format_key == "stara_shp":
            return StaraWriter()
        if profile.format_key == "gen4":
            return DeereGen4Writer()
        if profile.format_key == "deere_dual":
            return DeereDualWriter()
        raise RuntimeError(f"Escritor nao disponivel para: {profile_id}")
    return writer


def export_fields(
    bundles: list[FortSmartFieldBundle],
    output_root: Path,
    *,
    profile_id: str,
    template_gs3: Path | None = None,
    merge_setup: bool = True,
    sendto2020_reference: Path | None = None,
    guidance_options=None,
    gs3_pass_options=None,
) -> Path:
    writer = get_writer(profile_id)
    options: dict = {}
    guidance_options = guidance_options or gs3_pass_options
    if profile_id in ("john_deere_gs3", "john_deere_dual"):
        options["template_gs3"] = template_gs3
        options["merge_setup"] = merge_setup
    if profile_id in ("john_deere_gs3", "john_deere_gen4", "john_deere_dual") and guidance_options is not None:
        options["guidance_options"] = guidance_options
    if profile_id == "sendto2020" and sendto2020_reference:
        options["sendto2020_reference"] = sendto2020_reference
    return writer.write(bundles, output_root, **options)
