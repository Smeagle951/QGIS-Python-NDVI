"""Business/Farm/Field names for Precision Planting 20|20 SendTo2020.

The 20|20 groups imported boundaries by the exact Business and Farm text in the
DBF. A single different character creates a new grower/farm on the monitor, so
names are normalized and snapped to the hierarchy already present on the device.
"""

from __future__ import annotations

import re
import struct
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

# The .mcd declares Width='32' for Business, Farm and Field.
MAX_NAME_LENGTH = 32
BUSINESS_MATCH_RATIO = 0.88
FARM_MATCH_RATIO = 0.88


@dataclass(frozen=True)
class MonitorHierarchyEntry:
    business: str
    farm: str
    field: str
    source: str = ""


@dataclass
class ResolvedNames:
    business: str
    farm: str
    field: str
    notes: list[str] = field(default_factory=list)


def monitor_name(text: str) -> str:
    """Characters accepted by the 20|20 naming used in accepted packages."""
    value = unicodedata.normalize("NFC", str(text or "")).strip()
    value = re.sub(r"(?<=\d)[.,;/](?=\d)", "-", value)
    value = "".join(ch if ch.isalnum() or ch in " -_" else " " for ch in value)
    value = re.sub(r"\s+", " ", value).strip(" _")
    return value[:MAX_NAME_LENGTH].rstrip()


def name_key(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-z]", "", ascii_text.casefold())


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, name_key(a), name_key(b)).ratio()


def _read_dbf_records(path: Path) -> list[dict[str, str]]:
    data = path.read_bytes()
    if len(data) < 32:
        return []
    count, header_len, record_len = struct.unpack_from("<IHH", data, 4)
    fields: list[tuple[str, int]] = []
    offset = 32
    while offset + 32 <= header_len and data[offset] != 0x0D:
        name = data[offset:offset + 11].split(b"\0")[0].decode("latin1")
        fields.append((name, data[offset + 16]))
        offset += 32
    records = []
    for index in range(count):
        start = header_len + index * record_len
        record = data[start:start + record_len]
        if len(record) < record_len or record[:1] == b"*":
            continue
        values: dict[str, str] = {}
        position = 1
        for name, length in fields:
            raw = record[position:position + length]
            try:
                values[name] = raw.decode("utf-8").strip()
            except UnicodeDecodeError:
                values[name] = raw.decode("latin1").strip()
            position += length
        records.append(values)
    return records


def _sendto2020_dirs(root: Path) -> list[Path]:
    root = Path(root)
    if not root.exists():
        return []
    if root.name.casefold() == "sendto2020":
        return [root]
    direct = [child for child in root.iterdir() if child.is_dir() and child.name.casefold() == "sendto2020"]
    if direct:
        return direct
    return [path for path in root.glob("*/SendTo2020") if path.is_dir()]


def load_monitor_hierarchy(roots: list[Path]) -> list[MonitorHierarchyEntry]:
    """Read Business/Farm/Field from existing SendTo2020 DBF files."""
    entries: list[MonitorHierarchyEntry] = []
    seen: set[tuple[str, str, str]] = set()
    for root in roots:
        for folder in _sendto2020_dirs(Path(root)):
            for dbf in sorted(folder.glob("*.dbf")):
                try:
                    records = _read_dbf_records(dbf)
                except (OSError, struct.error):
                    continue
                for record in records:
                    business = record.get("Business", "").strip()
                    farm = record.get("Farm", "").strip()
                    field_name = record.get("Field", "").strip()
                    if not business or not farm:
                        continue
                    key = (business, farm, field_name)
                    if key in seen:
                        continue
                    seen.add(key)
                    entries.append(MonitorHierarchyEntry(business, farm, field_name, str(dbf)))
    return entries


def _best(candidates: dict[str, int], wanted: str, min_ratio: float) -> str:
    """Pick the monitor name for ``wanted``; near-duplicates collapse to the most used one."""
    scored = [
        (count, _ratio(value, wanted), value)
        for value, count in candidates.items()
        if _ratio(value, wanted) >= min_ratio
    ]
    if not scored:
        return ""
    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return scored[0][2]


def _counts(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def resolve_monitor_names(
    business: str,
    farm: str,
    field_name: str,
    existing: list[MonitorHierarchyEntry] | None = None,
) -> ResolvedNames:
    resolved = ResolvedNames(monitor_name(business), monitor_name(farm), monitor_name(field_name))
    for label, before, after in (
        ("Business", business, resolved.business),
        ("Farm", farm, resolved.farm),
        ("Field", field_name, resolved.field),
    ):
        if before.strip() != after:
            resolved.notes.append(f"{label} normalizado: {before.strip()!r} -> {after!r}")

    if not existing:
        return resolved

    businesses = _counts([entry.business for entry in existing])
    match = _best(businesses, resolved.business, BUSINESS_MATCH_RATIO)
    if match and match != resolved.business:
        resolved.notes.append(f"Business igual ao do monitor: {resolved.business!r} -> {match!r}")
        resolved.business = match
    if not match:
        return resolved

    farms = _counts([entry.farm for entry in existing if entry.business == match])
    farm_match = _best(farms, resolved.farm, FARM_MATCH_RATIO)
    if farm_match and farm_match != resolved.farm:
        resolved.notes.append(f"Farm igual ao do monitor: {resolved.farm!r} -> {farm_match!r}")
        resolved.farm = farm_match
    if not farm_match:
        return resolved

    # Field names like "TL-7" and "TL-8" are nearly identical; only exact keys match.
    fields = [
        entry.field
        for entry in existing
        if entry.business == match and entry.farm == farm_match and entry.field
    ]
    field_match = next((value for value in fields if name_key(value) == name_key(resolved.field)), "")
    if field_match and field_match != resolved.field:
        resolved.notes.append(f"Field igual ao do monitor: {resolved.field!r} -> {field_match!r}")
        resolved.field = field_match
    return resolved
