"""FortSmart — pasta John Deere JD-Data (logs .jdl + Gen4 companheiro).

A pasta ``JD-Data`` exportada do display Gen4/4600 contem apenas arquivos
``.jdl`` (registro de operacao: velocidade, status, telemetria). Os limites,
curvas, marcacoes e obstaculos ficam no pacote **Gen4** (``MasterData.xml`` +
``SpatialFiles/*.gjson``), normalmente copiado junto na mesma exportacao USB.

Este modulo inventaria os projetos nos logs e importa a geometria real a partir
do Gen4 encontrado automaticamente ao lado da pasta JD-Data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .gen4_import import collect_gen4_fields, detect_gen4, find_gen4_root
from .gs3_spatial_types import Gs3FieldImportResult


@dataclass
class JdlProject:
    folder_name: str
    field_hint: str
    jdl_files: list[Path] = field(default_factory=list)
    session_uuids: list[str] = field(default_factory=list)


@dataclass
class JdDataScan:
    root: Path
    projects: list[JdlProject]
    jdl_count: int
    companion_gen4: Path | None
    notes: list[str] = field(default_factory=list)

    @property
    def summary_lines(self) -> list[str]:
        lines = [
            f"Pasta JD-Data: {self.jdl_count} arquivo(s) .jdl em {len(self.projects)} projeto(s)",
        ]
        if self.companion_gen4:
            lines.append(f"Geometria encontrada em: {self.companion_gen4}")
        else:
            lines.append("Geometria NAO encontrada — copie tambem a pasta Gen4 do USB.")
        for note in self.notes:
            lines.append(note)
        return lines


def detect_jd_data(path: Path) -> bool:
    """True se a pasta contem logs .jdl e nao e GS3/Gen4 direto."""
    root = Path(path)
    if not root.is_dir():
        return False
    if detect_gen4(root):
        return False
    jdl_files = list(root.rglob("*.jdl"))
    if not jdl_files:
        return False
    if list(root.rglob("*.fdShape")):
        return False
    return True


def _field_hint_from_folder(folder_name: str) -> str:
    text = folder_name.strip()
    text = re.sub(r"^\d{4}_", "", text)
    if "_" in text:
        return text.rsplit("_", 1)[-1].strip()
    return text


def _extract_session_uuid(jdl_path: Path) -> str:
    match = re.search(r"\{([0-9a-f-]{36})\}", jdl_path.stem, re.I)
    return match.group(1).lower() if match else ""


def scan_jd_data(path: Path) -> JdDataScan:
    root = Path(path).resolve()
    jdl_files = sorted(root.rglob("*.jdl"))
    by_folder: dict[str, list[Path]] = {}
    for jdl in jdl_files:
        rel = jdl.parent.relative_to(root)
        key = str(rel).replace("\\", "/")
        by_folder.setdefault(key, []).append(jdl)

    projects: list[JdlProject] = []
    for folder_name, files in sorted(by_folder.items()):
        leaf = folder_name.split("/")[-1] if folder_name else root.name
        uuids = sorted({_extract_session_uuid(f) for f in files if _extract_session_uuid(f)})
        projects.append(
            JdlProject(
                folder_name=folder_name,
                field_hint=_field_hint_from_folder(leaf),
                jdl_files=files,
                session_uuids=uuids,
            )
        )

    companion = find_companion_gen4(root)
    notes: list[str] = []
    if jdl_files:
        notes.append(
            "Arquivos .jdl sao logs de operacao (telemetria), nao limites/linhas. "
            "A conversao usa o Gen4 companheiro."
        )

    return JdDataScan(
        root=root,
        projects=projects,
        jdl_count=len(jdl_files),
        companion_gen4=companion,
        notes=notes,
    )


def find_companion_gen4(jd_root: Path) -> Path | None:
    """Procura MasterData.xml + SpatialFiles perto da pasta JD-Data."""
    root = Path(jd_root).resolve()
    direct = [
        root.parent / "Gen4",
        root.parent / "Gen4" / "Gen4",
        root / "Gen4",
        root / "Gen4" / "Gen4",
    ]
    for candidate in direct:
        if detect_gen4(candidate):
            try:
                return find_gen4_root(candidate)
            except FileNotFoundError:
                continue

    current = root
    for _ in range(6):
        for master in sorted(current.rglob("MasterData.xml")):
            spatial = master.parent / "SpatialFiles"
            if spatial.is_dir() and any(spatial.glob("*.gjson")):
                try:
                    return find_gen4_root(master.parent)
                except FileNotFoundError:
                    continue
        if current.parent == current:
            break
        current = current.parent
    return None


def collect_jd_data_fields(path: Path, *, unify_farms: bool = True) -> tuple[Gs3FieldImportResult, JdDataScan]:
    scan = scan_jd_data(path)
    if not scan.companion_gen4:
        project_list = ", ".join(p.field_hint for p in scan.projects[:12])
        extra = "..." if len(scan.projects) > 12 else ""
        raise FileNotFoundError(
            "Pasta JD-Data contem apenas logs .jdl (telemetria).\n\n"
            f"Inventario: {scan.jdl_count} logs em {len(scan.projects)} projetos "
            f"({project_list}{extra}).\n\n"
            "Para ver limites, curvas e marcacoes no QGIS, selecione o pacote Gen4:\n"
            "  ...\\Gen4\\MasterData.xml  (com pasta SpatialFiles ao lado)\n\n"
            "Copie a pasta Gen4 do mesmo USB/exportacao onde veio o JD-Data."
        )

    result = collect_gen4_fields(scan.companion_gen4, unify_farms=unify_farms)
    result.format_source = "jd_data+gen4"
    return result, scan
