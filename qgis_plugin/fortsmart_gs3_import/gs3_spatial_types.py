"""FortSmart — tipos espaciais GS3/RCD organizados por talhao."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SpatialKind(str, Enum):
    LIMITE = "limite"
    LIMITE_INTERNO = "limite_interno"
    LINHA_AB = "linha_ab"
    CURVA_AB = "curva_ab"
    CURVA_ADAPTADA = "curva_adaptada"
    BORDADURA = "bordadura"
    TRABALHO = "trabalho"
    MARCAÇAO = "marcacao"
    OBSTACULO_PONTO = "obstaculo_ponto"
    OBSTACULO_AREA = "obstaculo_area"
    OUTRO = "outro"


LAYER_LABELS: dict[SpatialKind, str] = {
    SpatialKind.LIMITE: "Limite",
    SpatialKind.LIMITE_INTERNO: "Limite interno",
    SpatialKind.LINHA_AB: "Linha AB",
    SpatialKind.CURVA_AB: "Curva AB",
    SpatialKind.CURVA_ADAPTADA: "Curva adaptada",
    SpatialKind.BORDADURA: "Bordadura",
    SpatialKind.TRABALHO: "Trabalho",
    SpatialKind.MARCAÇAO: "Marcacao",
    SpatialKind.OBSTACULO_PONTO: "Obstaculo ponto",
    SpatialKind.OBSTACULO_AREA: "Obstaculo area",
    SpatialKind.OUTRO: "Outro",
}

LAYER_ORDER: list[SpatialKind] = [
    SpatialKind.LIMITE,
    SpatialKind.LIMITE_INTERNO,
    SpatialKind.BORDADURA,
    SpatialKind.LINHA_AB,
    SpatialKind.CURVA_AB,
    SpatialKind.CURVA_ADAPTADA,
    SpatialKind.TRABALHO,
    SpatialKind.MARCAÇAO,
    SpatialKind.OBSTACULO_PONTO,
    SpatialKind.OBSTACULO_AREA,
    SpatialKind.OUTRO,
]


@dataclass
class Gs3SpatialFeature:
    kind: SpatialKind
    cliente: str
    fazenda: str
    talhao: str
    field_erid: str
    item_erid: str
    name: str
    source_file: str
    catalog_file: str
    geometry: str  # polygon | line | point
    points: list[tuple[float, float]] = field(default_factory=list)
    rings: list[list[tuple[float, float]]] = field(default_factory=list)
    active: bool = True
    extra: dict[str, str] = field(default_factory=dict)
    format_source: str = "gs3"


@dataclass
class Gs3FieldBundle:
    cliente: str
    fazenda: str
    talhao: str
    field_erid: str
    features: list[Gs3SpatialFeature] = field(default_factory=list)

    def by_kind(self, kind: SpatialKind) -> list[Gs3SpatialFeature]:
        return [f for f in self.features if f.kind == kind]

    def kinds_present(self) -> list[SpatialKind]:
        seen: set[SpatialKind] = set()
        ordered: list[SpatialKind] = []
        for kind in LAYER_ORDER:
            if any(f.kind == kind for f in self.features):
                ordered.append(kind)
                seen.add(kind)
        for feature in self.features:
            if feature.kind not in seen:
                ordered.append(feature.kind)
        return ordered


@dataclass
class Gs3FieldImportResult:
    eic_root: str
    catalog_count: int
    field_count: int
    bundles: list[Gs3FieldBundle]
    farms_before_merge: int = 0
    farms_after_merge: int = 0
    format_source: str = "gs3"
    iso_version: str = ""
