"""Versioned, pipeline-independent viewer project registry."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    label: str
    group: str
    mission: str
    orbit: str
    look_direction: str
    bbox: tuple[float, float, float, float]
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
    gnss_path: Path | None
    publication_state_path: Path | None
    publication_status: str
    reference_point: tuple[float, float] | None


def _path(value: Any, base: Path) -> Path | None:
    if value is None:
        return None
    expanded = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: os.environ.get(m[1], m[0]), str(value))
    if "${" in expanded:
        raise ValueError(f"unresolved environment variable in path: {value}")
    result = Path(os.path.expanduser(expanded))
    if not result.is_absolute():
        result = base / result
    return result.resolve()


def load_projects(path: Path) -> dict[str, Project]:
    """Load and validate registry structure without opening science products."""
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("viewer registry must declare version: 1")
    records = payload.get("projects")
    if not isinstance(records, list):
        raise ValueError("viewer registry projects must be a list")
    base = path.resolve().parent
    projects: dict[str, Project] = {}
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("each project must be a mapping")
        required = ("id", "label", "group", "mission", "orbit", "look_direction", "aoi", "timeseries")
        missing = [key for key in required if key not in item]
        if missing:
            raise ValueError(f"project is missing required fields: {', '.join(missing)}")
        project_id = str(item["id"])
        if project_id in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", project_id) or project_id in projects:
            raise ValueError(f"invalid or duplicate project id: {project_id!r}")
        aoi = item["aoi"]
        bbox = aoi.get("bbox") if isinstance(aoi, dict) else None
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError(f"{project_id}: aoi.bbox must be [west, south, east, north]")
        bbox = tuple(float(v) for v in bbox)
        if not (-180 <= bbox[0] < bbox[2] <= 180 and -90 <= bbox[1] < bbox[3] <= 90):
            raise ValueError(f"{project_id}: invalid WGS84 AOI bbox")
        if str(item["orbit"]).upper() not in {"ASC", "DESC"}:
            raise ValueError(f"{project_id}: orbit must be ASC or DESC")
        if str(item["look_direction"]).lower() not in {"left", "right"}:
            raise ValueError(f"{project_id}: look_direction must be left or right")
        ts = item["timeseries"]
        if not isinstance(ts, dict) or not ts.get("path"):
            raise ValueError(f"{project_id}: timeseries.path is required")
        if not isinstance(ts.get("corrections", []), list):
            raise ValueError(f"{project_id}: timeseries.corrections must be a list")
        stack = item.get("ifgram_stack") or {}
        geometry = item.get("geometry") or {}
        quality = item.get("quality") or {}
        if not isinstance(stack, dict) or not isinstance(geometry, dict) or not isinstance(quality, dict):
            raise ValueError(f"{project_id}: ifgram_stack, geometry, and quality must be mappings")
        reference = item.get("reference_point")
        if reference is not None and (not isinstance(reference, dict) or "latitude" not in reference or "longitude" not in reference):
            raise ValueError(f"{project_id}: reference_point requires latitude and longitude")
        stack_path = _path(stack.get("path"), base)
        retained_dataset = stack.get("retained_dataset")
        if stack_path and not retained_dataset:
            raise ValueError(f"{project_id}: ifgram_stack.retained_dataset is required")
        quality_path = _path(quality.get("path"), base) or stack_path
        quality_dataset = quality.get("dataset") or stack.get("quality_dataset")
        projects[project_id] = Project(
            id=project_id, label=str(item["label"]), group=str(item["group"]),
            mission=str(item["mission"]).lower(), orbit=str(item["orbit"]).upper(),
            look_direction=str(item["look_direction"]).lower(), bbox=bbox,
            timeseries_path=_path(ts["path"], base),
            corrections=tuple(str(v) for v in ts.get("corrections", [])),
            ifgram_stack_path=stack_path, retained_dataset=str(retained_dataset) if retained_dataset else None,
            quality_path=quality_path, quality_dataset=str(quality_dataset) if quality_dataset else None,
            quality_label=str(quality.get("label", "")) or None,
            quality_type=str(quality.get("type", "coherence")) if quality_dataset else None,
            geometry_path=_path(geometry.get("path"), base),
            incidence_dataset=str(geometry.get("incidence_dataset", "incidenceAngle")),
            azimuth_dataset=str(geometry.get("azimuth_dataset", "azimuthAngle")),
            azimuth_convention=str(geometry.get("azimuth_convention", "mintpy_ground_to_satellite")),
            gnss_path=_path((item.get("gnss") or {}).get("path"), base),
            publication_state_path=_path((item.get("publication_state") or {}).get("path"), base),
            publication_status=str((item.get("publication_state") or {}).get("status", item.get("publication_status", "unassessed"))),
            reference_point=(float(reference["latitude"]), float(reference["longitude"])) if reference else None,
        )
    pair_keys = set()
    for project in projects.values():
        key = (project.mission, project.group, project.orbit)
        if key in pair_keys:
            raise ValueError(f"Ambiguous ASC/DESC group {project.group!r}: use a different group for each track pair")
        pair_keys.add(key)
    return projects
