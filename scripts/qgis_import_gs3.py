"""
FortSmart — importar GS3 2630 (Console Python).

Preferivel: instale o plugin (scripts/install_fortsmart_gs3_plugin.ps1)
e use o botao na barra de ferramentas do QGIS.

Console Python (alternativa):
  exec(compile(open(r"C:/Users/fortu/Desktop/QGIS_Python_NDVI/scripts/qgis_import_gs3.py", encoding="utf-8").read(), "qgis_import_gs3.py", "exec"))
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

if importlib.util.find_spec("qgis") is None:
    print("FortSmart GS3 Import: execute dentro do Console Python do QGIS.")
    sys.exit(0)

PROJECT_DIR = Path(__file__).resolve().parent.parent
PLUGIN_DIR = PROJECT_DIR / "qgis_plugin" / "fortsmart_gs3_import"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

try:
    from qgis.utils import iface as qgis_iface
except ImportError:
    qgis_iface = None

from import_ui import run


def _autostart() -> None:
    iface = globals().get("iface") or qgis_iface
    if iface is None:
        from qgis.PyQt.QtWidgets import QMessageBox
        QMessageBox.critical(None, "FortSmart GS3", "iface QGIS indisponivel.")
        return
    run(iface)


_autostart()
