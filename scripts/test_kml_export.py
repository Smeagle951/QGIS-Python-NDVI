"""Execute com python-qgis-ltr.bat; usa somente dados sintéticos temporários."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import sys
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'qgis_plugin'))
from qgis.core import QgsApplication, QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry
app = QgsApplication([], False)
app.initQgis()
from fortsmart_gs3_import.kml_exporter import write_kml, write_kml_layers, NS
from fortsmart_gs3_import import project_batch_exporter as batch
from fortsmart_gs3_import.project_export_ui import ProjectExportDialog
from fortsmart_gs3_import.export_destination_ui import project_export_tree_lines
N = {'k': NS}


def layer(kind, wkts, names=None, crs='EPSG:31981'):
    result = QgsVectorLayer(f'{kind}?crs={crs}&field=nome:string', 'Talhões & Pivôs', 'memory')
    for i, wkt in enumerate(wkts):
        f = QgsFeature(result.fields())
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        f['nome'] = names[i] if names else f'Pivô {i+1}'
        assert result.dataProvider().addFeatures([f])[0]
    result.updateExtents()
    return result


POLY = 'Polygon ((500000 8200000,500100 8200000,500100 8200100,500000 8200100,500000 8200000))'


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'mapa.kml'

    def tearDown(self):
        QgsProject.instance().removeAllMapLayers()
        self.temp.cleanup()

    def test_names_area_wgs84_and_source(self):
        src = layer('Polygon', [POLY, POLY], ['Talhão <01> & Água', 'Pivô 02'])
        before = [(f.geometry().asWkt(), f.attributes()) for f in src.getFeatures()]
        write_kml(src, self.path)
        root = ET.parse(self.path)
        shapes = root.findall('.//k:Folder', N)[1].findall('k:Placemark', N)
        self.assertEqual(len(shapes), 2)
        self.assertIn('Talhão <01> & Água', shapes[0].find('k:name', N).text)
        self.assertIn(' ha', shapes[1].find('k:name', N).text)
        area = float(shapes[0].find("k:ExtendedData/k:Data[@name='area_ha']/k:value", N).text)
        self.assertAlmostEqual(area, 1.0, places=2)
        x, y, z = map(float, shapes[0].find('.//k:coordinates', N).text.split()[0].split(','))
        self.assertTrue(-58 < x < -56 and -18 < y < -15)
        self.assertEqual(len(root.findall('.//k:Folder', N)[2].findall('k:Placemark', N)), 2)
        self.assertEqual(before, [(f.geometry().asWkt(), f.attributes()) for f in src.getFeatures()])

    def test_holes_and_multipart(self):
        src = layer('MultiPolygon', ['MultiPolygon (((0 0,1 0,1 1,0 1,0 0),(.2 .2,.2 .4,.4 .4,.4 .2,.2 .2)),((2 0,3 0,3 1,2 1,2 0)))'], crs='EPSG:4326')
        write_kml(src, self.path)
        root = ET.parse(self.path)
        self.assertEqual(len(root.findall('.//k:Polygon', N)), 2)
        self.assertEqual(len(root.findall('.//k:innerBoundaryIs', N)), 1)

    def test_points_lines_and_fallback(self):
        for kind, wkt in [('Point', 'Point (-57 -16)'), ('LineString', 'LineString (-57 -16,-57.1 -16.1)'), ('MultiPoint', 'MultiPoint ((-57 -16),(-57.1 -16.1))')]:
            src = layer(kind, [wkt], [None], crs='EPSG:4326')
            write_kml(src, self.path)
            root = ET.parse(self.path)
            self.assertEqual(root.find('.//k:Placemark/k:name', N).text, src.name())
            self.assertIsNone(root.find(".//k:Data[@name='area_ha']", N))

    def test_failure_preserves_destination(self):
        self.path.write_text('previous', encoding='utf-8')
        src = layer('Polygon', [POLY])
        src.setCrs(__import__('qgis.core', fromlist=['QgsCoordinateReferenceSystem']).QgsCoordinateReferenceSystem())
        with self.assertRaises(ValueError):
            write_kml(src, self.path)
        self.assertEqual(self.path.read_text(), 'previous')

    def test_export_modes(self):
        src = layer('Polygon', [POLY])
        QgsProject.instance().addMapLayer(src)
        with patch.object(batch, 'resolve_source_layers', return_value=[src]):
            result = batch.export_active_layers_direct(Path(self.temp.name), export_gpkg=False, export_shp=False, export_kml=True)
        self.assertEqual(result.errors, [])
        self.assertEqual(len([f for f in result.exported_files if f.endswith('.kml')]), 1)
        src.setName('01_Limite_Externo')
        result = batch.export_project_batch(Path(self.temp.name), selected_orders=[1], source_mode=batch.LayerSourceMode.EXACT_NAMES, export_gpkg=False, export_shp=False, export_kml=True)
        self.assertEqual(result.errors, [])
        self.assertIn('fortsmart_projeto.kml', result.exported_files)

    def test_multiple_layers_in_one_kml(self):
        first = layer('Polygon', [POLY], ['Talhão 01'])
        first.setName('Talhões')
        second = layer('Point', ['Point (-57 -16)'], ['Sede'], crs='EPSG:4326')
        second.setName('Construções')
        write_kml_layers([first, second], self.path)
        root = ET.parse(self.path)
        top_folders = root.findall('./k:Document/k:Folder', N)
        self.assertEqual([f.find('k:name', N).text for f in top_folders], ['Talhões', 'Construções'])
        self.assertEqual(len(root.findall('.//k:Placemark', N)), 3)

    def test_invalid_layer_does_not_block_unified_kml(self):
        valid = layer('Point', ['Point (-57 -16)'], ['Sede'], crs='EPSG:4326')
        invalid = layer('Polygon', [POLY])
        invalid.setCrs(__import__('qgis.core', fromlist=['QgsCoordinateReferenceSystem']).QgsCoordinateReferenceSystem())
        warnings = write_kml_layers([invalid, valid], self.path, strict=False)
        self.assertEqual(len(warnings), 1)
        self.assertTrue(self.path.exists())
        self.assertEqual(len(ET.parse(self.path).findall('.//k:Placemark', N)), 1)

    def test_dialog_and_preview(self):
        class Iface:
            def mainWindow(self):
                return None
        dlg = ProjectExportDialog(Iface())
        dlg.fmt_gpkg.setChecked(False)
        dlg.fmt_shp.setChecked(False)
        dlg.fmt_kml.setChecked(True)
        self.assertTrue(dlg.fmt_kml.isChecked())
        self.assertTrue(dlg.shp_separate.isChecked())
        dlg.shp_unified.setChecked(True)
        self.assertTrue(dlg.shp_unified.isChecked())
        lines = project_export_tree_lines([1], False, False, False, batch.EXPORT_LAYERS, export_kml=True)
        self.assertEqual(sum('.kml' in line for line in lines), 1)
        self.assertTrue(any('fortsmart_projeto.kml' in line for line in lines))
        dlg.close()

    def test_unified_shapefiles_by_geometry_type(self):
        polygon = layer('Polygon', [POLY], ['Talhão 01'])
        polygon.setName('Talhões')
        line = layer('LineString', ['LineString (-57 -16,-57.1 -16.1)'], ['Estrada'], crs='EPSG:4326')
        line.setName('Estradas')
        point = layer('Point', ['Point (-57 -16)'], ['Sede'], crs='EPSG:4326')
        point.setName('Construções')
        with patch.object(batch, 'resolve_source_layers', return_value=[polygon, line, point]):
            result = batch.export_active_layers_direct(
                Path(self.temp.name), export_gpkg=False, export_shp=True,
                shp_mode='unified', export_kml=False,
            )
        self.assertEqual(result.errors, [])
        expected = {'fortsmart_poligonos.shp', 'fortsmart_linhas.shp', 'fortsmart_pontos.shp'}
        self.assertEqual({name for name in result.exported_files if name.endswith('.shp')}, expected)
        for name in expected:
            reopened = QgsVectorLayer(str(Path(self.temp.name) / name), 'check', 'ogr')
            self.assertTrue(reopened.isValid(), name)
            self.assertEqual(reopened.featureCount(), 1)
            self.assertIn('camada', reopened.fields().names())
            self.assertIn('nome', reopened.fields().names())

    def test_all_formats_together(self):
        src = layer('Polygon', [POLY])
        with patch.object(batch, 'resolve_source_layers', return_value=[src]):
            result = batch.export_active_layers_direct(Path(self.temp.name), export_geojson=True, export_kml=True)
        self.assertEqual(result.errors, [])
        for extension in ('.gpkg', '.shp', '.geojson', '.kml'):
            files = [f for f in result.exported_files if f.endswith(extension)]
            self.assertEqual(len(files), 1)
            if extension == '.kml':
                self.assertGreater(len(ET.parse(Path(self.temp.name) / files[0]).findall('.//k:Placemark', N)), 0)
                continue
            reopened = QgsVectorLayer(str(Path(self.temp.name) / files[0]), 'check', 'ogr')
            self.assertTrue(reopened.isValid(), extension)
            self.assertGreater(reopened.featureCount(), 0)
            del reopened


if __name__ == '__main__':
    unittest.main()
