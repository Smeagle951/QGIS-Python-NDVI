from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "qgis_plugin"))

from fortsmart_gs3_import.gs3_setup import BT_NS, SETUP_NS, write_merged_setup  # noqa: E402


class Gs3SetupTest(unittest.TestCase):
    def test_native_infrastructure_is_preserved_and_identity_replaced(self):
        template = PROJECT_ROOT / "qgis_plugin" / "fortsmart_gs3_import" / "resources" / "gs3_native" / "setup_template.fds"
        with tempfile.TemporaryDirectory(prefix="fortsmart-setup-") as temp:
            output = Path(temp) / "setup.fds"
            write_merged_setup(
                template,
                output,
                client_name="Novo Proprietario",
                client_erid=str(uuid.uuid4()),
                farm_name="Nova Fazenda",
                farm_erid=str(uuid.uuid4()),
                fields=[(str(uuid.uuid4()), "TL-12", 141.08)],
            )
            text = output.read_text(encoding="utf-8")
            self.assertIn("Synchronization", text)
            self.assertIn("NodeVersions", text)
            self.assertIn("MachineType", text)
            self.assertIn("ImplementType", text)
            self.assertIn("Novo Proprietario", text)
            self.assertIn("Nova Fazenda", text)
            self.assertNotIn("SILVANDRO", text)
            self.assertNotIn("FERRADURA", text)
            root = ET.parse(output).getroot()
            fields = list(root.iter(f"{{{SETUP_NS}}}Field"))
            self.assertEqual(len(fields), 1)
            area = fields[0].find(f"{{{SETUP_NS}}}Area")
            self.assertEqual(area.attrib["sourceUOM"], "ha")
            self.assertAlmostEqual(float(area.attrib["value"]), 141.08, places=4)
            self.assertTrue(list(root.iter(f"{{{BT_NS}}}NodeVersions")))


if __name__ == "__main__":
    unittest.main()
