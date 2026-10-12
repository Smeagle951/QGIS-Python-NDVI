"""FortSmart — validacao e correcao de coordenadas WGS84."""

from __future__ import annotations


def fix_lon_lat(lon: float, lat: float) -> tuple[float, float] | None:
    """Corrige inversao lat/lon comum e descarta pontos invalidos."""
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        return None

    # Lixo binario: latitude ~0 com longitude longe do meridiano de Greenwich
    if abs(lat) < 0.05 and abs(lon) > 5.0:
        return None

    # (0,0) ou quase zero
    if abs(lon) < 1e-9 and abs(lat) < 1e-9:
        return None

    # Inversao tipica Brasil: lon=-15, lat=-54 -> trocar
    if lon > 0 and lat < 0 and abs(lon) < 35 and abs(lat) > 20:
        lon, lat = lat, lon
    elif abs(lon) < 35 and abs(lat) > 35:
        lon, lat = lat, lon

    # Brasil / America do Sul agricola: preferir lon west, lat south
    if lon > 0 and lat < 0 and lon < 60:
        return None

    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        return None
    return lon, lat


def sanitize_ring(points: list[tuple[float, float]], *, min_points: int = 2) -> list[tuple[float, float]]:
    clean: list[tuple[float, float]] = []
    for lon, lat in points:
        fixed = fix_lon_lat(lon, lat)
        if fixed is None:
            continue
        lon, lat = fixed
        if clean and abs(lon - clean[-1][0]) < 1e-12 and abs(lat - clean[-1][1]) < 1e-12:
            continue
        clean.append((lon, lat))
    return clean if len(clean) >= min_points else []


def sanitize_rings(rings: list[list[tuple[float, float]]], *, min_points: int = 3) -> list[list[tuple[float, float]]]:
    out: list[list[tuple[float, float]]] = []
    for ring in rings:
        cleaned = sanitize_ring(ring, min_points=min_points)
        if cleaned:
            out.append(cleaned)
    return out
