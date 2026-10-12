"""Read-only, standalone backend for geocoded InSAR products."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import h5py
import numpy as np
import rasterio
from rasterio.transform import Affine, rowcol
from rasterio.warp import Resampling, reproject, transform as transform_coords, transform_bounds
from rasterio.windows import Window, from_bounds, transform as window_transform

from .config import load_projects
from .geometry import los_unit_arrays, los_unit_vector, normalize_azimuth
from .gnss import GPSSeries, filter_to_bbox, load_gps

PACKAGE_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = PACKAGE_ROOT / "static"
PROJECTS_PATH = Path(os.environ.get("INSAR_VIEWER_CONFIG", PACKAGE_ROOT / "viewer.yaml"))
CACHE_ROOT = Path(os.environ.get("INSAR_VIEWER_CACHE", Path.home() / ".cache/insar-viewer")).resolve()

WEB_RENDER_MIN_LONG_SIDE = 2048


def _decode_h5_date(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _parse_yyyymmdd(value: str) -> date:
    if len(value) != 8 or not value.isdigit():
        raise ValueError(f"Invalid MintPy acquisition date: {value!r}")
    return datetime.strptime(value, "%Y%m%d").date()


def _date_token(value: str) -> str:
    text = value.strip()
    return text if len(text) == 8 and text.isdigit() else datetime.fromisoformat(text).strftime("%Y%m%d")


@dataclass(slots=True)
class WebProject:
    """One project exposed in the web viewer."""

    project_id: str
    label: str
    group: str
    orbit: str
    look_direction: str
    bbox: tuple[float, float, float, float]
    reference_point: tuple[float, float] | None
    timeseries_path: Path
    corrections: tuple[str, ...]
    ifgram_stack_path: Path | None
    retained_dataset: str | None
    quality_path: Path | None
    quality_dataset: str | None
    quality_label: str | None
    quality_type: str | None
    geometry_path: Path | None
    incidence_dataset: str
    azimuth_dataset: str
    azimuth_convention: str
    gps_path: Path | None
    pipeline_kind: str = "sentinel1"
    publication_status: str = "unassessed"
    publication_state_path: Path | None = None


@dataclass(frozen=True, slots=True)
class ViewerTimeSeriesSource:
    """A complete corrected time series accepted by the web viewer."""

    path: Path
    corrections: tuple[str, ...]
    dates: tuple[str, ...]
    spatial_shape: tuple[int, int]
    pipeline_kind: str = "sentinel1"

    @property
    def using_legacy_fallback(self) -> bool:
        return False

    @property
    def first_date(self) -> str:
        return self.dates[0]

    @property
    def last_date(self) -> str:
        return self.dates[-1]

    def to_payload(self) -> dict[str, Any]:
        return {
            "timeseries_file": self.path.name,
            "corrections": list(self.corrections),
            "correction_level": ", ".join(self.corrections) if self.corrections else "unassessed",
            "using_legacy_fallback": False,
            "correction_evidence": "operator_declared",
            "pipeline": self.pipeline_kind,
            "coverage_start_date": _parse_yyyymmdd(self.first_date).isoformat(),
            "coverage_end_date": _parse_yyyymmdd(self.last_date).isoformat(),
        }


def _inspect_viewer_timeseries(
    path: Path,
    *,
    corrections: tuple[str, ...],
    pipeline_kind: str = "sentinel1",
) -> ViewerTimeSeriesSource:
    with h5py.File(path, "r") as h5:
        if "date" not in h5 or "timeseries" not in h5:
            raise ValueError("missing date or timeseries dataset")

        dates = tuple(_decode_h5_date(value) for value in h5["date"][:])
        stack = h5["timeseries"]
        if stack.ndim != 3:
            raise ValueError(f"timeseries dataset is {stack.ndim}D, expected 3D")
        if len(dates) < 2:
            raise ValueError("fewer than two time-series epochs")
        if stack.shape[0] != len(dates):
            raise ValueError(
                f"date count {len(dates)} does not match time-series length {stack.shape[0]}"
            )
        if ("REF_X" in h5.attrs) != ("REF_Y" in h5.attrs):
            raise ValueError("REF_X and REF_Y must be provided together")
        if "REF_X" in h5.attrs:
            ref_x, ref_y = float(h5.attrs["REF_X"]), float(h5.attrs["REF_Y"])
            if not (np.isfinite(ref_x) and np.isfinite(ref_y) and 0 <= ref_x < stack.shape[2] and 0 <= ref_y < stack.shape[1]):
                raise ValueError("REF_X/REF_Y reference pixel lies outside the time-series grid")
        parsed_dates = tuple(_parse_yyyymmdd(value) for value in dates)
        if any(
            current <= previous
            for previous, current in zip(parsed_dates, parsed_dates[1:])
        ):
            raise ValueError("dates are not strictly increasing")

        required_attrs = ("EPSG", "X_FIRST", "Y_FIRST", "X_STEP", "Y_STEP", "UNIT")
        missing_attrs = [key for key in required_attrs if key not in h5.attrs]
        if missing_attrs:
            raise ValueError(f"missing geocoding attributes: {', '.join(missing_attrs)}")
        unit = h5.attrs["UNIT"]
        unit = unit.decode("utf-8") if isinstance(unit, bytes) else str(unit)
        if unit.strip().lower() not in {"m", "meter", "meters", "metre", "metres"}:
            raise ValueError(f"timeseries UNIT must be metres, got {unit!r}")

        return ViewerTimeSeriesSource(
            path=path,
            corrections=corrections,
            dates=dates,
            spatial_shape=(int(stack.shape[1]), int(stack.shape[2])),
            pipeline_kind=pipeline_kind,
        )


def resolve_viewer_timeseries(project: WebProject) -> ViewerTimeSeriesSource:
    """Inspect the exact configured time series without filename-based inference."""
    return _inspect_viewer_timeseries(
        project.timeseries_path,
        corrections=project.corrections,
        pipeline_kind=project.pipeline_kind,
    )


def main() -> None:
    global PROJECTS_PATH, CACHE_ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=PROJECTS_PATH)
    parser.add_argument("--cache-dir", type=Path, default=CACHE_ROOT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    PROJECTS_PATH, CACHE_ROOT = args.config.resolve(), args.cache_dir.resolve()
    if args.validate:
        report, errors = validate_products(load_web_projects())
        print(json.dumps({"config": str(PROJECTS_PATH), "projects": report, "errors": errors}, indent=2))
        if errors:
            raise SystemExit(1)
        return
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(CACHE_ROOT / ".matplotlib"))
    load_web_projects()
    host, port = args.host, args.port
    server = ThreadingHTTPServer((host, port), ViewerHandler)
    print(f"InSAR web viewer listening on http://{host}:{port}")
    server.serve_forever()


def validate_products(projects: dict[str, WebProject]) -> tuple[list[dict[str, Any]], list[str]]:
    report, errors = [], []
    for project in projects.values():
        item: dict[str, Any] = {"id": project.project_id, "timeseries": "invalid"}
        try:
            source = resolve_viewer_timeseries(project)
            item.update(timeseries="valid", coverage=[source.first_date, source.last_date], corrections=list(project.corrections))
            if project.ifgram_stack_path is not None:
                if not project.ifgram_stack_path.is_file():
                    raise FileNotFoundError(f"{project.project_id}: ifgram_stack not found")
                network = network_connectivity(project, [_parse_yyyymmdd(token) for token in source.dates])
                item["network_assessed"] = bool(network.get("assessed"))
                if not network.get("assessed"):
                    raise ValueError(f"{project.project_id}: {network.get('note', 'invalid retained network')}")
            else:
                item["network_assessed"] = False
            if project.quality_path is not None and project.quality_dataset:
                quality, _ = interval_mean_coherence(project, expected_shape=source.spatial_shape,
                    start_date=source.first_date, end_date=source.last_date)
                item["quality_available"] = quality is not None
                if quality is None:
                    raise ValueError(f"{project.project_id}: configured quality layer unavailable or grid-incompatible")
            else:
                item["quality_available"] = False
            if project.geometry_path is not None:
                if not project.geometry_path.is_file():
                    raise FileNotFoundError(f"{project.project_id}: geometry file not found")
                read_geometry_arrays(project, expected_shape=source.spatial_shape)
                item["geometry_available"] = True
            else:
                item["geometry_available"] = False
            item["publication_status"] = project_publication_status(project)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            item["error"] = str(exc)
            errors.append(str(exc))
        report.append(item)
    return report, errors


class ViewerHandler(SimpleHTTPRequestHandler):
    """Serve static files plus small JSON API endpoints."""

    server_version = "InSARWebViewer/0.1"

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/projects":
                self._json_response(list_projects())
                return
            if parsed.path == "/api/combined-projects":
                self._json_response(list_combined_projects())
                return
            if parsed.path == "/api/gps-stations":
                project = parse_qs(parsed.query).get("project", [""])[0]
                self._json_response(gps_stations(project))
                return
            if parsed.path.startswith("/cache/"):
                self._serve_file(CACHE_ROOT / unquote(parsed.path[len("/cache/"):]))
                return
            if parsed.path in {"/", "/index.html"}:
                self._serve_file(STATIC_ROOT / "index.html")
                return
            self._serve_file(STATIC_ROOT / unquote(parsed.path.lstrip("/")))
        except Exception as exc:
            self._json_response({"error": type(exc).__name__, "message": str(exc)}, status=500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self._read_json_body()
            if self.path == "/api/map-coordinates":
                self._json_response(map_coordinates(payload))
                return
            if self.path == "/api/interval-map":
                self._json_response(interval_map(payload))
                return
            if self.path == "/api/combined-interval":
                self._json_response(combined_interval_map(payload))
                return
            if self.path == "/api/combined-pixel-timeseries":
                self._json_response(combined_pixel_timeseries(payload))
                return
            if self.path == "/api/combined-gps-timeseries":
                self._json_response(combined_gps_timeseries(payload))
                return
            if self.path == "/api/pixel-timeseries":
                self._json_response(pixel_timeseries(payload))
                return
            if self.path == "/api/gps-timeseries":
                self._json_response(gps_timeseries(payload))
                return
            if self.path == "/api/gps-vectors":
                self._json_response(gps_vectors(payload))
                return
            self.send_error(404, "Unknown API endpoint")
        except Exception as exc:
            self._json_response(
                {"error": type(exc).__name__, "message": str(exc)},
                status=500,
            )

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 1_000_000:
            raise ValueError("JSON request body exceeds 1 MB limit")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _json_response(self, payload: Any, *, status: int = 200) -> None:
        encoded = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _serve_file(self, path: Path) -> None:
        root = CACHE_ROOT if CACHE_ROOT in path.resolve().parents else STATIC_ROOT
        path = path.resolve()
        if path != root.resolve() and root.resolve() not in path.parents:
            self.send_error(404, "File not found")
            return
        allowed = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".ico"}
        if root == STATIC_ROOT:
            allowed |= {".html", ".css", ".js", ".svg", ".webp", ".gif"}
        if path.suffix.lower() not in allowed:
            self.send_error(404, "File not found")
            return
        if not path.exists() or not path.is_file():
            self.send_error(404, "File not found")
            return
        content_type = "text/plain"
        if path.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        elif path.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif path.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif path.suffix == ".png":
            content_type = "image/png"
        elif path.suffix in {".jpg", ".jpeg"}:
            content_type = "image/jpeg"
        elif path.suffix in {".tif", ".tiff"}:
            content_type = "image/tiff"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def list_projects() -> dict[str, Any]:
    """Return project metadata for the UI."""
    projects = []
    for project in load_web_projects().values():
        available, availability_reason, source = project_readiness_details(project)
        record = {
            "id": project.project_id,
            "label": project.label,
            "volcano": project.group,
            "orbit": project.orbit,
            "available": available,
            "availability_reason": availability_reason,
            "pipeline": project.pipeline_kind,
            "publication_status": project_publication_status(project),
            "capabilities": {
                "pixel_timeseries": source is not None,
                "masked_interval_map": bool(project.quality_path and project.quality_dataset),
                "network_assessed": bool(project.ifgram_stack_path and project.retained_dataset),
                "geometry": project.geometry_path is not None,
                "gnss": project.gps_path is not None,
            },
        }
        if source is not None:
            record["timeseries_source"] = viewer_source_payload(project, source)
        projects.append(record)
    return {"projects": projects}


def project_readiness(project: WebProject) -> tuple[bool, str]:
    """Return whether a project has the corrected products needed by the viewer."""
    try:
        available, reason, _ = project_readiness_details(project)
        return available, reason
    except Exception as exc:  # Defensive boundary for the project-list endpoint.
        return False, f"cannot inspect corrected time series: {exc}"


def project_readiness_details(
    project: WebProject,
) -> tuple[bool, str, ViewerTimeSeriesSource | None]:
    """Return viewer readiness plus the selected corrected product."""
    try:
        source = resolve_viewer_timeseries(project)
    except (FileNotFoundError, OSError, ValueError) as exc:
        return False, str(exc), None

    end_date = _parse_yyyymmdd(source.last_date).isoformat()
    missing = []
    if project.ifgram_stack_path is None:
        missing.append("no retained network/coherence stack configured")
    if project.quality_path is None or not project.quality_dataset:
        missing.append("no coherence quality layer configured")
    reason = f"time series available through {end_date}"
    if missing:
        reason += "; pixel series available, masked interval map unavailable: " + "; ".join(missing)
    return True, reason, source


def project_publication_status(project: WebProject) -> str:
    """Return only operator-declared publication state."""
    if project.publication_state_path is not None:
        try:
            declared = json.loads(project.publication_state_path.read_text(encoding="utf-8"))["status"]
            if isinstance(declared, str) and declared:
                return declared
        except (OSError, ValueError, KeyError, TypeError):
            return "unassessed"
    return project.publication_status


def viewer_source_payload(
    project: WebProject,
    source: ViewerTimeSeriesSource,
) -> dict[str, Any]:
    """Return corrected-source metadata including its publication state."""
    payload = source.to_payload()
    payload["publication_status"] = project_publication_status(project)
    return payload


def list_combined_projects() -> dict[str, Any]:
    """Return ASC/DESC project pairs that can be decomposed in the viewer."""
    projects = load_web_projects()
    by_volcano: dict[
        tuple[str, str], dict[str, tuple[WebProject, ViewerTimeSeriesSource]]
    ] = {}
    for project in projects.values():
        available, _, source = project_readiness_details(project)
        if not available or source is None:
            continue
        direction = project.orbit.upper()
        if direction.startswith("ASC"):
            orbit_key = "asc"
        elif direction.startswith("DESC"):
            orbit_key = "desc"
        else:
            orbit_key = ""
        if not orbit_key:
            continue
        group_key = (project.pipeline_kind, project.group)
        by_volcano.setdefault(group_key, {})[orbit_key] = (
            project,
            source,
        )

    pairs = []
    for (pipeline_kind, volcano), members in sorted(by_volcano.items()):
        asc_record = members.get("asc")
        desc_record = members.get("desc")
        if asc_record is None or desc_record is None:
            continue
        asc, asc_source = asc_record
        desc, desc_source = desc_record
        if not matching_bbox(asc.bbox, desc.bbox):
            continue
        coverage_start = max(asc_source.first_date, desc_source.first_date)
        coverage_end = min(asc_source.last_date, desc_source.last_date)
        if coverage_start > coverage_end:
            continue
        pair_id = f"{pipeline_kind}:{volcano}"
        publication_status = asc.publication_status if asc.publication_status == desc.publication_status else "unassessed"
        pairs.append(
            {
                "id": pair_id,
                "label": f"{volcano.replace('_', ' ').title()} ({pipeline_kind})",
                "volcano": volcano,
                "pipeline": pipeline_kind,
                "publication_status": publication_status,
                "asc_project": asc.project_id,
                "desc_project": desc.project_id,
                "available": True,
                "using_legacy_fallback": (
                    asc_source.using_legacy_fallback or desc_source.using_legacy_fallback
                ),
                "coverage_start_date": _parse_yyyymmdd(coverage_start).isoformat(),
                "coverage_end_date": _parse_yyyymmdd(coverage_end).isoformat(),
                "timeseries_sources": {
                    "asc": viewer_source_payload(asc, asc_source),
                    "desc": viewer_source_payload(desc, desc_source),
                },
            }
        )
    return {"projects": pairs}


def matching_bbox(left: tuple[float, float, float, float] | None, right: tuple[float, float, float, float] | None, *, tolerance: float = 1.0e-8) -> bool:
    """Return true for matching AOI boxes."""
    if left is None or right is None:
        return left is right
    return (
        all(abs(a - b) <= tolerance for a, b in zip(left, right, strict=True))
    )


def interval_map(payload: dict[str, Any]) -> dict[str, Any]:
    """Generate/read an ERA5 interval displacement PNG overlay."""
    project = get_project(str(payload["project"]))
    threshold = float(payload.get("coherence_threshold", 0.5))
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("coherence_threshold must be between 0 and 1")

    start_token = _date_token(str(payload["start_date"]))
    end_token = _date_token(str(payload["end_date"]))
    data = read_era5_timeseries(project)
    start_index, end_index = date_window_indices(data["dates"], start_token, end_token)
    resolved_start = data["dates"][start_index]
    resolved_end = data["dates"][end_index]

    displacement_cm = (data["stack"][end_index] - data["stack"][start_index]) * np.float32(100.0)
    coherence, coherence_source = interval_mean_coherence(
        project,
        expected_shape=displacement_cm.shape,
        start_date=resolved_start,
        end_date=resolved_end,
    )
    if coherence is None:
        raise ValueError("No interval coherence layer is available for this project/date window")

    mask = np.isfinite(displacement_cm) & np.isfinite(coherence) & (coherence >= threshold)
    display_reference = requested_display_reference(payload)
    reference_cell: tuple[int, int] | None = None
    reference_record: dict[str, Any] | None = None
    if display_reference is not None:
        reference_latitude, reference_longitude = display_reference
        reference_cell = point_to_row_col(
            reference_latitude,
            reference_longitude,
            data["profile"],
        )
        reference_row, reference_col = reference_cell
        if not mask[reference_row, reference_col]:
            raise ValueError(
                "Temporary reference must be a finite pixel above the coherence threshold"
            )
        reference_offset_cm = float(displacement_cm[reference_row, reference_col])
        displacement_cm = displacement_cm - np.float32(reference_offset_cm)
        reference_record = {
            "latitude": reference_latitude,
            "longitude": reference_longitude,
            "row": reference_row,
            "col": reference_col,
            "interval_offset_cm": reference_offset_cm,
        }
    crop_window, crop_source = project_crop_window(project, data["profile"])
    display_profile = data["profile"]
    if crop_window is not None:
        displacement_cm = crop_array(displacement_cm, crop_window)
        coherence = crop_array(coherence, crop_window)
        mask = crop_array(mask, crop_window)
        display_profile = cropped_profile(data["profile"], crop_window)
    output = cache_interval_path(
        project,
        start_token,
        end_token,
        threshold,
        reference_cell=reference_cell,
    )
    hillshade, hillshade_source = read_hillshade(
        project,
        data["profile"],
        crop_window=crop_window,
        expected_shape=displacement_cm.shape,
    )
    contours = read_dem_contours(
        project,
        crop_window=crop_window,
        expected_shape=displacement_cm.shape,
    )
    look_direction = read_satellite_look_direction(project)
    render_size = write_interval_overlay_png(
        output,
        displacement_cm,
        mask,
        hillshade=hillshade,
    )
    export_paths = write_map_exports(
        output,
        displacement_cm,
        mask,
        display_profile,
        tags={
            "component": "line_of_sight",
            "project": project.project_id,
            "start_date": _parse_yyyymmdd(resolved_start).isoformat(),
            "end_date": _parse_yyyymmdd(resolved_end).isoformat(),
            "coherence_threshold": f"{threshold:.6g}",
            "coherence_source": coherence_source,
            "reference": "temporary_display" if reference_record else "mintpy",
            "positive_direction": "toward_satellite",
            "los_convention": "positive_toward_satellite",
        },
    )

    dates = [_parse_yyyymmdd(value) for value in data["dates"][start_index : end_index + 1]]
    network = network_connectivity(project, dates)
    bounds = profile_bounds_latlon(display_profile)
    finite_values = displacement_cm[mask]
    color_limit = robust_limit(finite_values)
    return {
        "pipeline": project.pipeline_kind,
        "image_url": f"/cache/{output.relative_to(CACHE_ROOT).as_posix()}",
        "downloads": download_payload(
            export_paths,
            filename_stem=(
                f"{project.project_id}_{_date_token(resolved_start)}_"
                f"{_date_token(resolved_end)}_los_cm"
            ),
        ),
        "timeseries_source": data["timeseries_source"],
        "bounds": bounds,
        "raster_grid": raster_grid_metadata(display_profile),
        "mintpy_reference_point": {
            "latitude": (data["reference_point"][0] if data["reference_point"] else None),
            "longitude": (data["reference_point"][1] if data["reference_point"] else None),
        },
        "display_reference_point": reference_record,
        "resolved_start_date": _parse_yyyymmdd(resolved_start).isoformat(),
        "resolved_end_date": _parse_yyyymmdd(resolved_end).isoformat(),
        "coherence_threshold": threshold,
        "coherence_source": coherence_source,
        "hillshade_source": hillshade_source,
        "dem_contours": contours,
        "display_crop": crop_source,
        "color_limit_cm": color_limit,
        "native_raster_width": int(displacement_cm.shape[1]),
        "native_raster_height": int(displacement_cm.shape[0]),
        "render_width": render_size[0],
        "render_height": render_size[1],
        "satellite_look_direction": look_direction,
        "unit": "cm",
        "los_convention": "positive is toward the satellite",
        "network_warning": any(band["kind"] == "disconnected" for band in network["bands"]),
        "network_gaps": [
            {
                "start_date": _parse_yyyymmdd(band["start_date"]).isoformat(),
                "end_date": _parse_yyyymmdd(band["end_date"]).isoformat(),
            }
            for band in network["bands"]
            if band["kind"] == "disconnected"
        ],
        "network": network,
        "valid_pixel_count": int(mask.sum()),
    }


def decomposition_reference(
    asc_project: WebProject,
    desc_project: WebProject,
    asc: dict[str, Any],
    desc: dict[str, Any],
    payload: dict[str, Any],
    threshold: float = 0.5,
) -> tuple[tuple[float, float] | None, str | None]:
    """Require assessed networks and a common source or explicitly chosen reference."""
    for project, data in ((asc_project, asc), (desc_project, desc)):
        network = network_connectivity(project, [_parse_yyyymmdd(token) for token in data["dates"]])
        if not network.get("assessed"):
            return None, f"{project.project_id}: retained network is unassessed; decomposition is blocked"
    a, d = asc.get("reference_point"), desc.get("reference_point")
    if a is None or d is None:
        return None, "Cannot verify ASC/DESC source reference pixels: HDF5 REF_X/REF_Y is missing"
    lat_scale = 111_320.0
    distance = float(np.hypot((a[0] - d[0]) * lat_scale, (a[1] - d[1]) * lat_scale * np.cos(np.deg2rad((a[0] + d[0]) / 2))))
    selected = requested_display_reference(payload)
    configured = asc_project.reference_point
    if selected is None and configured is not None and desc_project.reference_point is not None:
        c1, c2 = configured, desc_project.reference_point
        same = np.hypot((c1[0]-c2[0])*lat_scale, (c1[1]-c2[1])*lat_scale*np.cos(np.deg2rad((c1[0]+c2[0])/2))) <= 1.0
        if same:
            selected = configured
    if distance > 1.0 and selected is None:
        return None, "ASC/DESC HDF5 reference pixels differ; choose/configure one common display reference within both grids"
    if selected is not None:
        try:
            point_to_row_col(*selected, asc["profile"])
            point_to_row_col(*selected, desc["profile"])
        except ValueError:
            return None, "Common display reference must fall within both ASC and DESC grids"
    if selected is not None:
        for data in (asc, desc):
            if "coherence" in data:
                row, col = point_to_row_col(*selected, data["profile"])
                if (not np.isfinite(data["displacement_cm"][row, col])
                        or not np.isfinite(data["coherence"][row, col])
                        or data["coherence"][row, col] < threshold
                        or not np.isfinite(data["los_east"][row, col])
                        or not np.isfinite(data["los_up"][row, col])):
                    return None, "Common display reference must be finite and valid in both source grids"
    return selected, None


def combined_interval_map(payload: dict[str, Any]) -> dict[str, Any]:
    """Return east/up interval decomposition from matched ASC/DESC ERA5 products."""
    asc_project, desc_project = get_combined_pair(str(payload["project"]))
    threshold = float(payload.get("coherence_threshold", 0.5))
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("coherence_threshold must be between 0 and 1")

    start_token = _date_token(str(payload["start_date"]))
    end_token = _date_token(str(payload["end_date"]))
    asc = read_interval_grid(asc_project, start_token, end_token)
    desc = read_interval_grid(desc_project, start_token, end_token)

    asc_network = network_connectivity(
        asc_project,
        [_parse_yyyymmdd(value) for value in asc["dates"][asc["start_index"] : asc["end_index"] + 1]],
    )
    desc_network = network_connectivity(
        desc_project,
        [_parse_yyyymmdd(value) for value in desc["dates"][desc["start_index"] : desc["end_index"] + 1]],
    )
    if not asc_network.get("assessed") or not desc_network.get("assessed"):
        return {"blocked": True, "pipeline": asc_project.pipeline_kind,
                "message": "Cannot decompose: retained network evidence is unavailable or malformed.",
                "asc_project": asc_project.project_id, "desc_project": desc_project.project_id}
    reference, reference_error = decomposition_reference(asc_project, desc_project, asc, desc, payload, threshold)
    if reference_error:
        return {"blocked": True, "pipeline": asc_project.pipeline_kind, "message": reference_error,
                "asc_project": asc_project.project_id, "desc_project": desc_project.project_id,
                "timeseries_sources": {"asc": asc["timeseries_source"], "desc": desc["timeseries_source"]}}
    asc_gaps = disconnected_gaps(asc_network)
    desc_gaps = disconnected_gaps(desc_network)
    if asc_gaps or desc_gaps:
        return {
            "blocked": True,
            "pipeline": asc_project.pipeline_kind,
            "message": "Selected window crosses disconnected retained-interferogram network spans.",
            "asc_project": asc_project.project_id,
            "desc_project": desc_project.project_id,
            "asc_gaps": asc_gaps,
            "desc_gaps": desc_gaps,
            "timeseries_sources": {
                "asc": asc["timeseries_source"],
                "desc": desc["timeseries_source"],
            },
            "using_legacy_fallback": (
                asc["timeseries_source"]["using_legacy_fallback"]
                or desc["timeseries_source"]["using_legacy_fallback"]
            ),
        }

    reference_cell: tuple[int, int] | None = None
    reference_record: dict[str, Any] | None = None
    if reference is not None:
        reference_cell = point_to_row_col(*reference, asc["profile"])
        desc_reference_cell = point_to_row_col(*reference, desc["profile"])
        asc_offset_cm = float(asc["displacement_cm"][reference_cell])
        desc_offset_cm = float(desc["displacement_cm"][desc_reference_cell])
        # LOS reference offsets must be removed on native grids before inversion.
        asc["displacement_cm"] = asc["displacement_cm"] - np.float32(asc_offset_cm)
        desc["displacement_cm"] = desc["displacement_cm"] - np.float32(desc_offset_cm)
        reference_record = {
            "latitude": reference[0], "longitude": reference[1],
            "row": reference_cell[0], "col": reference_cell[1],
            "asc_interval_offset_cm": asc_offset_cm,
            "desc_interval_offset_cm": desc_offset_cm,
        }

    desc_on_asc = reproject_interval_grid(desc, asc["profile"])
    determinant = asc["los_east"] * desc_on_asc["los_up"] - asc["los_up"] * desc_on_asc["los_east"]
    coherence_min = np.minimum(asc["coherence"], desc_on_asc["coherence"]).astype(np.float32)
    valid = (
        np.isfinite(asc["displacement_cm"])
        & np.isfinite(desc_on_asc["displacement_cm"])
        & np.isfinite(asc["los_east"])
        & np.isfinite(asc["los_up"])
        & np.isfinite(desc_on_asc["los_east"])
        & np.isfinite(desc_on_asc["los_up"])
        & np.isfinite(coherence_min)
        & (coherence_min >= np.float32(threshold))
        & (np.abs(determinant) > np.float32(1.0e-4))
    )
    east_cm = (
        asc["displacement_cm"] * desc_on_asc["los_up"]
        - desc_on_asc["displacement_cm"] * asc["los_up"]
    ) / determinant
    up_cm = (
        asc["los_east"] * desc_on_asc["displacement_cm"]
        - desc_on_asc["los_east"] * asc["displacement_cm"]
    ) / determinant

    if reference_cell is not None:
        reference_row, reference_col = reference_cell
        if not valid[reference_row, reference_col]:
            raise ValueError(
                "Temporary reference must be finite in both components and above "
                "the ASC/DESC coherence threshold"
            )

    crop_window, crop_source = project_crop_window(asc_project, asc["profile"])
    display_profile = asc["profile"]
    if crop_window is not None:
        east_cm = crop_array(east_cm, crop_window)
        up_cm = crop_array(up_cm, crop_window)
        valid = crop_array(valid, crop_window)
        display_profile = cropped_profile(asc["profile"], crop_window)
    limit = max(robust_limit(east_cm[valid]), robust_limit(up_cm[valid]))

    hillshade, hillshade_source = read_hillshade(
        asc_project,
        asc["profile"],
        crop_window=crop_window,
        expected_shape=east_cm.shape,
    )
    contours = read_dem_contours(
        asc_project,
        crop_window=crop_window,
        expected_shape=east_cm.shape,
    )
    output_east = cache_combined_path(
        asc_project,
        desc_project,
        start_token,
        end_token,
        threshold,
        "east",
        reference_cell=reference_cell,
    )
    output_up = cache_combined_path(
        asc_project,
        desc_project,
        start_token,
        end_token,
        threshold,
        "up",
        reference_cell=reference_cell,
    )
    east_size = write_interval_overlay_png(output_east, east_cm, valid, hillshade=hillshade, color_limit=limit)
    up_size = write_interval_overlay_png(output_up, up_cm, valid, hillshade=hillshade, color_limit=limit)
    common_tags = {
        "asc_project": asc_project.project_id,
        "desc_project": desc_project.project_id,
        "start_date": max(
            _parse_yyyymmdd(asc["resolved_start"]).isoformat(),
            _parse_yyyymmdd(desc["resolved_start"]).isoformat(),
        ),
        "end_date": min(
            _parse_yyyymmdd(asc["resolved_end"]).isoformat(),
            _parse_yyyymmdd(desc["resolved_end"]).isoformat(),
        ),
        "coherence_threshold": f"{threshold:.6g}",
        "north_assumption": "zero",
        "reference": "temporary_display" if reference_record else "mintpy",
    }
    east_exports = write_map_exports(
        output_east,
        east_cm,
        valid,
        display_profile,
        tags={**common_tags, "component": "east", "positive_direction": "east"},
    )
    up_exports = write_map_exports(
        output_up,
        up_cm,
        valid,
        display_profile,
        tags={**common_tags, "component": "vertical", "positive_direction": "up"},
    )
    bounds = profile_bounds_latlon(display_profile)
    combined_resolved_start = max(asc["resolved_start"], desc["resolved_start"])
    combined_resolved_end = min(asc["resolved_end"], desc["resolved_end"])
    combined_filename_stem = (
        f"{asc_project.group}_{asc_project.pipeline_kind}_"
        f"{_date_token(combined_resolved_start)}_{_date_token(combined_resolved_end)}"
    )

    return {
        "blocked": False,
        "pipeline": asc_project.pipeline_kind,
        "project": asc_project.group,
        "asc_project": asc_project.project_id,
        "desc_project": desc_project.project_id,
        "timeseries_sources": {
            "asc": asc["timeseries_source"],
            "desc": desc["timeseries_source"],
        },
        "using_legacy_fallback": (
            asc["timeseries_source"]["using_legacy_fallback"]
            or desc["timeseries_source"]["using_legacy_fallback"]
        ),
        "east_image_url": f"/cache/{output_east.relative_to(CACHE_ROOT).as_posix()}",
        "up_image_url": f"/cache/{output_up.relative_to(CACHE_ROOT).as_posix()}",
        "downloads": {
            "up": download_payload(up_exports, filename_stem=f"{combined_filename_stem}_vertical_cm"),
            "east": download_payload(east_exports, filename_stem=f"{combined_filename_stem}_east_cm"),
        },
        "bounds": bounds,
        "raster_grid": raster_grid_metadata(display_profile),
        "mintpy_reference_points": {
            "asc": {
                "project": asc_project.project_id,
                "latitude": (asc["reference_point"][0] if asc["reference_point"] else None),
                "longitude": (asc["reference_point"][1] if asc["reference_point"] else None),
            },
            "desc": {
                "project": desc_project.project_id,
                "latitude": (desc["reference_point"][0] if desc["reference_point"] else None),
                "longitude": (desc["reference_point"][1] if desc["reference_point"] else None),
            },
        },
        "display_reference_point": reference_record,
        "resolved_start_date": max(
            _parse_yyyymmdd(asc["resolved_start"]).isoformat(),
            _parse_yyyymmdd(desc["resolved_start"]).isoformat(),
        ),
        "resolved_end_date": min(
            _parse_yyyymmdd(asc["resolved_end"]).isoformat(),
            _parse_yyyymmdd(desc["resolved_end"]).isoformat(),
        ),
        "coherence_threshold": threshold,
        "display_crop": crop_source,
        "hillshade_source": hillshade_source,
        "dem_contours": contours,
        "color_limit_cm": limit,
        "native_raster_width": int(east_cm.shape[1]),
        "native_raster_height": int(east_cm.shape[0]),
        "render_width": east_size[0],
        "render_height": east_size[1],
        "up_render_width": up_size[0],
        "up_render_height": up_size[1],
        "valid_pixel_count": int(valid.sum()),
        "unit": "cm",
        "assumption": "north displacement is assumed zero; ASC/DESC LOS solve east and up components.",
    }


def pixel_timeseries(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ERA5 LOS time series for one clicked pixel."""
    project = get_project(str(payload["project"]))
    latitude = float(payload["lat"])
    longitude = float(payload["lon"])
    data = read_era5_timeseries(project)
    row, col = point_to_row_col(latitude, longitude, data["profile"])
    start_token = _date_token(str(payload.get("start_date", data["dates"][0])))
    end_token = _date_token(str(payload.get("end_date", data["dates"][-1])))
    start_index, end_index = date_window_indices(data["dates"], start_token, end_token)
    series = data["stack"][start_index : end_index + 1, row, col]
    reference_series: np.ndarray | None = None
    display_reference = requested_display_reference(payload)
    reference_record: dict[str, Any] | None = None
    if display_reference is not None:
        reference_latitude, reference_longitude = display_reference
        reference_row, reference_col = point_to_row_col(
            reference_latitude,
            reference_longitude,
            data["profile"],
        )
        reference_series = np.asarray(
            data["stack"][start_index : end_index + 1, reference_row, reference_col],
            dtype=np.float32,
        )
        reference_record = {
            "latitude": reference_latitude,
            "longitude": reference_longitude,
            "row": reference_row,
            "col": reference_col,
        }
    values = rereference_insar_series(series, reference_series) * np.float32(100.0)
    return {
        "pipeline": project.pipeline_kind,
        "label": f"InSAR pixel {latitude:.5f}, {longitude:.5f}",
        "kind": "insar",
        "latitude": latitude,
        "longitude": longitude,
        "row": row,
        "col": col,
        "display_reference_point": reference_record,
        "dates": [
            _parse_yyyymmdd(value).isoformat()
            for value in data["dates"][start_index : end_index + 1]
        ],
        "values_cm": [float(value) if np.isfinite(value) else None for value in values],
        "timeseries_source": data["timeseries_source"],
        "unit": "cm",
        "los_convention": "positive is toward the satellite",
    }


def gps_stations(project_id: str) -> dict[str, Any]:
    """Return GPS station list filtered to a project's AOI."""
    project = get_project(project_id)
    series = load_project_gps(project)
    stations = [
        {
            "label": item.label,
            "latitude": item.latitude,
            "longitude": item.longitude,
            "epoch_count": len(item.dates),
        }
        for item in series
        if item.latitude is not None and item.longitude is not None
    ]
    return {"stations": stations}


def gps_timeseries(payload: dict[str, Any]) -> dict[str, Any]:
    """Return one GPS station projected to project LOS."""
    project = get_project(str(payload["project"]))
    station = str(payload["station"]).strip().lower()
    start_token = _date_token(str(payload.get("start_date", "19000101")))
    end_token = _date_token(str(payload.get("end_date", "21000101")))
    projected = project_gps_series(load_project_gps(project), project, start_token, end_token)
    for item in projected:
        if item.label.lower() != station:
            continue
        return {
            "pipeline": project.pipeline_kind,
            "label": item.label,
            "kind": "gps",
            "latitude": item.latitude,
            "longitude": item.longitude,
            "dates": [value.isoformat() for value in item.dates],
            "values_cm": item.values_cm,
            "sigma_cm": item.values_sigma_cm,
            "component": item.component,
            "incidence_angle_deg": item.incidence_angle_deg,
            "azimuth_angle_deg": item.azimuth_angle_deg,
            "los_unit_east": item.los_unit_east,
            "los_unit_north": item.los_unit_north,
            "los_unit_up": item.los_unit_up,
            "unit": "cm",
            "los_convention": "positive is toward the satellite",
        }
    raise ValueError(f"GPS station not found in AOI: {payload['station']}")


def decompose_pixel_series(
    asc: dict[str, Any],
    desc: dict[str, Any],
    matches: list[dict[str, Any]],
    *,
    asc_cell: tuple[int, int],
    desc_cell: tuple[int, int],
    asc_los_east: np.ndarray,
    asc_los_up: np.ndarray,
    desc_los_east: np.ndarray,
    desc_los_up: np.ndarray,
    asc_reference_cell: tuple[int, int] | None = None,
    desc_reference_cell: tuple[int, int] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Decompose matched ASC/DESC LOS samples into east and up series."""
    asc_row, asc_col = asc_cell
    desc_row, desc_col = desc_cell
    asc_los = np.asarray(
        [asc["stack"][match["asc_index"], asc_row, asc_col] for match in matches],
        dtype=np.float32,
    )
    desc_los = np.asarray(
        [desc["stack"][match["desc_index"], desc_row, desc_col] for match in matches],
        dtype=np.float32,
    )
    asc_reference = None
    desc_reference = None
    if asc_reference_cell is not None and desc_reference_cell is not None:
        asc_reference = np.asarray([asc["stack"][match["asc_index"], *asc_reference_cell] for match in matches], dtype=np.float32)
        desc_reference = np.asarray([desc["stack"][match["desc_index"], *desc_reference_cell] for match in matches], dtype=np.float32)
        if not np.isfinite(asc_reference).all() or not np.isfinite(desc_reference).all():
            raise ValueError("Common display reference series must be finite in both source grids")
    asc_los_cm = rereference_insar_series(asc_los, asc_reference) * np.float32(100.0)
    desc_los_cm = rereference_insar_series(desc_los, desc_reference) * np.float32(100.0)

    a = float(asc_los_east[asc_row, asc_col])
    b = float(asc_los_up[asc_row, asc_col])
    c = float(desc_los_east[desc_row, desc_col])
    d = float(desc_los_up[desc_row, desc_col])
    determinant = a * d - b * c
    if not np.isfinite(determinant) or abs(determinant) <= 1.0e-4:
        raise ValueError("ASC/DESC LOS geometry is ill-conditioned at this pixel")

    east_cm = (asc_los_cm * d - desc_los_cm * b) / np.float32(determinant)
    up_cm = (a * desc_los_cm - c * asc_los_cm) / np.float32(determinant)
    return east_cm, up_cm


def combined_pixel_timeseries(payload: dict[str, Any]) -> dict[str, Any]:
    """Return east/up time series for one clicked pixel from paired ASC/DESC LOS."""
    asc_project, desc_project = get_combined_pair(str(payload["project"]))
    latitude = float(payload["lat"])
    longitude = float(payload["lon"])
    start_token = _date_token(str(payload["start_date"]))
    end_token = _date_token(str(payload["end_date"]))

    asc = read_era5_timeseries(asc_project)
    desc = read_era5_timeseries(desc_project)
    reference, reference_error = decomposition_reference(asc_project, desc_project, asc, desc, payload)
    if reference_error:
        raise ValueError(reference_error)
    matches = match_asc_desc_dates(asc["dates"], desc["dates"], start_token, end_token)
    if len(matches) < 2:
        raise ValueError("Need at least two matched ASC/DESC acquisition dates in this window")

    asc_dates = [match["asc_token"] for match in matches]
    desc_dates = [match["desc_token"] for match in matches]
    asc_network = network_connectivity(asc_project, [_parse_yyyymmdd(value) for value in asc_dates])
    desc_network = network_connectivity(desc_project, [_parse_yyyymmdd(value) for value in desc_dates])
    if not asc_network.get("assessed") or not desc_network.get("assessed"):
        raise ValueError("Cannot decompose: retained network evidence is unavailable or malformed")
    asc_gaps = disconnected_gaps(asc_network)
    desc_gaps = disconnected_gaps(desc_network)
    if asc_gaps or desc_gaps:
        raise ValueError(
            "Cannot plot decomposed time series across disconnected networks. "
            f"ASC gaps: {asc_gaps or 'none'}; DESC gaps: {desc_gaps or 'none'}"
        )

    asc_row, asc_col = point_to_row_col(latitude, longitude, asc["profile"])
    desc_row, desc_col = point_to_row_col(latitude, longitude, desc["profile"])
    asc_geometry = read_geometry_arrays(asc_project, expected_shape=asc["stack"].shape[1:])
    desc_geometry = read_geometry_arrays(desc_project, expected_shape=desc["stack"].shape[1:])
    asc_los_east, _, asc_los_up = los_unit_arrays(
        asc_geometry["incidence"],
        asc_geometry["azimuth"],
    )
    desc_los_east, _, desc_los_up = los_unit_arrays(
        desc_geometry["incidence"],
        desc_geometry["azimuth"],
    )

    display_reference = reference
    reference_record: dict[str, Any] | None = None
    reference_asc_cell = None
    reference_desc_cell = None
    if display_reference is not None:
        reference_latitude, reference_longitude = display_reference
        reference_asc_cell = point_to_row_col(
            reference_latitude,
            reference_longitude,
            asc["profile"],
        )
        reference_desc_cell = point_to_row_col(
            reference_latitude,
            reference_longitude,
            desc["profile"],
        )
        reference_record = {
            "latitude": reference_latitude,
            "longitude": reference_longitude,
            "asc_row": reference_asc_cell[0],
            "asc_col": reference_asc_cell[1],
            "desc_row": reference_desc_cell[0],
            "desc_col": reference_desc_cell[1],
        }
    east_cm, up_cm = decompose_pixel_series(
        asc, desc, matches,
        asc_cell=(asc_row, asc_col), desc_cell=(desc_row, desc_col),
        asc_los_east=asc_los_east, asc_los_up=asc_los_up,
        desc_los_east=desc_los_east, desc_los_up=desc_los_up,
        asc_reference_cell=reference_asc_cell, desc_reference_cell=reference_desc_cell,
    )
    dates = [match["plot_date"].isoformat() for match in matches]
    label = f"InSAR pixel {latitude:.5f}, {longitude:.5f}"
    return {
        "pipeline": asc_project.pipeline_kind,
        "label": label,
        "kind": "insar",
        "latitude": latitude,
        "longitude": longitude,
        "asc_row": asc_row,
        "asc_col": asc_col,
        "desc_row": desc_row,
        "desc_col": desc_col,
        "display_reference_point": reference_record,
        "dates": dates,
        "asc_dates": [_parse_yyyymmdd(value).isoformat() for value in asc_dates],
        "desc_dates": [_parse_yyyymmdd(value).isoformat() for value in desc_dates],
        "timeseries_sources": {
            "asc": asc["timeseries_source"],
            "desc": desc["timeseries_source"],
        },
        "using_legacy_fallback": (
            asc["timeseries_source"]["using_legacy_fallback"]
            or desc["timeseries_source"]["using_legacy_fallback"]
        ),
        "unit": "cm",
        "assumption": "north displacement is assumed zero; ASC/DESC LOS solve east and up components.",
        "series": {
            "up": {
                "label": label,
                "kind": "insar",
                "component": "up",
                "dates": dates,
                "values_cm": nullable_float_list(up_cm),
            },
            "east": {
                "label": label,
                "kind": "insar",
                "component": "east",
                "dates": dates,
                "values_cm": nullable_float_list(east_cm),
            },
        },
    }


def combined_gps_timeseries(payload: dict[str, Any]) -> dict[str, Any]:
    """Return one GPS station east or up component for the combined viewer."""
    asc_project, _ = get_combined_pair(str(payload["project"]))
    station = str(payload["station"]).strip().lower()
    component = str(payload["component"]).strip().lower()
    if component not in {"east", "up"}:
        raise ValueError("component must be 'east' or 'up'")

    start_date = _parse_yyyymmdd(_date_token(str(payload.get("start_date", "19000101"))))
    end_date = _parse_yyyymmdd(_date_token(str(payload.get("end_date", "21000101"))))
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")

    for item in load_project_gps(asc_project):
        if item.label.lower() != station:
            continue
        source_values = item.up_cm if component == "up" else item.east_cm
        source_sigma = item.up_sigma_cm if component == "up" else item.east_sigma_cm
        if source_values is None:
            raise ValueError(f"GPS station {item.label} has no {component} component")
        selected = [
            index
            for index, value in enumerate(item.dates)
            if start_date <= value <= end_date
        ]
        if not selected:
            raise ValueError(f"GPS station {item.label} has no data in this date window")
        values = [source_values[index] for index in selected]
        sigma = [source_sigma[index] for index in selected] if source_sigma is not None else None
        return {
            "pipeline": asc_project.pipeline_kind,
            "label": item.label,
            "kind": "gps",
            "component": component,
            "latitude": item.latitude,
            "longitude": item.longitude,
            "dates": [item.dates[index].isoformat() for index in selected],
            "values_cm": rereference_values(values),
            "sigma_cm": rereference_uncertainty(sigma) if sigma is not None else None,
            "unit": "cm",
            "reference": "GPS component is re-referenced to the first selected epoch.",
        }
    raise ValueError(f"GPS station not found in AOI: {payload['station']}")


def gps_vectors(payload: dict[str, Any]) -> dict[str, Any]:
    """Return raw GPS ENU displacement vectors inside the selected date window."""
    mode = str(payload.get("mode", "los")).strip().lower()
    if mode == "combined":
        project, _ = get_combined_pair(str(payload["project"]))
    else:
        project = get_project(str(payload["project"]))

    start_date = _parse_yyyymmdd(_date_token(str(payload.get("start_date", "19000101"))))
    end_date = _parse_yyyymmdd(_date_token(str(payload.get("end_date", "21000101"))))
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")

    vectors = []
    for item in load_project_gps(project):
        if (
            item.latitude is None
            or item.longitude is None
            or item.east_cm is None
            or item.north_cm is None
            or item.up_cm is None
        ):
            continue
        indices = [
            index
            for index, value in enumerate(item.dates)
            if start_date <= value <= end_date
        ]
        if len(indices) < 2:
            continue
        first = indices[0]
        last = indices[-1]
        east = float(item.east_cm[last] - item.east_cm[first])
        north = float(item.north_cm[last] - item.north_cm[first])
        up = float(item.up_cm[last] - item.up_cm[first])
        east_sigma = endpoint_sigma(item.east_sigma_cm, first, last)
        north_sigma = endpoint_sigma(item.north_sigma_cm, first, last)
        up_sigma = endpoint_sigma(item.up_sigma_cm, first, last)
        vectors.append(
            {
                "label": item.label,
                "latitude": item.latitude,
                "longitude": item.longitude,
                "start_date": item.dates[first].isoformat(),
                "end_date": item.dates[last].isoformat(),
                "epoch_count": len(indices),
                "east_cm": east,
                "north_cm": north,
                "up_cm": up,
                "horizontal_cm": float(np.hypot(east, north)),
                "east_sigma_cm": east_sigma,
                "north_sigma_cm": north_sigma,
                "up_sigma_cm": up_sigma,
                "horizontal_sigma_cm": (
                    float(np.hypot(east_sigma, north_sigma))
                    if east_sigma is not None and north_sigma is not None
                    else None
                ),
            }
        )
    return {
        "project": project.project_id,
        "mode": mode,
        "vectors": vectors,
        "unit": "cm",
        "reference": "Each vector is last GPS epoch minus first GPS epoch inside the selected date window.",
    }


def load_web_projects() -> dict[str, WebProject]:
    projects = {}
    for item in load_projects(PROJECTS_PATH).values():
        projects[item.id] = WebProject(
            project_id=item.id, label=item.label, group=item.group, orbit=item.orbit,
            look_direction=item.look_direction, bbox=item.bbox,
            reference_point=item.reference_point, timeseries_path=item.timeseries_path,
            corrections=item.corrections, ifgram_stack_path=item.ifgram_stack_path,
            retained_dataset=item.retained_dataset, quality_path=item.quality_path,
            quality_dataset=item.quality_dataset, quality_label=item.quality_label,
            quality_type=item.quality_type, geometry_path=item.geometry_path,
            incidence_dataset=item.incidence_dataset, azimuth_dataset=item.azimuth_dataset,
            azimuth_convention=item.azimuth_convention, gps_path=item.gnss_path,
            pipeline_kind=item.mission, publication_status=item.publication_status,
            publication_state_path=item.publication_state_path,
        )
    return projects


def project_gps_series(series_list: list[GPSSeries], project: WebProject, start: str, end: str) -> list[GPSSeries]:
    selected = []
    for series in series_list:
        ids = [i for i, d in enumerate(series.dates) if _parse_yyyymmdd(start) <= d <= _parse_yyyymmdd(end)]
        if not ids:
            continue
        dates = [series.dates[i] for i in ids]
        incidence = azimuth = None
        unit = (None, None, None)
        if series.values_cm is not None:
            values = [v - series.values_cm[ids[0]] for v in (series.values_cm[i] for i in ids)]
            sigma = _rereference_sigma([series.values_sigma_cm[i] for i in ids]) if series.values_sigma_cm else None
        else:
            if project.geometry_path is None:
                raise ValueError("GPS ENU projection unavailable: project geometry is not configured")
            data = read_era5_timeseries(project)
            geom = read_geometry_arrays(project, expected_shape=data["stack"].shape[1:])
            row, col = point_to_row_col(float(series.latitude), float(series.longitude), data["profile"])
            incidence, azimuth = float(geom["incidence"][row, col]), float(geom["azimuth"][row, col])
            unit = los_unit_vector(incidence, azimuth)
            raw = [series.east_cm[i]*unit[0] + series.north_cm[i]*unit[1] + series.up_cm[i]*unit[2] for i in ids]
            values = [v - raw[0] for v in raw]
            sigma = _project_gnss_sigma(series, ids, unit)
            series.component = "GPS projected to satellite LOS"
        selected.append(GPSSeries(series.label, dates, values, series.component, series.latitude, series.longitude,
            values_sigma_cm=sigma,
            incidence_angle_deg=incidence, azimuth_angle_deg=azimuth, los_unit_east=unit[0], los_unit_north=unit[1], los_unit_up=unit[2], metadata=series.metadata))
    return selected


def _rereference_sigma(values: list[float]) -> list[float]:
    return [0.0 if i == 0 else float(np.hypot(value, values[0])) for i, value in enumerate(values)]


def _project_gnss_sigma(series: GPSSeries, ids: list[int], unit: tuple[float, float, float]) -> list[float] | None:
    terms = (series.east_sigma_cm, series.north_sigma_cm, series.up_sigma_cm,
             series.corr_east_north, series.corr_east_up, series.corr_north_up)
    if any(values is None for values in terms):
        return None
    e, n, u = unit
    projected = []
    for i in ids:
        se, sn, su, ren, reu, rnu = (float(values[i]) for values in terms)
        variance = e*e*se*se + n*n*sn*sn + u*u*su*su + 2*e*n*ren*se*sn + 2*e*u*reu*se*su + 2*n*u*rnu*sn*su
        projected.append(float(np.sqrt(max(variance, 0.0))))
    return _rereference_sigma(projected)


def get_project(project_id: str) -> WebProject:
    """Return one web project by ID."""
    projects = load_web_projects()
    if project_id not in projects:
        raise ValueError(f"Unknown project: {project_id}")
    return projects[project_id]


def get_combined_pair(pair_id: str) -> tuple[WebProject, WebProject]:
    """Return ASC/DESC projects for one combined viewer project."""
    for item in list_combined_projects()["projects"]:
        if item["id"] == pair_id:
            asc = get_project(item["asc_project"])
            desc = get_project(item["desc_project"])
            validate_combined_pair(asc, desc)
            return asc, desc
    raise ValueError(f"Unknown or unavailable ASC/DESC pair: {pair_id}")


def validate_combined_pair(asc: WebProject, desc: WebProject) -> None:
    """Reject cross-mission or spatially incompatible decomposition inputs."""
    if asc.pipeline_kind != desc.pipeline_kind:
        raise ValueError("ASC/DESC decomposition cannot mix missions")
    if asc.group != desc.group:
        raise ValueError("ASC/DESC decomposition projects must use the same configured group")
    if asc.orbit != "ASC":
        raise ValueError(f"Expected an ascending project, got {asc.project_id}")
    if desc.orbit != "DESC":
        raise ValueError(f"Expected a descending project, got {desc.project_id}")
    if not matching_bbox(asc.bbox, desc.bbox):
        raise ValueError("ASC/DESC decomposition projects must use the same AOI")


def read_era5_timeseries(project: WebProject) -> dict[str, Any]:
    """Read the best complete corrected MintPy time-series stack for display."""
    source = resolve_viewer_timeseries(project)
    with h5py.File(source.path, "r") as h5:
        dates = [_decode_h5_date(value) for value in h5["date"][...]]
        stack = np.asarray(h5["timeseries"][...], dtype=np.float32)
        profile = profile_from_attrs(h5.attrs, stack.shape[1:])
        ref_x, ref_y = h5.attrs.get("REF_X"), h5.attrs.get("REF_Y")
        reference_point = None
        if ref_x is not None and ref_y is not None:
            x, y = profile["transform"] * (float(ref_x) + 0.5, float(ref_y) + 0.5)
            lon, lat = transform_coords(profile["crs"], "EPSG:4326", [x], [y])
            reference_point = (float(lat[0]), float(lon[0]))
    return {
        "path": source.path,
        "dates": dates,
        "stack": stack,
        "profile": profile,
        "reference_point": reference_point,
        "timeseries_source": viewer_source_payload(project, source),
    }


def read_interval_grid(project: WebProject, start_token: str, end_token: str) -> dict[str, Any]:
    """Read one orbit's ERA5 interval displacement, coherence, and LOS geometry."""
    data = read_era5_timeseries(project)
    start_index, end_index = date_window_indices(data["dates"], start_token, end_token)
    resolved_start = data["dates"][start_index]
    resolved_end = data["dates"][end_index]
    displacement_cm = (
        data["stack"][end_index] - data["stack"][start_index]
    ) * np.float32(100.0)
    coherence, _ = interval_mean_coherence(
        project,
        expected_shape=displacement_cm.shape,
        start_date=resolved_start,
        end_date=resolved_end,
    )
    if coherence is None:
        raise ValueError(f"No interval coherence layer is available for {project.project_id}")
    geometry = read_geometry_arrays(project, expected_shape=displacement_cm.shape)
    los_east, los_north, los_up = los_unit_arrays(
        geometry["incidence"],
        geometry["azimuth"],
    )
    return {
        "project": project,
        "dates": data["dates"],
        "start_index": start_index,
        "end_index": end_index,
        "resolved_start": resolved_start,
        "resolved_end": resolved_end,
        "displacement_cm": displacement_cm,
        "coherence": coherence,
        "incidence": geometry["incidence"],
        "azimuth": geometry["azimuth"],
        "height": geometry["height"],
        "los_east": los_east,
        "los_north": los_north,
        "los_up": los_up,
        "profile": data["profile"],
        "reference_point": data["reference_point"],
        "timeseries_source": data["timeseries_source"],
    }


def read_geometry_arrays(project: WebProject, *, expected_shape: tuple[int, int]) -> dict[str, np.ndarray | None]:
    """Read declared incidence and azimuth arrays without mission inference."""
    geometry_path = project.geometry_path
    if geometry_path is None or not geometry_path.exists():
        raise FileNotFoundError("No configured geometry file; LOS map viewing does not require geometry")
    with h5py.File(geometry_path, "r") as h5:
        if project.incidence_dataset not in h5 or project.azimuth_dataset not in h5:
            raise ValueError(f"{geometry_path} must contain configured incidence/azimuth datasets")
        incidence = np.asarray(h5[project.incidence_dataset][...], dtype=np.float32)
        azimuth = np.asarray(h5[project.azimuth_dataset][...], dtype=np.float32)
        height = np.asarray(h5["height"][...], dtype=np.float32) if "height" in h5 else None
    if incidence.shape != expected_shape or azimuth.shape != expected_shape:
        raise ValueError(
            f"Geometry shape {incidence.shape}/{azimuth.shape} does not match {expected_shape}"
        )
    if height is not None and height.shape != expected_shape:
        height = None
    with h5py.File(project.timeseries_path, "r") as ts_h5:
        profile = profile_from_attrs(ts_h5.attrs, expected_shape)
        target_reference = (float(ts_h5.attrs["REF_X"]), float(ts_h5.attrs["REF_Y"])) if "REF_X" in ts_h5.attrs and "REF_Y" in ts_h5.attrs else None
    with h5py.File(geometry_path, "r") as geometry_h5:
        validate_artifact_grid(geometry_h5.attrs, profile, incidence.shape, geometry_path, target_reference)
    azimuth, azimuth_adjustment = normalize_azimuth(azimuth, project.azimuth_convention, mission=project.pipeline_kind, orbit=project.orbit)
    return {
        "incidence": incidence,
        "azimuth": azimuth,
        "azimuth_adjustment_deg": azimuth_adjustment,
        "height": height,
    }


def reproject_interval_grid(source: dict[str, Any], target_profile: dict[str, Any]) -> dict[str, Any]:
    """Reproject one interval grid onto the target profile."""
    return {
        **source,
        "displacement_cm": reproject_array(source["displacement_cm"], source["profile"], target_profile),
        "coherence": reproject_array(source["coherence"], source["profile"], target_profile),
        "los_east": reproject_array(source["los_east"], source["profile"], target_profile),
        "los_north": reproject_array(source["los_north"], source["profile"], target_profile),
        "los_up": reproject_array(source["los_up"], source["profile"], target_profile),
        "profile": target_profile,
    }


def reproject_array(
    data: np.ndarray,
    source_profile: dict[str, Any],
    target_profile: dict[str, Any],
) -> np.ndarray:
    """Reproject a float array onto another MintPy grid."""
    destination = np.full(
        (int(target_profile["height"]), int(target_profile["width"])),
        np.nan,
        dtype=np.float32,
    )
    reproject(
        source=data.astype(np.float32),
        destination=destination,
        src_transform=source_profile["transform"],
        src_crs=source_profile["crs"],
        src_nodata=np.nan,
        dst_transform=target_profile["transform"],
        dst_crs=target_profile["crs"],
        dst_nodata=np.nan,
        resampling=Resampling.bilinear,
    )
    return destination


def profile_from_attrs(attrs: h5py.AttributeManager, shape: tuple[int, int]) -> dict[str, Any]:
    """Build a raster profile from MintPy geocoded metadata."""
    height, width = shape
    x_first = float(attrs["X_FIRST"])
    y_first = float(attrs["Y_FIRST"])
    x_step = float(attrs["X_STEP"])
    y_step = float(attrs["Y_STEP"])
    return {
        "height": height,
        "width": width,
        "crs": rasterio.crs.CRS.from_epsg(int(attrs["EPSG"])),
        "transform": Affine(x_step, 0.0, x_first, 0.0, y_step, y_first),
    }


def validate_artifact_grid(attrs: Any, target: dict[str, Any], shape: tuple[int, int], path: Path, target_reference: tuple[float, float] | None = None) -> None:
    names = ("EPSG", "X_FIRST", "Y_FIRST", "X_STEP", "Y_STEP")
    missing = [name for name in names if name not in attrs]
    if missing:
        raise ValueError(f"{path}: missing grid metadata {', '.join(missing)}")
    if tuple(shape) != (target["height"], target["width"]):
        raise ValueError(f"{path}: shape differs from time-series grid")
    transform = target["transform"]
    expected = {"EPSG": target["crs"].to_epsg(), "X_FIRST": transform.c,
                "Y_FIRST": transform.f, "X_STEP": transform.a, "Y_STEP": transform.e}
    for name, value in expected.items():
        if not np.isclose(float(attrs[name]), float(value), rtol=1e-10, atol=1e-8):
            raise ValueError(f"{path}: {name} differs from time-series grid")
    if ("REF_X" in attrs) != ("REF_Y" in attrs):
        raise ValueError(f"{path}: REF_X and REF_Y must be provided together")
    if target_reference is not None and "REF_X" in attrs and any(
        not np.isclose(float(attrs[name]), value, rtol=0, atol=1e-6)
        for name, value in zip(("REF_X", "REF_Y"), target_reference, strict=True)
    ):
        raise ValueError(f"{path}: reference pixel differs from time-series reference")


def date_window_indices(dates: list[str], start_value: str, end_value: str) -> tuple[int, int]:
    """Return first/last acquisition indices inside a date window."""
    start = _parse_yyyymmdd(start_value)
    end = _parse_yyyymmdd(end_value)
    if end < start:
        raise ValueError("end_date must be on or after start_date")
    inside = [
        index
        for index, token in enumerate(dates)
        if start <= _parse_yyyymmdd(token) <= end
    ]
    if not inside:
        raise ValueError(f"No InSAR acquisitions inside {start_value} to {end_value}")
    return inside[0], inside[-1]


def match_asc_desc_dates(
    asc_dates: list[str],
    desc_dates: list[str],
    start_value: str,
    end_value: str,
    *,
    tolerance_days: int = 6,
) -> list[dict[str, Any]]:
    """Match ASC acquisitions to nearest DESC acquisitions inside the selected window."""
    start = _parse_yyyymmdd(start_value)
    end = _parse_yyyymmdd(end_value)
    if end < start:
        raise ValueError("end_date must be on or after start_date")

    desc_candidates = [
        (index, _parse_yyyymmdd(token), token)
        for index, token in enumerate(desc_dates)
        if start <= _parse_yyyymmdd(token) <= end
    ]
    if not desc_candidates:
        raise ValueError(f"No DESC acquisitions inside {start_value} to {end_value}")

    matches: list[dict[str, Any]] = []
    used_desc: set[int] = set()
    for asc_index, asc_token in enumerate(asc_dates):
        asc_date = _parse_yyyymmdd(asc_token)
        if not start <= asc_date <= end:
            continue
        best = min(
            (
                candidate
                for candidate in desc_candidates
                if candidate[0] not in used_desc
            ),
            key=lambda candidate: abs((candidate[1] - asc_date).days),
            default=None,
        )
        if best is None:
            continue
        desc_index, desc_date, desc_token = best
        offset_days = abs((desc_date - asc_date).days)
        if offset_days > tolerance_days:
            continue
        used_desc.add(desc_index)
        matches.append(
            {
                "asc_index": asc_index,
                "desc_index": desc_index,
                "asc_token": asc_token,
                "desc_token": desc_token,
                "plot_date": asc_date,
                "offset_days": offset_days,
            }
        )
    if len(matches) < 2:
        raise ValueError(
            "No usable ASC/DESC date pairs in this window. "
            f"Nearest-date tolerance is {tolerance_days} days."
        )
    return matches


def rereference_values(values: list[float]) -> list[float | None]:
    """Subtract the first finite value from a scalar time series."""
    reference = next((float(value) for value in values if np.isfinite(value)), None)
    if reference is None:
        return [None for _ in values]
    return [
        float(value - reference) if np.isfinite(value) else None
        for value in values
    ]


def rereference_insar_series(
    values: np.ndarray,
    spatial_reference: np.ndarray | None = None,
) -> np.ndarray:
    """Apply an optional spatial reference and zero the first selected epoch."""
    output = np.asarray(values, dtype=np.float32)
    if spatial_reference is not None:
        reference = np.asarray(spatial_reference, dtype=np.float32)
        if reference.shape != output.shape:
            raise ValueError("Target and temporary-reference time series have different shapes")
        output = output - reference
    if output.size == 0 or not np.isfinite(output[0]):
        raise ValueError("InSAR time series is unavailable at the first selected epoch")
    return output - output[0]


def rereference_uncertainty(values_sigma: list[float]) -> list[float | None]:
    """Approximate one-sigma uncertainty after subtracting the first selected epoch."""
    reference = next((float(value) for value in values_sigma if np.isfinite(value)), None)
    if reference is None:
        return [None for _ in values_sigma]
    output = []
    first_written = False
    for value in values_sigma:
        if not np.isfinite(value):
            output.append(None)
        elif not first_written:
            output.append(0.0)
            first_written = True
        else:
            output.append(float(np.hypot(value, reference)))
    return output


def endpoint_sigma(values_sigma: list[float] | None, first: int, last: int) -> float | None:
    """Return one-sigma uncertainty for last-minus-first displacement."""
    if values_sigma is None:
        return None
    if first >= len(values_sigma) or last >= len(values_sigma):
        return None
    first_sigma = float(values_sigma[first])
    last_sigma = float(values_sigma[last])
    if not np.isfinite(first_sigma) or not np.isfinite(last_sigma):
        return None
    return float(np.hypot(first_sigma, last_sigma))


def nullable_float_list(values: np.ndarray) -> list[float | None]:
    """Convert a numeric array to JSON-friendly floats with null for NaN."""
    return [float(value) if np.isfinite(value) else None for value in values]


def interval_mean_coherence(
    project: WebProject,
    *,
    expected_shape: tuple[int, int],
    start_date: str,
    end_date: str,
) -> tuple[np.ndarray | None, str | None]:
    """Read only the operator-declared quality layer/source."""
    if project.quality_path is None or project.quality_dataset is None:
        return None, None
    try:
        with h5py.File(project.quality_path, "r") as h5:
            if project.quality_dataset not in h5:
                return None, None
            dataset = h5[project.quality_dataset]
            with h5py.File(project.timeseries_path, "r") as ts_h5:
                target_profile = profile_from_attrs(ts_h5.attrs, (int(ts_h5["timeseries"].shape[1]), int(ts_h5["timeseries"].shape[2])))
                target_reference = (float(ts_h5.attrs["REF_X"]), float(ts_h5.attrs["REF_Y"])) if "REF_X" in ts_h5.attrs and "REF_Y" in ts_h5.attrs else None
            if dataset.ndim == 2:
                validate_artifact_grid(h5.attrs, target_profile, dataset.shape, project.quality_path, target_reference)
                layer = np.asarray(dataset[...], dtype=np.float32)
            elif dataset.ndim == 3 and project.ifgram_stack_path == project.quality_path:
                validate_artifact_grid(h5.attrs, target_profile, dataset.shape[1:], project.quality_path, target_reference)
                if "date" not in h5 or not project.retained_dataset or project.retained_dataset not in h5:
                    return None, None
                pairs = h5["date"][...]
                flags = h5[project.retained_dataset]
                if flags.dtype.kind != "b" or flags.shape != (len(pairs),):
                    return None, None
                retained = flags[...]
                ids = [i for i, pair in enumerate(pairs) if retained[i] and all(start_date <= _decode_h5_date(v) <= end_date for v in pair)]
                if not ids:
                    return None, None
                layers = np.asarray(dataset[ids, :, :], dtype=np.float32)
                for name in ("_FillValue", "missing_value", "NO_DATA_VALUE", "nodata"):
                    nodata = dataset.attrs.get(name, h5.attrs.get(name))
                    if nodata is not None:
                        layers[layers == float(nodata)] = np.nan
                finite = np.isfinite(layers)
                counts = finite.sum(axis=0)
                layer = np.divide(np.where(finite, layers, 0).sum(axis=0), counts,
                                  out=np.full(expected_shape, np.nan, dtype=np.float32), where=counts > 0)
            else:
                return None, None
        if layer.shape != expected_shape:
            return None, None
        return layer, f"{project.quality_label or project.quality_type}: {project.quality_path}"
    except OSError:
        return None, None


def network_connectivity(project: WebProject, dates: list[date]) -> dict[str, Any]:
    """Return connected/disconnected bands for displayed InSAR dates."""
    stack_path = project.ifgram_stack_path
    if stack_path is None or project.retained_dataset is None or not stack_path.exists():
        return {"source": None, "assessed": False, "bands": [], "components": [], "note": "Retained network is unassessed"}

    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    with h5py.File(stack_path, "r") as h5:
        if "date" not in h5 or project.retained_dataset not in h5:
            return {"source": str(stack_path), "assessed": False, "bands": [], "components": [], "note": "Configured pair dates or retained flags missing"}
        pairs = np.asarray(h5["date"][...])
        flags = h5[project.retained_dataset]
        if pairs.ndim != 2 or pairs.shape[1] != 2 or flags.dtype.kind != "b" or flags.shape != (len(pairs),):
            return {"source": str(stack_path), "assessed": False, "bands": [], "components": [], "note": "Retained flags must be a Boolean vector matching pair dates"}
        retained = flags[...]

    try:
        for value in pairs.flat:
            _parse_yyyymmdd(_decode_h5_date(value))
    except ValueError:
        return {"source": str(stack_path), "assessed": False, "bands": [], "components": [], "note": "Malformed acquisition dates in retained network"}

    for pair, keep in zip(pairs, retained, strict=False):
        left = _decode_h5_date(pair[0])
        right = _decode_h5_date(pair[1])
        find(left)
        find(right)
        if bool(keep):
            union(left, right)

    tokens = [value.strftime("%Y%m%d") for value in dates]
    components_by_root: dict[str, list[str]] = {}
    for token in tokens:
        components_by_root.setdefault(find(token), []).append(token)
    components = sorted((sorted(values) for values in components_by_root.values()), key=lambda v: v[0])

    bands = []
    extents = []
    for component in components:
        start, end = component[0], component[-1]
        extents.append((start, end))
        if start != end:
            bands.append({"kind": "connected", "start_date": start, "end_date": end})
    for left, right in zip(tokens, tokens[1:]):
        if find(left) != find(right):
            bands.append({"kind": "disconnected", "start_date": left, "end_date": right})
    bands.sort(key=lambda item: (item["start_date"], item["kind"]))
    return {
        "source": str(stack_path),
        "assessed": True,
        "bands": bands,
        "components": components,
        "note": "Red disconnected spans mean offsets across the gap are not constrained by retained interferograms.",
    }


def disconnected_gaps(network: dict[str, Any]) -> list[dict[str, str]]:
    """Return disconnected network spans as ISO date dictionaries."""
    return [
        {
            "start_date": _parse_yyyymmdd(band["start_date"]).isoformat(),
            "end_date": _parse_yyyymmdd(band["end_date"]).isoformat(),
        }
        for band in network.get("bands", [])
        if band.get("kind") == "disconnected"
    ]


def write_interval_overlay_png(
    output: Path,
    displacement_cm: np.ndarray,
    mask: np.ndarray,
    *,
    hillshade: np.ndarray | None,
    color_limit: float | None = None,
) -> tuple[int, int]:
    """Write a high-resolution PNG while preserving discrete InSAR pixels."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from PIL import Image

    output.parent.mkdir(parents=True, exist_ok=True)
    limit = color_limit if color_limit is not None and color_limit > 0 else robust_limit(displacement_cm[mask])
    normed = np.clip((displacement_cm + limit) / (2.0 * limit), 0.0, 1.0)
    cmap = plt.get_cmap("RdBu_r")
    data_rgb = np.asarray(cmap(normed)[..., :3] * 255.0, dtype=np.uint8)

    height, width = displacement_cm.shape
    integer_scale = max(
        1,
        int(np.ceil(WEB_RENDER_MIN_LONG_SIDE / max(height, width))),
    )
    render_size = (width * integer_scale, height * integer_scale)
    data_image = Image.fromarray(data_rgb, mode="RGB").resize(
        render_size,
        resample=Image.Resampling.NEAREST,
    )
    display_mask = Image.fromarray(mask.astype(np.uint8) * 255, mode="L").resize(
        render_size,
        resample=Image.Resampling.NEAREST,
    )
    data_large = np.asarray(data_image, dtype=np.float32)
    mask_large = np.asarray(display_mask, dtype=np.uint8) > 0

    if hillshade is None:
        rgba = np.zeros((render_size[1], render_size[0], 4), dtype=np.uint8)
        rgba[..., :3] = data_large.astype(np.uint8)
        rgba[..., 3] = np.where(mask_large, 240, 0).astype(np.uint8)
    else:
        terrain_valid = np.isfinite(hillshade)
        terrain_source = np.where(terrain_valid, hillshade, 0.0)
        terrain_image = Image.fromarray(
            np.asarray(np.clip(terrain_source, 0.0, 1.0) * 255.0, dtype=np.uint8),
            mode="L",
        ).resize(
            render_size,
            resample=Image.Resampling.BICUBIC,
        )
        terrain_valid_image = Image.fromarray(
            terrain_valid.astype(np.uint8) * 255,
            mode="L",
        ).resize(
            render_size,
            resample=Image.Resampling.NEAREST,
        )
        terrain = np.asarray(terrain_image, dtype=np.float32)
        terrain_rgb = np.repeat(terrain[..., np.newaxis], 3, axis=2)
        rgb = terrain_rgb.copy()
        rgb[mask_large] = (
            terrain_rgb[mask_large] * np.float32(0.38)
            + data_large[mask_large] * np.float32(0.62)
        )
        rgba = np.empty((render_size[1], render_size[0], 4), dtype=np.uint8)
        rgba[..., :3] = np.asarray(np.clip(rgb, 0.0, 255.0), dtype=np.uint8)
        rgba[..., 3] = np.asarray(terrain_valid_image, dtype=np.uint8)
    Image.fromarray(rgba, mode="RGBA").save(output, format="PNG", optimize=True)
    return render_size


def write_map_exports(
    visual_png: Path,
    displacement_cm: np.ndarray,
    mask: np.ndarray,
    profile: dict[str, Any],
    *,
    tags: dict[str, Any],
) -> dict[str, Path]:
    """Write report-ready JPEG and analysis-ready displacement GeoTIFF."""
    from PIL import Image

    jpeg_path = visual_png.with_suffix(".jpg")
    geotiff_path = visual_png.with_suffix(".tif")
    with Image.open(visual_png).convert("RGBA") as source:
        background = Image.new("RGBA", source.size, (246, 248, 247, 255))
        jpeg = Image.alpha_composite(background, source).convert("RGB")
        jpeg.save(
            jpeg_path,
            format="JPEG",
            quality=95,
            subsampling=0,
            optimize=True,
            progressive=True,
        )

    values = np.where(mask, displacement_cm, np.nan).astype(np.float32)
    with rasterio.open(
        geotiff_path,
        "w",
        driver="GTiff",
        width=values.shape[1],
        height=values.shape[0],
        count=1,
        dtype="float32",
        crs=profile["crs"],
        transform=profile["transform"],
        nodata=np.nan,
        compress="deflate",
        predictor=3,
        BIGTIFF="IF_SAFER",
    ) as dataset:
        dataset.write(values, 1)
        dataset.write_mask(mask.astype(np.uint8) * 255)
        component = str(tags.get("component", "displacement"))
        dataset.set_band_description(1, f"{component}_displacement_cm")
        dataset.update_tags(
            AREA_OR_POINT="Area",
            units="cm",
            **{key: str(value) for key, value in tags.items()},
        )
        dataset.update_tags(1, units="cm", valid_pixels="coherence_masked")
    return {"jpeg": jpeg_path, "geotiff": geotiff_path}


def download_payload(paths: dict[str, Path], *, filename_stem: str) -> dict[str, dict[str, str]]:
    """Return cache URLs and explicit friendly filenames for browser downloads."""
    suffixes = {"jpeg": ".jpg", "geotiff": ".tif"}
    return {
        kind: {
            "url": f"/cache/{path.relative_to(CACHE_ROOT).as_posix()}",
            "filename": f"{filename_stem}{suffixes[kind]}",
        }
        for kind, path in paths.items()
    }


def read_dem_contours(
    project: WebProject,
    *,
    crop_window: Window | None,
    expected_shape: tuple[int, int],
    interval_m: int = 100,
) -> dict[str, Any] | None:
    """Return normalized DEM contour polylines for browser SVG overlays."""
    dem, source = read_dem(project, crop_window=crop_window, expected_shape=expected_shape)
    if dem is None:
        return None
    lines = dem_contour_lines(dem, interval_m=interval_m)
    if not lines:
        return None
    return {
        "source": source,
        "interval_m": interval_m,
        "line_count": len(lines),
        "lines": lines,
    }


def read_dem(
    project: WebProject,
    *,
    crop_window: Window | None,
    expected_shape: tuple[int, int],
) -> tuple[np.ndarray | None, str | None]:
    """Read cropped MintPy DEM if it matches the requested display shape."""
    geometry_path = project.geometry_path
    if geometry_path is None:
        return None, None
    if not geometry_path.exists():
        return None, None
    with h5py.File(geometry_path, "r") as h5:
        if "height" not in h5:
            return None, None
        dem = np.asarray(h5["height"][...], dtype=np.float32)
    if crop_window is not None:
        dem = crop_array(dem, crop_window)
    if dem.shape != expected_shape:
        return None, None
    return dem, str(geometry_path)


def dem_contour_lines(
    dem: np.ndarray,
    *,
    interval_m: int,
    max_points_per_line: int = 260,
    max_lines: int = 900,
) -> list[dict[str, Any]]:
    """Build normalized contour line vertices from a DEM array."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    valid = np.isfinite(dem)
    if not valid.any() or interval_m <= 0:
        return []
    min_level = int(np.ceil(float(np.nanmin(dem[valid])) / interval_m) * interval_m)
    max_level = int(np.floor(float(np.nanmax(dem[valid])) / interval_m) * interval_m)
    if max_level <= min_level:
        return []
    levels = np.arange(min_level, max_level + interval_m, interval_m, dtype=float)
    if levels.size < 2:
        return []

    height, width = dem.shape
    masked = np.ma.masked_invalid(dem)
    fig, ax = plt.subplots(figsize=(2, 2), dpi=72)
    try:
        contour_set = ax.contour(masked, levels=levels, linewidths=0.5)
        lines: list[dict[str, Any]] = []
        for level, segments in zip(contour_set.levels, contour_set.allsegs, strict=False):
            for segment in segments:
                if len(segment) < 2:
                    continue
                stride = max(1, int(np.ceil(len(segment) / max_points_per_line)))
                sampled = segment[::stride]
                points = [
                    [
                        float(np.clip(x / max(width - 1, 1), 0.0, 1.0)),
                        float(np.clip(y / max(height - 1, 1), 0.0, 1.0)),
                    ]
                    for x, y in sampled
                    if np.isfinite(x) and np.isfinite(y)
                ]
                if len(points) >= 2:
                    lines.append({"elevation_m": float(level), "points": points})
                if len(lines) >= max_lines:
                    return lines
        return lines
    finally:
        plt.close(fig)


def read_hillshade(
    project: WebProject,
    profile: dict[str, Any],
    *,
    crop_window: Window | None,
    expected_shape: tuple[int, int],
) -> tuple[np.ndarray | None, str | None]:
    """Return a simple hillshade from MintPy geometry height when available."""
    dem, source = read_dem(project, crop_window=crop_window, expected_shape=expected_shape)
    if dem is None:
        return None, None
    return hillshade(
        dem,
        x_step=abs(float(profile["transform"].a)),
        y_step=abs(float(profile["transform"].e)),
    ), source


def read_satellite_look_direction(project: WebProject) -> dict[str, float | str] | None:
    """Return median horizontal satellite-looking direction from MintPy geometry."""
    geometry_path = project.geometry_path
    if geometry_path is None or not geometry_path.exists():
        return None
    with h5py.File(geometry_path, "r") as h5:
        if project.incidence_dataset not in h5 or project.azimuth_dataset not in h5:
            return None
        incidence_array = np.asarray(h5[project.incidence_dataset][...], dtype=np.float32)
        azimuth_array = np.asarray(h5[project.azimuth_dataset][...], dtype=np.float32)
    valid = np.isfinite(incidence_array) & (incidence_array > 0.0)
    azimuth_array, azimuth_adjustment = normalize_azimuth(azimuth_array, project.azimuth_convention, mission=project.pipeline_kind, orbit=project.orbit)
    incidence = float(np.nanmedian(incidence_array[valid]))
    azimuth = float(np.nanmedian(azimuth_array[valid]))
    if not np.isfinite(incidence) or not np.isfinite(azimuth):
        return None

    los_east, los_north, los_up = los_unit_vector(incidence, azimuth)
    look_east = -los_east
    look_north = -los_north
    norm = float(np.hypot(look_east, look_north))
    if norm <= 0.0:
        return None
    look_east /= norm
    look_north /= norm
    screen_angle = float(np.rad2deg(np.arctan2(-look_north, look_east)))
    return {
        "source": str(geometry_path),
        "incidence_angle_deg": incidence,
        "azimuth_angle_deg": azimuth,
        "azimuth_adjustment_deg": azimuth_adjustment,
        "positive_los_unit_east": los_east,
        "positive_los_unit_north": los_north,
        "positive_los_unit_up": los_up,
        "look_unit_east": look_east,
        "look_unit_north": look_north,
        "screen_angle_deg": screen_angle,
        "convention": "arrow shows satellite-to-ground horizontal look direction",
    }


def los_unit_vector(
    incidence_angle_deg: float,
    azimuth_angle_deg: float,
) -> tuple[float, float, float]:
    """Return MintPy-convention ENU LOS vector, positive toward the satellite."""
    incidence = np.deg2rad(incidence_angle_deg)
    azimuth = np.deg2rad(azimuth_angle_deg)
    east = float(-np.sin(incidence) * np.sin(azimuth))
    north = float(np.sin(incidence) * np.cos(azimuth))
    up = float(np.cos(incidence))
    return east, north, up


def hillshade(
    dem: np.ndarray,
    *,
    x_step: float,
    y_step: float,
    azimuth_deg: float = 315.0,
    altitude_deg: float = 45.0,
) -> np.ndarray:
    """Compute normalized northwest-lit hillshade from a DEM raster."""
    valid = np.isfinite(dem)
    if not valid.any():
        return np.zeros_like(dem, dtype=np.float32)
    filled = np.where(valid, dem, np.nanmedian(dem[valid]))
    gradient_y, gradient_x = np.gradient(filled, y_step, x_step)
    slope = np.pi / 2.0 - np.arctan(np.hypot(gradient_x, gradient_y))
    aspect = np.arctan2(-gradient_x, gradient_y)
    azimuth = np.deg2rad(azimuth_deg)
    altitude = np.deg2rad(altitude_deg)
    shaded = (
        np.sin(altitude) * np.sin(slope)
        + np.cos(altitude) * np.cos(slope) * np.cos(azimuth - aspect)
    )
    shaded = (shaded + 1.0) / 2.0
    shaded = 0.18 + 0.74 * np.clip(shaded, 0.0, 1.0)
    return np.where(valid, shaded, np.nan).astype(np.float32)


def robust_limit(values: np.ndarray) -> float:
    """Return symmetric display scale from finite values."""
    finite = np.abs(values[np.isfinite(values)])
    if finite.size == 0:
        return 1.0
    limit = float(np.percentile(finite, 95.0))
    return limit if limit > 0 else 1.0


def cache_interval_path(
    project: WebProject,
    start: str,
    end: str,
    threshold: float,
    *,
    reference_cell: tuple[int, int] | None = None,
) -> Path:
    """Return deterministic cache path for one interval overlay."""
    threshold_text = f"{int(round(threshold * 100)):03d}"
    reference_text = (
        f"_ref{reference_cell[0]}_{reference_cell[1]}"
        if reference_cell is not None
        else ""
    )
    return (
        CACHE_ROOT
        / project.project_id
        / f"interval_era5_{start}_{end}_thr{threshold_text}{reference_text}_aoi_hires.png"
    )


def cache_combined_path(
    asc_project: WebProject,
    desc_project: WebProject,
    start: str,
    end: str,
    threshold: float,
    component: str,
    *,
    reference_cell: tuple[int, int] | None = None,
) -> Path:
    """Return deterministic cache path for ASC/DESC component overlays."""
    threshold_text = f"{int(round(threshold * 100)):03d}"
    reference_text = (
        f"_ref{reference_cell[0]}_{reference_cell[1]}"
        if reference_cell is not None
        else ""
    )
    pair_id = f"{asc_project.project_id}__{desc_project.project_id}"
    return (
        CACHE_ROOT
        / "combined"
        / pair_id
        / (
            f"asc_desc_{component}_era5_{start}_{end}_thr{threshold_text}"
            f"{reference_text}_aoi_hires.png"
        )
    )


def project_crop_window(
    project: WebProject,
    profile: dict[str, Any],
) -> tuple[Window | None, str]:
    """Return a raster window cropped to the configured project AOI/grid bbox."""
    bbox = project_display_bbox(project)
    if bbox is None:
        return None, "full_mintpy_grid"
    left, bottom, right, top = transform_bounds(
        "EPSG:4326",
        profile["crs"],
        bbox[0],
        bbox[1],
        bbox[2],
        bbox[3],
        densify_pts=21,
    )
    window = from_bounds(left, bottom, right, top, transform=profile["transform"])
    col_start = max(0, int(np.floor(window.col_off)))
    row_start = max(0, int(np.floor(window.row_off)))
    col_stop = min(
        int(profile["width"]),
        int(np.ceil(window.col_off + window.width)),
    )
    row_stop = min(
        int(profile["height"]),
        int(np.ceil(window.row_off + window.height)),
    )
    if col_stop <= col_start or row_stop <= row_start:
        return None, "full_mintpy_grid"
    full_width = int(profile["width"])
    full_height = int(profile["height"])
    if col_start == 0 and row_start == 0 and col_stop == full_width and row_stop == full_height:
        return None, "full_mintpy_grid"
    return Window(
        col_off=col_start,
        row_off=row_start,
        width=col_stop - col_start,
        height=row_stop - row_start,
    ), "project_aoi"


def project_display_bbox(project: WebProject) -> tuple[float, float, float, float]:
    return project.bbox


def parse_web_bbox(raw: Any) -> tuple[float, float, float, float] | None:
    return tuple(map(float, raw)) if raw is not None else None


def crop_array(data: np.ndarray, window: Window) -> np.ndarray:
    """Crop a 2D array to a rasterio window."""
    row_start = int(window.row_off)
    row_stop = row_start + int(window.height)
    col_start = int(window.col_off)
    col_stop = col_start + int(window.width)
    return data[row_start:row_stop, col_start:col_stop]


def cropped_profile(profile: dict[str, Any], window: Window) -> dict[str, Any]:
    """Return a profile adjusted to a crop window."""
    return {
        **profile,
        "height": int(window.height),
        "width": int(window.width),
        "transform": window_transform(window, profile["transform"]),
    }


def raster_grid_metadata(profile: dict[str, Any]) -> dict[str, Any]:
    """Describe the displayed raster, using pixel edges rather than centers."""
    return {
        "epsg": rasterio.crs.CRS.from_user_input(profile["crs"]).to_epsg(),
        "transform": list(profile["transform"])[:6],
        "width": int(profile["width"]),
        "height": int(profile["height"]),
    }


def map_coordinates(payload: dict[str, Any]) -> dict[str, Any]:
    """Transform map clicks/markers against the exact grid of a rendered snapshot.

    This endpoint performs coordinate arithmetic only; no files are accessed.
    Normalized x/y range from zero to one across the displayed raster edges.
    """
    grid = payload["raster_grid"]
    width, height = int(grid["width"]), int(grid["height"])
    coefficients = np.asarray(grid["transform"], dtype=float)
    if (not 0 < width <= 1_000_000 or not 0 < height <= 1_000_000
            or coefficients.shape != (6,) or not np.all(np.isfinite(coefficients))):
        raise ValueError("Invalid raster grid")
    affine = Affine(*coefficients)
    if abs(affine.determinant) < 1e-20:
        raise ValueError("Raster transform is singular")
    crs = rasterio.crs.CRS.from_epsg(int(grid["epsg"]))
    points = payload.get("points", [])
    direction = payload.get("direction", "to_raster")
    if direction not in {"to_raster", "to_geographic"}:
        raise ValueError("Invalid coordinate direction")
    if not isinstance(points, list) or len(points) > 1000:
        raise ValueError("At most 1000 points may be transformed")
    if not points:
        return {"points": []}
    keys = ("lon", "lat") if direction == "to_raster" else ("x", "y")
    values = np.asarray([[float(p[k]) for k in keys] for p in points])
    if not np.all(np.isfinite(values)):
        raise ValueError("Coordinates must be finite")
    if direction == "to_raster":
        if np.any(np.abs(values[:, 0]) > 180) or np.any(np.abs(values[:, 1]) > 90):
            raise ValueError("Invalid longitude/latitude")
        east, north = transform_coords("EPSG:4326", crs, values[:, 0], values[:, 1])
        col, row = ~affine * (np.asarray(east), np.asarray(north))
        output = [{"x": float(c / width), "y": float(r / height)}
                  for c, r in zip(col, row)]
    else:
        if np.any(values < 0) or np.any(values > 1):
            raise ValueError("Map clicks must lie inside the raster")
        east, north = affine * (values[:, 0] * width, values[:, 1] * height)
        lon, lat = transform_coords(crs, "EPSG:4326", east, north)
        output = [{"lon": float(x), "lat": float(y)} for x, y in zip(lon, lat)]
    if not all(np.isfinite(v) for point in output for v in point.values()):
        raise ValueError("Coordinates cannot be transformed into this CRS")
    return {"points": output}


def profile_bounds_latlon(profile: dict[str, Any]) -> list[list[float]]:
    """Return Leaflet image bounds [[south, west], [north, east]]."""
    transform = profile["transform"]
    width = int(profile["width"])
    height = int(profile["height"])
    west = transform.c
    north = transform.f
    east = west + transform.a * width
    south = north + transform.e * height
    left, bottom, right, top = transform_bounds(
        profile["crs"],
        "EPSG:4326",
        min(west, east),
        min(south, north),
        max(west, east),
        max(south, north),
        densify_pts=21,
    )
    return [[bottom, left], [top, right]]


def point_to_row_col(latitude: float, longitude: float, profile: dict[str, Any]) -> tuple[int, int]:
    """Convert lat/lon to raster row/column."""
    x_values, y_values = transform_coords(
        "EPSG:4326",
        profile["crs"],
        [longitude],
        [latitude],
    )
    row, col = rowcol(profile["transform"], x_values[0], y_values[0])
    if row < 0 or col < 0 or row >= profile["height"] or col >= profile["width"]:
        raise ValueError("Clicked point is outside the MintPy raster")
    return int(row), int(col)


def requested_display_reference(payload: dict[str, Any]) -> tuple[float, float] | None:
    """Return an optional temporary display-reference latitude/longitude."""
    latitude = payload.get("reference_lat")
    longitude = payload.get("reference_lon")
    if latitude is None and longitude is None:
        return None
    if latitude is None or longitude is None:
        raise ValueError("reference_lat and reference_lon must be provided together")
    latitude_value = float(latitude)
    longitude_value = float(longitude)
    if not np.isfinite(latitude_value) or not np.isfinite(longitude_value):
        raise ValueError("Temporary reference coordinates must be finite")
    return latitude_value, longitude_value


def load_project_gps(project: WebProject) -> list[GPSSeries]:
    """Load and AOI-filter GPS series for one project."""
    if project.gps_path is None or not project.gps_path.exists():
        return []
    # shortcut: GNSS AOI filtering uses the configured bbox only; add polygons if required by a deployment.
    return filter_to_bbox(load_gps(project.gps_path), project.bbox)


if __name__ == "__main__":
    main()
