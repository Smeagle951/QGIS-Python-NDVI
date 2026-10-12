"""Plugin QGIS FortSmart — hub import/export para monitores agricolas."""

from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox
from qgis.core import QgsApplication

from .export_ui import run_export
from .import_ui import run as run_import
from .project_export_ui import run_project_export
from .tools_ui import run_tools


def _purge_plugin_modules() -> None:
    """Reativar/atualizar o plugin sem reiniciar o QGIS precisa reimportar todos os submodulos."""
    import sys

    package = __name__.rsplit(".", 1)[0]
    for name in [n for n in sys.modules if n.startswith(f"{package}.")]:
        sys.modules.pop(name, None)


class FortSmartGs3ImportPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.actions = []
        self.menu_name = "&FortSmart"

    def initGui(self):
        icon_import = (
            QIcon(str(self._icon_path()))
            if self._icon_path().exists()
            else QgsApplication.getThemeIcon("mActionAddPolygonLayer.svg")
        )
        icon_export = QgsApplication.getThemeIcon("mActionSaveAllEdits.svg")
        icon_tools = QgsApplication.getThemeIcon("mActionOptions.svg")

        icon_project = QgsApplication.getThemeIcon("mActionFileExport.svg")

        act_project = QAction(icon_project, "Exportar Projeto FortSmart", self.iface.mainWindow())
        act_project.setStatusTip("Lote padronizado: 01_limite_externo … 13_shutoff (GPKG + SHP)")
        act_project.triggered.connect(self._on_project_export)

        act_export = QAction(icon_export, "Exportar para monitor", self.iface.mainWindow())
        act_export.setStatusTip("QGIS → GS3 / Gen4 / ISOXML / Jacto / Stara — trabalhos prontos")
        act_export.triggered.connect(self._on_export)

        act_import = QAction(icon_import, "Importar do pendrive", self.iface.mainWindow())
        act_import.setStatusTip("Pendrive → QGIS (GS3, Gen4, ISOXML, Jacto, Stara)")
        act_import.triggered.connect(self._on_import)

        act_tools = QAction(icon_tools, "Ferramentas FortSmart", self.iface.mainWindow())
        act_tools.setStatusTip("Round-trip, calibrar USB, copiar para pendrive FAT32")
        act_tools.triggered.connect(self._on_tools)

        for action in (act_project, act_export, act_import, act_tools):
            self.iface.addToolBarIcon(action)
            self.iface.addPluginToMenu(self.menu_name, action)
            self.actions.append(action)

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.menu_name, action)
            self.iface.removeToolBarIcon(action)
        self.actions = []
        _purge_plugin_modules()

    def _on_import(self):
        run_import(self.iface)

    def _on_project_export(self):
        try:
            run_project_export(self.iface)
        except Exception as exc:
            QMessageBox.critical(self.iface.mainWindow(), "FortSmart Projeto", str(exc))
            raise

    def _on_export(self):
        try:
            run_export(self.iface)
        except RuntimeError as exc:
            if str(exc) != "Export cancelado.":
                QMessageBox.warning(self.iface.mainWindow(), "FortSmart Export", str(exc))
        except Exception as exc:
            QMessageBox.critical(self.iface.mainWindow(), "FortSmart Export", str(exc))
            raise

    def _on_tools(self):
        run_tools(self.iface)

    @staticmethod
    def _icon_path():
        from pathlib import Path

        return Path(__file__).resolve().parent / "icon.svg"
