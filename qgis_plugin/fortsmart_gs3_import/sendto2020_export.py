"""FortSmart export for Precision Planting 20|20 SendTo2020."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from . import precision2020_boundary as safe_boundary
from .gs3_spatial_types import Gs3FieldBundle, Gs3SpatialFeature, SpatialKind
from .sendto2020_geometry import SendTo2020Config, prepare_sendto2020_boundary
from .sendto2020_names import load_monitor_hierarchy, resolve_monitor_names
from .sendto2020_preflight import DBF_FIELDS, SendTo2020PreflightValidator


MCD_CONTENT = """<Columns>
<Column  Key='Business' Name='Business' DataType='8' Width='32' DataUse='8' DataScope='1' Selected='False' />
<Column  Key='Farm' Name='Farm' DataType='8' Width='32' DataUse='8' DataScope='1' Selected='False' />
<Column  Key='Field' Name='Field' DataType='8' Width='32' DataUse='8' DataScope='1' Selected='False' />
</Columns>"""

WGS84_PRJ = (
    'GEOGCS["WGS 84", DATUM["World Geodetic System 1984", '
    'SPHEROID["WGS 84", 6378137, 298.257223563, AUTHORITY["EPSG", "7030"]], '
    'AUTHORITY["EPSG", "6326"]], PRIMEM["Greenwich", 0, '
    'AUTHORITY["EPSG", "8901"]], UNIT["degree", 0.017453292519943295, '
    'AUTHORITY["EPSG", "9102"]], AUTHORITY["EPSG", "4326"]]'
)

SKIPPED_ROLES = {
    "INTERNAL_BOUNDARY",
    "HEADLAND",
    "BORDER_PASS",
    "FIELD_HEADLAND_PATH",
    "WORK_PATH",
    "FINAL_WORK_PATH",
    "WORK_OFF",
    "WORK_ON",
    "SHUTOFF",
    "HEADLAND_TURN",
    "SIMULATION",
}


def _field_code(name: str, index: int) -> str:
    text = re.sub(r"\s*-\s*", "-", (name or "").strip())
    text = re.sub(r"[^0-9A-Za-z._-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._-")
    return (text or f"PV-{index:02d}")[:40]


def _role(feature: Gs3SpatialFeature) -> str:
    role = str(
        feature.extra.get("fortsmart_role")
        or feature.extra.get("canonical_role")
        or ""
    ).strip().upper()
    if role:
        return role
    fallback = {
        SpatialKind.LIMITE: "EXTERNAL_BOUNDARY",
        SpatialKind.LIMITE_INTERNO: "INTERNAL_BOUNDARY",
        SpatialKind.BORDADURA: "HEADLAND",
        SpatialKind.TRABALHO: "WORK_PATH",
    }
    return fallback.get(feature.kind, feature.kind.value.upper())


def _source_bundle_map(
    bundles: list[Gs3FieldBundle],
) -> dict[tuple[str, str], Gs3FieldBundle]:
    return {(bundle.field_erid, bundle.talhao): bundle for bundle in bundles}


def build_sendto2020_export_plan(
    bundles: list[Gs3FieldBundle],
    *,
    source_bundles: list[Gs3FieldBundle] | None = None,
) -> dict[str, Any]:
    sources = _source_bundle_map(source_bundles or bundles)
    fields: list[dict[str, Any]] = []
    total_skipped: Counter[str] = Counter()
    total_external = 0

    for bundle in bundles:
        source = sources.get((bundle.field_erid, bundle.talhao), bundle)
        detected = Counter(_role(feature) for feature in source.features)
        included = sum(1 for feature in bundle.features if _role(feature) == "EXTERNAL_BOUNDARY")
        skipped = {
            role: count
            for role, count in sorted(detected.items())
            if role != "EXTERNAL_BOUNDARY" and (role in SKIPPED_ROLES or count)
        }
        total_skipped.update(skipped)
        total_external += included
        fields.append(
            {
                "field_erid": bundle.field_erid,
                "business": bundle.cliente,
                "farm": bundle.fazenda,
                "field": bundle.talhao,
                "detected": dict(sorted(detected.items())),
                "included": {"EXTERNAL_BOUNDARY": included},
                "skipped": skipped,
            }
        )

    return {
        "format": "SendTo2020",
        "target": "Precision Planting 20|20",
        "generated_at": datetime.now().astimezone().isoformat(),
        "rule": "EXTERNAL_BOUNDARY_ONLY",
        "fields": fields,
        "totals": {
            "fields": len(fields),
            "external_boundaries_included": total_external,
            "skipped_by_role": dict(sorted(total_skipped.items())),
        },
    }


def _write_plan(root: Path, plan: dict[str, Any]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "SENDTO2020_EXPORT_PLAN.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def sendto2020_diagnostic_export(
    bundles: list[Gs3FieldBundle],
    output_root: Path,
) -> Path:
    root = output_root / "SENDTO2020_DIAGNOSTIC_EXPORT"
    _write_plan(root, build_sendto2020_export_plan(bundles))
    return root


def _write_boundary_shapefile(
    base_path: Path,
    rings: list[list[tuple[float, float]]],
    attrs: dict[str, str],
) -> None:
    _write_boundary_geometry(base_path, {"type": "Polygon", "coordinates": rings}, attrs)


def _write_boundary_geometry(
    base_path: Path,
    geometry: dict[str, Any],
    attrs: dict[str, str],
) -> None:
    try:
        import fiona
    except ImportError as exc:
        raise RuntimeError(
            "SENDTO2020_DEPENDENCY_MISSING: Fiona e obrigatorio para exportar."
        ) from exc

    base_path.parent.mkdir(parents=True, exist_ok=True)
    # Shapefile type 5 holds Polygon and MultiPolygon records alike.
    schema = {
        "geometry": "Polygon",
        "properties": {field: "str:254" for field in DBF_FIELDS},
    }
    with fiona.open(
        base_path.with_suffix(".shp"),
        "w",
        driver="ESRI Shapefile",
        schema=schema,
        crs=safe_boundary.PRECISION2020_EXPORT_CRS,
        encoding="UTF-8",
    ) as sink:
        sink.write(
            {
                "geometry": geometry,
                "properties": attrs,
            }
        )

    base_path.with_suffix(".prj").write_text(WGS84_PRJ, encoding="ascii")
    base_path.with_suffix(".cpg").write_text("65001", encoding="utf-8-sig")
    base_path.with_suffix(".mcd").write_text(
        MCD_CONTENT,
        encoding="utf-8-sig",
        newline="\r\n",
    )


def _write_report(root: Path, report: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SENDTO2020_EXPORT_REPORT.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "FortSmart SendTo2020 Export Report",
        f"Status: {report.get('status', 'UNKNOWN')}",
        f"Verification: {report.get('verification_state', 'NOT_VERIFIED')}",
        f"Fields: {len(report.get('fields', []))}",
    ]
    for field in report.get("fields", []):
        lines.append(
            f"- {field['business']} / {field['farm']} / {field['field']}: "
            f"{field['export_vertices']} vertices, "
            f"{field['export_area_ha']:.2f} ha, "
            f"area diff {field['area_difference_percent']:.6f}%"
        )
    for warning in report.get("warnings", []):
        lines.append(f"Aviso: {warning}")
    if report.get("error"):
        lines.append(f"Error: {report['error']}")
    (root / "SENDTO2020_EXPORT_REPORT.txt").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _safe_config(config: SendTo2020Config | None) -> safe_boundary.Precision2020ExportConfig:
    cfg = config or SendTo2020Config()
    return safe_boundary.Precision2020ExportConfig(
        vertex_warning=cfg.vertex_warning,
        vertex_target=cfg.vertex_target,
        vertex_max=cfg.vertex_max,
        max_area_difference_percent=cfg.max_area_difference_percent,
    )


def export_sendto2020(
    bundles: list[Gs3FieldBundle],
    output_root: Path,
    *,
    source_bundles: list[Gs3FieldBundle] | None = None,
    config: SendTo2020Config | None = None,
    reference_roots: list[Path] | None = None,
    traffic_exclusions=None,
) -> Path:
    """Export one validated WGS84 boundary shapefile per canonical field.

    ``reference_roots`` point to folders holding the monitor's existing
    SendTo2020 files; Business/Farm are snapped to those names so the new
    boundary joins the fields already on the 20|20.

    ``traffic_exclusions`` (SendTo2020TrafficExclusionMode) is opt-in: when
    None the export is the BOUNDARY_ONLY path, byte for byte.
    """
    if not bundles:
        raise ValueError("SENDTO2020_NO_FIELDS: nenhum talhao para exportar.")
    if traffic_exclusions is not None and not safe_boundary.ENABLE_PRECISION2020_SAFE_BOUNDARY:
        raise ValueError(
            "SENDTO2020_TRAFFIC_SAFE_PIPELINE_REQUIRED: o boundary com exclusoes exige o "
            "pipeline Precision2020BoundaryExporter."
        )

    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    plan = build_sendto2020_export_plan(bundles, source_bundles=source_bundles)
    diagnostic_root = output_root / "SENDTO2020_DIAGNOSTIC_EXPORT"
    _write_plan(diagnostic_root, plan)

    stage_root = Path(tempfile.mkdtemp(prefix="fs2020_"))
    stage_send = stage_root / "SendTo2020"
    stage_send.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "format": "SendTo2020",
        "status": "BUILDING",
        "verification_state": "NOT_VERIFIED",
        "generated_at": datetime.now().astimezone().isoformat(),
        "fields": [],
        "warnings": [],
        "monitor_action_required": [
            "Importar o arquivo em Setup > Settings > Import Data > Field Boundaries.",
            "Criar ou selecionar Client, Farm e Field em Setup > Fields.",
            "Abrir Field Setup > Field Boundary e associar o boundary importado ao talhao.",
            "Ativar o talhao para exibir o limite e habilitar recursos de headland.",
        ],
    }

    existing = load_monitor_hierarchy(list(reference_roots or []))
    report["monitor_reference"] = {
        "roots": [str(path) for path in reference_roots or []],
        "entries": [
            {"business": e.business, "farm": e.farm, "field": e.field, "source": e.source}
            for e in existing
        ],
    }
    if not existing:
        report["warnings"].append(
            "Nenhum SendTo2020 existente do monitor foi informado; Business/Farm "
            "precisam ser identicos aos talhoes ja cadastrados no 20|20."
        )

    safe = safe_boundary.ENABLE_PRECISION2020_SAFE_BOUNDARY
    safe_exporter = (
        safe_boundary.Precision2020BoundaryExporter(_safe_config(config)) if safe else None
    )
    try:
        used_names: set[str] = set()
        validator = SendTo2020PreflightValidator()
        for index, bundle in enumerate(bundles, start=1):
            if (
                not bundle.field_erid.strip()
                or not bundle.talhao.strip()
                or not bundle.fazenda.strip()
                or not bundle.cliente.strip()
            ):
                raise ValueError(
                    "SENDTO2020_HIERARCHY_INVALID: field_id, Business, Farm e Field sao obrigatorios."
                )

            if safe_exporter is not None:
                field_plan = safe_exporter.prepare_field(
                    bundle, lambda item: _role(item) == "EXTERNAL_BOUNDARY"
                )
                if traffic_exclusions is not None:
                    field_plan = traffic_exclusions.apply(
                        field_plan, vertex_max=safe_exporter.config.vertex_max
                    )
                prepared = field_plan.prepared
            else:
                external = [
                    feature
                    for feature in bundle.features
                    if _role(feature) == "EXTERNAL_BOUNDARY"
                ]
                if len(external) != 1:
                    raise ValueError(
                        "SENDTO2020_BOUNDARY_MULTIPLE_FEATURES: O boundary destinado ao "
                        "20|20 deve conter somente o EXTERNAL_BOUNDARY do talhao. "
                        f"Foram encontrados {len(external)} poligonos."
                    )
                feature = external[0]
                if feature.geometry.lower() != "polygon":
                    raise ValueError(
                        "SENDTO2020_GEOMETRY_TYPE_INVALID: EXTERNAL_BOUNDARY deve ser Polygon."
                    )
                rings = feature.rings or ([feature.points] if feature.points else [])
                prepared = prepare_sendto2020_boundary(rings, config=config)

            filename = _field_code(bundle.talhao, index)
            candidate = filename
            suffix = 2
            while candidate.lower() in used_names:
                candidate = f"{filename[:35]}_{suffix}"
                suffix += 1
            used_names.add(candidate.lower())

            names = resolve_monitor_names(bundle.cliente, bundle.fazenda, bundle.talhao, existing)
            if not names.business or not names.farm or not names.field:
                raise ValueError(
                    "SENDTO2020_HIERARCHY_INVALID: Business, Farm e Field ficaram vazios "
                    "apos normalizar para o 20|20."
                )
            report["warnings"].extend(f"{bundle.talhao}: {note}" for note in names.notes)
            attrs = {
                "Business": names.business,
                "Farm": names.farm,
                "Field": names.field,
                "CODE": names.field,
            }
            base_path = stage_send / candidate
            if safe_exporter is not None:
                safe_exporter.write_field(
                    field_plan,
                    base_path,
                    attrs,
                    _write_boundary_geometry,
                    debug_root=diagnostic_root / "precision2020_debug",
                )
                roundtrip = validator.validate(
                    base_path, attrs, geometry_types=(prepared.geometry_type,)
                )
                if traffic_exclusions is not None:
                    traffic_exclusions.validate_written(field_plan, base_path)
                report["warnings"].extend(field_plan.result.warnings)
                source_crs = field_plan.result.crs.get("source", "")
                source_components = prepared.part_count
            else:
                _write_boundary_shapefile(base_path, prepared.rings, attrs)
                roundtrip = validator.validate(base_path, attrs)
                report["warnings"].extend(prepared.warnings)
                source_crs = "EPSG:4326"
                source_components = prepared.source_components
            report["fields"].append(
                {
                    "field_erid": bundle.field_erid,
                    "business": names.business,
                    "farm": names.farm,
                    "field": names.field,
                    "source_names": {
                        "business": bundle.cliente,
                        "farm": bundle.fazenda,
                        "field": bundle.talhao,
                    },
                    "boundary_source": "EXTERNAL_BOUNDARY",
                    "filename": candidate,
                    "source_crs": source_crs,
                    "output_crs": safe_boundary.PRECISION2020_EXPORT_CRS,
                    "original_vertices": prepared.original_vertices,
                    "export_vertices": prepared.export_vertices,
                    "original_area_ha": prepared.original_area_ha,
                    "export_area_ha": prepared.export_area_ha,
                    "area_difference_percent": prepared.area_difference_percent,
                    "simplification_tolerance_m": prepared.tolerance_m,
                    "source_components": source_components,
                    "output_feature_count": 1,
                    "roundtrip": roundtrip,
                }
            )
            if traffic_exclusions is not None:
                report["fields"][-1].update(traffic_exclusions.report_entry(field_plan))

        if not report["fields"]:
            raise ValueError("SENDTO2020_NO_FIELDS: nenhum boundary foi serializado.")

        if safe_exporter is not None:
            safe_exporter.check_field_spread()
            report["warnings"].extend(
                warning
                for result in safe_exporter.results
                for warning in result.warnings
                if safe_boundary.REGION_MISMATCH in warning
            )
            report["precision2020_boundary"] = safe_exporter.summary()
            safe_boundary.write_precision2020_report(diagnostic_root, report["precision2020_boundary"])
        report["status"] = "READY_FOR_DEVICE_TEST"
        report["verification_state"] = "ROUNDTRIP_TESTED"
        if traffic_exclusions is not None:
            report["boundary_mode"] = traffic_exclusions.mode
            report["traffic_exclusions"] = traffic_exclusions.summary()
            traffic_exclusions.write_report(diagnostic_root)
        _write_report(diagnostic_root, report)

        final_root = output_root / "SendTo2020"
        backup = None
        if final_root.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = output_root / f"SendTo2020_backup_{stamp}"
            final_root.replace(backup)
        try:
            shutil.move(str(stage_send), str(final_root))
        except Exception:
            if backup is not None and backup.exists() and not final_root.exists():
                backup.replace(final_root)
            raise
        return final_root
    except Exception as exc:
        report["status"] = "SENDTO2020_PACKAGE_NOT_READY"
        report["verification_state"] = "FAILED"
        report["error"] = str(exc)
        if safe_exporter is not None:
            report["precision2020_boundary"] = safe_exporter.summary()
            safe_boundary.write_precision2020_report(diagnostic_root, report["precision2020_boundary"])
        if traffic_exclusions is not None:
            report["boundary_mode"] = traffic_exclusions.mode
            report["traffic_exclusions"] = traffic_exclusions.summary()
            traffic_exclusions.write_report(diagnostic_root)
        _write_report(diagnostic_root, report)
        raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)
