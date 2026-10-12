"""Sonda de engenharia reversa dos arquivos John Deere GS3 (.fdShape / SpatialCatalog).

Valida a decodificacao binaria contra os arquivos Gen4 (.gjson) do mesmo UUID,
que servem como verdade de campo.
"""

from __future__ import annotations

import json
import math
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HEADER_SIZE = 64
RECORD_SIZE = 20
COUNT_OFFSET = 0x38


def local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def norm_guid(value: str) -> str:
    return (value or "").strip().strip("{}").lower()


def read_records(path: Path) -> list[tuple[float, float, float]]:
    data = path.read_bytes()
    if len(data) < HEADER_SIZE + RECORD_SIZE:
        return []
    count = struct.unpack("<q", data[COUNT_OFFSET : COUNT_OFFSET + 8])[0]
    available = (len(data) - HEADER_SIZE) // RECORD_SIZE
    if count <= 0 or count > available:
        count = available
    out = []
    for i in range(count):
        off = HEADER_SIZE + i * RECORD_SIZE
        out.append(struct.unpack("<ddf", data[off : off + RECORD_SIZE]))
    return out


def index_catalogs(eic_root: Path) -> dict[str, dict]:
    """Mapeia fileName -> metadados do SpatialCatalog."""
    index: dict[str, dict] = {}
    for cat in eic_root.rglob("*.SpatialCatalog"):
        try:
            root = ET.parse(cat).getroot()
        except ET.ParseError:
            continue
        field_erid = ""
        for el in root.iter():
            if local(el.tag) == "SpatialItems":
                field_erid = norm_guid(el.attrib.get("eridFieldRef", ""))
        for el in root.iter():
            file_name = el.attrib.get("fileName", "")
            if not file_name:
                continue
            entry = {
                "tag": local(el.tag),
                "erid": norm_guid(el.attrib.get("erid", "")),
                "name": el.attrib.get("name", ""),
                "field_erid": field_erid,
                "catalog": str(cat),
                "dir": str(cat.parent),
                "geom_type": el.attrib.get("spatialGeometryType", ""),
                "ref_lon": None,
                "ref_lat": None,
                "east_shift_mm": 0.0,
                "north_shift_mm": 0.0,
                "mbr": None,
            }
            for child in el.iter():
                ctag = local(child.tag)
                val = child.attrib.get("value")
                if ctag == "MBR":
                    try:
                        entry["mbr"] = (
                            float(child.attrib["west"]),
                            float(child.attrib["south"]),
                            float(child.attrib["east"]),
                            float(child.attrib["north"]),
                        )
                    except (KeyError, ValueError):
                        pass
                elif ctag == "vrReferenceLongitude" and val:
                    entry["ref_lon"] = float(val)
                elif ctag == "vrReferenceLatitude" and val:
                    entry["ref_lat"] = float(val)
                elif ctag == "vrEastShiftComponent" and val:
                    entry["east_shift_mm"] = float(val)
                elif ctag == "vrNorthShiftComponent" and val:
                    entry["north_shift_mm"] = float(val)
            index[file_name] = entry
    return index


def _inside(mbr, lon: float, lat: float, pad: float = 1e-6) -> bool:
    west, south, east, north = mbr
    return (west - pad) <= lon <= (east + pad) and (south - pad) <= lat <= (north + pad)


def _dedup(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for p in points:
        if not out or abs(p[0] - out[-1][0]) > 1e-13 or abs(p[1] - out[-1][1]) > 1e-13:
            out.append(p)
    return out


def decode_shape(path: Path, entry: dict) -> tuple[str, list[tuple[float, float]]]:
    """Devolve (modo, pontos_absolutos_lon_lat).

    O MBR do SpatialCatalog e o arbitro: os pontos verdadeiros tem de cair
    dentro dele. Isso distingue arquivos absolutos de arquivos em delta sem
    depender de heuristica de magnitude.
    """
    records = read_records(path)
    if not records:
        return "vazio", []

    mbr = entry.get("mbr")
    ref_lon, ref_lat = entry.get("ref_lon"), entry.get("ref_lat")

    raw = [(lon, lat) for lon, lat, _z in records]
    shifted = (
        [(ref_lon + lon, ref_lat + lat) for lon, lat in raw]
        if ref_lon is not None and ref_lat is not None
        else []
    )

    if mbr:
        n_raw = sum(1 for lon, lat in raw if _inside(mbr, lon, lat))
        n_shift = sum(1 for lon, lat in shifted if _inside(mbr, lon, lat))
        if n_raw >= n_shift and n_raw >= 2:
            return "absoluto", _dedup([p for p in raw if _inside(mbr, *p)])
        if n_shift >= 2:
            return "delta", _dedup([p for p in shifted if _inside(mbr, *p)])
        return "indeterminado", []

    plausible = [(lon, lat) for lon, lat in raw if abs(lon) > 1e-6 and abs(lat) > 1e-6]
    if len(plausible) >= 2:
        return "absoluto-sem-mbr", _dedup(plausible)
    return "indeterminado", []


def gen4_points(gjson: Path) -> list[list[tuple[float, float]]]:
    data = json.loads(gjson.read_text(encoding="utf-8-sig"))
    if data.get("type") == "FeatureCollection":
        geoms = [f.get("geometry") for f in data.get("features") or []]
    else:
        geoms = [data.get("geometry") or (data if "coordinates" in data else None)]

    parts: list[list[tuple[float, float]]] = []
    for geom in geoms:
        if not isinstance(geom, dict):
            continue
        gtype, coords = geom.get("type"), geom.get("coordinates")
        if not coords:
            continue
        if gtype == "Point":
            parts.append([(coords[0], coords[1])])
        elif gtype in ("LineString", "MultiPoint"):
            parts.append([(c[0], c[1]) for c in coords])
        elif gtype in ("MultiLineString", "Polygon"):
            parts.extend([(c[0], c[1]) for c in part] for part in coords)
        elif gtype == "MultiPolygon":
            parts.extend([(c[0], c[1]) for c in ring] for poly in coords for ring in poly)
    return parts


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371008.8
    p1, p2 = math.radians(a[1]), math.radians(b[1])
    dp = p2 - p1
    dl = math.radians(b[0] - a[0])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def point_to_polyline_m(pt: tuple[float, float], line: list[tuple[float, float]]) -> float:
    return min(haversine_m(pt, q) for q in line)


def main() -> int:
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/analysis/GS3_2630")
    gen4 = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(r"C:\Users\fortu\Downloads\Gen4\Gen4")
    eic = next(base.rglob("EIC"), None)
    if eic is None:
        print("EIC nao encontrado em", base)
        return 1

    catalogs = index_catalogs(eic)
    print(f"SpatialCatalog: {len(catalogs)} itens indexados\n")

    gen4_files = {}
    spatial_dir = gen4 / "SpatialFiles"
    if spatial_dir.exists():
        for g in spatial_dir.glob("*.gjson"):
            uuid = norm_guid(g.stem.split("{")[-1])
            gen4_files[uuid] = g

    by_tag: dict[str, list] = {}
    unmatched = []
    for shape in sorted(eic.rglob("*.fdShape")):
        entry = catalogs.get(shape.stem)
        if entry is None:
            unmatched.append(shape.name)
            entry = {}
        mode, pts = decode_shape(shape, entry)
        tag = entry.get("tag", "SEM-CATALOGO")
        by_tag.setdefault(tag, []).append((shape, entry, mode, pts))

    print("=== INVENTARIO POR TIPO DE OBJETO ===")
    for tag, items in sorted(by_tag.items()):
        modes: dict[str, int] = {}
        empties = 0
        for _s, _e, mode, pts in items:
            modes[mode] = modes.get(mode, 0) + 1
            if not pts:
                empties += 1
        print(f"  {tag:24} n={len(items):4}  modos={modes}  sem_geometria={empties}")
    if unmatched:
        print(f"  ATENCAO: {len(unmatched)} fdShape sem entrada no catalogo")
    print()

    print("=== VALIDACAO CONTRA GEN4 (verdade de campo) ===")
    checked = worst_overall = 0.0
    checked = 0
    rows = []
    for tag, items in sorted(by_tag.items()):
        for shape, entry, mode, pts in items:
            uuid = entry.get("erid") or norm_guid(shape.stem)
            g = gen4_files.get(uuid)
            if not g or not pts:
                continue
            parts = gen4_points(g)
            if not parts:
                continue
            truth = [p for part in parts for p in part]
            # erro maximo: cada ponto Gen4 ate a polilinha decodificada e vice-versa
            fwd = max(point_to_polyline_m(p, pts) for p in truth)
            bwd = max(point_to_polyline_m(p, truth) for p in pts)
            worst = max(fwd, bwd)
            worst_overall = max(worst_overall, worst)
            checked += 1
            rows.append((worst, tag, entry.get("name", ""), len(pts), len(truth), mode))

    per_tag: dict[str, list[float]] = {}
    count_mismatch = 0
    for worst, tag, _name, n_dec, n_truth, _mode in rows:
        per_tag.setdefault(tag, []).append(worst)
        if n_dec != n_truth:
            count_mismatch += 1

    for tag, errs in sorted(per_tag.items()):
        print(
            f"  {tag:24} validados={len(errs):4}  "
            f"erro_max={max(errs):.8f} m  erro_medio={sum(errs)/len(errs):.8f} m"
        )

    print("\n  --- piores casos ---")
    rows.sort(reverse=True)
    for worst, tag, name, n_dec, n_truth, mode in rows[:8]:
        print(
            f"  erro_max={worst:12.8f} m  {tag:18} {name[:22]:22} "
            f"pts_decod={n_dec:4} pts_gen4={n_truth:4} modo={mode}"
        )

    print("\n  --- amostra de curvas (modo delta) ---")
    for worst, tag, name, n_dec, n_truth, mode in rows:
        if mode == "delta":
            print(
                f"  erro_max={worst:12.8f} m  {name[:22]:22} "
                f"pts_decod={n_dec:4} pts_gen4={n_truth:4}"
            )

    print(f"\n  objetos validados: {checked}")
    print(f"  divergencia de contagem de pontos: {count_mismatch}")
    print(f"  erro maximo global: {worst_overall:.8f} m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
