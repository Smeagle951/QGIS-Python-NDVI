"""FortSmart — leitura de camadas ativas no projeto QGIS (origem do export)."""

from __future__ import annotations

import re
from enum import Enum

from qgis.core import QgsLayerTreeGroup, QgsLayerTreeLayer, QgsProject, QgsVectorLayer

from .canonical_project import CanonicalProjectBuilder


class LayerSourceMode(str, Enum):
    """De onde vêm as camadas para exportar."""

    VISIBLE = "visible"
    SELECTED = "selected"
    ALL = "all"
    EXACT_NAMES = "exact"
    AUTO_MAP = "auto_map"
    DIRECT = "direct"


# Palavras-chave por slot 01-13 (ordem = spec order)
SPEC_KEYWORDS: dict[int, list[str]] = {
    1: ["limite_externo", "limite externo", "boundary", "perimeter", "limite"],
    2: ["bordadura", "limite bor", "headland", " bor", "boradura"],
    3: ["limite_interno", "limite interno", "inner boundary", "interno"],
    4: ["linha_ab", "linha ab", "abline", " ab ", " reta", "rete", "straight"],
    5: ["passada", "passadas", "spray", "pulv", "aplicacao"],
    6: ["linhas_plantio", "linhas plantio", "plantio", " linhas", "guidance", "adaptivecurve"],
    7: ["linha_trator", "linha trator", "trator", "tractor"],
    8: ["rastro", "pneu", "track", "rastros"],
    9: ["obstaculo", "obstacle", "rtk"],
    10: ["zona", "seguranca", "safety"],
    11: ["desvio", "deviation", "desvios"],
    12: ["linhas_finais", "linhas finais", "curva adt", "curva adapt", "final path"],
    13: ["shutoff", "shut off", "corte"],
}

# Evita falso positivo entre slots parecidos
SPEC_NEGATIVES: dict[int, list[str]] = {
    1: ["interno", " bor", "bordadura", "headland"],
    4: ["plantio", " linhas", "adaptivecurve", "limite"],
    6: [" limite", "bordadura", " reta"],
    12: [" limite", "bordadura"],
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").casefold())


def sanitize_filename(name: str, max_len: int = 64) -> str:
    text = re.sub(r"[^\w\-]+", "_", (name or "camada").strip(), flags=re.UNICODE)
    text = re.sub(r"_+", "_", text).strip("_").casefold()
    return (text or "camada")[:max_len]


def get_visible_vector_layers() -> list[QgsVectorLayer]:
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    out: list[QgsVectorLayer] = []
    for layer in project.mapLayers().values():
        if not isinstance(layer, QgsVectorLayer):
            continue
        node = root.findLayer(layer.id())
        if node and node.isVisible():
            out.append(layer)
    return out


def get_selected_vector_layers() -> list[QgsVectorLayer]:
    """Camadas/grupos selecionados no painel Camadas do QGIS.

    A seleção pertence a QgsLayerTreeView (GUI), não a QgsLayerTree
    retornada por QgsProject.layerTreeRoot().
    """
    project = QgsProject.instance()
    ids: set[str] = set()

    def collect_from_group(group: QgsLayerTreeGroup) -> None:
        for child in group.children():
            if isinstance(child, QgsLayerTreeLayer):
                ids.add(child.layerId())
            elif isinstance(child, QgsLayerTreeGroup):
                collect_from_group(child)

    try:
        from qgis.utils import iface

        view = iface.layerTreeView() if iface else None
        if view is not None:
            for node in view.selectedNodes():
                if isinstance(node, QgsLayerTreeLayer):
                    ids.add(node.layerId())
                elif isinstance(node, QgsLayerTreeGroup):
                    collect_from_group(node)

            # Compatibilidade: algumas versões/providers expõem a seleção
            # de camadas por selectedLayers(), sem incluir grupos.
            if not ids and hasattr(view, "selectedLayers"):
                for layer in view.selectedLayers():
                    if isinstance(layer, QgsVectorLayer):
                        ids.add(layer.id())

        # Se o painel não informar seleção, usa a camada ativa.
        if not ids and iface:
            active = iface.activeLayer()
            if isinstance(active, QgsVectorLayer):
                ids.add(active.id())
    except (AttributeError, RuntimeError):
        # Execução sem GUI (testes/headless): seleção não está disponível.
        return []

    layers: list[QgsVectorLayer] = []
    for lid in ids:
        layer = project.mapLayer(lid)
        if isinstance(layer, QgsVectorLayer):
            layers.append(layer)
    return layers


def get_all_vector_layers() -> list[QgsVectorLayer]:
    return [
        layer
        for layer in QgsProject.instance().mapLayers().values()
        if isinstance(layer, QgsVectorLayer)
    ]


def resolve_source_layers(mode: LayerSourceMode) -> list[QgsVectorLayer]:
    if mode == LayerSourceMode.VISIBLE:
        return get_visible_vector_layers()
    if mode == LayerSourceMode.SELECTED:
        return get_selected_vector_layers()
    if mode in (LayerSourceMode.ALL, LayerSourceMode.EXACT_NAMES, LayerSourceMode.AUTO_MAP, LayerSourceMode.DIRECT):
        visible = get_visible_vector_layers()
        if visible and mode in (LayerSourceMode.AUTO_MAP, LayerSourceMode.DIRECT):
            return visible
        return get_all_vector_layers()
    return get_all_vector_layers()


def score_layer_for_spec(layer_name: str, spec: dict) -> int:
    name = layer_name.casefold()
    order = spec["order"]
    score = 0

    n_layer = _norm(layer_name)
    n_spec = _norm(spec["qgis_name"])
    n_file = _norm(spec["filename"])
    if n_layer == n_spec or n_layer == n_file:
        return 1000
    if n_spec in n_layer or n_file in n_layer:
        score += 200

    for neg in SPEC_NEGATIVES.get(order, []):
        if neg in name:
            return 0

    for kw in SPEC_KEYWORDS.get(order, []):
        if kw in name:
            score += 15

    # AdaptiveCurve + RETA → linha AB; AdaptiveCurve + LINHAS → plantio
    if "adaptivecurve" in name:
        if order == 4 and "reta" in name:
            score += 40
        if order == 6 and "linhas" in name:
            score += 40
        if order == 12 and ("curva" in name or "adt" in name):
            score += 35
        if order == 6 and score < 20:
            score += 10

    # "Limite 11", "Limite 12"
    if order == 1 and re.search(r"\blimite\b", name) and "bor" not in name and "interno" not in name:
        score += 25

    # TH x LINHAS PULV → passadas
    if order == 5 and ("pulv" in name or "spray" in name):
        score += 30

    return score


def find_layers_for_spec(spec: dict, candidates: list[QgsVectorLayer]) -> list[QgsVectorLayer]:
    role = str(spec.get("role", "") or "").upper()
    if role:
        canonical = CanonicalProjectBuilder().build(candidates)
        refs = canonical.layers_by_role.get(role, [])
        if refs:
            # A role layer that exists but is empty remains authoritative. The
            # validator must report it as empty instead of selecting a namesake.
            return [ref.layer for ref in refs]

    scored: list[tuple[int, QgsVectorLayer]] = []
    for layer in candidates:
        s = score_layer_for_spec(layer.name(), spec)
        if s > 0 and layer.featureCount() > 0:
            scored.append((s, layer))
    if not scored:
        return []
    scored.sort(key=lambda x: (-x[0], x[1].name().casefold()))
    best_score = scored[0][0]
    # Camadas empatadas no melhor score (ex.: Limite 11 + Limite 12)
    return [layer for score, layer in scored if score >= best_score * 0.85]


def find_layer_by_exact_spec(spec: dict, candidates: list[QgsVectorLayer] | None = None) -> QgsVectorLayer | None:
    layers = candidates if candidates is not None else get_all_vector_layers()
    role = str(spec.get("role", "") or "").upper()
    if role:
        canonical = CanonicalProjectBuilder().build(layers)
        refs = canonical.layers_by_role.get(role, [])
        if refs:
            for ref in refs:
                if ref.layer.featureCount() > 0:
                    return ref.layer
            return refs[0].layer

    target = _norm(spec["qgis_name"])
    target_file = _norm(spec["filename"])
    best = None
    for layer in layers:
        n = _norm(layer.name())
        if n == target or n == target_file:
            return layer
        if target in n or target_file in n:
            best = best or layer
    return best


def merge_vector_layers(layers: list[QgsVectorLayer], merged_name: str) -> QgsVectorLayer | None:
    if not layers:
        return None
    if len(layers) == 1:
        return layers[0]

    from qgis.core import QgsCoordinateTransform, QgsFeature, QgsGeometry, QgsVectorLayer

    ref = layers[0]
    type_map = {0: "Point", 1: "LineString", 2: "Polygon", 3: "MultiPoint"}
    gtype = type_map.get(int(ref.geometryType()), "LineString")
    crs = ref.crs().authid() if ref.crs().isValid() else "EPSG:4326"
    mem = QgsVectorLayer(f"{gtype}?crs={crs}", merged_name, "memory")
    if not mem.isValid():
        return layers[0]

    mem.startEditing()
    for layer in layers:
        xform = None
        if layer.crs().isValid() and ref.crs().isValid() and layer.crs() != ref.crs():
            xform = QgsCoordinateTransform(layer.crs(), ref.crs(), QgsProject.instance())
        for feat in layer.getFeatures():
            if feat.geometry() is None or feat.geometry().isEmpty():
                continue
            geom = QgsGeometry(feat.geometry())
            if xform:
                geom.transform(xform)
            nf = QgsFeature()
            nf.setGeometry(geom)
            mem.addFeature(nf)
    mem.commitChanges()
    return mem


def summarize_active_layers(layers: list[QgsVectorLayer]) -> str:
    if not layers:
        return (
            "Nenhuma camada vetorial ativa encontrada.\n\n"
            "Marque camadas como visiveis no painel Camadas,\n"
            "ou selecione camadas/grupos antes de exportar."
        )
    lines = [f"Camadas no QGIS: {len(layers)}", ""]
    total = 0
    for layer in sorted(layers, key=lambda l: l.name().casefold()):
        n = layer.featureCount()
        total += n
        mark = "OK" if n > 0 else "!!"
        lines.append(f"  {mark}  {layer.name():<40} {n} feicoes")
    lines.extend(["", f"Total de feicoes: {total}"])
    return "\n".join(lines)
