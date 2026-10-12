"""Geometry convention handling for configured geocoded products."""

from __future__ import annotations

import numpy as np


def normalize_azimuth(values: np.ndarray, convention: str, *, mission: str, orbit: str) -> tuple[np.ndarray, float]:
    if convention == "mintpy_ground_to_satellite":
        return np.asarray(values, dtype=np.float32), 0.0
    if convention == "legacy_nisar":
        if mission.lower() != "nisar" or orbit.upper() not in {"ASC", "DESC"}:
            raise ValueError("legacy_nisar azimuth convention requires mission nisar and ASC/DESC orbit")
        array = np.asarray(values, dtype=np.float32)
        valid = array[np.isfinite(array)]
        if not valid.size:
            raise ValueError("NISAR azimuth array has no finite values")
        mean = np.rad2deg(np.angle(np.mean(np.exp(1j * np.deg2rad(valid)))))
        expected = -90.0 if orbit.upper() == "ASC" else 90.0
        distance = abs((mean - expected + 180.0) % 360.0 - 180.0)
        corrected_distance = abs((mean + 90.0 - expected + 180.0) % 360.0 - 180.0)
        if corrected_distance > 60.0 or corrected_distance >= distance:
            raise ValueError("configured legacy NISAR correction is inconsistent with orbit look direction")
        return (array + 90.0) % 360.0, 90.0
    raise ValueError(f"unsupported azimuth_convention: {convention}")


def los_unit_arrays(incidence: np.ndarray, azimuth: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    inc, az = np.deg2rad(incidence.astype(np.float32)), np.deg2rad(azimuth.astype(np.float32))
    return (-np.sin(inc) * np.sin(az)).astype(np.float32), (np.sin(inc) * np.cos(az)).astype(np.float32), np.cos(inc).astype(np.float32)


def los_unit_vector(incidence: float, azimuth: float) -> tuple[float, float, float]:
    east, north, up = los_unit_arrays(np.asarray(incidence), np.asarray(azimuth))
    return float(east), float(north), float(up)
