"""FortSmart — roteamento GS3 das guias John Deere (CurveTrack x ABCurve).

Estrutura observada nos pacotes de referencia (sem nomes, coordenadas ou UUIDs):

* guias importadas do desktop no GS3 2630: somente ``CurvedTrackLine`` em
  ``ImportExport.SpatialCatalog`` + ``CurveTrack{UUID}.fdShape``;
* ``ABCurve`` em ``ABCurves.SpatialCatalog`` aparece apenas em pistas gravadas
  no proprio monitor (dtiSignalTypeDigitized, vrHeading, deslocamentos
  leste/norte) — estrutura que o writer ABCurve atual nao reproduz.

Por isso plantio e bordadura vao para CurveTrack. ABCurve so sera usado quando
``GS3_ABCURVE_GUIDANCE_PROVEN`` for comprovado em monitor; ate la, pedir ABCurve
aborta (nunca converte em silencio).
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .gs3_pass_guidance import GS3FieldGuidanceResult, GS3PassGuidanceWriter
from .gs3_spatial_types import Gs3SpatialFeature
from .jd_guidance_plan import (
    BORDER_PASS,
    PASS_CATEGORIES,
    PLANTING_PASS,
    FieldGuidancePlan,
    GuidancePassItem,
    JohnDeereGuidanceOptions,
    pass_category,
)

AB_CURVE = "AB_CURVE"
CURVE_TRACK = "CURVE_TRACK"
GS3_GUIDANCE_TYPE_BY_CATEGORY = {PLANTING_PASS: CURVE_TRACK, BORDER_PASS: CURVE_TRACK}
GS3_ABCURVE_GUIDANCE_PROVEN = False
ABCURVE_NOT_PROVEN = "GS3_GUIDANCE_ABCURVE_NOT_PROVEN"

GS3CurveTrackWriter = GS3PassGuidanceWriter


def classify_gs3_guidance(
    feature: Gs3SpatialFeature | GuidancePassItem,
    type_by_category: Mapping[str, str] | None = None,
) -> str | None:
    """Tipo GS3 pela categoria canonica; nunca pela quantidade de vertices."""
    if isinstance(feature, GuidancePassItem):
        category = feature.category
    else:
        category = pass_category(feature)
        if category is None:
            role = str(feature.extra.get("canonical_role", "") or "").upper()
            return AB_CURVE if role == "AB_CURVE" else None
    return (type_by_category or GS3_GUIDANCE_TYPE_BY_CATEGORY).get(category)


class GS3GuidanceRouter:
    """Escolhe o writer GS3 por categoria; CurveTrack reutiliza o writer comprovado."""

    def __init__(
        self,
        options: JohnDeereGuidanceOptions,
        type_by_category: Mapping[str, str] | None = None,
    ) -> None:
        self.options = options
        self.type_by_category = dict(type_by_category or GS3_GUIDANCE_TYPE_BY_CATEGORY)
        self.curve_track = GS3CurveTrackWriter(options)

    def write_field(self, field_plan: FieldGuidancePlan, field_dir: Path) -> GS3FieldGuidanceResult:
        for category in PASS_CATEGORIES:
            if not self.options.requested(category):
                continue
            for item in field_plan.items[category]:
                kind = classify_gs3_guidance(item, self.type_by_category)
                if kind == CURVE_TRACK:
                    continue
                if kind == AB_CURVE and not GS3_ABCURVE_GUIDANCE_PROVEN:
                    raise ValueError(
                        f"{ABCURVE_NOT_PROVEN}: {field_plan.talhao} {item.name} classificada como ABCurve, "
                        "mas o ABCurve importado nao foi comprovado no GS3 2630."
                    )
                raise ValueError(f"GS3_GUIDANCE_TYPE_UNKNOWN: {field_plan.talhao} {item.name} ({kind}).")
        return self.curve_track.write_field(field_plan, field_dir)
