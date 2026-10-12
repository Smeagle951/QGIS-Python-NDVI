"""FortSmart — preflight e relatorio combinado das passadas John Deere."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Iterable, TypeVar

from .gen4_pass_guidance import GEN4_GUIDANCE_DEBUG, format_gen4_guidance_report
from .gs3_pass_guidance import format_gs3_guidance_report, guidance_failure_message
from .jd_guidance_plan import (
    GEN4,
    GS3,
    JohnDeereGuidanceExportPlan,
    format_preflight_report,
    normalized_uuid,
)


REPORT_JSON = "john_deere_guidance_report.json"
REPORT_TEXT = "JOHN_DEERE_GUIDANCE_REPORT.txt"
GS3_DEBUG = "fortsmart_gs3_export_debug.json"

T = TypeVar("T")


def _fresh_section(path: Path, started: float, key: str | None = None) -> dict | None:
    try:
        if path.stat().st_mtime < started:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get(key) if key else data


def _both(row: dict, key: str) -> int:
    return sum(int((row.get(category) or {}).get(key, 0) or 0) for category in ("planting", "border"))


def format_guidance_summary(report: dict) -> list[str]:
    """Resumo por talhao: coleta canonica -> plano -> pacote de cada plataforma."""
    gs3_rows = {row["field_erid"]: row for row in (report.get("gs3") or {}).get("fields", [])}
    gen4_rows = {row["field_erid"]: row for row in (report.get("gen4") or {}).get("fields", [])}
    lines: list[str] = []
    for field in report.get("preflight", {}).get("fields", []):
        planting, border = field.get("planting", {}), field.get("border", {})
        lines.extend(
            [
                "FIELD",
                field.get("name", "-"),
                "",
                "CANONICAL",
                f"WORK_PATH = {planting.get('detected', 0)}",
                f"FIELD_HEADLAND_PATH = {border.get('detected', 0)}",
                "",
                "EXPORT PLAN",
                f"PLANTING_GUIDANCE = {planting.get('eligible', 0)}",
                f"BORDER_GUIDANCE = {border.get('eligible', 0)}",
                "",
            ]
        )
        gs3 = gs3_rows.get(field["field_erid"])
        if gs3 is not None:
            lines.extend(
                [
                    "GS3",
                    f"requested = {_both(gs3, 'requested')}",
                    f"serialized = {_both(gs3, 'serialized')}",
                    f"ABCurve files = {_both(gs3, 'abcurve_files')}",
                    f"CurveTrack files = {_both(gs3, 'curve_track_files')}",
                    f"catalog refs = {_both(gs3, 'catalog_objects')}",
                    f"coordinates = {gs3.get('coordinates', '-')}",
                    "",
                ]
            )
        gen4 = gen4_rows.get(field["field_erid"])
        if gen4 is not None:
            lines.extend(
                [
                    "GEN4",
                    f"requested = {_both(gen4, 'requested')}",
                    f"serialized = {_both(gen4, 'serialized')}",
                    f"AdaptiveCurve files = {_both(gen4, 'adaptive_curve_files')}",
                    f"MasterData refs = {_both(gen4, 'masterdata_refs')}",
                    f"coordinates = {gen4.get('coordinates', '-')}",
                    "",
                ]
            )
    lines.extend(["PREFLIGHT", report.get("preflight_status", "-"), "", "STATUS", report.get("status", "-"), ""])
    return lines


def write_guidance_report(
    output_root: Path,
    plan: JohnDeereGuidanceExportPlan,
    platforms: Iterable[str],
    *,
    started: float,
    errors: list[str],
    preflight_ok: bool = True,
) -> dict:
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    platforms = list(platforms)
    report: dict = {
        "preflight": plan.preflight_report(),
        "preflight_status": "READY_TO_PACKAGE" if preflight_ok else "BLOCKED",
        "platforms": platforms,
    }
    if GS3 in platforms:
        report["gs3"] = _fresh_section(output_root / GS3_DEBUG, started, "guidance_passes")
    if GEN4 in platforms:
        report["gen4"] = _fresh_section(output_root / GEN4_GUIDANCE_DEBUG, started)
    report["errors"] = list(errors)
    ok = not errors and all(
        report.get(platform) and not report[platform].get("errors") for platform in platforms
    )
    report["status"] = "READY_FOR_DEVICE_TEST" if ok else "ABORTED"

    lines = ["JOHN DEERE GUIDANCE REPORT", ""]
    lines.extend(format_guidance_summary(report))
    lines.extend(["DETALHES", ""])
    lines.extend(format_preflight_report(report["preflight"]))
    if report.get("gs3"):
        lines.extend(format_gs3_guidance_report(report["gs3"]))
    if report.get("gen4"):
        lines.extend(format_gen4_guidance_report(report["gen4"]))
    if errors:
        lines.append("")
        lines.extend(f"Erro: {error}" for error in errors)
    lines.extend(["", f"Status: {report['status']}"])
    (output_root / REPORT_JSON).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_root / REPORT_TEXT).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def _passes_line(label: str, counts: dict) -> str:
    if not counts.get("requested"):
        return f"{label}: nao solicitadas"
    return f"{label}: {counts.get('serialized', 0)}/{counts.get('requested', 0)}"


def success_summary(report: dict) -> list[str]:
    """Mensagem final por talhao e plataforma (somente apos validacao)."""
    if report.get("status") != "READY_FOR_DEVICE_TEST":
        return ["Pacote incompleto: verifique JOHN_DEERE_GUIDANCE_REPORT.txt."]
    lines = ["Exportação John Deere concluída."]
    gs3_rows = {row["field_erid"]: row for row in (report.get("gs3") or {}).get("fields", [])}
    gen4_rows = {row["field_erid"]: row for row in (report.get("gen4") or {}).get("fields", [])}
    for field in report.get("preflight", {}).get("fields", []):
        erid = field["field_erid"]
        lines.extend(["", field["name"]])
        gs3 = gs3_rows.get(erid)
        if gs3 is not None:
            lines.extend(
                [
                    "",
                    "GS3",
                    f"Boundary: {gs3.get('boundary', 0)}",
                    f"HeadLand: {gs3.get('headland', 0)}",
                    _passes_line("Planting passes", gs3["planting"]),
                    _passes_line("Border passes", gs3["border"]),
                    "CurveTrack files: "
                    f"{gs3['planting'].get('curve_track_files', 0) + gs3['border'].get('curve_track_files', 0)}",
                    f"Status: {report['gs3'].get('status')}",
                ]
            )
        gen4 = gen4_rows.get(erid)
        if gen4 is not None:
            lines.extend(
                [
                    "",
                    "Gen4",
                    f"Boundary: {gen4.get('boundary', 0)}",
                    _passes_line("Planting passes", gen4["planting"]),
                    _passes_line("Border passes", gen4["border"]),
                    "AdaptiveCurve files: "
                    f"{gen4['planting'].get('adaptive_curve_files', 0) + gen4['border'].get('adaptive_curve_files', 0)}",
                    f"Status: {report['gen4'].get('status')}",
                ]
            )
    return lines


def read_guidance_report(output_root: Path) -> dict | None:
    try:
        return json.loads((Path(output_root) / REPORT_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_guidance_export(
    output_root: Path,
    plan: JohnDeereGuidanceExportPlan,
    exported_fields: dict[str, Iterable[str]],
    export: Callable[[], T],
) -> T:
    """Preflight antes dos writers; relatorio sempre gravado; nunca sucesso falso."""
    started = time.time() - 1.0
    platforms = [
        platform for platform in (GS3, GEN4)
        if platform in exported_fields and plan.options.any_platform_requested(platform)
    ]
    errors: list[str] = []
    for platform in platforms:
        erids = {normalized_uuid(erid) for erid in exported_fields[platform]}
        errors.extend(plan.preflight_errors(platform, erids))
    errors = list(dict.fromkeys(errors))
    if errors:
        write_guidance_report(output_root, plan, [], started=started, errors=errors, preflight_ok=False)
        raise ValueError(guidance_failure_message(errors))
    try:
        result = export()
    except Exception as exc:
        write_guidance_report(output_root, plan, platforms, started=started, errors=[str(exc)])
        raise
    report = write_guidance_report(output_root, plan, platforms, started=started, errors=[])
    if report["status"] != "READY_FOR_DEVICE_TEST":
        raise RuntimeError(
            guidance_failure_message(
                [f"{platform.upper()}: relatorio de guidance ausente ou com erro." for platform in platforms]
            )
        )
    return result
