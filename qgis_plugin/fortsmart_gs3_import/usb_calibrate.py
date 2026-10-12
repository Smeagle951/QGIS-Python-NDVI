"""FortSmart — calibracao de estrutura USB (Jacto/Stara/GS3/ISOXML/Gen4)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


EXPECTED = {
    "john_deere_gs3": {
        "markers": ["setup.fds", "GS3_2630"],
        "patterns": ["**/setup.fds", "**/*.SpatialCatalog", "**/*.fdShape"],
        "notes": "Raiz USB deve ter GS3_2630/JD4600/RCD/EIC/",
    },
    "isoxml": {
        "markers": ["TASKDATA.XML", "TASKDATA"],
        "patterns": ["**/TASKDATA.XML", "**/LINKLIST.XML"],
        "notes": "v4: TASKDATA.XML + LINKLIST.XML + AFE",
    },
    "jacto": {
        "markers": ["OMNI", "_perimeter.shp"],
        "patterns": ["**/OMNI/Data/**/_perimeter.shp", "**/_runline.shp"],
        "notes": "Esperado: OMNI/Data/{Fazenda}/{Talhao}/_perimeter.shp + _runline.shp",
    },
    "sendto2020": {
        "markers": ["SendTo2020", ".mcd"],
        "patterns": ["**/SendTo2020/*.shp", "**/SendTo2020/*.mcd"],
        "notes": "Esperado: SendTo2020/{Talhao}.shp + {Talhao}.mcd",
    },
    "stara": {
        "markers": ["DADOS", "LIMITE.shp", "LINHAS.shp"],
        "patterns": ["**/DADOS/TALHOES/**/LIMITE.shp", "**/DADOS/GUIAS/**/LINHAS.shp"],
        "notes": "Esperado: DADOS/TALHOES/{Talhao}/LIMITE.shp e DADOS/GUIAS/{Talhao}/LINHAS.shp",
    },
    "john_deere_gen4": {
        "markers": ["MasterData.xml", "SpatialFiles"],
        "patterns": ["**/MasterData.xml", "**/SpatialFiles/*.gjson"],
        "notes": "Esperado: Gen4/MasterData.xml + Gen4/SpatialFiles/*.gjson",
    },
}


@dataclass
class StructureFinding:
    format_guess: str
    path: str
    matched_files: list[str] = field(default_factory=list)
    missing_expected: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    sample_tree: list[str] = field(default_factory=list)
    ok: bool = False


@dataclass
class CalibrationReport:
    root: str
    findings: list[StructureFinding] = field(default_factory=list)
    created_at: str = ""

    def summary_text(self) -> str:
        lines = [f"Calibracao USB FortSmart — {self.root}", ""]
        if not self.findings:
            lines.append("Nenhuma estrutura conhecida encontrada.")
            lines.append("Envie a arvore de pastas do pendrive para ajustarmos o adaptador.")
            return "\n".join(lines)
        for f in self.findings:
            lines.append(f"[{'OK' if f.ok else 'AJUSTAR'}] {f.format_guess}")
            lines.append(f"  Path: {f.path}")
            if f.matched_files:
                lines.append(f"  Arquivos: {len(f.matched_files)} (ex.: {f.matched_files[0]})")
            if f.missing_expected:
                lines.append("  Faltando vs FortSmart:")
                for m in f.missing_expected[:8]:
                    lines.append(f"    - {m}")
            for s in f.suggestions:
                lines.append(f"  -> {s}")
            if f.sample_tree:
                lines.append("  Amostra:")
                for row in f.sample_tree[:12]:
                    lines.append(f"    {row}")
            lines.append("")
        return "\n".join(lines)


def _sample_tree(root: Path, limit: int = 40) -> list[str]:
    rows: list[str] = []
    try:
        for p in sorted(root.rglob("*"))[:limit]:
            rel = p.relative_to(root)
            suffix = "/" if p.is_dir() else ""
            rows.append(str(rel).replace("\\", "/") + suffix)
    except OSError:
        pass
    return rows


def _find_marker(root: Path, name: str) -> Path | None:
    name_l = name.casefold()
    if root.name.casefold() == name_l:
        return root
    direct = root / name
    if direct.exists():
        return direct
    for p in root.rglob("*"):
        if p.name.casefold() == name_l:
            return p
    return None


def calibrate_usb(root: Path) -> CalibrationReport:
    root = Path(root)
    findings: list[StructureFinding] = []

    # GS3
    setup = next(iter(root.rglob("setup.fds")), None)
    if setup or (root / "GS3_2630").exists() or root.name.upper() == "GS3_2630":
        base = setup.parent if setup else (root / "GS3_2630" if (root / "GS3_2630").exists() else root)
        catalogs = list(base.rglob("*.SpatialCatalog"))[:5]
        shapes = list(base.rglob("*.fdShape"))[:5]
        findings.append(
            StructureFinding(
                format_guess="john_deere_gs3",
                path=str(base),
                matched_files=[str(p) for p in ([setup] if setup else []) + catalogs + shapes],
                missing_expected=[] if setup else ["setup.fds"],
                suggestions=["Use Importar FortSmart → GS3 2630"],
                sample_tree=_sample_tree(base),
                ok=bool(setup),
            )
        )

    # ISOXML
    task = next(iter(root.rglob("TASKDATA.XML")), None)
    if task:
        link = task.parent / "LINKLIST.XML"
        missing = [] if link.exists() else ["LINKLIST.XML (recomendado v4)"]
        findings.append(
            StructureFinding(
                format_guess="isoxml",
                path=str(task.parent),
                matched_files=[str(task)] + ([str(link)] if link.exists() else []),
                missing_expected=missing,
                suggestions=["Export ISOXML v4 gera LINKLIST + AFE automaticamente"],
                sample_tree=_sample_tree(task.parent),
                ok=True,
            )
        )

    # Jacto
    perims = list(root.rglob("_perimeter.shp"))
    omni = _find_marker(root, "OMNI")
    if perims or omni:
        missing = []
        suggestions = []
        if not perims:
            missing.append("_perimeter.shp sob OMNI/Data/{Fazenda}/{Talhao}/")
            suggestions.append("Pendrive Jacto sem _perimeter.shp — envie arvore para calibrar nomes.")
        else:
            # verificar se path parece OMNI/Data/Fazenda/Talhao
            sample = perims[0]
            parts = sample.parts
            if "OMNI" not in [p.upper() for p in parts]:
                suggestions.append(f"Perimeter fora de OMNI: {sample} — adaptar jacto_import se necessario.")
            guidance = list(root.rglob("_guidance.shp"))
            if not guidance:
                suggestions.append("Sem _guidance.shp (ok se so limites).")
        findings.append(
            StructureFinding(
                format_guess="jacto",
                path=str(omni or (perims[0].parents[2] if perims else root)),
                matched_files=[str(p) for p in perims[:8]],
                missing_expected=missing,
                suggestions=suggestions or ["Estrutura compativel com FortSmart OMNI"],
                sample_tree=_sample_tree(omni or root),
                ok=bool(perims),
            )
        )

    # Stara
    limites = list(root.rglob("LIMITE.shp"))
    linhas = list(root.rglob("LINHAS.shp"))
    dados = _find_marker(root, "DADOS")
    if limites or linhas or dados:
        missing = []
        suggestions = []
        if not limites:
            missing.append("DADOS/TALHOES/{Talhao}/LIMITE.shp")
        if not linhas:
            suggestions.append("Sem LINHAS.shp (ok se so limites).")
        # nomes alternativos comuns
        alt = list(root.rglob("*.shp"))
        alt_names = sorted({p.name for p in alt})[:20]
        if not limites and alt_names:
            suggestions.append(f"Shapefiles encontrados: {', '.join(alt_names)} — possivel renomear no adaptador.")
        findings.append(
            StructureFinding(
                format_guess="stara",
                path=str(dados or root),
                matched_files=[str(p) for p in (limites + linhas)[:10]],
                missing_expected=missing,
                suggestions=suggestions or ["Estrutura compativel com FortSmart DADOS"],
                sample_tree=_sample_tree(dados or root),
                ok=bool(limites or linhas),
            )
        )

    # Gen4
    master = next(iter(root.rglob("MasterData.xml")), None)
    if master:
        spatial = master.parent / "SpatialFiles"
        gjson = list(spatial.glob("*.gjson")) if spatial.exists() else []
        findings.append(
            StructureFinding(
                format_guess="john_deere_gen4",
                path=str(master.parent),
                matched_files=[str(master)] + [str(p) for p in gjson[:5]],
                missing_expected=[] if spatial.exists() else ["SpatialFiles/"],
                suggestions=["Use perfil John Deere Gen4 no FortSmart"],
                sample_tree=_sample_tree(master.parent),
                ok=spatial.exists(),
            )
        )

    report = CalibrationReport(
        root=str(root),
        findings=findings,
        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    return report


def save_calibration_report(report: CalibrationReport, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "usb_calibration_report.json"
    json_path.write_text(json.dumps(asdict(report), indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "usb_calibration_report.txt").write_text(report.summary_text(), encoding="utf-8")
    return json_path
