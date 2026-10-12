"""Small GNSS CSV/PBO-POS reader and AOI selector for the viewer."""

from __future__ import annotations

from dataclasses import dataclass
import csv
from datetime import date, datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class GPSSeries:
    label: str
    dates: list[date]
    values_cm: list[float] | None
    component: str
    latitude: float | None = None
    longitude: float | None = None
    east_cm: list[float] | None = None
    north_cm: list[float] | None = None
    up_cm: list[float] | None = None
    values_sigma_cm: list[float] | None = None
    east_sigma_cm: list[float] | None = None
    north_sigma_cm: list[float] | None = None
    up_sigma_cm: list[float] | None = None
    corr_east_north: list[float] | None = None
    corr_east_up: list[float] | None = None
    corr_north_up: list[float] | None = None
    incidence_angle_deg: float | None = None
    azimuth_angle_deg: float | None = None
    los_unit_east: float | None = None
    los_unit_north: float | None = None
    los_unit_up: float | None = None
    metadata: dict[str, str] | None = None


def _date(value: str) -> date:
    text = value.strip()
    return datetime.strptime(text, "%Y%m%d").date() if len(text) == 8 and text.isdigit() else datetime.fromisoformat(text).date()


def _num(row: dict[str, str], names: tuple[str, ...]) -> float | None:
    for name in names:
        for key in (f"{name}_cm", f"{name.upper()}_cm", f"d{name}_cm", f"{name}(cm)", f"{name}_mm", f"{name.upper()}_mm", f"d{name}_mm", f"{name}(mm)"):
            value = row.get(key)
            if value not in (None, ""):
                return float(value) * (0.1 if "mm" in key.lower() else 1.0)
    return None


def _uncertainty(row: dict[str, str], component: str) -> float | None:
    return _num(row, (f"{component}_sigma", f"sigma_{component}", f"{component}_uncertainty", f"{component}_std"))


def _correlation(row: dict[str, str], pair: str) -> float | None:
    for key in (f"corr_{pair}", f"correlation_{pair}", f"rho_{pair}"):
        value = row.get(key)
        if value not in (None, ""):
            parsed = float(value)
            if not -1.0 <= parsed <= 1.0:
                raise ValueError(f"{key} must be between -1 and 1")
            return parsed
    return None


def load_gps(path: Path | None) -> list[GPSSeries]:
    if path is None or not path.exists():
        return []
    if path.is_dir():
        return [series for file in sorted(path.glob("*.pos")) for series in _load_pos(file)]
    if path.suffix.lower() == ".pos":
        return _load_pos(path)
    grouped: dict[str, list[dict[str, Any]]] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"GPS CSV has no header: {path}")
        for i, row in enumerate(reader, 1):
            def first(keys: tuple[str, ...]) -> str | None:
                return next((row[k] for k in keys if row.get(k) not in (None, "")), None)
            label = first(("Site", "site", "name", "label", "station")) or f"gps_{i}"
            lat, lon = first(("Latitude", "latitude", "lat")), first(("Longitude", "longitude", "lon", "lng"))
            if not lat or not lon:
                raise ValueError("GPS CSV requires latitude and longitude")
            day = first(("date", "Date", "datetime", "Datetime", "epoch", "time"))
            if not day:
                raise ValueError("GPS CSV requires a date column")
            los = _num(row, ("los", "dlos"))
            enu = [_num(row, (key,)) for key in ("east", "north", "up")]
            if los is None and any(v is None for v in enu):
                raise ValueError("GPS CSV requires LOS or east/north/up displacement columns")
            grouped.setdefault(label, []).append({
                "date": _date(day), "lat": float(lat), "lon": float(lon), "los": los, "enu": enu,
                "los_sigma": _uncertainty(row, "los"),
                "sigmas": [_uncertainty(row, part) for part in ("east", "north", "up")],
                "correlations": [_correlation(row, pair) for pair in ("east_north", "east_up", "north_up")],
            })
    output = []
    for label, rows in grouped.items():
        rows.sort(key=lambda row: row["date"])
        if any(current["date"] <= prior["date"] for prior, current in zip(rows, rows[1:])):
            raise ValueError(f"GPS station {label!r} has duplicate epochs")
        dates = [row["date"] for row in rows]
        los_rows = [r["los"] for r in rows]
        has_los = all(v is not None for v in los_rows)
        values = [float(v) - float(los_rows[0]) for v in los_rows] if has_los else None
        enu_cols = [[float(r["enu"][i]) for r in rows] for i in range(3)] if not has_los else [None] * 3
        def complete(field: str, column: int | None = None):
            vals = [r[field] if column is None else r[field][column] for r in rows]
            return [float(v) for v in vals] if all(v is not None for v in vals) else None
        output.append(GPSSeries(label, dates, values, "GPS LOS" if has_los else "GPS ENU", rows[0]["lat"], rows[0]["lon"], *enu_cols,
            values_sigma_cm=complete("los_sigma") if has_los else None,
            east_sigma_cm=complete("sigmas", 0) if not has_los else None,
            north_sigma_cm=complete("sigmas", 1) if not has_los else None,
            up_sigma_cm=complete("sigmas", 2) if not has_los else None,
            corr_east_north=complete("correlations", 0) if not has_los else None,
            corr_east_up=complete("correlations", 1) if not has_los else None,
            corr_north_up=complete("correlations", 2) if not has_los else None))
    return output


def _load_pos(path: Path) -> list[GPSSeries]:
    label, reference = path.name.split(".")[0], None
    rows, table = [], False
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if line.startswith("4-character ID:"):
                label = line.split(":", 1)[1].strip() or label
            elif line.startswith("NEU Reference position"):
                values = line.split(":", 1)[1].split()
                if len(values) >= 2:
                    reference = (float(values[0]), float(values[1]))
            elif line.startswith("*YYYYMMDD"):
                table = True
            elif table:
                fields = line.split()
                if len(fields) >= 24 and fields[0].isdigit():
                    rows.append(fields)
    if not rows:
        raise ValueError(f"No GPS epochs found in POS file: {path}")
    rows.sort(key=lambda r: r[0])
    if any(current[0] <= prior[0] for prior, current in zip(rows, rows[1:])):
        raise ValueError(f"GPS station {label!r} has duplicate epochs")
    lat = reference[0] if reference else sum(float(r[12]) for r in rows) / len(rows)
    lon = reference[1] if reference else sum(float(r[13]) - 360 if float(r[13]) > 180 else float(r[13]) for r in rows) / len(rows)
    return [GPSSeries(label, [_date(r[0]) for r in rows], None, "GPS NEU POS", lat, lon,
        [float(r[16])*100 for r in rows], [float(r[15])*100 for r in rows], [float(r[17])*100 for r in rows],
        east_sigma_cm=[float(r[19])*100 for r in rows], north_sigma_cm=[float(r[18])*100 for r in rows], up_sigma_cm=[float(r[20])*100 for r in rows],
        corr_east_north=[float(r[21]) for r in rows], corr_north_up=[float(r[22]) for r in rows], corr_east_up=[float(r[23]) for r in rows], metadata={"source": str(path), "format": "pbo_pos"})]


def filter_to_bbox(series: list[GPSSeries], bbox: tuple[float, float, float, float]) -> list[GPSSeries]:
    west, south, east, north = bbox
    return [s for s in series if s.latitude is not None and s.longitude is not None and south <= s.latitude <= north and west <= s.longitude <= east]
