# INSAR_VIEWER

Standalone, read-only browser viewer for explicitly configured, already-produced geocoded MintPy products. It does not import or modify the original INSAR processing repository, run MintPy, download observations, or calculate ERA5 corrections.

## Requirements and installation

Python 3.11 or newer is required. The project runtime is intentionally smaller than the full MintPy processing environment.

With pip (from the repository root):

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

With Conda/Mamba, use the included environment:

```bash
conda env create -f environment.yml
conda activate insar_viewer
```

## Configure

Copy `viewer.example.yaml` to a private file such as `viewer.local.yaml` and edit it. The example contains placeholder paths only. `.gitignore` excludes `viewer.yaml`, `viewer.local.yaml`, `data/`, `cache/`, logs and common product extensions. Keep source data (including any `source_data` tree), GNSS CSVs and credentials outside the checkout; do not upload them to GitHub. Ignore rules do not cover every filename or remove already-tracked files.

The registry is YAML `version: 1` with a `projects` list. Each project requires `id`, `label`, `group`, `mission`, `orbit` (`ASC`/`DESC`), `look_direction` (`left`/`right`), `aoi.bbox` (`[west, south, east, north]` in WGS84 longitude/latitude degrees), and `timeseries.path`. Optional sections are `ifgram_stack` (`path`, required `retained_dataset` when a path is supplied, and optional `quality_dataset`), `quality` (`path`, `dataset`, optional `label` and `type`), `geometry` (`path`, `incidence_dataset`, `azimuth_dataset`, `azimuth_convention`), `gnss.path`, and `reference_point` (`latitude`, `longitude`). Set `timeseries.corrections: [ERA5]` only for an already-corrected product whose provenance you can establish. This declaration is optional in the current parser and reader; their acceptance does not enforce or verify ERA5 correction.

Paths may be absolute, relative to the directory containing the YAML file, use `~`, or contain `${VARIABLE}` references to environment variables. There is no institute-directory assumption. For example:

```yaml
version: 1
projects:
  - id: site_a_asc
    label: Site A ascending
    group: site_a
    mission: sentinel1
    orbit: ASC
    look_direction: right
    aoi:
      bbox: [-117.20, 32.78, -117.15, 32.83]
    timeseries:
      path: ${INSAR_DATA}/site_a/asc/timeseries.h5
      corrections: [ERA5]
    ifgram_stack:
      path: ${INSAR_DATA}/site_a/asc/ifgramStack.h5
      retained_dataset: dropIfgram
      quality_dataset: coherence
    geometry:
      path: ${INSAR_DATA}/site_a/asc/geometry.h5
      azimuth_convention: mintpy_ground_to_satellite
    gnss:
      path: ${INSAR_DATA}/site_a/stations.csv
```

For example, export `INSAR_DATA=/path/to/your/geocoded/products` before starting the viewer. Only configure geocoded products; radar-grid inputs are not silently assumed or geocoded by this application. Source HDF5, GNSS, and POS files are read-only inputs. Rendered assets and downloads go to the cache, by default `~/.cache/insar-viewer` (override with `--cache-dir` or `INSAR_VIEWER_CACHE`).

`gnss.path` can point to a CSV, one `.pos` file, or a directory of `.pos` files. CSV rows require station/name, date, latitude, longitude, and either LOS displacement or east/north/up displacement. Displacements can use centimeters or millimeters (`*_cm`/`*_mm`); optional component uncertainties are accepted using corresponding sigma/uncertainty/std fields. PBO `.pos` input includes component uncertainties and correlations when present.

## Run

Pass the config path explicitly:

```bash
insar-viewer --config /path/to/viewer.local.yaml
```

Then open <http://127.0.0.1:8080>. Useful options are `--host`, `--port`, `--cache-dir`, and `--validate`. The default bind is loopback on port 8080. To keep local logs and cache outside the checkout:

```bash
mkdir -p "$HOME/.cache/insar-viewer" "$HOME/.local/state/insar-viewer"
nohup insar-viewer --config /path/to/viewer.local.yaml \
  --cache-dir "$HOME/.cache/insar-viewer" \
  > "$HOME/.local/state/insar-viewer/viewer.log" 2>&1 < /dev/null &
echo $! > "$HOME/.local/state/insar-viewer/viewer.pid"
```

This background command survives an SSH hangup. Check it with `ps -p "$(cat "$HOME/.local/state/insar-viewer/viewer.pid")"`; confirm the process identity before stopping it with `kill "$(cat "$HOME/.local/state/insar-viewer/viewer.pid")"`. The process runs as the invoking user. Do not bind to a public interface without a production reverse proxy providing TLS and authentication. Keep source datasets read-only (for example, read-only bind mounts), and keep cache and logs in user-owned writable locations. The built-in server is not itself a production authentication or TLS layer.

## Synthetic demo

To try the interface without real data, use an empty output directory:

```bash
mkdir -p /tmp/insar-viewer-demo
python examples/make_demo.py /tmp/insar-viewer-demo
insar-viewer --config /tmp/insar-viewer-demo/viewer.yaml
```

The generator refuses a non-empty output directory. It writes only tiny synthetic HDF5 products, a synthetic station CSV, and a demo registry into the chosen directory. These are not observations, are not sourced from ERA5, and do not represent an ERA5 correction calculation. Stop the viewer with Ctrl-C.

## Viewer functions

- LOS displacement interval maps with a selectable coherence threshold and available date range.
- Click-to-sample LOS time series and station selection/overlay.
- ASC/DESC east and up decomposition maps and series, with optional GPS vectors.
- Map pan/zoom, temporary display referencing, project/group selection, and JPEG/GeoTIFF exports.
- Configured quality/coherence, hillshade, DEM contours, and satellite look-direction overlays where available; GNSS uncertainties are retained when supplied.

LOS maps still render without optional geometry; hillshade, contours, and look-direction overlays are then unavailable. Decomposition and geometry-based GNSS projection require geometry. Interval maps require a configured quality layer. A readable file is not proof of scientific suitability.

## Scientific interpretation and limits

- `corrections: [ERA5]` is an explicit provider/operator declaration. The viewer does not validate correction provenance, completeness, or scientific quality. Publication readiness is also unassessed unless explicitly declared; the viewer does not qualify a dataset for publication.
- Source time-series displacement must be in metres; display and displacement GeoTIFF values are in centimeters, positive toward the satellite. Incidence and azimuth angles are in degrees. `mintpy_ground_to_satellite` azimuth is counterclockwise from north for the ground-to-satellite vector.
- East/up decomposition assumes north displacement is zero. Pairing requires the same configured group, mission, and AOI. Unknown/malformed retained-network evidence blocks decomposition, as do selected windows crossing disconnected spans, including interleaved components.
- Actual ASC/DESC HDF5 reference pixels (`REF_X`/`REF_Y`) are checked. If their locations differ, choose one common viewer reference in both grids, through the UI or matching `reference_point` entries on both projects. Each orbit's LOS reference offset is subtracted on its native grid **before** reprojection and east/up inversion. Subtracting a decomposed reference afterward is not equivalent when geometry varies spatially. Time series are zeroed at the first selected/matched epoch.
- Quality averages retain finite zero values unless an explicit nodata attribute marks them missing. For the retained 3-D quality stack, supported dataset attributes are `_FillValue`, `missing_value`, `NO_DATA_VALUE`, and `nodata`, with file attributes as fallback. The LOS mask requires finite displacement and quality at or above the selected threshold. Decomposition requires finite values and threshold-passing coherence from **both** orbits, finite geometry, and a nonsingular inversion.
- Explicit geocoded grid metadata (`EPSG`, `X_FIRST`, `Y_FIRST`, `X_STEP`, `Y_STEP`) is required and companion grids are checked against their time series. `X_FIRST`/`Y_FIRST` describe the outer raster corner; pixel centers use a half-cell offset. Valid ASC/DESC products may use different grids or CRSs: the viewer reprojects DESC displacement, quality, and LOS geometry onto the ASC grid using bilinear interpolation. It does not geocode raw radar-grid inputs or establish scientific datum compatibility.
- NISAR products require explicit configuration. The `legacy_nisar` geometry convention is a separate opt-in correction with orbit checks; it is never inferred automatically.

## Checks

Validate the registry and configured products without starting the server:

```bash
insar-viewer --config /path/to/viewer.local.yaml --validate
```

The command reports product checks as JSON and exits nonzero on errors; it does not establish scientific validity. Install test dependencies with `python -m pip install -e '.[test]'`, then run `python -m pytest`. Browser checks are opt-in and additionally require a browser installation.

Synthetic regression tests include source-file SHA-256 checks before and after rendering. These tests assess implementation behavior, not field-data accuracy or scientific validation. Current build and verification outcomes are tracked in [PROJECT_PLAN.md](PROJECT_PLAN.md).
