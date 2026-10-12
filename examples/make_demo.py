"""Generate tiny synthetic products; these are not observations or an ERA5 calculation."""

import argparse
import csv
from pathlib import Path

import h5py
import numpy as np
import yaml


def make_demo(root: Path) -> Path:
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"Demo output must be an empty directory: {root}")
    root.mkdir(parents=True, exist_ok=True)
    dates = ["20260101", "20260201", "20260301", "20260401", "20260501", "20260601"]
    rows, cols = 100, 120
    yy, xx = np.mgrid[:rows, :cols]
    bump = np.exp(-((xx - 64) ** 2 + (yy - 50) ** 2) / 700)
    east = (xx - 64) / 80 * bump * .025
    up = bump * .04
    east -= east[70, 20]
    up -= up[70, 20]
    quality = (.55 + .4 * bump).astype("float32")
    attrs = {"EPSG": 4326, "X_FIRST": -117.20, "Y_FIRST": 32.83,
             "X_STEP": .05 / cols, "Y_STEP": -.05 / rows,
             "WIDTH": cols, "LENGTH": rows, "REF_X": 20, "REF_Y": 70,
             "REF_DATE": dates[0], "PLATFORM": "SENTINEL1",
             "CENTER_LINE_UTC": 0, "UNIT": "m"}
    pairs = [(i, i + 1) for i in range(5)] + [(0, 2)]
    projects = []
    for orbit, azimuth in (("asc", 100.), ("desc", -100.)):
        folder = root / orbit
        folder.mkdir(exist_ok=True)
        inc, az = np.deg2rad(35), np.deg2rad(azimuth)
        los = -np.sin(inc) * np.sin(az) * east + np.cos(inc) * up
        stack = np.asarray([i * los for i in range(6)], dtype="float32")
        metadata = attrs | {"ORBIT_DIRECTION": "ASCENDING" if orbit == "asc" else "DESCENDING"}
        with h5py.File(folder / "displacement.h5", "w") as f:
            f.attrs.update(metadata)
            f.create_dataset("timeseries", data=stack)
            f.create_dataset("date", data=np.asarray(dates, dtype="S8"))
        with h5py.File(folder / "network.h5", "w") as f:
            f.attrs.update(metadata | {"UNIT": "1"})
            f.create_dataset("date", data=np.asarray([[dates[a], dates[b]] for a, b in pairs], dtype="S8"))
            f.create_dataset("dropIfgram", data=np.ones(len(pairs), dtype=bool))
            f.create_dataset("coherence", data=np.repeat(quality[None], len(pairs), axis=0))
        with h5py.File(folder / "terrain.h5", "w") as f:
            f.attrs.update(metadata)
            f.create_dataset("incidenceAngle", data=np.full((rows, cols), 35., dtype="float32"))
            f.create_dataset("azimuthAngle", data=np.full((rows, cols), azimuth, dtype="float32"))
            f.create_dataset("height", data=(200 + bump * 1800).astype("float32"))
        projects.append({"id": f"demo_{orbit}", "label": f"Synthetic demo - {orbit.upper()}",
                         "group": "synthetic_demo", "mission": "sentinel1",
                         "orbit": orbit.upper(), "look_direction": "right",
                         "aoi": {"bbox": [-117.20, 32.78, -117.15, 32.83]},
                         "timeseries": {"path": f"{orbit}/displacement.h5", "corrections": ["ERA5"]},
                         "ifgram_stack": {"path": f"{orbit}/network.h5", "retained_dataset": "dropIfgram", "quality_dataset": "coherence"},
                         "geometry": {"path": f"{orbit}/terrain.h5", "azimuth_convention": "mintpy_ground_to_satellite"},
                         "gnss": {"path": "stations.csv"}})
    with (root / "stations.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["station", "date", "latitude", "longitude", "east_cm", "north_cm", "up_cm"])
        writer.writeheader()
        for token, factor in zip(dates, range(6)):
            writer.writerow({"station": "DEMO", "date": token, "latitude": 32.805,
                             "longitude": -117.173, "east_cm": factor * .25,
                             "north_cm": 0, "up_cm": factor * 2})
    registry = root / "viewer.yaml"
    registry.write_text(yaml.safe_dump({"version": 1, "projects": projects}, sort_keys=False), encoding="utf-8")
    return registry


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    print(make_demo(parser.parse_args().output))
