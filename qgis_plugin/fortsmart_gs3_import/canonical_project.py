"""Canonical layer identity shared by every FortSmart monitor exporter.

The QGIS display name is intentionally the last-resort compatibility path.
New projects are identified by ``fortsmart_role`` and remain exportable after
the user renames or reorganizes a layer in the layer tree.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

from .gs3_spatial_types import SpatialKind


ROLE_PROPERTIES = (
    "fortsmart_role",
    "fortsmart_precision/role",
)
INTERNAL_KEY_PROPERTIES = (
    "fortsmart_internal_key",
    "fortsmart_precision/internal_key",
)
SCHEMA_VERSION_PROPERTIES = (
    "fortsmart_schema_version",
    "fortsmart_precision/schema_version",
)


INTERNAL_KEY_ROLES = {
    "01_LIMITE_EXTERNO": "EXTERNAL_BOUNDARY",
    "02_BORDADURA": "HEADLAND",
    "03_LIMITE_INTERNO": "INTERNAL_BOUNDARY",
    "04_LINHA_AB": "AB_LINE",
    "05_PASSADAS": "GUIDANCE_TRACK",
    "06_PASSADAS_PLANTIO": "WORK_PATH",
    "07_LINHA_TRATOR": "TRACTOR_PATH",
    "08_RASTROS_PNEUS": "TIRE_TRACK",
    "09_OBSTACULOS_RTK": "OBSTACLE",
    "10_ZONAS_SEGURANCA": "SAFETY_ZONE",
    "11_DESVIOS_OBSTACULOS": "OBSTACLE_DETOUR",
    "12_LINHAS_FINAIS": "FINAL_WORK_PATH",
    "13_SHUTOFF": "SHUTOFF",
    "14_LIMITE_OPERACIONAL": "OPERATIONAL_BOUNDARY",
    "15_PASSADAS_BORDADURA": "FIELD_HEADLAND_PATH",
}


# Exact legacy aliases. They are classification fallbacks and never rename a
# user layer or overwrite its data.
LEGACY_ROLE_ALIASES = {
    "01_LIMITE_EXTERNO": "EXTERNAL_BOUNDARY",
    "LIMITE_EXTERNO": "EXTERNAL_BOUNDARY",
    "LIMITE EXTERNO": "EXTERNAL_BOUNDARY",
    "LIMITE": "EXTERNAL_BOUNDARY",
    "BOUNDARY": "EXTERNAL_BOUNDARY",
    "02_LIMITE_INTERNO": "INTERNAL_BOUNDARY",
    "LIMITE_INTERNO": "INTERNAL_BOUNDARY",
    "LIMITE INTERNO": "INTERNAL_BOUNDARY",
    "03_BORDADURA": "HEADLAND",
    "02_BORDADURA": "HEADLAND",
    "BORDADURA": "HEADLAND",
    "HEADLAND": "HEADLAND",
    "04_LINHA_AB": "AB_LINE",
    "LINHA_AB": "AB_LINE",
    "LINHA AB": "AB_LINE",
    "ABLINE": "AB_LINE",
    "AB LINE": "AB_LINE",
    "ABCURVE": "AB_CURVE",
    "AB CURVE": "AB_CURVE",
    "CURVA_AB": "AB_CURVE",
    "CURVA AB": "AB_CURVE",
    "ADAPTIVECURVE": "GUIDANCE_TRACK",
    "CURVA_ADAPTADA": "GUIDANCE_TRACK",
    "CURVA ADAPTADA": "GUIDANCE_TRACK",
    "11_PASSADAS": "GUIDANCE_TRACK",
    "05_PASSADAS": "GUIDANCE_TRACK",
    "PASSADAS": "GUIDANCE_TRACK",
    "12_PASSADAS_PLANTIO": "WORK_PATH",
    "06_PASSADAS_PLANTIO": "WORK_PATH",
    "PASSADAS_PLANTIO": "WORK_PATH",
    "PASSADAS PLANTIO": "WORK_PATH",
    "31_LINHAS_FINAIS": "FINAL_WORK_PATH",
    "12_LINHAS_FINAIS": "FINAL_WORK_PATH",
    "LINHAS_FINAIS": "FINAL_WORK_PATH",
    "LINHAS FINAIS": "FINAL_WORK_PATH",
    "21_OBSTACULOS_RTK": "OBSTACLE",
    "09_OBSTACULOS_RTK": "OBSTACLE",
    "OBSTACULO": "OBSTACLE",
    "OBSTACULOS": "OBSTACLE",
    "OBSTACULO_PONTO": "OBSTACLE",
    "OBSTACULO_AREA": "OBSTACLE",
    "FLAG": "OBSTACLE",
    "22_ZONAS_SEGURANCA": "SAFETY_ZONE",
    "32_WORK_OFF": "WORK_OFF",
    "33_WORK_ON": "WORK_ON",
    "34_SHUTOFF": "SHUTOFF",
    "16_MANOBRAS_CABECEIRA": "HEADLAND_TURN",
    "TRABALHO": "WORK_PATH",
    "MARCACAO": "MARKER",
    "LINHA_PLANTIO": "WORK_PATH",
    "LINHAS_PLANTIO": "WORK_PATH",
    "LINHA_DE_PLANTIO": "WORK_PATH",
    "LINHAS_DE_PLANTIO": "WORK_PATH",
    "PIVO": "EXTERNAL_BOUNDARY",
}


ROLE_KIND_MAP = {
    "EXTERNAL_BOUNDARY": SpatialKind.LIMITE,
    "OPERATIONAL_BOUNDARY": SpatialKind.LIMITE,
    "INTERNAL_BOUNDARY": SpatialKind.LIMITE_INTERNO,
    "HEADLAND": SpatialKind.BORDADURA,
    "AB_LINE": SpatialKind.LINHA_AB,
    "AB_CURVE": SpatialKind.CURVA_AB,
    "GUIDANCE_TRACK": SpatialKind.CURVA_ADAPTADA,
    "OBSTACLE": SpatialKind.OBSTACULO_PONTO,
    "SAFETY_ZONE": SpatialKind.OBSTACULO_AREA,
    "WORK_PATH": SpatialKind.TRABALHO,
    "FINAL_WORK_PATH": SpatialKind.TRABALHO,
    "TRACTOR_PATH": SpatialKind.TRABALHO,
    "TIRE_TRACK": SpatialKind.TRABALHO,
    "FIELD_HEADLAND_PATH": SpatialKind.TRABALHO,
    "HEADLAND_TURN": SpatialKind.TRABALHO,
    "OBSTACLE_DETOUR": SpatialKind.TRABALHO,
    "WORK_OFF": SpatialKind.TRABALHO,
    "WORK_ON": SpatialKind.TRABALHO,
    "SHUTOFF": SpatialKind.TRABALHO,
    "MARKER": SpatialKind.MARCAÇAO,
}


KIND_DEFAULT_ROLES = {
    SpatialKind.LIMITE: "EXTERNAL_BOUNDARY",
    SpatialKind.LIMITE_INTERNO: "INTERNAL_BOUNDARY",
    SpatialKind.LINHA_AB: "AB_LINE",
    SpatialKind.CURVA_AB: "AB_CURVE",
    SpatialKind.CURVA_ADAPTADA: "GUIDANCE_TRACK",
    SpatialKind.BORDADURA: "HEADLAND",
    SpatialKind.TRABALHO: "WORK_PATH",
    SpatialKind.MARCAÇAO: "MARKER",
    SpatialKind.OBSTACULO_PONTO: "OBSTACLE",
    SpatialKind.OBSTACULO_AREA: "OBSTACLE",
}


ROLE_SECTIONS = {
    "EXTERNAL_BOUNDARY": "00_REFERENCIAS",
    "OPERATIONAL_BOUNDARY": "00_REFERENCIAS",
    "INTERNAL_BOUNDARY": "00_REFERENCIAS",
    "HEADLAND": "00_REFERENCIAS",
    "AB_LINE": "00_REFERENCIAS",
    "AB_CURVE": "00_REFERENCIAS",
    "GUIDANCE_TRACK": "10_PLANEJAMENTO",
    "WORK_PATH": "10_PLANEJAMENTO",
    "FINAL_WORK_PATH": "30_OPERACAO",
    "OBSTACLE": "20_OBSTACULOS",
    "SAFETY_ZONE": "20_OBSTACULOS",
    "SHUTOFF": "30_OPERACAO",
    "MARKER": "20_OBSTACULOS",
}


def _normal(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\s*\(EPSG:\d+\)\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\{[0-9A-F-]{8,}\}", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return text.upper()


def _custom_property(layer, keys: tuple[str, ...]) -> str:
    getter = getattr(layer, "customProperty", None)
    if getter is None:
        return ""
    for key in keys:
        try:
            value = getter(key, "")
        except Exception:
            value = ""
        if value not in (None, ""):
            return str(value).strip().upper()
    return ""


def _field_names(layer) -> dict[str, str]:
    try:
        return {field.name().casefold(): field.name() for field in layer.fields()}
    except Exception:
        return {}


def _constant_field_value(layer, candidate_names: tuple[str, ...]) -> str:
    names = _field_names(layer)
    actual = next((names[name.casefold()] for name in candidate_names if name.casefold() in names), "")
    if not actual:
        return ""
    values: set[str] = set()
    try:
        for index, feature in enumerate(layer.getFeatures()):
            value = feature[actual]
            if value not in (None, ""):
                values.add(str(value).strip().upper())
            if len(values) > 1:
                return ""
            # Role metadata is homogeneous by layer. Sampling avoids scanning
            # hundreds of thousands of agricultural paths during export.
            if index >= 63:
                break
    except Exception:
        return ""
    return next(iter(values), "")


_LINE_WORDS = frozenset(
    {"LINHA", "LINHAS", "PASSADA", "PASSADAS", "FAIXA", "FAIXAS", "TRILHA", "TRILHAS",
     "PATH", "PATHS", "LINE", "LINES", "GUIA", "GUIAS"}
)
_PLANTING_WORDS = frozenset(
    {"PLANTIO", "PLANTIOS", "PLANTAR", "PLANTADEIRA", "PLANTADORA", "SEMEADURA", "SEMEADORA",
     "PLANTING", "PLANTER", "TRABALHO", "WORK", "WORKPATH"}
)
_BORDER_WORDS = frozenset(
    {"BORDADURA", "BORDADURAS", "CABECEIRA", "CABECEIRAS", "HEADLAND", "HEADLANDS", "BORDA", "BORDAS"}
)
_FINAL_WORDS = frozenset({"FINAL", "FINAIS"})
_BOUNDARY_WORDS = frozenset(
    {"LIMITE", "LIMITES", "BOUNDARY", "BOUNDARIES", "PERIMETRO", "CONTORNO", "TALHAO", "TALHOES",
     "DIVISA", "FIELD"}
)
_INNER_WORDS = frozenset({"INTERNO", "INTERNA", "INTERNOS", "INTERNAS", "INTERNAL", "INNER", "EXCLUSAO"})
_OPERATIONAL_WORDS = frozenset({"OPERACIONAL", "OPERATIONAL"})
_PIVOT_WORDS = frozenset({"PIVO", "PIVOS", "PIVOT", "PIVOTS"})
_MARKER_WORDS = frozenset(
    {"MARCACAO", "MARCACOES", "MARCADOR", "MARCADORES", "MARKER", "MARKERS", "MARCO", "MARCOS"}
)
_OBSTACLE_WORDS = frozenset(
    {"OBSTACULO", "OBSTACULOS", "OBSTACLE", "OBSTACLES", "FLAG", "FLAGS", "BANDEIRA", "BANDEIRAS",
     "PEDRA", "PEDRAS", "TOCO", "TOCOS", "CUPIM", "CUPINS"}
)
_STRAIGHT_WORDS = frozenset({"LINHA", "RETA", "LINE"})
_CURVE_WORDS = frozenset({"CURVA", "CURVE"})
_ADAPTIVE_WORDS = frozenset({"ADAPTADA", "ADAPTATIVA", "ADAPTIVE", "ADAPTIVECURVE"})

LINE_ROLES = frozenset(
    {"WORK_PATH", "FINAL_WORK_PATH", "FIELD_HEADLAND_PATH", "AB_LINE", "AB_CURVE", "GUIDANCE_TRACK"}
)
POLYGON_ROLES = frozenset({"EXTERNAL_BOUNDARY", "INTERNAL_BOUNDARY", "OPERATIONAL_BOUNDARY", "HEADLAND"})


def _name_core(normalized: str) -> str:
    """``03_LIMITE_EXTERNO_EPSG_31981`` -> ``LIMITE_EXTERNO``; ``MARCACAO_10`` -> ``MARCACAO``."""
    text = re.sub(r"(?:^|_)EPSG_?\d+$", "", normalized)
    text = re.sub(r"^(?:\d+_)+", "", text)
    text = re.sub(r"(?:_\d+)+$", "", text)
    return text.strip("_")


def _role_from_words(words: set[str], geometry: str) -> str:
    if "ABLINE" in words or ("AB" in words and words & _STRAIGHT_WORDS):
        return "AB_LINE"
    if "ABCURVE" in words or ("AB" in words and words & _CURVE_WORDS):
        return "AB_CURVE"
    if "ZONA" in words and words & {"SEGURANCA", "SAFETY"}:
        return "SAFETY_ZONE"
    if words & _OBSTACLE_WORDS:
        return "OBSTACLE"
    if words & _INNER_WORDS and words & (_BOUNDARY_WORDS | {"AREA"}):
        return "INTERNAL_BOUNDARY"
    if words & _OPERATIONAL_WORDS and words & _BOUNDARY_WORDS:
        return "OPERATIONAL_BOUNDARY"
    if words & _FINAL_WORDS and words & (_LINE_WORDS | _PLANTING_WORDS):
        return "FINAL_WORK_PATH"
    if words & _BORDER_WORDS:
        if words & _LINE_WORDS or geometry == "line":
            return "FIELD_HEADLAND_PATH"
        return "HEADLAND"
    if words & _PLANTING_WORDS:
        return "" if geometry == "polygon" else "WORK_PATH"
    if words & (_BOUNDARY_WORDS | _PIVOT_WORDS):
        return "EXTERNAL_BOUNDARY"
    if words & _MARKER_WORDS:
        return "MARKER"
    if "RETA" in words:
        return "AB_LINE"
    if words & _ADAPTIVE_WORDS:
        return "GUIDANCE_TRACK"
    if words & {"PASSADA", "PASSADAS"}:
        return "GUIDANCE_TRACK"
    return ""


def _fit_geometry(role: str, geometry: str) -> str:
    """Nome e geometria precisam concordar: bordadura em linha sao passadas."""
    if role == "HEADLAND" and geometry == "line":
        return "FIELD_HEADLAND_PATH"
    if role == "FIELD_HEADLAND_PATH" and geometry == "polygon":
        return "HEADLAND"
    if role in LINE_ROLES and geometry in ("polygon", "point"):
        return ""
    if role in POLYGON_ROLES and geometry in ("line", "point"):
        return ""
    return role


def role_from_layer_name(name: str, geometry: str = "") -> str:
    """Role pelo nome da camada: ``linha plantio``, ``31_linhas_finais``, ``pivo``, ``marcacao_10_epsg_31981``.

    ``geometry`` ("point", "line", "polygon" ou vazio) desempata nomes que
    servem a mais de um tipo, como "bordadura" (area) vs. passadas de bordadura.
    """
    normalized = _normal(name)
    if not normalized:
        return ""
    core = _name_core(normalized)
    for candidate in (normalized, re.sub(r"_\d+$", "", normalized), core):
        if candidate in LEGACY_ROLE_ALIASES:
            return _fit_geometry(LEGACY_ROLE_ALIASES[candidate], geometry)
    if core.startswith(("FLAG", "OBSTACLE", "OBSTACULO", "POINT", "PNT")):
        return "OBSTACLE"
    if core.startswith("BOUNDARY"):
        return _fit_geometry("EXTERNAL_BOUNDARY", geometry)
    words = set(core.split("_"))
    role = _role_from_words(words, geometry)
    if role in POLYGON_ROLES and geometry in ("line", "point"):
        # "pivo_marcacao" em linhas e marcacao: limite/pivo so descrevem areas.
        area_words = _BOUNDARY_WORDS | _PIVOT_WORDS | _INNER_WORDS | _OPERATIONAL_WORDS
        role = _role_from_words(words - area_words, geometry)
    return _fit_geometry(role, geometry)


def role_from_legacy_name(name: str) -> str:
    return role_from_layer_name(name)


def layer_geometry_name(layer) -> str:
    try:
        value = layer.geometryType()
    except Exception:
        return ""
    value = getattr(value, "value", value)
    try:
        return {0: "point", 1: "line", 2: "polygon"}.get(int(value), "")
    except (TypeError, ValueError):
        return ""


def role_to_spatial_kind(role: str, geometry_type: str = "") -> SpatialKind:
    canonical = str(role or "").strip().upper()
    if canonical == "OBSTACLE" and geometry_type == "polygon":
        return SpatialKind.OBSTACULO_AREA
    return ROLE_KIND_MAP.get(canonical, SpatialKind.OUTRO)


def spatial_kind_default_role(kind: SpatialKind) -> str:
    return KIND_DEFAULT_ROLES.get(kind, "")


def canonical_feature_role(feature) -> str:
    """Return role stored on a feature, if the layer contains mixed roles."""
    try:
        names = {field.name().casefold(): field.name() for field in feature.fields()}
    except Exception:
        return ""
    for candidate in ("fortsmart_role", "role"):
        actual = names.get(candidate.casefold())
        if actual:
            try:
                value = feature[actual]
            except Exception:
                value = ""
            if value not in (None, ""):
                return str(value).strip().upper()
    return ""


@dataclass(frozen=True)
class CanonicalLayerRef:
    layer: object
    role: str
    resolution: str
    internal_key: str = ""
    schema_version: str = ""


@dataclass
class CanonicalProject:
    """Semantic view of a QGIS project, independent from display names."""

    layers_by_role: dict[str, list[CanonicalLayerRef]] = field(default_factory=dict)
    unknown_layers: list[object] = field(default_factory=list)

    def add(self, ref: CanonicalLayerRef) -> None:
        self.layers_by_role.setdefault(ref.role, []).append(ref)

    def find_role(self, role: str):
        refs = self.layers_by_role.get(str(role).upper(), [])
        for ref in refs:
            try:
                if ref.layer.featureCount() > 0:
                    return ref.layer
            except Exception:
                return ref.layer
        return refs[0].layer if refs else None

    def find_roles(self, *roles: str) -> list[object]:
        return [ref.layer for role in roles for ref in self.layers_by_role.get(role.upper(), [])]

    @property
    def external_boundary(self):
        return self.find_role("EXTERNAL_BOUNDARY")

    @property
    def internal_boundaries(self) -> list[object]:
        return self.find_roles("INTERNAL_BOUNDARY")

    @property
    def headland(self):
        return self.find_role("HEADLAND")

    @property
    def ab_line(self):
        return self.find_role("AB_LINE")

    @property
    def guidance_tracks(self) -> list[object]:
        return self.find_roles("GUIDANCE_TRACK", "AB_CURVE")

    @property
    def work_paths(self) -> list[object]:
        return self.find_roles("WORK_PATH")

    @property
    def final_work_paths(self) -> list[object]:
        return self.find_roles("FINAL_WORK_PATH")

    @property
    def obstacles(self) -> list[object]:
        return self.find_roles("OBSTACLE", "SAFETY_ZONE")


class CanonicalProjectBuilder:
    """Resolve layer identity in the mandated priority order."""

    def resolve_layer(self, layer) -> CanonicalLayerRef | None:
        role = _custom_property(layer, ROLE_PROPERTIES)
        if role:
            return CanonicalLayerRef(
                layer,
                role,
                "fortsmart_role",
                _custom_property(layer, INTERNAL_KEY_PROPERTIES),
                _custom_property(layer, SCHEMA_VERSION_PROPERTIES),
            )

        feature_role = _constant_field_value(layer, ("fortsmart_role", "role"))
        if feature_role:
            return CanonicalLayerRef(layer, feature_role, "feature_role")

        internal_key = _custom_property(layer, INTERNAL_KEY_PROPERTIES)
        if not internal_key:
            internal_key = _constant_field_value(layer, ("fortsmart_internal_key", "internal_key"))
        internal_role = INTERNAL_KEY_ROLES.get(_normal(internal_key))
        if internal_role:
            return CanonicalLayerRef(layer, internal_role, "internal_key", internal_key)

        try:
            name = layer.name()
        except Exception:
            name = ""
        legacy_role = role_from_layer_name(name, layer_geometry_name(layer))
        if legacy_role:
            return CanonicalLayerRef(layer, legacy_role, "legacy_alias")
        return None

    def build(self, layers: Iterable[object]) -> CanonicalProject:
        project = CanonicalProject()
        for layer in layers:
            ref = self.resolve_layer(layer)
            if ref is None:
                project.unknown_layers.append(layer)
            else:
                project.add(ref)
        return project


def feature_canonical_role(feature) -> str:
    return str(getattr(feature, "extra", {}).get("canonical_role", "") or "").upper()


def canonical_feature_allowed(
    feature,
    *,
    allowed_roles: set[str],
    legacy_kinds: set[str],
) -> bool:
    """Strict role filtering for new projects, kind fallback for old imports."""
    role = feature_canonical_role(feature)
    if role:
        return role in allowed_roles
    return feature.kind.value in legacy_kinds
