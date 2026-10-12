"""FortSmart — validacao round-trip (origem → export → reimport)."""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .fortsmart_model import FortSmartFieldBundle, FortSmartImportResult, SpatialKind
from .monitor_registry import export_fields, read_fields


KIND_LABELS = {
    "limite": "Limites",
    "limite_interno": "Limites internos",
    "linha_ab": "Linhas AB",
    "curva_ab": "Curvas AB",
    "curva_adaptada": "Curvas adaptadas",
    "bordadura": "Bordaduras",
    "trabalho": "Trabalhos",
    "marcacao": "Marcacoes",
    "obstaculo_ponto": "Obstaculos ponto",
    "obstaculo_area": "Obstaculos area",
}

# Remapeamentos esperados entre formatos (nao falham o round-trip)
KIND_GROUPS = {
    "boundary": {"limite", "limite_interno", "obstaculo_area"},
    "ab_line": {"linha_ab"},
    # bordadura linear GS3 vira curva no ISOXML/Gen4
    "curve": {"curva_ab", "curva_adaptada", "trabalho", "bordadura"},
    "point": {"marcacao", "obstaculo_ponto"},
}


def _group_counts(counts: dict[str, int]) -> dict[str, int]:
    grouped = {name: 0 for name in KIND_GROUPS}
    for kind, n in counts.items():
        placed = False
        for gname, members in KIND_GROUPS.items():
            if kind in members:
                grouped[gname] += n
                placed = True
                break
        if not placed:
            grouped[kind] = grouped.get(kind, 0) + n
    return grouped


@dataclass
class KindCounts:
    counts: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_bundles(cls, bundles: list[FortSmartFieldBundle]) -> "KindCounts":
        counts: dict[str, int] = {}
        for bundle in bundles:
            for feat in bundle.features:
                key = feat.kind.value
                counts[key] = counts.get(key, 0) + 1
        return cls(counts=counts)

    def total(self) -> int:
        return sum(self.counts.values())


@dataclass
class RoundTripReport:
    profile_id: str
    source_path: str
    export_path: str
    source_fields: int
    reimport_fields: int
    source_counts: dict[str, int]
    reimport_counts: dict[str, int]
    missing_fields: list[str] = field(default_factory=list)
    extra_fields: list[str] = field(default_factory=list)
    kind_deltas: dict[str, dict[str, int]] = field(default_factory=dict)
    ok: bool = False
    notes: list[str] = field(default_factory=list)
    created_at: str = ""

    def summary_text(self) -> str:
        lines = [
            f"Round-trip FortSmart — {self.profile_id}",
            f"Origem: {self.source_path}",
            f"Export: {self.export_path}",
            f"Talhoes origem/reimport: {self.source_fields} / {self.reimport_fields}",
            f"Resultado: {'OK' if self.ok else 'DIVERGENCIAS'}",
            "",
            "Contagens por tipo:",
        ]
        keys = sorted(set(self.source_counts) | set(self.reimport_counts))
        for key in keys:
            src = self.source_counts.get(key, 0)
            dst = self.reimport_counts.get(key, 0)
            mark = "=" if src == dst else "!"
            lines.append(f"  {mark} {KIND_LABELS.get(key, key)}: {src} -> {dst}")
        if self.missing_fields:
            lines.append("")
            lines.append(f"Talhoes ausentes no reimport ({len(self.missing_fields)}):")
            lines.append("  " + ", ".join(self.missing_fields[:12]) + ("..." if len(self.missing_fields) > 12 else ""))
        if self.extra_fields:
            lines.append("")
            lines.append(f"Talhoes extras no reimport ({len(self.extra_fields)}):")
            lines.append("  " + ", ".join(self.extra_fields[:12]))
        for note in self.notes:
            lines.append(f"- {note}")
        return "\n".join(lines)


def _field_keys(bundles: list[FortSmartFieldBundle]) -> set[str]:
    return {(b.fazenda or "").casefold().strip() + "::" + (b.talhao or "").casefold().strip() for b in bundles}


def _field_labels(bundles: list[FortSmartFieldBundle]) -> dict[str, str]:
    out: dict[str, str] = {}
    for b in bundles:
        key = (b.fazenda or "").casefold().strip() + "::" + (b.talhao or "").casefold().strip()
        out[key] = f"{b.fazenda} / {b.talhao}"
    return out


def compare_results(
    source: FortSmartImportResult,
    reimport: FortSmartImportResult,
    *,
    profile_id: str,
    source_path: Path,
    export_path: Path,
) -> RoundTripReport:
    src_counts = KindCounts.from_bundles(source.bundles)
    dst_counts = KindCounts.from_bundles(reimport.bundles)
    src_keys = _field_keys(source.bundles)
    dst_keys = _field_keys(reimport.bundles)
    labels = _field_labels(source.bundles)
    labels.update(_field_labels(reimport.bundles))

    missing = sorted(labels[k] for k in (src_keys - dst_keys) if k in labels)
    extra = sorted(labels[k] for k in (dst_keys - src_keys) if k in labels)

    deltas: dict[str, dict[str, int]] = {}
    for key in set(src_counts.counts) | set(dst_counts.counts):
        s = src_counts.counts.get(key, 0)
        d = dst_counts.counts.get(key, 0)
        if s != d:
            deltas[key] = {"source": s, "reimport": d, "delta": d - s}

    notes: list[str] = []
    # GS3: marcacoes nao vao no USB — nao falhar por isso
    if profile_id == "john_deere_gs3":
        for kind in ("marcacao", "obstaculo_ponto", "obstaculo_area"):
            if kind in deltas:
                notes.append(f"Esperado no GS3: '{kind}' nao sobrevive no pendrive USB.")
                deltas.pop(kind, None)

    # ISOXML/Gen4: tipos podem ser remapeados (bordadura->limite, adaptada->curva_ab)
    group_ok = True
    if profile_id in ("isoxml_v4", "isoxml_v3", "john_deere_gen4", "cnh", "fendt"):
        src_g = _group_counts(src_counts.counts)
        dst_g = _group_counts(dst_counts.counts)
        for gname in set(src_g) | set(dst_g):
            if src_g.get(gname, 0) != dst_g.get(gname, 0):
                group_ok = False
                notes.append(
                    f"Grupo {gname}: {src_g.get(gname, 0)} -> {dst_g.get(gname, 0)}"
                )
        if group_ok:
            notes.append("Tipos individuais remapeados, mas grupos (limite/AB/curva) conferem.")
            deltas = {}

    # Tolerancia final
    if profile_id == "john_deere_gs3":
        ok = not missing and not deltas
        if missing:
            notes.append("Verifique talhoes sem Boundary no pendrive original.")
    elif profile_id in ("isoxml_v4", "isoxml_v3", "john_deere_gen4", "cnh", "fendt"):
        ok = not missing and group_ok
    else:
        ok = not missing and not deltas

    return RoundTripReport(
        profile_id=profile_id,
        source_path=str(source_path),
        export_path=str(export_path),
        source_fields=len(source.bundles),
        reimport_fields=len(reimport.bundles),
        source_counts=src_counts.counts,
        reimport_counts=dst_counts.counts,
        missing_fields=missing,
        extra_fields=extra,
        kind_deltas=deltas,
        ok=ok,
        notes=notes,
        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def run_roundtrip(
    source_path: Path,
    *,
    profile_id: str,
    output_root: Path | None = None,
    keep_export: bool = True,
    template_gs3: Path | None = None,
) -> RoundTripReport:
    source_path = Path(source_path)
    # Sempre detectar origem automaticamente; o profile_id e o formato de EXPORT/reimport
    source = read_fields(source_path, profile_id="auto", unify_farms=False)
    if not source.bundles:
        raise RuntimeError(f"Nenhum dado lido em {source_path}")

    if output_root is None:
        output_root = Path(tempfile.mkdtemp(prefix="fortsmart_rt_"))
    else:
        output_root = Path(output_root)
        if output_root.exists():
            shutil.rmtree(output_root)
        output_root.mkdir(parents=True, exist_ok=True)

    options: dict = {}
    if profile_id == "john_deere_gs3":
        options["template_gs3"] = template_gs3 or source_path
        options["merge_setup"] = True

    export_path = export_fields(
        source.bundles,
        output_root,
        profile_id=profile_id,
        **options,
    )

    # ponto de reimport: raiz do export ou pasta especifica
    reimport_path = Path(export_path)
    if profile_id == "john_deere_gs3":
        # export retorna pasta GS3_2630 ou EIC
        candidates = [
            reimport_path,
            output_root / "GS3_2630",
            output_root,
        ]
        for c in candidates:
            if c.exists() and (list(c.rglob("setup.fds")) or c.name.upper() == "GS3_2630"):
                reimport_path = c
                break
    elif profile_id.startswith("isoxml") or profile_id in ("cnh", "fendt", "precision_planting"):
        if (reimport_path / "TASKDATA.XML").exists():
            pass
        elif (output_root / "TASKDATA" / "TASKDATA.XML").exists():
            reimport_path = output_root / "TASKDATA"
    elif profile_id == "jacto":
        reimport_path = output_root / "OMNI" if (output_root / "OMNI").exists() else reimport_path
    elif profile_id == "stara":
        reimport_path = output_root / "DADOS" if (output_root / "DADOS").exists() else reimport_path
    elif profile_id == "john_deere_gen4":
        reimport_path = output_root / "Gen4" if (output_root / "Gen4").exists() else reimport_path

    reimport = read_fields(reimport_path, profile_id=profile_id, unify_farms=False)
    report = compare_results(
        source,
        reimport,
        profile_id=profile_id,
        source_path=source_path,
        export_path=export_path,
    )

    report_path = output_root / "roundtrip_report.json"
    report_path.write_text(json.dumps(asdict(report), indent=2, ensure_ascii=False), encoding="utf-8")
    (output_root / "roundtrip_report.txt").write_text(report.summary_text(), encoding="utf-8")

    if not keep_export and output_root.exists() and str(output_root).startswith(tempfile.gettempdir()):
        shutil.rmtree(output_root, ignore_errors=True)

    return report


def checklist_gs3() -> str:
    return """Checklist round-trip GS3 2630
1. Importar FortSmart → E:\\GS3_2630 (organizar por talhao)
2. Editar 1 talhao de teste (limite ou linha AB)
3. Exportar FortSmart → John Deere GS3 (template E:\\GS3_2630)
4. Usar wizard USB → copiar GS3_2630 para raiz FAT32
5. Monitor → Data Transfer → Import Data
6. Reimportar o USB no QGIS
7. Rodar Validar Round-trip (origem vs pasta exportada)
"""


def checklist_isoxml() -> str:
    return """Checklist round-trip ISOXML v4
1. Importar dados (GS3 ou ISOXML) no QGIS
2. Exportar FortSmart → ISOXML v4.3
3. Confirmar TASKDATA.XML + LINKLIST.XML + AFE
4. Copiar pasta TASKDATA para USB / software ISO 11783-10
5. Reimportar TASKDATA no QGIS
6. Rodar Validar Round-trip (perfil isoxml_v4)
"""
