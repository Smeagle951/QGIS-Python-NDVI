"""FortSmart — coletor de camadas QGIS para exportacao a monitores."""

from __future__ import annotations

import uuid
from dataclasses import replace

from .canonical_project import (
    ROLE_KIND_MAP,
    CanonicalProjectBuilder,
    canonical_feature_role,
    role_from_legacy_name,
    role_to_spatial_kind,
)
from .field_group_export import _field_node
from .gs3_spatial_types import Gs3FieldBundle, Gs3SpatialFeature, SpatialKind

# Grupos raiz criados pelo FortSmart (import) + genericos
FORTSMART_ROOT_GROUPS = (
    "FortSmart GS3",
    "FortSmart ISOXML",
    "FortSmart Gen4",
    "FortSmart Jacto",
    "FortSmart Stara",
    "FortSmart",
)


FORTSMART_EXPORT_NAMESPACE = uuid.UUID("c775cc5b-781f-46b0-bbb8-143a5522bf74")

# Passadas planejadas John Deere: a role da camada vale quando a feicao traz
# um valor que nao e role canonica (ex.: atributo "role" = "traffic").
PASS_LAYER_ROLES = ("WORK_PATH", "FIELD_HEADLAND_PATH", "FINAL_WORK_PATH")
AREA_KINDS = (SpatialKind.LIMITE, SpatialKind.LIMITE_INTERNO, SpatialKind.OBSTACULO_AREA)


def _valid_or_stable_uuid(value: str, *identity: object) -> str:
    """Return a bare UUID accepted as a GS3 ERID.

    Imported Deere UUIDs are preserved. Legacy textual IDs such as
    ``qgis-field:bordadura`` and ``itm-<layer>-<fid>`` are replaced by a
    deterministic UUID so repeated exports of the same QGIS object keep the
    same identity without creating invalid Windows directory names.
    """
    clean = str(value or "").strip().strip("{}")
    try:
        return str(uuid.UUID(clean))
    except (ValueError, AttributeError, TypeError):
        seed = "|".join(str(part or "").strip().casefold() for part in identity)
        return str(uuid.uuid5(FORTSMART_EXPORT_NAMESPACE, seed or clean.casefold()))


def _kind_from_attr(tipo: str, layer_name: str) -> SpatialKind:
    """Legacy fallback used only when canonical metadata is unavailable."""
    text = (tipo or layer_name or "").casefold()
    mapping = {
        "limite_interno": SpatialKind.LIMITE_INTERNO,
        "limite": SpatialKind.LIMITE,
        "linha_ab": SpatialKind.LINHA_AB,
        "linha ab": SpatialKind.LINHA_AB,
        "curva_ab": SpatialKind.CURVA_AB,
        "curva ab": SpatialKind.CURVA_AB,
        "curva_adaptada": SpatialKind.CURVA_ADAPTADA,
        "curva adaptada": SpatialKind.CURVA_ADAPTADA,
        "bordadura": SpatialKind.BORDADURA,
        "trabalho": SpatialKind.TRABALHO,
        "marcacao": SpatialKind.MARCAÇAO,
        "marca": SpatialKind.MARCAÇAO,
        "obstaculo_ponto": SpatialKind.OBSTACULO_PONTO,
        "obstaculo ponto": SpatialKind.OBSTACULO_PONTO,
        "obstaculo_area": SpatialKind.OBSTACULO_AREA,
        "obstaculo area": SpatialKind.OBSTACULO_AREA,
        "flag": SpatialKind.OBSTACULO_PONTO,
        "boundary": SpatialKind.LIMITE,
        "abline": SpatialKind.LINHA_AB,
        "abcurve": SpatialKind.CURVA_AB,
        "adaptivecurve": SpatialKind.CURVA_ADAPTADA,
        "linhas": SpatialKind.TRABALHO,
        " reta": SpatialKind.LINHA_AB,
        "rete": SpatialKind.LINHA_AB,
        "pulv": SpatialKind.TRABALHO,
        "pivo": SpatialKind.LINHA_AB,
    }
    for key, kind in mapping.items():
        if key in text:
            return kind
    return SpatialKind.OUTRO


def _attr(feat, fields, names: list[str], default: str = "") -> str:
    for name in names:
        idx = fields.indexOf(name)
        if idx >= 0:
            val = feat.attribute(idx)
            if val not in (None, ""):
                return str(val)
    return default


def _geom_to_parts(geom) -> tuple[str, list[tuple[float, float]], list[list[tuple[float, float]]]]:
    from qgis.core import QgsWkbTypes

    geometry_type = QgsWkbTypes.geometryType(geom.wkbType())
    if geometry_type == QgsWkbTypes.PolygonGeometry:
        if geom.isMultipart():
            mpoly = geom.asMultiPolygon()
            rings = [[(p.x(), p.y()) for p in part[0]] for part in mpoly if part and part[0]]
            return "polygon", [], rings
        poly = geom.asPolygon()
        return "polygon", [], [[(p.x(), p.y()) for p in ring] for ring in poly if ring]
    if geometry_type == QgsWkbTypes.LineGeometry:
        if geom.isMultipart():
            mline = geom.asMultiPolyline()
            points = [(p.x(), p.y()) for part in mline for p in part]
            return "line", points, []
        line = geom.asPolyline()
        return "line", [(p.x(), p.y()) for p in line], []
    if geometry_type == QgsWkbTypes.PointGeometry:
        if geom.isMultipart():
            points = geom.asMultiPoint()
            return "point", [(p.x(), p.y()) for p in points], []
        pt = geom.asPoint()
        if not pt.isEmpty():
            return "point", [(pt.x(), pt.y())], []
    return "point", [], []


def _line_part_breaks(geom) -> str:
    """Indices where each part after the first starts in the flattened points."""
    if not geom.isMultipart():
        return ""
    try:
        parts = geom.asMultiPolyline()
    except Exception:
        return ""
    breaks: list[str] = []
    cursor = 0
    for part in parts[:-1]:
        cursor += len(part)
        breaks.append(str(cursor))
    return ",".join(breaks)


def _to_wgs84(geom, layer):
    return _to_wgs84_with_status(geom, layer)[0]


def _operation_succeeded(result) -> bool:
    value = getattr(result, "value", result)
    try:
        return int(value) == 0
    except (TypeError, ValueError):
        return str(result).endswith("Success")


def _to_wgs84_with_status(geom, layer):
    """Clone em EPSG:4326 e o resultado: IDENTITY, OK, FAILED ou UNDEFINED (CRS invalido)."""
    from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsProject

    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    g = QgsGeometry(geom)
    if not layer.crs().isValid():
        return g, "UNDEFINED"
    if layer.crs() == wgs84:
        return g, "IDENTITY"
    result = g.transform(QgsCoordinateTransform(layer.crs(), wgs84, QgsProject.instance()))
    return g, "OK" if _operation_succeeded(result) else "FAILED"


def _polygon_parts(geom) -> list[list[list[tuple[float, float]]]]:
    polygons = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
    return [
        [[(p.x(), p.y()) for p in ring] for ring in polygon if ring]
        for polygon in polygons
        if polygon
    ]


def _source_geometry_metadata(source_geom, layer, export_geom, transform_status: str) -> dict:
    """CRS, envelope e centro de origem para o preflight Precision 20|20."""
    import math

    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsProject,
        QgsUnitTypes,
        QgsWkbTypes,
    )

    crs = layer.crs()
    wkb = source_geom.wkbType()
    bbox = source_geom.boundingBox()
    centroid = source_geom.centroid().asPoint()
    meta = {
        "crs": crs.authid() if crs.isValid() else "",
        "crs_wkt": crs.toWkt() if crs.isValid() else "",
        "crs_valid": bool(crs.isValid()),
        "crs_geographic": bool(crs.isValid() and crs.isGeographic()),
        "transform": transform_status,
        "bbox": [bbox.xMinimum(), bbox.yMinimum(), bbox.xMaximum(), bbox.yMaximum()],
        "centroid": [centroid.x(), centroid.y()],
        "has_z": bool(QgsWkbTypes.hasZ(wkb)),
        "has_m": bool(QgsWkbTypes.hasM(wkb)),
        "wkb_type": QgsWkbTypes.displayString(wkb),
        "parts": source_geom.constGet().numGeometries() if source_geom.isMultipart() else 1,
        "valid": bool(source_geom.isGeosValid()),
    }
    if transform_status == "IDENTITY":
        meta["roundtrip_centroid_error_m"] = 0.0
    elif transform_status == "OK":
        back = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), crs, QgsProject.instance())
        point = back.transform(export_geom.centroid().asPoint())
        if crs.isGeographic():
            lon1, lat1, lon2, lat2 = map(math.radians, (centroid.x(), centroid.y(), point.x(), point.y()))
            h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
            error_m = 2 * 6_371_008.8 * math.asin(min(1.0, math.sqrt(h)))
        else:
            factor = QgsUnitTypes.fromUnitToUnitFactor(crs.mapUnits(), QgsUnitTypes.DistanceMeters)
            error_m = math.hypot(point.x() - centroid.x(), point.y() - centroid.y()) * factor
        meta["roundtrip_centroid_error_m"] = float(error_m)
    return meta


def _layer_in_fortsmart_tree(layer) -> bool:
    from qgis.core import QgsProject

    tree_layer = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
    if not tree_layer:
        return False
    node = tree_layer.parent()
    while node:
        name = (node.name() or "").strip()
        if name in FORTSMART_ROOT_GROUPS or name.startswith("FortSmart"):
            return True
        node = node.parent()
    return False


def _layer_looks_exportable(layer) -> bool:
    """Camada avulsa com atributos FortSmart / nomes tipicos."""
    if CanonicalProjectBuilder().resolve_layer(layer) is not None:
        return True
    names = {f.name().casefold() for f in layer.fields()}
    if {"talhao", "fazenda"} & names or "tipo" in names:
        return True
    lname = layer.name().casefold()
    return any(
        k in lname
        for k in (
            "limite",
            "talh",
            "linha ab",
            "linhas",
            "curva",
            "bordadura",
            "trabalho",
            "marcacao",
            "obstaculo",
            "flag",
            "boundary",
            "guidance",
            "adaptivecurve",
            "reta",
            "pulv",
            "pivo",
            " bor",
        )
    )


def _is_flag_layer(layer) -> bool:
    ref = CanonicalProjectBuilder().resolve_layer(layer)
    if ref is not None and ref.role == "OBSTACLE":
        return True
    name = (layer.name() or "").strip().casefold()
    return name.startswith(("flag", "obstacle", "obstaculo", "point", "pnt"))


def _collect_field_contexts(project, *, selected_only: bool, canonical=None) -> list[dict]:
    """Coleta poligonos de talhao para associar flags/pontos por intersecao."""
    from qgis.core import QgsVectorLayer

    contexts: list[dict] = []
    canonical = canonical or CanonicalProjectBuilder().build(project.mapLayers().values())
    boundary_ids = {
        layer.id()
        for layer in canonical.find_roles("EXTERNAL_BOUNDARY", "OPERATIONAL_BOUNDARY")
    }
    for layer in project.mapLayers().values():
        if not isinstance(layer, QgsVectorLayer):
            continue
        if selected_only and not layer.selectedFeatureCount():
            continue
        if not selected_only:
            lname = (layer.name() or "").casefold()
            if layer.id() not in boundary_ids and not any(k in lname for k in ("boundary", "limite", "talh")):
                continue

        fields = layer.fields()
        features = layer.selectedFeatures() if selected_only else layer.getFeatures()
        for feat in features:
            if feat.geometry() is None or feat.geometry().isEmpty():
                continue
            geom = _to_wgs84(feat.geometry(), layer)
            if geom.isEmpty():
                continue
            gtype, _, _ = _geom_to_parts(geom)
            if gtype != "polygon":
                continue

            cliente = _attr(
                feat,
                fields,
                ["proprietario", "proprietário", "owner", "cliente", "client", "grower", "Business"],
                "Proprietario",
            )
            fazenda = _attr(feat, fields, ["fazenda", "farm", "Farm"], "Fazenda")
            talhao = _attr(feat, fields, ["talhao", "nome", "name", "field", "Field", "CODE"], f"Talhao_{feat.id()}")
            raw_field_erid = _attr(feat, fields, ["field_erid"], "")
            field_erid = _valid_or_stable_uuid(
                raw_field_erid,
                "field",
                cliente,
                fazenda,
                talhao,
            )
            contexts.append(
                {
                    "geom": geom,
                    "cliente": cliente,
                    "fazenda": fazenda,
                    "talhao": talhao,
                    "field_erid": field_erid,
                    "source_layer": layer.name(),
                }
            )
    return contexts


def _context_for_geometry(geom, contexts: list[dict]) -> dict | None:
    for ctx in contexts:
        if ctx["geom"].intersects(geom):
            return ctx
    return None


def collect_export_bundles(
    *,
    only_selected_layers: bool = False,
    selected_layer_ids: set[str] | None = None,
    include_loose_layers: bool = True,
    field_names_by_layer_id: dict[str, str] | None = None,
    audit: list[dict] | None = None,
) -> list[Gs3FieldBundle]:
    """
    Coleta trabalhos prontos do projeto QGIS para enviar ao monitor.

    Prioridade:
    1) Camadas sob grupos FortSmart*
    2) Camadas avulsas com atributos/nome de agricultura de precisao

    ``audit`` recebe uma entrada por camada vetorial (coletada ou nao, com o motivo).
    """
    from qgis.core import QgsProject, QgsVectorLayer

    by_field: dict[str, Gs3FieldBundle] = {}
    project = QgsProject.instance()
    tree_root = project.layerTreeRoot()

    def record(layer, canonical_ref, *, selected: bool, in_scope: bool, collected: int, reason: str) -> None:
        if audit is None:
            return
        node = tree_root.findLayer(layer.id())
        try:
            features = int(layer.featureCount())
        except Exception:
            features = 0
        if field_names_by_layer_id is None:
            field_id, field_scope = "", None
        else:
            field_id = field_names_by_layer_id.get(layer.id(), "")
            if not field_id:
                # Camada fora da selecao (ex.: oculta) dentro de um talhao exportado.
                group = _field_node(layer, tree_root)
                siblings = group.findLayerIds() if group is not None else []
                field_id = next(
                    (field_names_by_layer_id[i] for i in siblings if i in field_names_by_layer_id), ""
                )
            field_scope = bool(field_id)
        audit.append(
            {
                "layer_name": layer.name(),
                "layer_id": layer.id(),
                "field_id": field_id,
                "field_scope": field_scope,
                "internal_key": canonical_ref.internal_key if canonical_ref is not None else "",
                "fortsmart_role": canonical_ref.role if canonical_ref is not None else "",
                "resolution": canonical_ref.resolution if canonical_ref is not None else "",
                "visible": bool(node.isVisible()) if node is not None else None,
                "selected": selected,
                "in_scope": in_scope,
                "features": features,
                "feature_count": features,
                "collected": collected,
                "reason": reason,
                "skip_reason": reason,
            }
        )
    vector_layers = [
        layer
        for layer in project.mapLayers().values()
        if isinstance(layer, QgsVectorLayer)
    ]
    canonical_project = CanonicalProjectBuilder().build(vector_layers)
    canonical_refs = {
        ref.layer.id(): ref
        for refs in canonical_project.layers_by_role.values()
        for ref in refs
    }
    selected_contexts = _collect_field_contexts(
        project,
        selected_only=True,
        canonical=canonical_project,
    )
    fallback_contexts = (
        []
        if selected_contexts
        else _collect_field_contexts(
            project,
            selected_only=False,
            canonical=canonical_project,
        )
    )
    field_contexts = selected_contexts or fallback_contexts

    for layer in vector_layers:
        canonical_ref = canonical_refs.get(layer.id())
        if only_selected_layers and selected_layer_ids is not None and layer.id() not in selected_layer_ids:
            node = tree_root.findLayer(layer.id())
            hidden = node is not None and not node.isVisible()
            record(
                layer, canonical_ref, selected=False, in_scope=False, collected=0,
                reason="hidden_in_layer_panel" if hidden else "not_selected_for_export",
            )
            continue
        if field_names_by_layer_id is not None and layer.id() not in field_names_by_layer_id:
            record(
                layer, canonical_ref, selected=True, in_scope=False, collected=0,
                reason="field_group_not_selected",
            )
            continue

        layer_role = canonical_ref.role if canonical_ref is not None else ""
        in_tree = _layer_in_fortsmart_tree(layer)
        is_flag_layer = _is_flag_layer(layer)
        if not canonical_ref and not in_tree and not is_flag_layer and not (include_loose_layers and _layer_looks_exportable(layer)):
            record(
                layer, canonical_ref, selected=True, in_scope=False, collected=0,
                reason="not_exportable_without_role",
            )
            continue

        collected = 0
        skipped_outside = 0
        geometry_conflicts = 0
        conflict_role = ""
        fields = layer.fields()
        for feat in layer.getFeatures():
            if feat.geometry() is None or feat.geometry().isEmpty():
                continue
            geom, transform_status = _to_wgs84_with_status(feat.geometry(), layer)
            if geom.isEmpty():
                continue
            gtype, points, rings = _geom_to_parts(geom)
            cliente = _attr(
                feat,
                fields,
                ["proprietario", "proprietário", "owner", "cliente", "client", "grower", "Business"],
                "Proprietario",
            )
            fazenda = _attr(feat, fields, ["fazenda", "farm"], "Fazenda")
            talhao = _attr(feat, fields, ["talhao", "nome", "name", "field"], f"Talhao_{feat.id()}")
            raw_field_erid = _attr(feat, fields, ["field_erid"], "")
            raw_item_erid = _attr(feat, fields, ["item_erid"], "")
            nome = _attr(feat, fields, ["nome", "name"], layer.name())
            tipo = _attr(feat, fields, ["tipo", "kind", "type"], "")
            raw_feature_role = canonical_feature_role(feat)
            feature_role = raw_feature_role or layer_role
            if layer_role in PASS_LAYER_ROLES and feature_role not in ROLE_KIND_MAP:
                feature_role = layer_role
            kind = role_to_spatial_kind(feature_role, gtype)
            resolution = canonical_ref.resolution if canonical_ref is not None else ""
            if kind == SpatialKind.OUTRO:
                role_from_type = role_from_legacy_name(tipo)
                if role_from_type:
                    feature_role = role_from_type
                    kind = role_to_spatial_kind(feature_role, gtype)
                    resolution = "legacy_attribute"
            if kind == SpatialKind.OUTRO:
                kind = _kind_from_attr(tipo, layer.name())
                resolution = resolution or "legacy_name"
            if kind == SpatialKind.OUTRO:
                # heuristica por geometria
                if gtype == "polygon":
                    kind = SpatialKind.LIMITE
                elif gtype == "line" and len(points) == 2:
                    kind = SpatialKind.LINHA_AB
                elif gtype == "line":
                    kind = SpatialKind.CURVA_AB
                elif gtype == "point":
                    kind = SpatialKind.OBSTACULO_PONTO if is_flag_layer else SpatialKind.MARCAÇAO

            if kind in AREA_KINDS and gtype != "polygon":
                # Limite em linha/ponto derruba o Field GS3/Gen4 ("sem anel externo").
                geometry_conflicts += 1
                conflict_role = feature_role or kind.value
                kind = SpatialKind.OUTRO
                feature_role = ""
                resolution = "geometry_conflict"

            if is_flag_layer and gtype == "point":
                kind = SpatialKind.OBSTACULO_PONTO
                feature_role = feature_role or "OBSTACLE"
                ctx = _context_for_geometry(geom, field_contexts)
                if ctx is None:
                    # Flags soltas, fora do talhao selecionado/limite, nao devem virar talhoes falsos.
                    skipped_outside += 1
                    continue
                cliente = ctx["cliente"]
                fazenda = ctx["fazenda"]
                talhao = ctx["talhao"]
                raw_field_erid = ctx["field_erid"]
                nome = nome if nome != layer.name() else f"OBSTACULO_{feat.id()}"

            # The field group selected in the export dialog is authoritative.
            # This prevents a missing attribute from creating Talhao_1 and
            # keeps boundaries, headlands and paths in the same field bundle.
            if field_names_by_layer_id is not None:
                talhao = field_names_by_layer_id[layer.id()]
                # One UUID per selected field name. Never use the display
                # label as a folder name/ERID (':' is illegal on Windows).
                field_erid = _valid_or_stable_uuid(
                    raw_field_erid,
                    "qgis-field",
                    talhao,
                )
            else:
                field_erid = _valid_or_stable_uuid(
                    raw_field_erid,
                    "field",
                    cliente,
                    fazenda,
                    talhao,
                )

            item_erid = _valid_or_stable_uuid(
                raw_item_erid,
                "item",
                field_erid,
                feature_role or kind.value,
                layer.id(),
                feat.id(),
            )

            bundle = by_field.get(field_erid)
            if bundle is None:
                bundle = Gs3FieldBundle(
                    cliente=cliente,
                    fazenda=fazenda,
                    talhao=talhao,
                    field_erid=field_erid,
                )
                by_field[field_erid] = bundle

            extra = {
                "canonical_role": feature_role,
                "role_resolution": resolution or "geometry_heuristic",
            }
            if canonical_ref is not None:
                extra["fortsmart_internal_key"] = canonical_ref.internal_key
                extra["fortsmart_schema_version"] = canonical_ref.schema_version
                extra["layer_role"] = layer_role
            if raw_feature_role and raw_feature_role != feature_role:
                extra["feature_role_raw"] = raw_feature_role
            if gtype == "line":
                pass_number = _attr(feat, fields, ["pass_number", "line_number", "pass_no", "line_no"], "")
                pass_label = _attr(feat, fields, ["label"], "")
                if pass_number:
                    extra["pass_number"] = pass_number
                if pass_label:
                    extra["pass_label"] = pass_label
                part_breaks = _line_part_breaks(geom)
                if part_breaks:
                    extra["part_breaks"] = part_breaks
                if feature_role in PASS_LAYER_ROLES:
                    crs = layer.crs()
                    extra["source_crs"] = {
                        "crs": crs.authid() if crs.isValid() else "",
                        "valid": bool(crs.isValid()),
                        "geographic": bool(crs.isValid() and crs.isGeographic()),
                        "transform": transform_status,
                    }
            if gtype == "polygon":
                try:
                    extra["polygon_parts"] = _polygon_parts(geom)
                    extra["source_geometry"] = _source_geometry_metadata(
                        feat.geometry(), layer, geom, transform_status
                    )
                except Exception:
                    extra.pop("polygon_parts", None)
                    extra.pop("source_geometry", None)

            bundle.features.append(
                Gs3SpatialFeature(
                    kind=kind,
                    cliente=cliente,
                    fazenda=fazenda,
                    talhao=talhao,
                    field_erid=field_erid,
                    item_erid=item_erid,
                    name=nome,
                    source_file=layer.name(),
                    catalog_file="qgis",
                    geometry=gtype,
                    points=points,
                    rings=rings,
                    format_source="qgis",
                    extra=extra,
                )
            )
            collected += 1

        if geometry_conflicts:
            reason = (
                f"geometry_conflict: {geometry_conflicts} feicao(oes) linha/ponto nao podem ser "
                f"{conflict_role} (limite exige poligono); exportadas como 'outro'"
            )
        elif collected:
            reason = ""
        elif skipped_outside:
            reason = "points_outside_field_boundary"
        else:
            reason = "no_features_with_geometry"
        record(layer, canonical_ref, selected=True, in_scope=True, collected=collected, reason=reason)

    return sorted(by_field.values(), key=lambda b: (b.fazenda.casefold(), b.talhao.casefold()))


def apply_monitor_hierarchy(
    bundles: list[Gs3FieldBundle],
    *,
    proprietario: str,
    fazenda: str,
) -> list[Gs3FieldBundle]:
    """Apply the monitor hierarchy Owner -> Farm -> Fields to every bundle."""
    owner_name = " ".join(str(proprietario or "").split())
    farm_name = " ".join(str(fazenda or "").split())
    if not owner_name:
        raise ValueError("Informe o nome do proprietario.")
    if not farm_name:
        raise ValueError("Informe o nome da fazenda.")

    normalized: list[Gs3FieldBundle] = []
    for bundle in bundles:
        features = [
            replace(feature, cliente=owner_name, fazenda=farm_name)
            for feature in bundle.features
        ]
        normalized.append(
            Gs3FieldBundle(
                cliente=owner_name,
                fazenda=farm_name,
                talhao=bundle.talhao,
                field_erid=bundle.field_erid,
                features=features,
            )
        )
    return normalized


def summarize_bundles(bundles: list[Gs3FieldBundle]) -> dict[str, int]:
    counts: dict[str, int] = {"talhoes": len(bundles)}
    pass_keys = {"WORK_PATH": "passadas_plantio", "FIELD_HEADLAND_PATH": "passadas_bordadura"}
    for bundle in bundles:
        for feat in bundle.features:
            key = pass_keys.get(str(feat.extra.get("canonical_role", "") or "").upper(), feat.kind.value)
            counts[key] = counts.get(key, 0) + 1
    counts["total_itens"] = sum(v for k, v in counts.items() if k not in ("talhoes", "total_itens"))
    return counts


def summarize_text(bundles: list[Gs3FieldBundle], profile_name: str = "") -> str:
    c = summarize_bundles(bundles)
    labels = {
        "limite": "Limites",
        "limite_interno": "Limites internos",
        "linha_ab": "Linhas AB",
        "curva_ab": "Curvas AB",
        "curva_adaptada": "Curvas adaptadas",
        "bordadura": "Bordaduras",
        "passadas_plantio": "Passadas de plantio",
        "passadas_bordadura": "Passadas de bordadura",
        "trabalho": "Trabalhos / faixas",
        "marcacao": "Marcacoes",
        "obstaculo_ponto": "Obstaculos ponto",
        "obstaculo_area": "Obstaculos area",
    }
    lines = [
        f"Pronto para exportar{(' → ' + profile_name) if profile_name else ''}",
        (
            f"Hierarquia: {bundles[0].cliente} → {bundles[0].fazenda} → Talhoes"
            if bundles
            else "Hierarquia: Proprietario → Fazenda → Talhoes"
        ),
        f"Talhoes: {c.get('talhoes', 0)} | Itens: {c.get('total_itens', 0)}",
        "",
    ]
    for key, label in labels.items():
        if c.get(key):
            lines.append(f"  • {label}: {c[key]}")
    if c.get("talhoes", 0) == 0:
        lines.append("")
        lines.append("Nenhuma camada encontrada.")
        lines.append("Importe com 'Organizar por talhao' ou crie camadas com")
        lines.append("atributos: cliente, fazenda, talhao, tipo.")
    return "\n".join(lines)


# Compatibilidade com nome antigo usado em isoxml_export / export_ui
def collect_bundles_from_project(group_names: tuple[str, ...] | None = None) -> list[Gs3FieldBundle]:
    return collect_export_bundles(include_loose_layers=True)
