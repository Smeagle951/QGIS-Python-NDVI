"""FortSmart — utilitarios Shapefile (Jacto/Stara) com ou sem QGIS."""

from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Any


def safe_folder_name(name: str, fallback: str = "SemNome") -> str:
    text = " ".join((name or fallback).split())
    text = re.sub(r'[<>:"/\\|?*]', "_", text)
    return text[:60] if text else fallback


def _write_prj_wgs84(path: Path) -> None:
    path.write_text(
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]',
        encoding="utf-8",
    )


def _write_shp_polygons(path: Path, rings_list: list[list[tuple[float, float]]]) -> None:
    """Escritor shapefile poligono minimo (WGS84 lon/lat) — sem dependencias."""
    if not rings_list:
        return
    records: list[list[tuple[float, float]]] = []
    for ring in rings_list:
        pts = list(ring)
        if len(pts) < 3:
            continue
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        records.append(pts)
    if not records:
        return

    shp_path = path.with_suffix(".shp")
    shx_path = path.with_suffix(".shx")
    dbf_path = path.with_suffix(".dbf")

    all_x = [p[0] for ring in records for p in ring]
    all_y = [p[1] for ring in records for p in ring]
    xmin, xmax = min(all_x), max(all_x)
    ymin, ymax = min(all_y), max(all_y)

    shp_parts: list[bytes] = []
    shx_parts: list[bytes] = []
    offset = 50

    for ring in records:
        pts = ring
        num_pts = len(pts)
        content_length = 24 + num_pts * 8
        record = struct.pack("<ii", 1, content_length)
        record += struct.pack("<i", 5)
        record += struct.pack("<4d", xmin, ymin, xmax, ymax)
        record += struct.pack("<2i", 1, num_pts)
        for lon, lat in pts:
            record += struct.pack("<2d", lon, lat)
        shp_parts.append(record)
        shx_parts.append(struct.pack(">ii", offset, content_length))
        offset += 4 + content_length

    file_length = offset
    header = struct.pack(
        "<iiiiiiiii",
        9994,
        0,
        0,
        0,
        0,
        0,
        file_length,
        1000,
        5,
    )
    header += struct.pack("<4d", xmin, ymin, xmax, ymax)
    header += b"\x00" * 50
    shp_path.write_bytes(header + b"".join(shp_parts))
    shx_header = struct.pack("<iiiiiiiii", 9994, 0, 0, 0, 0, 0, 50 + len(records) * 4, 1000, 5)
    shx_header += struct.pack("<4d", xmin, ymin, xmax, ymax)
    shx_header += b"\x00" * 50
    shx_path.write_bytes(shx_header + b"".join(shx_parts))

    num = len(records)
    dbf = bytearray()
    dbf.extend(b"\x03" + struct.pack("<3I", 24 + 32 * num - 1, num, 33)[1:])
    dbf.extend(b"NAME".ljust(11, b" ") + b"C" + struct.pack("<2I", 0, 40))
    dbf.extend(b"\x0d")
    for i in range(num):
        dbf.extend(b" " + f"F{i+1}".ljust(40)[:40].encode("ascii", errors="replace"))
    dbf_path.write_bytes(dbf)
    _write_prj_wgs84(path.with_suffix(".prj"))


def _write_shp_lines(path: Path, lines: list[list[tuple[float, float]]]) -> None:
    if not lines:
        return
    records = [ln for ln in lines if len(ln) >= 2]
    if not records:
        return

    shp_path = path.with_suffix(".shp")
    shx_path = path.with_suffix(".shx")
    dbf_path = path.with_suffix(".dbf")

    all_x = [p[0] for ln in records for p in ln]
    all_y = [p[1] for ln in records for p in ln]
    xmin, xmax = min(all_x), max(all_x)
    ymin, ymax = min(all_y), max(all_y)

    shp_parts: list[bytes] = []
    shx_parts: list[bytes] = []
    offset = 50

    for idx, pts in enumerate(records, start=1):
        num_pts = len(pts)
        content_length = 24 + num_pts * 8
        record = struct.pack("<ii", idx, content_length)
        record += struct.pack("<i", 3)
        record += struct.pack("<4d", xmin, ymin, xmax, ymax)
        record += struct.pack("<2i", 1, num_pts)
        for lon, lat in pts:
            record += struct.pack("<2d", lon, lat)
        shp_parts.append(record)
        shx_parts.append(struct.pack(">ii", offset, content_length))
        offset += 4 + content_length

    file_length = offset
    header = struct.pack("<iiiiiiiii", 9994, 0, 0, 0, 0, 0, file_length, 1000, 3)
    header += struct.pack("<4d", xmin, ymin, xmax, ymax)
    header += b"\x00" * 50
    shp_path.write_bytes(header + b"".join(shp_parts))
    shx_header = struct.pack("<iiiiiiiii", 9994, 0, 0, 0, 0, 0, 50 + len(records) * 4, 1000, 3)
    shx_header += struct.pack("<4d", xmin, ymin, xmax, ymax)
    shx_header += b"\x00" * 50
    shx_path.write_bytes(shx_header + b"".join(shx_parts))

    num = len(records)
    dbf = bytearray()
    dbf.extend(b"\x03" + struct.pack("<3I", 24 + 32 * num - 1, num, 33)[1:])
    dbf.extend(b"NAME".ljust(11, b" ") + b"C" + struct.pack("<2I", 0, 40))
    dbf.extend(b"\x0d")
    for i in range(num):
        dbf.extend(b" " + f"L{i+1}".ljust(40)[:40].encode("ascii", errors="replace"))
    dbf_path.write_bytes(dbf)
    _write_prj_wgs84(path.with_suffix(".prj"))


def _make_qgis_fields(field_names: list[str]):
    from qgis.core import QgsField, QgsFields
    from qgis.PyQt.QtCore import QMetaType

    fields = QgsFields()
    for name in field_names:
        fields.append(QgsField(name, QMetaType.Type.QString))
    return fields


def _attrs_for_record(attrs: list[dict[str, Any]] | None, idx: int, field_names: list[str], default_name: str) -> list[str]:
    source = attrs[idx] if attrs and idx < len(attrs) else {}
    values: list[str] = []
    for name in field_names:
        value = source.get(name, default_name if name.upper() == "NAME" or name == "CODE" else "")
        values.append("" if value is None else str(value))
    return values


def _write_cpg(path: Path) -> None:
    path.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")


def _write_fiona_polygons(
    path: Path,
    rings_list: list[list[tuple[float, float]]],
    field_names: list[str],
    attrs: list[dict[str, Any]] | None,
    geometry_type: str,
) -> bool:
    try:
        import fiona
        from fiona.crs import CRS
    except Exception:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    schema = {"geometry": geometry_type, "properties": {name: "str:254" for name in field_names}}
    shp_path = str(path.with_suffix(".shp"))
    with fiona.open(
        shp_path,
        "w",
        driver="ESRI Shapefile",
        crs=CRS.from_epsg(4326),
        schema=schema,
        encoding="UTF-8",
    ) as dst:
        for idx, ring in enumerate(rings_list):
            pts = list(ring)
            if len(pts) < 3:
                continue
            if pts[0] != pts[-1]:
                pts.append(pts[0])
            props = dict(zip(field_names, _attrs_for_record(attrs, idx, field_names, f"F{idx+1}")))
            geom = (
                {"type": "MultiPolygon", "coordinates": [[pts]]}
                if geometry_type == "MultiPolygon"
                else {"type": "Polygon", "coordinates": [pts]}
            )
            dst.write({"type": "Feature", "geometry": geom, "properties": props})
    _write_cpg(path)
    return True


def _write_fiona_lines(
    path: Path,
    lines: list[list[tuple[float, float]]],
    field_names: list[str],
    attrs: list[dict[str, Any]] | None,
) -> bool:
    try:
        import fiona
        from fiona.crs import CRS
    except Exception:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    schema = {"geometry": "LineString", "properties": {name: "str:254" for name in field_names}}
    shp_path = str(path.with_suffix(".shp"))
    with fiona.open(
        shp_path,
        "w",
        driver="ESRI Shapefile",
        crs=CRS.from_epsg(4326),
        schema=schema,
        encoding="UTF-8",
    ) as dst:
        for idx, line in enumerate(lines):
            if len(line) < 2:
                continue
            props = dict(zip(field_names, _attrs_for_record(attrs, idx, field_names, f"L{idx+1}")))
            dst.write(
                {
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": list(line)},
                    "properties": props,
                }
            )
    _write_cpg(path)
    return True


def write_shapefile_polygons(
    path: Path,
    rings_list: list[list[tuple[float, float]]],
    *,
    field_names: list[str] | None = None,
    attrs: list[dict[str, Any]] | None = None,
    geometry_type: str = "Polygon",
) -> None:
    try:
        from qgis.core import (
            QgsCoordinateReferenceSystem,
            QgsFeature,
            QgsGeometry,
            QgsPointXY,
            QgsVectorFileWriter,
        )

        path.parent.mkdir(parents=True, exist_ok=True)
        base = str(path.with_suffix(""))
        field_names = field_names or ["NAME"]
        fields = _make_qgis_fields(field_names)
        wkb_type = __import__("qgis.core", fromlist=["Qgis"]).Qgis.WkbType.MultiPolygon if geometry_type == "MultiPolygon" else __import__("qgis.core", fromlist=["Qgis"]).Qgis.WkbType.Polygon
        writer = QgsVectorFileWriter(
            base + ".shp",
            "UTF-8",
            fields,
            wkb_type,
            QgsCoordinateReferenceSystem("EPSG:4326"),
            "ESRI Shapefile",
        )
        for i, ring in enumerate(rings_list):
            if len(ring) < 3:
                continue
            pts = [QgsPointXY(lon, lat) for lon, lat in ring]
            if pts[0] != pts[-1]:
                pts.append(pts[0])
            feat = QgsFeature(fields)
            geom = QgsGeometry.fromPolygonXY([pts])
            if geometry_type == "MultiPolygon":
                geom = QgsGeometry.fromMultiPolygonXY([[pts]])
            feat.setGeometry(geom)
            feat.setAttributes(_attrs_for_record(attrs, i, field_names, f"F{i+1}"))
            writer.addFeature(feat)
        del writer
        _write_cpg(path)
    except Exception:
        field_names = field_names or ["NAME"]
        if _write_fiona_polygons(path, rings_list, field_names, attrs, geometry_type):
            return
        _write_shp_polygons(path, rings_list)


def write_shapefile_lines(
    path: Path,
    lines: list[list[tuple[float, float]]],
    *,
    field_names: list[str] | None = None,
    attrs: list[dict[str, Any]] | None = None,
) -> None:
    try:
        from qgis.core import (
            QgsCoordinateReferenceSystem,
            QgsFeature,
            QgsGeometry,
            QgsPointXY,
            QgsVectorFileWriter,
        )

        path.parent.mkdir(parents=True, exist_ok=True)
        base = str(path.with_suffix(""))
        field_names = field_names or ["NAME"]
        fields = _make_qgis_fields(field_names)
        writer = QgsVectorFileWriter(
            base + ".shp",
            "UTF-8",
            fields,
            __import__("qgis.core", fromlist=["Qgis"]).Qgis.WkbType.LineString,
            QgsCoordinateReferenceSystem("EPSG:4326"),
            "ESRI Shapefile",
        )
        for i, line in enumerate(lines):
            if len(line) < 2:
                continue
            feat = QgsFeature(fields)
            feat.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(lon, lat) for lon, lat in line]))
            feat.setAttributes(_attrs_for_record(attrs, i, field_names, f"L{i+1}"))
            writer.addFeature(feat)
        del writer
        _write_cpg(path)
    except Exception:
        field_names = field_names or ["NAME"]
        if _write_fiona_lines(path, lines, field_names, attrs):
            return
        _write_shp_lines(path, lines)
