"""Gera SHPs unificados por tipo das camadas visíveis de um projeto salvo."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'qgis_plugin'))
from qgis.core import QgsApplication, QgsProject

app = QgsApplication([], False)
app.initQgis()
try:
    project_path = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])
    project = QgsProject.instance()
    if not project.read(str(project_path)):
        raise RuntimeError(f'Não foi possível abrir o projeto: {project_path}')
    from fortsmart_gs3_import.project_batch_exporter import export_active_layers_direct
    from fortsmart_gs3_import.qgis_layer_source import LayerSourceMode
    result = export_active_layers_direct(
        output_dir,
        source_mode=LayerSourceMode.VISIBLE,
        export_gpkg=False,
        export_shp=True,
        shp_mode='unified',
        export_geojson=False,
        export_kml=False,
        write_manifest=False,
    )
    print(f'ARQUIVOS={result.exported_files}')
    print(f'AVISOS={result.errors}')
    if not any(name.endswith('.shp') for name in result.exported_files):
        raise RuntimeError('Nenhum Shapefile unificado foi criado.')
finally:
    QgsProject.instance().clear()
    app.exitQgis()
