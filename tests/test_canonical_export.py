from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "qgis_plugin"))

from fortsmart_gs3_import.canonical_project import (  # noqa: E402
    CanonicalProjectBuilder,
    canonical_feature_allowed,
    role_from_layer_name,
    role_from_legacy_name,
    role_to_spatial_kind,
)
from fortsmart_gs3_import.gs3_spatial_types import (  # noqa: E402
    Gs3SpatialFeature,
    SpatialKind,
)
from fortsmart_gs3_import.adapters.sendto2020_writer import SendTo2020Writer  # noqa: E402
from fortsmart_gs3_import.gs3_spatial_types import Gs3FieldBundle  # noqa: E402


class FakeLayer:
    def __init__(self, name: str, properties: dict[str, str] | None = None):
        self._name = name
        self._properties = properties or {}

    def name(self):
        return self._name

    def set_name(self, name: str):
        self._name = name

    def customProperty(self, key: str, default=""):
        return self._properties.get(key, default)

    def fields(self):
        raise RuntimeError("No fields in unit-test layer")


def feature(role: str, kind: SpatialKind) -> Gs3SpatialFeature:
    return Gs3SpatialFeature(
        kind=kind,
        cliente="Cliente",
        fazenda="Fazenda",
        talhao="Talhao",
        field_erid="field-1",
        item_erid="item-1",
        name="Item",
        source_file="unit-test",
        catalog_file="qgis",
        geometry="line",
        extra={"canonical_role": role},
    )


class CanonicalProjectTest(unittest.TestCase):
    def test_role_survives_display_name_change(self):
        layer = FakeLayer("12_Passadas_Plantio", {"fortsmart_role": "WORK_PATH"})
        layer.set_name("Passadas de Plantio")
        ref = CanonicalProjectBuilder().resolve_layer(layer)
        self.assertIsNotNone(ref)
        self.assertEqual(ref.role, "WORK_PATH")
        self.assertEqual(ref.resolution, "fortsmart_role")

    def test_internal_key_precedes_legacy_name(self):
        layer = FakeLayer(
            "Boundary",
            {"fortsmart_internal_key": "06_Passadas_Plantio"},
        )
        ref = CanonicalProjectBuilder().resolve_layer(layer)
        self.assertEqual(ref.role, "WORK_PATH")
        self.assertEqual(ref.resolution, "internal_key")

    def test_legacy_aliases_remain_supported(self):
        self.assertEqual(role_from_legacy_name("06_Passadas_Plantio"), "WORK_PATH")
        self.assertEqual(role_from_legacy_name("Boundary — Talhao 12"), "EXTERNAL_BOUNDARY")
        self.assertEqual(role_from_legacy_name("Flag{12345678-1234-1234-1234-123456789012}"), "OBSTACLE")

    def test_free_layer_names_resolve_to_roles(self):
        cases = {
            ("31_linhas_finais", "line"): "FINAL_WORK_PATH",
            ("15_passadas_bordadura", "line"): "FIELD_HEADLAND_PATH",
            ("03_bordadura", "polygon"): "HEADLAND",
            ("bordadura", "line"): "FIELD_HEADLAND_PATH",
            ("01_limite_externo", "polygon"): "EXTERNAL_BOUNDARY",
            ("02_limite_interno", "polygon"): "INTERNAL_BOUNDARY",
            ("marcacao_10_epsg_31981", "line"): "MARKER",
            ("marcacao_marcacao_5_epsg_31981", "line"): "MARKER",
            ("Linha Plantio", "line"): "WORK_PATH",
            ("linhas de plantio (EPSG:31981)", "line"): "WORK_PATH",
            ("Plantio TH-11", "line"): "WORK_PATH",
            ("Trabalho", "line"): "WORK_PATH",
            ("boundary", "polygon"): "EXTERNAL_BOUNDARY",
            ("boundary_3", "polygon"): "EXTERNAL_BOUNDARY",
            ("Limite", "polygon"): "EXTERNAL_BOUNDARY",
            ("Pivô 2", "polygon"): "EXTERNAL_BOUNDARY",
            ("Perímetro talhão", "polygon"): "EXTERNAL_BOUNDARY",
            ("Limite operacional", "polygon"): "OPERATIONAL_BOUNDARY",
            ("Reta AB", "line"): "AB_LINE",
            ("Linha AB 1", "line"): "AB_LINE",
            ("Curva AB", "line"): "AB_CURVE",
            ("Obstáculos", "point"): "OBSTACLE",
            ("pedras", "point"): "OBSTACLE",
        }
        for (name, geometry), role in cases.items():
            with self.subTest(name=name, geometry=geometry):
                self.assertEqual(role_from_layer_name(name, geometry), role)

    def test_name_never_contradicts_geometry(self):
        self.assertEqual(role_from_layer_name("pivo_marcacao", "line"), "MARKER")
        self.assertEqual(role_from_layer_name("pivo_marcacao", "polygon"), "EXTERNAL_BOUNDARY")
        self.assertEqual(role_from_layer_name("limite", "line"), "")
        self.assertEqual(role_from_layer_name("pivo", "point"), "")
        self.assertEqual(role_from_layer_name("plantio", "polygon"), "")
        self.assertEqual(role_from_layer_name("Linha Plantio", "point"), "")
        self.assertEqual(role_from_layer_name("camada 1", "line"), "")

    def test_role_maps_to_monitor_spatial_kind(self):
        self.assertEqual(role_to_spatial_kind("EXTERNAL_BOUNDARY", "polygon"), SpatialKind.LIMITE)
        self.assertEqual(role_to_spatial_kind("AB_LINE", "line"), SpatialKind.LINHA_AB)
        self.assertEqual(role_to_spatial_kind("OBSTACLE", "polygon"), SpatialKind.OBSTACULO_AREA)

    def test_strict_role_filter_blocks_work_path_as_guidance(self):
        allowed = {"AB_LINE", "AB_CURVE", "GUIDANCE_TRACK"}
        legacy = {SpatialKind.TRABALHO.value, SpatialKind.LINHA_AB.value}
        self.assertFalse(
            canonical_feature_allowed(
                feature("WORK_PATH", SpatialKind.TRABALHO),
                allowed_roles=allowed,
                legacy_kinds=legacy,
            )
        )
        self.assertTrue(
            canonical_feature_allowed(
                feature("AB_LINE", SpatialKind.LINHA_AB),
                allowed_roles=allowed,
                legacy_kinds=legacy,
            )
        )

    def test_sendto2020_includes_only_external_boundary(self):
        bundle = Gs3FieldBundle(
            cliente="Cliente",
            fazenda="Fazenda",
            talhao="TL-12",
            field_erid="field",
            features=[
                feature("EXTERNAL_BOUNDARY", SpatialKind.LIMITE),
                feature("INTERNAL_BOUNDARY", SpatialKind.LIMITE_INTERNO),
                feature("HEADLAND", SpatialKind.BORDADURA),
                feature("FINAL_WORK_PATH", SpatialKind.TRABALHO),
                feature("WORK_OFF", SpatialKind.TRABALHO),
                feature("WORK_ON", SpatialKind.TRABALHO),
            ],
        )
        filtered = SendTo2020Writer()._filter_bundles([bundle])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(len(filtered[0].features), 1)
        self.assertEqual(filtered[0].features[0].extra["canonical_role"], "EXTERNAL_BOUNDARY")


if __name__ == "__main__":
    unittest.main()
