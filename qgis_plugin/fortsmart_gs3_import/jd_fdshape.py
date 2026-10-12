"""Decodificador canonico do formato binario John Deere ``.fdShape`` (GS3 2630 / RCD).

Especificacao obtida por engenharia reversa e validada byte a byte contra os
arquivos Gen4 ``.gjson`` dos mesmos UUIDs (204/204 objetos, bbox identico ao MBR
do ``SpatialCatalog``, erro maximo de 7.7e-06 m contra o Gen4).

Layout do arquivo::

    offset  tamanho  conteudo
    0x00    16       zeros
    0x10    4        marcador 01 02 00 00
    0x38    8        int64 little-endian: quantidade de registros
    0x40    20*N     registros

Cada registro tem 20 bytes::

    <d  longitude (ou delta de longitude)
    <d  latitude  (ou delta de latitude)
    <f  altitude/sentinela

Modos de coordenada
-------------------
``absoluto``
    ``Boundary`` e ``FlagSessionLayer`` guardam graus decimais WGS84 direto.

``delta``
    ``CurvedTrackLine`` guarda deslocamentos em graus relativos a
    ``vrReferenceLongitude`` / ``vrReferenceLatitude`` do ``SpatialCatalog``.
    O primeiro registro e uma sentinela (altitude 999.9) e nao e coordenada.

O MBR do ``SpatialCatalog`` e o arbitro entre os dois modos: os pontos
verdadeiros caem dentro dele. Nunca inferimos o modo pela magnitude dos valores,
porque deltas pequenos tambem sao latitudes/longitudes validas.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

HEADER_SIZE = 64
RECORD_SIZE = 20
COUNT_OFFSET = 0x38
SENTINEL_ALTITUDE = 999.9

# Tolerancia para o teste de pertinencia ao MBR. O MBR e gravado com a mesma
# precisao dos pontos, entao uma folga minima basta para absorver arredondamento.
MBR_EPSILON = 1e-9

MODE_ABSOLUTE = "absoluto"
MODE_DELTA = "delta"
MODE_EMPTY = "vazio"
MODE_UNKNOWN = "indeterminado"

Mbr = tuple[float, float, float, float]


@dataclass
class FdShapeResult:
    """Resultado da decodificacao, com dados suficientes para auditoria."""

    mode: str
    points: list[tuple[float, float]] = field(default_factory=list)
    altitudes: list[float] = field(default_factory=list)
    record_count: int = 0
    dropped_outside_mbr: int = 0
    dropped_duplicate: int = 0
    reference: tuple[float, float] | None = None
    mbr_match: bool = False

    @property
    def ok(self) -> bool:
        return self.mode in (MODE_ABSOLUTE, MODE_DELTA) and len(self.points) >= 1


def read_raw_records(data: bytes) -> list[tuple[float, float, float]]:
    """Le os registros crus (lon, lat, altitude) sem interpretar o modo."""
    if len(data) < HEADER_SIZE + RECORD_SIZE:
        return []
    count = struct.unpack("<q", data[COUNT_OFFSET : COUNT_OFFSET + 8])[0]
    available = (len(data) - HEADER_SIZE) // RECORD_SIZE
    if count <= 0 or count > available:
        count = available
    return [
        struct.unpack("<ddf", data[HEADER_SIZE + i * RECORD_SIZE : HEADER_SIZE + (i + 1) * RECORD_SIZE])
        for i in range(count)
    ]


def _inside(mbr: Mbr, lon: float, lat: float, eps: float = MBR_EPSILON) -> bool:
    west, south, east, north = mbr
    return (west - eps) <= lon <= (east + eps) and (south - eps) <= lat <= (north + eps)


def _bbox_matches(mbr: Mbr, points: list[tuple[float, float]], eps: float = 1e-9) -> bool:
    if not points:
        return False
    west, south, east, north = mbr
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return (
        abs(min(lons) - west) <= eps
        and abs(max(lons) - east) <= eps
        and abs(min(lats) - south) <= eps
        and abs(max(lats) - north) <= eps
    )


def is_sentinel_altitude(altitude: float) -> bool:
    """A altitude e float32: 999.9 e lido como 999.9000244, nunca igual a 999.9."""
    return abs(altitude - SENTINEL_ALTITUDE) < 0.01


@dataclass
class FdShapeTracks:
    """Trilhas de um ``CurvedTrackLine``, separadas pelos registros sentinela."""

    mode: str
    tracks: list[list[tuple[float, float]]] = field(default_factory=list)
    record_count: int = 0
    sentinel_count: int = 0
    dropped_outside_mbr: int = 0


def decode_fdshape_tracks(
    path: Path,
    *,
    mbr: Mbr | None = None,
    reference: tuple[float, float] | None = None,
) -> FdShapeTracks:
    """Decodifica um ``CurvedTrackLine`` devolvendo cada trilha separada.

    Cada trilha comeca num registro sentinela (altitude 999.9) que repete o
    ultimo ponto da trilha anterior; ele nao e vertice. Sem sentinelas, o
    arquivo inteiro e uma unica trilha.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return FdShapeTracks(mode=MODE_EMPTY)

    records = read_raw_records(data)
    if not records:
        return FdShapeTracks(mode=MODE_EMPTY)

    selected = _select_records(records, mbr, reference)
    if selected is None:
        return FdShapeTracks(mode=MODE_UNKNOWN, record_count=len(records))
    mode, keep = selected
    kept = dict(keep)

    tracks: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    sentinels = 0
    dropped_outside = 0
    for idx, record in enumerate(records):
        if is_sentinel_altitude(record[2]):
            sentinels += 1
            if current:
                tracks.append(current)
            current = []
            continue
        point = kept.get(idx)
        if point is None:
            dropped_outside += 1
            continue
        if current and abs(point[0] - current[-1][0]) <= 1e-13 and abs(point[1] - current[-1][1]) <= 1e-13:
            continue
        current.append(point)
    if current:
        tracks.append(current)

    return FdShapeTracks(
        mode=mode,
        tracks=[track for track in tracks if len(track) >= 2],
        record_count=len(records),
        sentinel_count=sentinels,
        dropped_outside_mbr=dropped_outside,
    )


def _select_records(
    records: list[tuple[float, float, float]],
    mbr: Mbr | None,
    reference: tuple[float, float] | None,
) -> tuple[str, list[tuple[int, tuple[float, float]]]] | None:
    raw = [(lon, lat) for lon, lat, _alt in records]
    shifted: list[tuple[float, float]] = []
    if reference is not None:
        ref_lon, ref_lat = reference
        shifted = [(ref_lon + lon, ref_lat + lat) for lon, lat in raw]

    if mbr:
        n_raw = sum(1 for lon, lat in raw if _inside(mbr, lon, lat))
        n_shifted = sum(1 for lon, lat in shifted if _inside(mbr, lon, lat))
        if n_raw >= n_shifted and n_raw >= 1:
            mode, candidates = MODE_ABSOLUTE, raw
        elif n_shifted >= 1:
            mode, candidates = MODE_DELTA, shifted
        else:
            return None
        return mode, [(i, p) for i, p in enumerate(candidates) if _inside(mbr, *p)]
    plausible = [
        (i, p)
        for i, p in enumerate(raw)
        if -180.0 <= p[0] <= 180.0 and -90.0 <= p[1] <= 90.0 and (abs(p[0]) > 1e-9 or abs(p[1]) > 1e-9)
    ]
    if not plausible:
        return None
    return MODE_ABSOLUTE, plausible


def decode_fdshape(
    path: Path,
    *,
    mbr: Mbr | None = None,
    reference: tuple[float, float] | None = None,
    close_ring: bool = False,
) -> FdShapeResult:
    """Decodifica um ``.fdShape`` preservando a ordem e a densidade originais.

    ``reference`` e o par (lon, lat) de ``vrReferenceLongitude`` /
    ``vrReferenceLatitude``, obrigatorio para objetos em modo delta.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return FdShapeResult(mode=MODE_EMPTY)

    records = read_raw_records(data)
    if not records:
        return FdShapeResult(mode=MODE_EMPTY)

    raw = [(lon, lat) for lon, lat, _alt in records]
    shifted: list[tuple[float, float]] = []
    if reference is not None:
        ref_lon, ref_lat = reference
        shifted = [(ref_lon + lon, ref_lat + lat) for lon, lat in raw]

    if mbr:
        n_raw = sum(1 for lon, lat in raw if _inside(mbr, lon, lat))
        n_shifted = sum(1 for lon, lat in shifted if _inside(mbr, lon, lat))
        if n_raw >= n_shifted and n_raw >= 1:
            mode, candidates = MODE_ABSOLUTE, raw
        elif n_shifted >= 1:
            mode, candidates = MODE_DELTA, shifted
        else:
            return FdShapeResult(mode=MODE_UNKNOWN, record_count=len(records))
        keep = [(i, p) for i, p in enumerate(candidates) if _inside(mbr, *p)]
    else:
        # Sem MBR so podemos aceitar o modo absoluto, e ainda assim descartando
        # a origem falsa (0, 0) que aparece nos arquivos em delta.
        plausible = [
            (i, p)
            for i, p in enumerate(raw)
            if -180.0 <= p[0] <= 180.0 and -90.0 <= p[1] <= 90.0 and (abs(p[0]) > 1e-9 or abs(p[1]) > 1e-9)
        ]
        if len(plausible) < 1:
            return FdShapeResult(mode=MODE_UNKNOWN, record_count=len(records))
        mode, keep = MODE_ABSOLUTE, plausible

    dropped_outside = len(records) - len(keep)

    points: list[tuple[float, float]] = []
    altitudes: list[float] = []
    dropped_dup = 0
    for idx, point in keep:
        altitude = records[idx][2]
        if is_sentinel_altitude(altitude) and (mode == MODE_DELTA or not points):
            continue
        if points and abs(point[0] - points[-1][0]) <= 1e-13 and abs(point[1] - points[-1][1]) <= 1e-13:
            dropped_dup += 1
            continue
        points.append(point)
        altitudes.append(altitude)

    mbr_match = _bbox_matches(mbr, points) if mbr else False

    if close_ring and len(points) >= 3 and points[0] != points[-1]:
        points.append(points[0])
        altitudes.append(altitudes[0])

    return FdShapeResult(
        mode=mode,
        points=points,
        altitudes=altitudes,
        record_count=len(records),
        dropped_outside_mbr=dropped_outside,
        dropped_duplicate=dropped_dup,
        reference=reference,
        mbr_match=mbr_match,
    )
