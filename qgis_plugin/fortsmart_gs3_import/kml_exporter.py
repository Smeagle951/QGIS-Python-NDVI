"""KML WGS84 com identificação e área por feição, sem editar a origem."""

import json
import math
import os
from pathlib import Path
import tempfile
import unicodedata
import xml.etree.ElementTree as ET

NS = 'http://www.opengis.net/kml/2.2'
ET.register_namespace('', NS)


def _element(parent, tag, text=None, **attrs):
    node = ET.SubElement(parent, '{%s}%s' % (NS, tag), attrs)
    if text is not None:
        node.text = str(text)
    return node


def _coordinates(points):
    values = []
    for point in points:
        x, y = point[:2]
        if not (math.isfinite(x) and math.isfinite(y) and -180 <= x <= 180 and -90 <= y <= 90):
            raise ValueError('Coordenadas inválidas após transformar para WGS84.')
        values.append(f'{x:.12f},{y:.12f},0')
    return ' '.join(values)


def _geometry(parent, geom):
    kind = geom['type']
    coords = geom.get('coordinates')
    if kind in ('Point', 'LineString'):
        node = _element(parent, kind)
        _element(node, 'altitudeMode', 'clampToGround')
        _element(node, 'coordinates', _coordinates([coords] if kind == 'Point' else coords))
    elif kind == 'Polygon':
        node = _element(parent, 'Polygon')
        _element(node, 'altitudeMode', 'clampToGround')
        for index, ring in enumerate(coords):
            boundary = _element(node, 'outerBoundaryIs' if index == 0 else 'innerBoundaryIs')
            _element(_element(boundary, 'LinearRing'), 'coordinates', _coordinates(ring))
    elif kind in ('MultiPoint', 'MultiLineString', 'MultiPolygon', 'GeometryCollection'):
        node = _element(parent, 'MultiGeometry')
        parts = geom.get('geometries') if kind == 'GeometryCollection' else (
            {'type': kind[5:], 'coordinates': part} for part in coords)
        for part in parts:
            _geometry(node, part)
    else:
        raise ValueError(f'Tipo de geometria não suportado no KML: {kind}')


def _feature_name(layer, feature):
    from qgis.core import QgsVariantUtils

    def norm(value):
        return ''.join(c for c in unicodedata.normalize('NFKD', value.casefold())
                       if c.isalnum() and not unicodedata.combining(c))

    fields = {norm(field.name()): field.name() for field in layer.fields()}
    for candidate in ('nome', 'name', 'nometalhao', 'nomepivo', 'talhao', 'pivo', 'fieldname', 'blockname', 'label'):
        field = fields.get(candidate)
        if field:
            value = feature[field]
            if not QgsVariantUtils.isNull(value) and str(value).strip():
                return str(value).strip()
    return layer.name() if layer.featureCount() == 1 else f'{layer.name()} — {feature.id()}'


def _append_feature(layer, feature, shapes, labels, transform, measure):
    from qgis.core import QgsGeometry, QgsUnitTypes, QgsWkbTypes

    geom = QgsGeometry(feature.geometry())
    if geom.isNull() or geom.isEmpty():
        raise ValueError(f'Feição {feature.id()} sem geometria.')
    name = _feature_name(layer, feature)
    area = None
    label_point = None
    if geom.type() == QgsWkbTypes.PolygonGeometry:
        if not geom.isGeosValid():
            raise ValueError(f'Feição {feature.id()} com polígono inválido.')
        area = measure.convertAreaMeasurement(measure.measureArea(geom), QgsUnitTypes.AreaHectares)
        if not math.isfinite(area) or area <= 0:
            raise ValueError(f'Área inválida na feição {feature.id()}.')
        label_point = geom.pointOnSurface()
        if label_point.isEmpty() or label_point.transform(transform) != 0:
            raise ValueError(f'Não foi possível posicionar o nome da feição {feature.id()}.')
    if geom.transform(transform) != 0:
        raise ValueError(f'Não foi possível transformar a feição {feature.id()} para WGS84.')
    area_text = f'{area:.2f}'.replace('.', ',') if area is not None else None
    title = name if area is None else f'{name} — {area_text} ha'
    placemark = _element(shapes, 'Placemark')
    _element(placemark, 'name', title)
    _element(placemark, 'styleUrl', '#area')
    _element(placemark, 'description', f'Nome: {name}\nCamada: {layer.name()}' +
             (f'\nÁrea: {area:.4f} ha' if area is not None else ''))
    data = _element(placemark, 'ExtendedData')
    for key, value in [('nome', name), ('camada', layer.name()), ('id_origem', feature.id())] + (
            [('area_ha', f'{area:.8f}')] if area is not None else []):
        _element(_element(data, 'Data', name=key), 'value', value)
    _geometry(placemark, json.loads(geom.asJson(15)))
    if label_point is not None:
        label = _element(labels, 'Placemark')
        _element(label, 'name', title)
        _element(label, 'styleUrl', '#label')
        _geometry(label, json.loads(label_point.asJson(15)))


def _append_layer(document, layer, strict=True):
    from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                           QgsDistanceArea, QgsGeometry, QgsProject, QgsUnitTypes,
                           QgsWkbTypes)

    if not layer.crs().isValid():
        raise ValueError('A camada precisa de um CRS válido para exportar KML.')
    project = QgsProject.instance()
    transform = QgsCoordinateTransform(layer.crs(), QgsCoordinateReferenceSystem('EPSG:4326'), project)
    measure = QgsDistanceArea()
    measure.setSourceCrs(layer.crs(), project.transformContext())
    ellipsoid = project.ellipsoid()
    measure.setEllipsoid(ellipsoid if ellipsoid and ellipsoid != 'NONE' else 'WGS84')
    layer_folder = _element(document, 'Folder')
    _element(layer_folder, 'name', layer.name())
    shapes = _element(layer_folder, 'Folder')
    _element(shapes, 'name', 'Feições')
    labels = _element(layer_folder, 'Folder')
    _element(labels, 'name', 'Nomes e áreas')
    count = 0
    warnings = []
    for feature in layer.getFeatures():
        try:
            _append_feature(layer, feature, shapes, labels, transform, measure)
        except Exception as exc:
            if strict:
                raise
            warnings.append(str(exc))
            continue
        count += 1
    if not count:
        raise ValueError('Camada vazia, sem feições para exportar.')
    return warnings


def write_kml_layers(layers, destination, document_name='FortSmart Projeto', strict=True):
    layers = list(layers)
    if not layers:
        raise ValueError('Nenhuma camada com feições para exportar no KML.')
    root = ET.Element('{%s}kml' % NS)
    document = _element(root, 'Document')
    _element(document, 'name', document_name)
    style = _element(document, 'Style', id='area')
    _element(_element(style, 'LineStyle'), 'color', 'ff00aaff')
    _element(_element(style, 'PolyStyle'), 'color', '5500aaff')
    label_style = _element(document, 'Style', id='label')
    _element(_element(label_style, 'IconStyle'), 'scale', '0')
    _element(_element(label_style, 'LabelStyle'), 'scale', '1')
    warnings = []
    appended = 0
    for layer in layers:
        temporary_document = ET.Element('{%s}Document' % NS)
        try:
            feature_warnings = _append_layer(temporary_document, layer, strict=strict)
        except Exception as exc:
            if strict:
                raise
            warnings.append(f'{layer.name()}: {exc}')
            continue
        for folder in list(temporary_document):
            document.append(folder)
        warnings.extend(f'{layer.name()}: {warning}' for warning in feature_warnings)
        appended += 1
    if not appended:
        details = '; '.join(warnings) if warnings else 'nenhuma camada válida'
        raise ValueError(f'Não foi possível exportar nenhuma camada para KML: {details}')
    destination = Path(destination)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.kml', delete=False) as stream:
            temp_path = stream.name
            ET.ElementTree(root).write(stream, encoding='utf-8', xml_declaration=True)
        os.replace(temp_path, destination)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
    return warnings


def write_kml(layer, destination):
    """Compatibilidade: grava uma camada usando a mesma estrutura agrupada."""
    write_kml_layers([layer], destination, layer.name())
