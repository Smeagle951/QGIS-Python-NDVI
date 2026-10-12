"""FortSmart — copia segura para pendrive USB (FAT32)."""

from __future__ import annotations

import json
import os
import shutil
import string
import subprocess
from dataclasses import dataclass
from pathlib import Path


PAYLOAD_HINTS = {
    "john_deere_dual": (".", "Copie Gen4/ e GS3_2630/ para a RAIZ do USB."),
    "john_deere_gs3": ("GS3_2630", "Copie a pasta GS3_2630 para a RAIZ do USB."),
    "isoxml_v4": ("TASKDATA", "Copie a pasta TASKDATA para a RAIZ (ou pasta aceita pelo monitor)."),
    "isoxml_v3": ("TASKDATA", "Copie a pasta TASKDATA para a RAIZ."),
    "jacto": ("OMNI", "Copie a pasta OMNI para a RAIZ do USB Jacto."),
    "sendto2020": ("SendTo2020", "Copie a pasta SendTo2020 para a RAIZ do USB do monitor 20/20."),
    "stara": ("DADOS", "Copie a pasta DADOS para a RAIZ do USB Stara."),
    "john_deere_gen4": ("Gen4", "Copie a pasta Gen4 (MasterData.xml + SpatialFiles) para o USB."),
    "cnh": ("TASKDATA", "Copie TASKDATA (ISOXML)."),
    "fendt": ("TASKDATA", "Copie TASKDATA (ISOXML)."),
}


@dataclass
class UsbDrive:
    letter: str
    label: str
    file_system: str
    free_gb: float
    is_removable: bool

    @property
    def root(self) -> Path:
        return Path(f"{self.letter}:\\")

    @property
    def fat32_ok(self) -> bool:
        return self.file_system.upper() in ("FAT32", "FAT")


def _powershell_executable() -> str:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    executable = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(executable) if executable.is_file() else "powershell"


def list_usb_drives() -> list[UsbDrive]:
    """Lista unidades removiveis no Windows via PowerShell."""
    drives: list[UsbDrive] = []
    try:
        ps = (
            "Get-CimInstance Win32_LogicalDisk | "
            "Where-Object { $_.DriveType -eq 2 -or $_.DriveType -eq 3 } | "
            "Select-Object DeviceID, VolumeName, FileSystem, FreeSpace, DriveType | "
            "ConvertTo-Csv -NoTypeInformation"
        )
        raw = subprocess.check_output(
            [_powershell_executable(), "-NoProfile", "-Command", ps],
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if len(lines) < 2:
            return _fallback_drives()
        headers = [h.strip('"') for h in lines[0].split(",")]
        for line in lines[1:]:
            cols = [c.strip('"') for c in line.split(",")]
            row = dict(zip(headers, cols))
            device = row.get("DeviceID", "")
            if not device or ":" not in device:
                continue
            letter = device[0].upper()
            try:
                free = float(row.get("FreeSpace") or 0) / (1024**3)
            except ValueError:
                free = 0.0
            drive_type = str(row.get("DriveType") or "")
            is_removable = drive_type == "2"
            fs = (row.get("FileSystem") or "").upper()
            # incluir removiveis; fixos so se parecerem pendrive de dados (E: tipico)
            if not is_removable and letter in ("C",):
                continue
            drives.append(
                UsbDrive(
                    letter=letter,
                    label=row.get("VolumeName") or "",
                    file_system=fs or "UNKNOWN",
                    free_gb=free,
                    is_removable=is_removable,
                )
            )
    except Exception:
        return _fallback_drives()
    return drives


def _fallback_drives() -> list[UsbDrive]:
    out: list[UsbDrive] = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        if not root.exists():
            continue
        if letter == "C":
            continue
        out.append(UsbDrive(letter=letter, label="", file_system="UNKNOWN", free_gb=0.0, is_removable=True))
    return out


def detect_payload(export_root: Path) -> tuple[str, Path, str]:
    """Retorna (profile_hint, pasta_a_copiar, mensagem).

    Para pacote dual (Gen4 + GS3_2630), retorna a pasta raiz do export.
    """
    root = Path(export_root)
    has_gs3 = (root / "GS3_2630").exists() or root.name.upper() == "GS3_2630"
    has_gen4 = (root / "Gen4").exists() or (root / "MasterData.xml").exists()
    if has_gs3 and has_gen4 and (root / "GS3_2630").exists() and (root / "Gen4").exists():
        return (
            "john_deere_dual",
            root,
            PAYLOAD_HINTS["john_deere_dual"][1],
        )

    candidates = [
        ("john_deere_gs3", root / "GS3_2630", "GS3_2630"),
        ("john_deere_gs3", root if root.name.upper() == "GS3_2630" else None, "GS3_2630"),
        ("isoxml_v4", root / "TASKDATA", "TASKDATA"),
        ("isoxml_v4", root if (root / "TASKDATA.XML").exists() else None, "TASKDATA"),
        ("jacto", root / "OMNI", "OMNI"),
        ("sendto2020", root / "SendTo2020", "SendTo2020"),
        (
            "sendto2020",
            root if root.name.casefold() == "sendto2020" else None,
            "SendTo2020",
        ),
        ("stara", root / "DADOS", "DADOS"),
        ("john_deere_gen4", root / "Gen4", "Gen4"),
        ("john_deere_gen4", root if (root / "MasterData.xml").exists() else None, "Gen4"),
    ]
    for profile, path, name in candidates:
        if path and path.exists():
            hint = PAYLOAD_HINTS.get(profile, (name, f"Copie {name} para a raiz do USB."))[1]
            if name == "TASKDATA" and path.is_dir() and (path / "TASKDATA.XML").exists():
                return profile, path, hint
            if name == "TASKDATA" and path.is_file():
                return profile, path.parent, hint
            return profile, path, hint
    raise FileNotFoundError(
        f"Nao encontrei GS3_2630, TASKDATA, OMNI, SendTo2020, DADOS ou Gen4 em {export_root}"
    )


def copy_payload_to_usb(
    export_root: Path,
    usb_root: Path,
    *,
    overwrite: bool = True,
) -> Path:
    profile, payload, _hint = detect_payload(export_root)

    if profile == "john_deere_dual":
        # Copia Gen4/ e GS3_2630/ para a raiz do USB (layout dual Gen4+GS3)
        last = usb_root
        for folder_name in ("Gen4", "GS3_2630"):
            src = payload / folder_name
            if not src.exists():
                continue
            dest = usb_root / folder_name
            if dest.exists() and overwrite:
                shutil.rmtree(dest)
            elif dest.exists() and not overwrite:
                raise FileExistsError(f"Ja existe {dest}. Marque sobrescrever.")
            shutil.copytree(src, dest)
            last = dest
        return last

    dest_name = payload.name
    if (payload / "TASKDATA.XML").exists() and payload.name.upper() != "TASKDATA":
        dest_name = "TASKDATA"
        dest = usb_root / dest_name
        if dest.exists() and overwrite:
            shutil.rmtree(dest)
        shutil.copytree(payload, dest)
        return dest

    dest = usb_root / dest_name
    if dest.exists():
        if not overwrite:
            raise FileExistsError(f"Ja existe {dest}. Marque sobrescrever.")
        shutil.rmtree(dest)
    shutil.copytree(payload, dest)
    return dest


def verify_copy(dest: Path) -> list[str]:
    issues: list[str] = []
    if not dest.exists():
        return [f"Destino inexistente: {dest}"]

    drive = dest.drive.rstrip(":")
    if drive:
        try:
            command = (
                f"Get-Volume -DriveLetter {drive} | "
                "Select-Object HealthStatus,OperationalStatus | "
                "ConvertTo-Json -Compress"
            )
            raw = subprocess.check_output(
                [_powershell_executable(), "-NoProfile", "-Command", command],
                text=True,
                encoding="utf-8",
                errors="replace",
            ).strip()
            health = json.loads(raw) if raw else {}
            health_status = str(health.get("HealthStatus", ""))
            operational_status = str(health.get("OperationalStatus", ""))
            if health_status not in ("", "Healthy") or operational_status not in ("", "OK"):
                issues.append(
                    "USB requer verificacao/reparo no Windows: "
                    f"HealthStatus={health_status or 'desconhecido'}, "
                    f"OperationalStatus={operational_status or 'desconhecido'}."
                )
        except Exception:
            # File validation below remains authoritative when Windows does
            # not expose volume-health information to this process.
            pass

    # Dual: dest pode ser Gen4 ou GS3_2630; validar irmao se existir
    parent = dest.parent
    if (parent / "Gen4").exists() and (parent / "GS3_2630").exists():
        if not (parent / "Gen4" / "MasterData.xml").exists():
            issues.append("USB dual: Gen4 sem MasterData.xml.")
        if not list((parent / "GS3_2630").rglob("setup.fds")):
            issues.append("USB dual: GS3_2630 sem setup.fds.")
        return issues

    name = dest.name.upper()
    if name == "GS3_2630":
        if not list(dest.rglob("setup.fds")):
            issues.append("USB sem setup.fds apos copia GS3.")
    elif name == "TASKDATA":
        if not (dest / "TASKDATA.XML").exists():
            issues.append("USB sem TASKDATA.XML.")
    elif name == "OMNI":
        if not list(dest.rglob("_perimeter.shp")):
            issues.append("USB OMNI sem _perimeter.shp.")
    elif name == "DADOS":
        if not list(dest.rglob("LIMITE.shp")) and not list(dest.rglob("LINHAS.shp")):
            issues.append("USB DADOS sem LIMITE/LINHAS.")
    elif name == "SENDTO2020":
        shapefiles = sorted(dest.glob("*.shp"))
        if not shapefiles:
            issues.append("USB SendTo2020 sem shapefile de talhao.")
        required = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".mcd")
        for shapefile in shapefiles:
            base = shapefile.with_suffix("")
            missing = [ext for ext in required if not base.with_suffix(ext).is_file()]
            if missing:
                issues.append(
                    f"USB SendTo2020 {base.name}: faltando {', '.join(missing)}."
                )
        if not issues:
            try:
                from .sendto2020_compare import inspect_sendto2020_package

                inspection = inspect_sendto2020_package(dest)
                for field in inspection["fields"]:
                    if field["feature_count"] != 1:
                        issues.append(
                            f"USB SendTo2020 {field['name']}: feature_count diferente de 1."
                        )
                    if field["geometry_types"] != ["Polygon"]:
                        issues.append(
                            f"USB SendTo2020 {field['name']}: geometria nao e Polygon."
                        )
                    if field["crs_epsg"] != 4326:
                        issues.append(
                            f"USB SendTo2020 {field['name']}: CRS nao e EPSG:4326."
                        )
            except Exception as exc:
                issues.append(f"USB SendTo2020 nao pode ser reaberto: {exc}")
    elif name == "GEN4" or (dest / "MasterData.xml").exists():
        if not (dest / "MasterData.xml").exists():
            issues.append("USB Gen4 sem MasterData.xml.")
    return issues
