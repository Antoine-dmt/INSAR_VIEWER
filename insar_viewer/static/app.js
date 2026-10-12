const state = {
  mode: "los",
  projects: [],
  combinedProjects: [],
  currentMap: null,
  currentCombinedMap: null,
  stations: [],
  pixelSeries: null,
  gpsSeries: null,
  selectedPixel: null,
  combinedPixelSeries: null,
  combinedGpsUpSeries: null,
  combinedGpsEastSeries: null,
  combinedSelectedPixel: null,
  gpsVectors: null,
  displayReference: null,
  selectingReference: false,
};

const els = {
  losTab: document.getElementById("losTabButton"),
  combinedTab: document.getElementById("combinedTabButton"),
  project: document.getElementById("projectSelect"),
  start: document.getElementById("startDate"),
  end: document.getElementById("endDate"),
  threshold: document.getElementById("coherenceThreshold"),
  thresholdValue: document.getElementById("thresholdValue"),
  gpsVectorsToggle: document.getElementById("gpsVectorsToggle"),
  setReference: document.getElementById("setReferenceButton"),
  resetReference: document.getElementById("resetReferenceButton"),
  render: document.getElementById("renderButton"),
  status: document.getElementById("statusPanel"),
  mapFrame: document.getElementById("mapFrame"),
  mapMeta: document.getElementById("mapMeta"),
  clickMeta: document.getElementById("clickMeta"),
  losExportRow: document.getElementById("losExportRow"),
  losJpegDownload: document.getElementById("losJpegDownload"),
  losGeotiffDownload: document.getElementById("losGeotiffDownload"),
  gps: document.getElementById("gpsSelect"),
  canvas: document.getElementById("seriesCanvas"),
  details: document.getElementById("seriesDetails"),
  losWorkspace: document.getElementById("losWorkspace"),
  combinedWorkspace: document.getElementById("combinedWorkspace"),
  combinedUpFrame: document.getElementById("combinedUpFrame"),
  combinedEastFrame: document.getElementById("combinedEastFrame"),
  combinedUpMeta: document.getElementById("combinedUpMeta"),
  combinedEastMeta: document.getElementById("combinedEastMeta"),
  combinedUpGps: document.getElementById("combinedUpGpsSelect"),
  combinedEastGps: document.getElementById("combinedEastGpsSelect"),
  combinedUpCanvas: document.getElementById("combinedUpCanvas"),
  combinedEastCanvas: document.getElementById("combinedEastCanvas"),
  combinedUpDetails: document.getElementById("combinedUpDetails"),
  combinedEastDetails: document.getElementById("combinedEastDetails"),
  combinedUpExportRow: document.getElementById("combinedUpExportRow"),
  combinedUpJpegDownload: document.getElementById("combinedUpJpegDownload"),
  combinedUpGeotiffDownload: document.getElementById("combinedUpGeotiffDownload"),
  combinedEastExportRow: document.getElementById("combinedEastExportRow"),
  combinedEastJpegDownload: document.getElementById("combinedEastJpegDownload"),
  combinedEastGeotiffDownload: document.getElementById("combinedEastGeotiffDownload"),
};

function setStatus(message, kind = "") {
  els.status.textContent = message;
  els.status.className = `status-panel ${kind}`.trim();
}

function resultSources(result) {
  if (result?.timeseries_sources) {
    return Object.entries(result.timeseries_sources).map(
      ([orbit, source]) => ({ orbit, ...source }),
    );
  }
  return result?.timeseries_source ? [{ orbit: "", ...result.timeseries_source }] : [];
}

function correctionWarning(result) {
  const legacy = resultSources(result).filter((source) => source.using_legacy_fallback);
  const engineering = resultSources(result).filter(
    (source) => source.pipeline === "nisar" && source.publication_status === "engineering_ready",
  );
  const warnings = [];
  if (resultSources(result).some((source) => source.correction_evidence === "operator_declared")) {
    warnings.push("ERA5 correction is declared by the data provider; scientific validation is not assessed by the viewer.");
  }
  if (legacy.length) {
    const coverage = legacy.map((source) => {
      const orbit = source.orbit ? `${source.orbit.toUpperCase()} ` : "";
      return `${orbit}${source.timeseries_file} through ${source.coverage_end_date}`;
    }).join("; ");
    warnings.push(`Showing previously completed ERA5 data (${coverage}); newer full correction is pending.`);
  }
  if (engineering.length) {
    warnings.push("NISAR engineering result: correction audit passed, but the interferogram network has no cycle redundancy.");
  }
  return warnings.join(" ");
}

function correctionSummary(result) {
  const sources = resultSources(result);
  if (!sources.length) return "Corrected time-series source unavailable";
  return sources.map((source) => {
    const orbit = source.orbit ? `${source.orbit.toUpperCase()}: ` : "";
    const level = source.correction_evidence === "operator_declared"
      ? "provider-declared ERA5 correction"
      : (source.correction_level || "correction status unassessed");
    return `${orbit}${source.timeseries_file} (${level}, through ${source.coverage_end_date})`;
  }).join("; ");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok || payload.error) {
    throw new Error(payload.message || payload.error || `Request failed: ${response.status}`);
  }
  return payload;
}

function requestPayload(extra = {}) {
  const payload = {
    project: els.project.value,
    start_date: els.start.value,
    end_date: els.end.value,
    coherence_threshold: Number(els.threshold.value),
    ...extra,
  };
  if (state.displayReference) {
    payload.reference_lat = state.displayReference.lat;
    payload.reference_lon = state.displayReference.lon;
  }
  return payload;
}

function updateReferenceControls() {
  const hasMap = state.mode === "combined"
    ? Boolean(state.currentCombinedMap)
    : Boolean(state.currentMap);
  els.setReference.disabled = !hasMap;
  els.resetReference.disabled = !state.displayReference;
  els.setReference.classList.toggle("active", state.selectingReference);
  els.setReference.textContent = state.selectingReference ? "Click map" : "Set reference";
  els.mapFrame.classList.toggle("selecting-reference", state.mode === "los" && state.selectingReference);
  els.combinedUpFrame.classList.toggle(
    "selecting-reference",
    state.mode === "combined" && state.selectingReference,
  );
  els.combinedEastFrame.classList.toggle(
    "selecting-reference",
    state.mode === "combined" && state.selectingReference,
  );
}

function referenceStatus(result) {
  const reference = result?.display_reference_point;
  if (!reference) return "Original MintPy spatial reference.";
  return `Temporary reference at ${reference.latitude.toFixed(5)}, ${reference.longitude.toFixed(5)}.`;
}

async function loadProjects() {
  const [payload, combinedPayload] = await Promise.all([
    api("/api/projects"),
    api("/api/combined-projects"),
  ]);
  state.projects = payload.projects || [];
  state.combinedProjects = combinedPayload.projects || [];
  populateProjectSelect();
}

function populateProjectSelect() {
  els.project.innerHTML = "";
  const projects = state.mode === "combined" ? state.combinedProjects : state.projects;
  for (const project of projects) {
    const option = document.createElement("option");
    option.value = project.id;
    const legacy = project.timeseries_source?.using_legacy_fallback
      || project.using_legacy_fallback;
    const coverageEnd = project.coverage_end_date
      || project.timeseries_source?.coverage_end_date;
    const engineering = project.publication_status === "engineering_ready";
    option.textContent = project.available
      ? `${project.label}${legacy ? ` [legacy through ${coverageEnd}]` : ""}${engineering ? " [engineering, no cycles]" : ""}`
      : `${project.label} (${project.availability_reason || "not ready"})`;
    option.disabled = !project.available;
    els.project.appendChild(option);
  }
  const firstAvailable = projects.find((project) => project.available);
  if (firstAvailable) {
    els.project.value = firstAvailable.id;
    applyProjectDates();
    loadStations().then(() => {
      const text = state.mode === "combined"
        ? "Choose dates and render an ASC/DESC east-up decomposition."
        : "Choose dates and render an ERA5 displacement map.";
      setStatus(text);
    });
  } else {
    const text = state.mode === "combined"
      ? "No ASC/DESC project pair has completed corrected time-series products yet."
      : "No configured corrected time-series product is available.";
    setStatus(text, "error");
  }
}

function applyProjectDates() {
  const projects = state.mode === "combined" ? state.combinedProjects : state.projects;
  const project = projects.find((item) => item.id === els.project.value);
  const start = project?.coverage_start_date || project?.timeseries_source?.coverage_start_date;
  const end = project?.coverage_end_date || project?.timeseries_source?.coverage_end_date;
  for (const input of [els.start, els.end]) {
    input.min = start || "";
    input.max = end || "";
  }
  els.start.value = start || "";
  els.end.value = end || "";
}

async function loadStations() {
  state.stations = [];
  els.gps.innerHTML = '<option value="">No station selected</option>';
  els.combinedUpGps.innerHTML = '<option value="">No station selected</option>';
  els.combinedEastGps.innerHTML = '<option value="">No station selected</option>';
  if (!els.project.value) return;
  const combinedProject = state.mode === "combined" ? selectedCombinedProject() : null;
  if (state.mode === "combined" && !combinedProject) return;
  const stationProject = state.mode === "combined"
    ? combinedProject.asc_project
    : els.project.value;
  const payload = await api(`/api/gps-stations?project=${encodeURIComponent(stationProject)}`);
  state.stations = payload.stations || [];
  for (const station of state.stations) {
    const option = document.createElement("option");
    option.value = station.label;
    option.textContent = `${station.label} (${station.epoch_count} epochs)`;
    if (state.mode === "combined") {
      els.combinedUpGps.appendChild(option.cloneNode(true));
      els.combinedEastGps.appendChild(option.cloneNode(true));
    } else {
      els.gps.appendChild(option);
    }
  }
}

async function renderInterval() {
  if (state.mode === "combined") {
    return renderCombinedInterval();
  }
  if (!els.project.value) return;
  const previousMap = state.currentMap;
  els.render.disabled = true;
  state.pixelSeries = null;
  state.gpsSeries = null;
  state.selectedPixel = null;
  drawSeries();
  setStatus("Rendering ERA5 interval map...");
  try {
    const result = await api("/api/interval-map", {
      method: "POST",
      body: JSON.stringify(requestPayload()),
    });
    await prepareMapCoordinates(result);
    state.currentMap = result;
    paintMap(result);
    await refreshGpsVectors();
    const gaps = result.network_gaps || [];
    const gapText = gaps.map((gap) => `${gap.start_date} to ${gap.end_date}`).join("; ");
    const base = `${result.resolved_start_date} to ${result.resolved_end_date}, threshold ${result.coherence_threshold.toFixed(2)}, ${result.valid_pixel_count} pixels shown. ${referenceStatus(result)}`;
    const sourceWarning = correctionWarning(result);
    if (result.network_warning) {
      setStatus(`${base} Warning: disconnected network span(s): ${gapText}.${sourceWarning ? ` ${sourceWarning}` : ""}`, "warning");
    } else if (sourceWarning) {
      setStatus(`${base} Network connected inside this date window. ${sourceWarning}`, "warning");
    } else {
      setStatus(`${base} Network connected inside this date window.`);
    }
    updateReferenceControls();
    return true;
  } catch (error) {
    state.currentMap = previousMap;
    if (previousMap) {
      paintMap(previousMap);
    } else {
      paintEmpty(error.message);
    }
    setStatus(error.message, "error");
    updateReferenceControls();
    return false;
  } finally {
    els.render.disabled = false;
  }
}

async function renderCombinedInterval() {
  if (!els.project.value) return;
  const previousMap = state.currentCombinedMap;
  els.render.disabled = true;
  state.combinedPixelSeries = null;
  state.combinedGpsUpSeries = null;
  state.combinedGpsEastSeries = null;
  state.combinedSelectedPixel = null;
  clearCombinedMaps("Rendering ASC/DESC decomposition...");
  drawCombinedSeries("up");
  drawCombinedSeries("east");
  setStatus("Rendering ASC/DESC east-up decomposition...");
  try {
    const result = await api("/api/combined-interval", {
      method: "POST",
      body: JSON.stringify(requestPayload()),
    });
    if (result.blocked) {
      const ascText = (result.asc_gaps || []).map((gap) => `${gap.start_date} to ${gap.end_date}`).join("; ") || "none";
      const descText = (result.desc_gaps || []).map((gap) => `${gap.start_date} to ${gap.end_date}`).join("; ") || "none";
      state.currentCombinedMap = null;
      clearCombinedMaps("No map rendered because the selected window crosses a disconnected network span.");
      const sourceWarning = correctionWarning(result);
      setStatus(`Cannot decompose this window. ASC disconnected spans: ${ascText}. DESC disconnected spans: ${descText}.${sourceWarning ? ` ${sourceWarning}` : ""}`, "warning");
      updateReferenceControls();
      return;
    }
    await prepareMapCoordinates(result);
    state.currentCombinedMap = result;
    paintCombinedMap(result);
    await refreshGpsVectors();
    const base = `${result.resolved_start_date} to ${result.resolved_end_date}, threshold ${result.coherence_threshold.toFixed(2)}, ${result.valid_pixel_count} pixels shown. ${referenceStatus(result)}`;
    const sourceWarning = correctionWarning(result);
    setStatus(
      `${base} ASC/DESC networks connected; east/up decomposition rendered.${sourceWarning ? ` ${sourceWarning}` : ""}`,
      sourceWarning ? "warning" : "",
    );
    updateReferenceControls();
    return true;
  } catch (error) {
    state.currentCombinedMap = previousMap;
    if (previousMap) {
      paintCombinedMap(previousMap);
    } else {
      clearCombinedMaps(error.message);
    }
    setStatus(error.message, "error");
    updateReferenceControls();
    return false;
  } finally {
    els.render.disabled = false;
  }
}

function selectedCombinedProject() {
  return state.combinedProjects.find((project) => project.id === els.project.value);
}

const mapViews = new Map();
const viewGroups = new Map();

function removeMapView(frame) {
  mapViews.get(frame)?.destroy();
  mapViews.delete(frame);
}

function mountMapView(frame, img, result, combined) {
  if (!img.isConnected) return;
  const key = JSON.stringify([combined, els.project.value, result.raster_grid]);
  if (!viewGroups.has(key)) {
    viewGroups.clear();
    viewGroups.set(key, { zoom: 1, x: 0.5, y: 0.5, views: new Set() });
  }
  const redraw = () => {
    addDemContoursToFrame(frame, result);
    if (combined) addCombinedReferenceMarkers(frame, result);
    else addReferenceMarker();
    addDisplayReferenceMarker(frame, result);
    addStationMarkersToFrame(frame, result);
    addGpsVectorsToFrame(frame, result);
    if (combined) addCombinedPixelMarker(frame);
    else addPixelMarker();
  };
  mapViews.set(frame, new RasterViewport(frame, img, viewGroups.get(key), redraw,
    (x, y) => sampleMapCoordinate(result, combined, x, y)));
}

function coordinateKey(lat, lon) {
  return `${lat},${lon}`;
}

async function prepareMapCoordinates(result, extraPoints = []) {
  result.markerCoordinates ||= new Map();
  const references = result.mintpy_reference_points || {};
  const locations = [...state.stations, result.mintpy_reference_point,
    result.display_reference_point, references.asc, references.desc,
    ...(state.gpsVectors?.vectors || [])].filter(Boolean);
  const points = [...locations.map(p => ({ lat: p.latitude, lon: p.longitude })), ...extraPoints]
    .filter(p => !result.markerCoordinates.has(coordinateKey(p.lat, p.lon)));
  if (!points.length) return;
  const converted = await api("/api/map-coordinates", {
    method: "POST", body: JSON.stringify({ raster_grid: result.raster_grid, points }),
  });
  points.forEach((p, i) => result.markerCoordinates.set(coordinateKey(p.lat, p.lon), converted.points[i]));
}

async function sampleMapCoordinate(result, combined, x, y) {
  const referenceSelection = state.selectingReference;
  const isCurrent = () => result === (combined ? state.currentCombinedMap : state.currentMap);
  try {
    const converted = await api("/api/map-coordinates", {
      method: "POST", body: JSON.stringify({ raster_grid: result.raster_grid,
        direction: "to_geographic", points: [{ x, y }] }),
    });
    if (!isCurrent()) return;
    const { lat, lon } = converted.points[0];
    result.markerCoordinates.set(coordinateKey(lat, lon), { x, y });
    if (referenceSelection) await applyTemporaryReference(lat, lon);
    else if (combined) await sampleCombinedPixel(lat, lon);
    else await sampleLosPixel(lat, lon);
  } catch (error) {
    if (isCurrent()) setStatus(error.message, "error");
  }
}

function paintMap(result) {
  removeMapView(els.mapFrame);
  els.mapFrame.innerHTML = "";
  const img = document.createElement("img");
  img.alt = "ERA5 LOS displacement map";
  img.addEventListener("load", () => mountMapView(els.mapFrame, img, result, false));
  img.src = `${result.image_url}?t=${Date.now()}`;
  els.mapFrame.appendChild(img);
  addMapOverlays(result);
  setExportLinks(
    els.losExportRow,
    els.losJpegDownload,
    els.losGeotiffDownload,
    result.downloads,
  );
  const hillshadeText = result.hillshade_source ? "DEM hillshade on" : "DEM hillshade unavailable";
  const cropText = result.display_crop === "view_bbox"
    ? "viewer zoom crop"
    : (result.display_crop === "project_aoi" ? "AOI crop" : "full MintPy grid");
  const resolutionText = `${result.native_raster_width}x${result.native_raster_height} cells rendered at ${result.render_width}x${result.render_height}px`;
  els.mapMeta.textContent = `LOS displacement in cm. Positive is toward the satellite. Color range: +/-${result.color_limit_cm.toFixed(2)} cm. ${hillshadeText}. ${cropText}. ${resolutionText}. ${referenceStatus(result)} ${correctionSummary(result)}.`;
  els.clickMeta.textContent = "Click the map to sample an InSAR pixel.";
}

function clearCombinedMaps(message) {
  hideExportRows(els.combinedUpExportRow, els.combinedEastExportRow);
  for (const frame of [els.combinedUpFrame, els.combinedEastFrame]) {
    removeMapView(frame);
    frame.innerHTML = "";
    const div = document.createElement("div");
    div.className = "empty-map";
    div.textContent = message || "No map rendered.";
    frame.appendChild(div);
  }
}

function paintCombinedMap(result) {
  paintCombinedComponent(
    els.combinedUpFrame,
    result.up_image_url,
    "Vertical displacement map",
    result,
  );
  setExportLinks(
    els.combinedUpExportRow,
    els.combinedUpJpegDownload,
    els.combinedUpGeotiffDownload,
    result.downloads?.up,
  );
  setExportLinks(
    els.combinedEastExportRow,
    els.combinedEastJpegDownload,
    els.combinedEastGeotiffDownload,
    result.downloads?.east,
  );
  paintCombinedComponent(
    els.combinedEastFrame,
    result.east_image_url,
    "East-west displacement map",
    result,
  );
  const shared = `Positive up/east. Relative to ASC and DESC MintPy reference pixels. Color range: +/-${result.color_limit_cm.toFixed(2)} cm. ${result.native_raster_width}x${result.native_raster_height} cells rendered at ${result.render_width}x${result.render_height}px. ${referenceStatus(result)} ${correctionSummary(result)}.`;
  els.combinedUpMeta.textContent = `Vertical displacement in cm. ${shared}`;
  els.combinedEastMeta.textContent = `East displacement in cm. ${shared}`;
}

function paintCombinedComponent(frame, imageUrl, altText, result) {
  removeMapView(frame);
  frame.innerHTML = "";
  const img = document.createElement("img");
  img.alt = altText;
  img.addEventListener("load", () => mountMapView(frame, img, result, true));
  img.src = `${imageUrl}?t=${Date.now()}`;
  frame.appendChild(img);
  addComponentOverlay(frame, result);
}

function addComponentOverlay(frame, result) {
  const colorScale = document.createElement("div");
  colorScale.className = "color-scale";
  colorScale.innerHTML = `
    <div class="color-scale-title">cm</div>
    <div class="color-scale-row">
      <div class="color-scale-labels">
        <span>+${result.color_limit_cm.toFixed(1)}</span>
        <span>0</span>
        <span>-${result.color_limit_cm.toFixed(1)}</span>
      </div>
      <div class="color-scale-bar" aria-hidden="true"></div>
    </div>
  `;
  frame.appendChild(colorScale);
}

function addMapOverlays(result) {
  const colorScale = document.createElement("div");
  colorScale.className = "color-scale";
  colorScale.innerHTML = `
    <div class="color-scale-title">LOS cm</div>
    <div class="color-scale-row">
      <div class="color-scale-labels">
        <span>+${result.color_limit_cm.toFixed(1)}</span>
        <span>0</span>
        <span>-${result.color_limit_cm.toFixed(1)}</span>
      </div>
      <div class="color-scale-bar" aria-hidden="true"></div>
    </div>
  `;
  els.mapFrame.appendChild(colorScale);

  if (!result.satellite_look_direction) return;
  const look = result.satellite_look_direction;
  const lookArrow = document.createElement("div");
  lookArrow.className = "look-direction";
  lookArrow.title = "Satellite-to-ground horizontal look direction";
  lookArrow.innerHTML = `
    <div class="look-direction-label">satellite look</div>
    <div class="look-direction-compass">
      <div class="look-direction-arrow" style="transform: rotate(${look.screen_angle_deg}deg)"></div>
    </div>
  `;
  els.mapFrame.appendChild(lookArrow);
}

function paintEmpty(message) {
  removeMapView(els.mapFrame);
  hideExportRows(els.losExportRow);
  els.mapFrame.innerHTML = "";
  const div = document.createElement("div");
  div.className = "empty-map";
  div.textContent = message || "No map rendered.";
  els.mapFrame.appendChild(div);
}

function hideExportRows(...rows) {
  rows.forEach(row => row.classList.add("hidden"));
}

function setExportLinks(row, jpegLink, geotiffLink, downloads) {
  if (!downloads?.jpeg?.url || !downloads?.geotiff?.url) {
    row.classList.add("hidden");
    return;
  }
  for (const [link, item] of [
    [jpegLink, downloads.jpeg],
    [geotiffLink, downloads.geotiff],
  ]) {
    link.href = item.url;
    link.download = item.filename;
  }
  row.classList.remove("hidden");
}

function markerPixels(latitude, longitude) {
  if (!state.currentMap) return null;
  return markerPixelsFor(els.mapFrame, state.currentMap, latitude, longitude);
}

function markerPixelsFor(frame, mapResult, latitude, longitude) {
  if (!mapResult) return null;
  const img = frame.querySelector("img");
  if (!img) return null;
  const projected = mapResult.markerCoordinates?.get(coordinateKey(latitude, longitude));
  if (!projected || projected.x < 0 || projected.x > 1 || projected.y < 0 || projected.y > 1) return null;
  const imageRect = renderedImageRect(img);
  const frameRect = frame.getBoundingClientRect();
  const xRatio = projected.x;
  const yRatio = projected.y;
  return {
    x: imageRect.left - frameRect.left - frame.clientLeft + xRatio * imageRect.width,
    y: imageRect.top - frameRect.top - frame.clientTop + yRatio * imageRect.height,
  };
}

function renderedImageRect(img) {
  const rect = img.getBoundingClientRect();
  if (img.classList.contains("map-raster")) return rect;
  const naturalWidth = img.naturalWidth || rect.width;
  const naturalHeight = img.naturalHeight || rect.height;
  const imageAspect = naturalWidth / Math.max(naturalHeight, 1);
  const frameAspect = rect.width / Math.max(rect.height, 1);
  if (frameAspect > imageAspect) {
    const height = rect.height;
    const width = height * imageAspect;
    return {
      left: rect.left + (rect.width - width) / 2,
      top: rect.top,
      width,
      height,
    };
  }
  const width = rect.width;
  const height = width / imageAspect;
  return {
    left: rect.left,
    top: rect.top + (rect.height - height) / 2,
    width,
    height,
  };
}

function addStationMarkers() {
  addStationMarkersToFrame(els.mapFrame, state.currentMap);
}

function addStationMarkersToFrame(frame, mapResult) {
  frame.querySelectorAll(".station-marker").forEach((marker) => marker.remove());
  for (const station of state.stations) {
    const point = markerPixelsFor(frame, mapResult, station.latitude, station.longitude);
    if (!point) continue;
    const marker = document.createElement("div");
    marker.className = "station-marker";
    marker.dataset.label = station.label;
    marker.style.left = `${point.x}px`;
    marker.style.top = `${point.y}px`;
    frame.appendChild(marker);
  }
}

async function refreshGpsVectors() {
  if (!els.gpsVectorsToggle.checked) {
    state.gpsVectors = null;
    clearGpsVectorOverlays();
    return;
  }
  const hasMap = state.mode === "combined" ? state.currentCombinedMap : state.currentMap;
  if (!hasMap || !els.project.value) return;
  try {
    state.gpsVectors = await api("/api/gps-vectors", {
      method: "POST",
      body: JSON.stringify(requestPayload({ mode: state.mode })),
    });
    await prepareMapCoordinates(hasMap);
    drawGpsVectorsOnVisibleMaps();
  } catch (error) {
    state.gpsVectors = null;
    clearGpsVectorOverlays();
    setStatus(error.message, "error");
  }
}

function clearGpsVectorOverlays() {
  for (const frame of [els.mapFrame, els.combinedUpFrame, els.combinedEastFrame]) {
    frame.querySelectorAll(".gps-vector").forEach((marker) => marker.remove());
  }
}

function drawGpsVectorsOnVisibleMaps() {
  if (state.mode === "combined") {
    addGpsVectorsToFrame(els.combinedUpFrame, state.currentCombinedMap);
    addGpsVectorsToFrame(els.combinedEastFrame, state.currentCombinedMap);
  } else {
    addGpsVectorsToFrame(els.mapFrame, state.currentMap);
  }
}

function addGpsVectorsToFrame(frame, mapResult) {
  frame.querySelectorAll(".gps-vector").forEach((marker) => marker.remove());
  const vectors = state.gpsVectors && Array.isArray(state.gpsVectors.vectors)
    ? state.gpsVectors.vectors
    : [];
  if (!mapResult || vectors.length === 0) return;
  const maxHorizontal = Math.max(
    1,
    ...vectors.map((vector) => Math.abs(vector.horizontal_cm || 0)),
  );
  const maxVertical = Math.max(
    1,
    ...vectors.map((vector) => Math.abs(vector.up_cm || 0)),
  );
  const arrowScale = 34 / maxHorizontal;
  const verticalScale = 9 / maxVertical;
  for (const vector of vectors) {
    const point = markerPixelsFor(frame, mapResult, vector.latitude, vector.longitude);
    if (!point) continue;
    const east = Number(vector.east_cm) || 0;
    const north = Number(vector.north_cm) || 0;
    const up = Number(vector.up_cm) || 0;
    const x = east * arrowScale;
    const y = -north * arrowScale;
    const horizontalSigma = Number.isFinite(vector.horizontal_sigma_cm)
      ? Math.max(2, vector.horizontal_sigma_cm * arrowScale)
      : 0;
    const upSigma = Number.isFinite(vector.up_sigma_cm)
      ? Math.max(2, vector.up_sigma_cm * verticalScale)
      : 0;
    const verticalLength = Math.max(7, Math.min(23, Math.abs(up) * verticalScale + 5));
    const verticalX = -10;
    const verticalY = up >= 0 ? -verticalLength : verticalLength;
    const horizontalAngle = Math.atan2(y, x) * 180 / Math.PI;
    const horizontalMarkerId = `gps-horizontal-arrow-${safeId(vector.label)}-${Math.random().toString(36).slice(2)}`;
    const verticalMarkerId = `gps-vertical-arrow-${safeId(vector.label)}-${Math.random().toString(36).slice(2)}`;
    const marker = document.createElement("div");
    marker.className = "gps-vector";
    marker.style.left = `${point.x}px`;
    marker.style.top = `${point.y}px`;
    marker.title = `${vector.label}: E ${east.toFixed(2)} cm, N ${north.toFixed(2)} cm, U ${up.toFixed(2)} cm`;
    marker.innerHTML = `
      <svg viewBox="-45 -45 90 90" aria-label="${escapeHtml(vector.label)} GPS vector">
        <defs>
          <marker id="${horizontalMarkerId}" class="gps-vector-horizontal-marker" markerWidth="5" markerHeight="5" refX="4.5" refY="2.5" orient="auto">
            <path d="M0,0 L5,2.5 L0,5 Z"></path>
          </marker>
          <marker id="${verticalMarkerId}" class="gps-vector-vertical-marker" markerWidth="5" markerHeight="5" refX="4.5" refY="2.5" orient="auto">
            <path d="M0,0 L5,2.5 L0,5 Z"></path>
          </marker>
        </defs>
        ${horizontalSigma ? `<ellipse class="gps-vector-horizontal-sigma" cx="${x.toFixed(2)}" cy="${y.toFixed(2)}" rx="${horizontalSigma.toFixed(2)}" ry="${Math.max(1.6, horizontalSigma * 0.45).toFixed(2)}" transform="rotate(${horizontalAngle.toFixed(2)} ${x.toFixed(2)} ${y.toFixed(2)})"></ellipse>` : ""}
        ${upSigma ? `<ellipse class="gps-vector-up-sigma" cx="${verticalX}" cy="${verticalY.toFixed(2)}" rx="${Math.max(1.6, upSigma * 0.45).toFixed(2)}" ry="${upSigma.toFixed(2)}"></ellipse>` : ""}
        <line class="gps-vector-horizontal-arrow" x1="0" y1="0" x2="${x.toFixed(2)}" y2="${y.toFixed(2)}" marker-end="url(#${horizontalMarkerId})"></line>
        <line class="gps-vector-vertical-arrow" x1="${verticalX}" y1="0" x2="${verticalX}" y2="${verticalY.toFixed(2)}" marker-end="url(#${verticalMarkerId})"></line>
      </svg>
    `;
    frame.appendChild(marker);
  }
}

function addDemContoursToFrame(frame, mapResult) {
  const contours = mapResult ? mapResult.dem_contours : null;
  if (!contours || !Array.isArray(contours.lines) || contours.lines.length === 0) return;
  const img = frame.querySelector("img");
  if (!img) return;
  const imageRect = renderedImageRect(img);
  const frameRect = frame.getBoundingClientRect();
  const position = (layer) => Object.assign(layer.style, {
    left: `${imageRect.left - frameRect.left - frame.clientLeft}px`,
    top: `${imageRect.top - frameRect.top - frame.clientTop}px`,
    width: `${imageRect.width}px`, height: `${imageRect.height}px`,
  });
  const existing = frame.querySelector(".dem-contours");
  if (existing) {
    position(existing);
    return;
  }
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("dem-contours");
  svg.setAttribute("viewBox", "0 0 100 100");
  svg.setAttribute("preserveAspectRatio", "none");
  position(svg);
  svg.setAttribute("aria-label", `DEM contours every ${contours.interval_m} m`);
  for (const line of contours.lines) {
    const points = line.points
      .map((point) => `${(point[0] * 100).toFixed(3)},${(point[1] * 100).toFixed(3)}`)
      .join(" ");
    if (!points) continue;
    const polyline = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    polyline.setAttribute("points", points);
    polyline.setAttribute("data-elevation-m", String(line.elevation_m));
    svg.appendChild(polyline);
  }
  frame.appendChild(svg);
}

function addReferenceMarker() {
  els.mapFrame.querySelectorAll(".reference-marker").forEach((marker) => marker.remove());
  const reference = state.currentMap ? state.currentMap.mintpy_reference_point : null;
  if (!reference) return;
  const point = markerPixels(reference.latitude, reference.longitude);
  if (!point) return;
  const marker = document.createElement("div");
  marker.className = "reference-marker";
  marker.title = "MintPy reference point";
  marker.style.left = `${point.x}px`;
  marker.style.top = `${point.y}px`;
  els.mapFrame.appendChild(marker);
}

function addCombinedReferenceMarkers(frame, mapResult) {
  frame.querySelectorAll(".reference-marker").forEach((marker) => marker.remove());
  const references = mapResult ? mapResult.mintpy_reference_points : null;
  if (!references) return;
  for (const orbit of ["asc", "desc"]) {
    const reference = references[orbit];
    if (!reference) continue;
    const point = markerPixelsFor(frame, mapResult, reference.latitude, reference.longitude);
    if (!point) continue;
    const marker = document.createElement("div");
    marker.className = `reference-marker reference-marker-${orbit}`;
    marker.title = `${orbit.toUpperCase()} MintPy reference point (${reference.project})`;
    marker.dataset.label = `${orbit.toUpperCase()} ref`;
    marker.style.left = `${point.x}px`;
    marker.style.top = `${point.y}px`;
    frame.appendChild(marker);
  }
}

function addDisplayReferenceMarker(frame, mapResult) {
  frame.querySelectorAll(".display-reference-marker").forEach((marker) => marker.remove());
  const reference = mapResult?.display_reference_point;
  if (!reference) return;
  const point = markerPixelsFor(
    frame,
    mapResult,
    reference.latitude,
    reference.longitude,
  );
  if (!point) return;
  const marker = document.createElement("div");
  marker.className = "display-reference-marker";
  marker.title = "Temporary display reference";
  marker.style.left = `${point.x}px`;
  marker.style.top = `${point.y}px`;
  frame.appendChild(marker);
}

function addPixelMarker() {
  if (!state.selectedPixel) return;
  els.mapFrame.querySelectorAll(".pixel-marker").forEach((marker) => marker.remove());
  const point = markerPixels(state.selectedPixel.lat, state.selectedPixel.lon);
  if (!point) return;
  const marker = document.createElement("div");
  marker.className = "pixel-marker";
  marker.style.left = `${point.x}px`;
  marker.style.top = `${point.y}px`;
  els.mapFrame.appendChild(marker);
}

function addCombinedPixelMarker(frame) {
  frame.querySelectorAll(".pixel-marker").forEach((marker) => marker.remove());
  if (!state.combinedSelectedPixel || !state.currentCombinedMap) return;
  const point = markerPixelsFor(
    frame,
    state.currentCombinedMap,
    state.combinedSelectedPixel.lat,
    state.combinedSelectedPixel.lon,
  );
  if (!point) return;
  const marker = document.createElement("div");
  marker.className = "pixel-marker";
  marker.title = "Selected InSAR pixel";
  marker.style.left = `${point.x}px`;
  marker.style.top = `${point.y}px`;
  frame.appendChild(marker);
}

async function sampleLosPixel(lat, lon) {
  const map = state.currentMap;
  const payload = requestPayload({ lat, lon });
  state.selectedPixel = { lat, lon };
  els.clickMeta.textContent = `Sampling ${lat.toFixed(5)}, ${lon.toFixed(5)}...`;
  try {
    await prepareMapCoordinates(map, [{ lat, lon }]);
    const series = await api("/api/pixel-timeseries", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (map !== state.currentMap || state.selectedPixel?.lat !== lat || state.selectedPixel?.lon !== lon) return;
    state.pixelSeries = series;
    els.clickMeta.textContent = `Pixel row ${state.pixelSeries.row}, col ${state.pixelSeries.col}`;
    addPixelMarker();
    drawSeries();
  } catch (error) {
    els.clickMeta.textContent = error.message;
  }
}

async function sampleCombinedPixel(lat, lon) {
  const map = state.currentCombinedMap;
  const payload = requestPayload({ lat, lon });
  state.combinedSelectedPixel = { lat, lon };
  try {
    await prepareMapCoordinates(map, [{ lat, lon }]);
    const result = await api("/api/combined-pixel-timeseries", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (map !== state.currentCombinedMap || state.combinedSelectedPixel?.lat !== lat || state.combinedSelectedPixel?.lon !== lon) return;
    state.combinedPixelSeries = result;
    addCombinedPixelMarker(els.combinedUpFrame);
    addCombinedPixelMarker(els.combinedEastFrame);
    drawCombinedSeries("up");
    drawCombinedSeries("east");
    const sourceWarning = correctionWarning(result);
    setStatus(
      `Sampled combined InSAR pixel at ${lat.toFixed(5)}, ${lon.toFixed(5)}. ${referenceStatus(result)}${sourceWarning ? ` ${sourceWarning}` : ""}`,
      sourceWarning ? "warning" : "",
    );
  } catch (error) {
    setStatus(error.message, "error");
  }
}

function beginReferenceSelection() {
  const hasMap = state.mode === "combined"
    ? Boolean(state.currentCombinedMap)
    : Boolean(state.currentMap);
  if (!hasMap) return;
  state.selectingReference = !state.selectingReference;
  updateReferenceControls();
  if (state.selectingReference) {
    setStatus("Click a finite high-coherence map pixel to use as the temporary display reference.");
  }
}

async function applyTemporaryReference(lat, lon) {
  const previousReference = state.displayReference;
  const previousPixel = state.mode === "combined"
    ? state.combinedSelectedPixel
    : state.selectedPixel;
  state.displayReference = { lat, lon };
  state.selectingReference = false;
  updateReferenceControls();
  const succeeded = await renderInterval();
  if (!succeeded) {
    state.displayReference = previousReference;
    updateReferenceControls();
  } else if (previousPixel) {
    if (state.mode === "combined") {
      await sampleCombinedPixel(previousPixel.lat, previousPixel.lon);
    } else {
      await sampleLosPixel(previousPixel.lat, previousPixel.lon);
    }
  }
}

async function resetTemporaryReference() {
  if (!state.displayReference) return;
  const previousReference = state.displayReference;
  const previousPixel = state.mode === "combined"
    ? state.combinedSelectedPixel
    : state.selectedPixel;
  state.displayReference = null;
  state.selectingReference = false;
  updateReferenceControls();
  const succeeded = await renderInterval();
  if (!succeeded) {
    state.displayReference = previousReference;
    updateReferenceControls();
  } else if (previousPixel) {
    if (state.mode === "combined") {
      await sampleCombinedPixel(previousPixel.lat, previousPixel.lon);
    } else {
      await sampleLosPixel(previousPixel.lat, previousPixel.lon);
    }
  }
}

async function onGpsChange() {
  state.gpsSeries = null;
  const station = els.gps.value;
  if (!station) {
    drawSeries();
    return;
  }
  try {
    state.gpsSeries = await api("/api/gps-timeseries", {
      method: "POST",
      body: JSON.stringify(requestPayload({ station })),
    });
    drawSeries();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function onCombinedGpsChange(component) {
  const select = component === "up" ? els.combinedUpGps : els.combinedEastGps;
  const key = component === "up" ? "combinedGpsUpSeries" : "combinedGpsEastSeries";
  state[key] = null;
  const station = select.value;
  if (!station) {
    drawCombinedSeries(component);
    return;
  }
  try {
    state[key] = await api("/api/combined-gps-timeseries", {
      method: "POST",
      body: JSON.stringify(requestPayload({ station, component })),
    });
    drawCombinedSeries(component);
  } catch (error) {
    setStatus(error.message, "error");
  }
}

function drawSeries() {
  const canvas = els.canvas;
  const { context, width, height } = prepareChartCanvas(canvas);
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#ffffff";
  context.fillRect(0, 0, width, height);

  const series = [];
  if (state.pixelSeries) {
    series.push({ ...state.pixelSeries, color: "#111111", dash: [8, 5] });
  }
  if (state.gpsSeries) {
    series.push({ ...state.gpsSeries, color: "#168b3f", dash: [] });
  }
  if (series.length === 0) {
    context.fillStyle = "#5c6964";
    context.font = "18px system-ui, sans-serif";
    context.fillText("Click a map pixel or select a GPS station.", 34, 58);
    els.details.textContent = "Select a station and/or click a pixel to compare LOS time series.";
    return;
  }

  const margins = { left: 58, right: 20, top: 26, bottom: 52 };
  const plot = {
    x0: margins.left,
    y0: margins.top,
    x1: width - margins.right,
    y1: height - margins.bottom,
  };
  const points = [];
  for (const item of series) {
    item.parsed = item.dates.map((value, index) => ({
      time: new Date(value).getTime(),
      value: item.values_cm[index],
      sigma: item.sigma_cm ? item.sigma_cm[index] : null,
    })).filter((point) => Number.isFinite(point.time) && Number.isFinite(point.value));
    points.push(...item.parsed);
  }
  if (points.length === 0) return;

  const minTime = Math.min(...points.map((point) => point.time));
  const maxTime = Math.max(...points.map((point) => point.time));
  const yValues = [0];
  for (const item of series) {
    for (const point of item.parsed) {
      yValues.push(point.value);
      if (Number.isFinite(point.sigma)) {
        yValues.push(point.value - point.sigma, point.value + point.sigma);
      }
    }
  }
  let minY = Math.min(...yValues);
  let maxY = Math.max(...yValues);
  if (minY === maxY) {
    minY -= 1;
    maxY += 1;
  }
  const padY = (maxY - minY) * 0.12;
  minY -= padY;
  maxY += padY;
  if (minY > 0) minY = 0;
  if (maxY < 0) maxY = 0;

  const xScale = (time) => plot.x0 + ((time - minTime) / Math.max(maxTime - minTime, 1)) * (plot.x1 - plot.x0);
  const yScale = (value) => plot.y1 - ((value - minY) / (maxY - minY)) * (plot.y1 - plot.y0);

  drawNetworkBackground(context, plot, minTime, maxTime, xScale);
  drawAxes(context, plot, minY, maxY, minTime, maxTime);
  for (const item of series) {
    if (item.sigma_cm) {
      drawUncertainty(context, item, xScale, yScale);
    }
    drawLine(context, item, xScale, yScale);
  }
  drawLegend(context, series, plot);
  updateDetails(series);
}

function drawCombinedSeries(component) {
  const isUp = component === "up";
  const canvas = isUp ? els.combinedUpCanvas : els.combinedEastCanvas;
  const details = isUp ? els.combinedUpDetails : els.combinedEastDetails;
  const gpsSeries = isUp ? state.combinedGpsUpSeries : state.combinedGpsEastSeries;
  const pixelSeries = state.combinedPixelSeries
    ? state.combinedPixelSeries.series[component]
    : null;
  const yLabel = isUp ? "Vertical displacement (cm)" : "East displacement (cm)";
  const series = [];
  if (pixelSeries) {
    series.push({ ...pixelSeries, color: "#111111", dash: [8, 5] });
  }
  if (gpsSeries) {
    series.push({ ...gpsSeries, color: "#168b3f", dash: [] });
  }
  drawComponentChart(canvas, details, series, yLabel, component);
}

function drawComponentChart(canvas, details, series, yLabel, component) {
  const { context, width, height } = prepareChartCanvas(canvas);
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#ffffff";
  context.fillRect(0, 0, width, height);

  if (series.length === 0) {
    context.fillStyle = "#5c6964";
    context.font = "17px system-ui, sans-serif";
    context.fillText("Click a map pixel or select a GPS station.", 30, 52);
    details.textContent = `No ${component} series selected yet.`;
    return;
  }

  const margins = { left: 58, right: 18, top: 22, bottom: 48 };
  const plot = {
    x0: margins.left,
    y0: margins.top,
    x1: width - margins.right,
    y1: height - margins.bottom,
  };
  const points = [];
  for (const item of series) {
    item.parsed = item.dates.map((value, index) => ({
      time: new Date(value).getTime(),
      value: item.values_cm[index],
      sigma: item.sigma_cm ? item.sigma_cm[index] : null,
    })).filter((point) => Number.isFinite(point.time) && Number.isFinite(point.value));
    points.push(...item.parsed);
  }
  if (points.length === 0) {
    details.textContent = `No finite ${component} values in this date window.`;
    return;
  }

  const minTime = Math.min(...points.map((point) => point.time));
  const maxTime = Math.max(...points.map((point) => point.time));
  const yValues = [0];
  for (const item of series) {
    for (const point of item.parsed) {
      yValues.push(point.value);
      if (Number.isFinite(point.sigma)) {
        yValues.push(point.value - point.sigma, point.value + point.sigma);
      }
    }
  }
  let minY = Math.min(...yValues);
  let maxY = Math.max(...yValues);
  if (minY === maxY) {
    minY -= 1;
    maxY += 1;
  }
  const padY = (maxY - minY) * 0.12;
  minY -= padY;
  maxY += padY;
  if (minY > 0) minY = 0;
  if (maxY < 0) maxY = 0;

  const xScale = (time) => plot.x0 + ((time - minTime) / Math.max(maxTime - minTime, 1)) * (plot.x1 - plot.x0);
  const yScale = (value) => plot.y1 - ((value - minY) / (maxY - minY)) * (plot.y1 - plot.y0);

  drawAxes(context, plot, minY, maxY, minTime, maxTime, yLabel);
  for (const item of series) {
    if (item.sigma_cm) {
      drawUncertainty(context, item, xScale, yScale);
    }
    drawLine(context, item, xScale, yScale);
  }
  drawLegend(context, series, plot);
  updateCombinedDetails(details, series, component);
}

function drawNetworkBackground(context, plot, minTime, maxTime, xScale) {
  const bands = state.currentMap && state.currentMap.network
    ? state.currentMap.network.bands || []
    : [];
  const disconnected = bands.filter((band) => band.kind === "disconnected");
  if (disconnected.length === 0) return;

  context.save();
  context.beginPath();
  context.rect(plot.x0, plot.y0, plot.x1 - plot.x0, plot.y1 - plot.y0);
  context.clip();
  context.fillStyle = "rgba(221, 126, 143, 0.12)";

  for (const band of disconnected) {
    const start = new Date(formatNetworkDate(band.start_date)).getTime();
    const end = new Date(formatNetworkDate(band.end_date)).getTime();
    if (!Number.isFinite(start) || !Number.isFinite(end)) continue;
    const leftTime = Math.max(Math.min(start, end), minTime);
    const rightTime = Math.min(Math.max(start, end), maxTime);
    if (rightTime <= leftTime) continue;
    const x0 = xScale(leftTime);
    const x1 = xScale(rightTime);
    context.fillRect(x0, plot.y0, x1 - x0, plot.y1 - plot.y0);
  }
  context.restore();
}

function formatNetworkDate(value) {
  const text = String(value);
  if (/^\d{8}$/.test(text)) {
    return `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}`;
  }
  return text;
}

function prepareChartCanvas(canvas) {
  const ratio = Math.max(1, window.devicePixelRatio || 1);
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, Math.round(canvas.clientWidth || rect.width || 720));
  const height = Math.max(1, Math.round(canvas.clientHeight || rect.height || 300));
  const pixelWidth = Math.max(1, Math.round(width * ratio));
  const pixelHeight = Math.max(1, Math.round(height * ratio));

  if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
    canvas.width = pixelWidth;
    canvas.height = pixelHeight;
  }
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  return { context, width, height };
}

function formatChartDate(time) {
  const date = new Date(time);
  const day = String(date.getUTCDate()).padStart(2, "0");
  const month = String(date.getUTCMonth() + 1).padStart(2, "0");
  const year = String(date.getUTCFullYear()).slice(-2);
  return `${day}/${month}/${year}`;
}

function dateTickBounds(context, tick) {
  const width = context.measureText(tick.label).width;
  if (tick.align === "left") return { left: tick.x, right: tick.x + width };
  if (tick.align === "right") return { left: tick.x - width, right: tick.x };
  return { left: tick.x - width / 2, right: tick.x + width / 2 };
}

function buildDateTicks(plot, minTime, maxTime, tickCount) {
  const plotWidth = Math.max(1, plot.x1 - plot.x0);
  return Array.from({ length: tickCount }, (_, index) => {
    const fraction = index / (tickCount - 1);
    const time = minTime + fraction * (maxTime - minTime);
    return {
      time,
      x: plot.x0 + fraction * plotWidth,
      label: formatChartDate(time),
      align: index === 0 ? "left" : index === tickCount - 1 ? "right" : "center",
      row: 0,
    };
  });
}

function calculateDateTicks(context, plot, minTime, maxTime) {
  if (maxTime <= minTime) {
    return [{
      time: minTime,
      x: (plot.x0 + plot.x1) / 2,
      label: formatChartDate(minTime),
      align: "center",
      row: 0,
    }];
  }

  const minimumGap = 12;
  for (let tickCount = 6; tickCount >= 2; tickCount -= 1) {
    const ticks = buildDateTicks(plot, minTime, maxTime, tickCount);
    const bounds = ticks.map((tick) => dateTickBounds(context, tick));
    const readable = bounds.every((bound, index) => (
      index === 0 || bound.left - bounds[index - 1].right >= minimumGap
    ));
    if (readable) return ticks;
  }

  const edgeTicks = buildDateTicks(plot, minTime, maxTime, 2);
  edgeTicks[1].row = 1;
  return edgeTicks;
}

function drawAxes(context, plot, minY, maxY, minTime, maxTime, yLabel = "LOS displacement (cm)") {
  context.strokeStyle = "#cfd9d4";
  context.lineWidth = 1;
  context.beginPath();
  context.moveTo(plot.x0, plot.y0);
  context.lineTo(plot.x0, plot.y1);
  context.lineTo(plot.x1, plot.y1);
  context.stroke();
  context.fillStyle = "#5c6964";
  context.font = "12px system-ui, sans-serif";
  context.textAlign = "right";
  context.textBaseline = "middle";
  for (let i = 0; i <= 4; i += 1) {
    const value = minY + (i / 4) * (maxY - minY);
    const y = plot.y1 - (i / 4) * (plot.y1 - plot.y0);
    context.fillText(value.toFixed(1), plot.x0 - 8, y);
    context.strokeStyle = "#edf1ef";
    context.beginPath();
    context.moveTo(plot.x0, y);
    context.lineTo(plot.x1, y);
    context.stroke();
  }
  if (minY < 0 && maxY > 0) {
    const yZero = plot.y1 - ((0 - minY) / (maxY - minY)) * (plot.y1 - plot.y0);
    context.strokeStyle = "#aab8b1";
    context.lineWidth = 1.3;
    context.beginPath();
    context.moveTo(plot.x0, yZero);
    context.lineTo(plot.x1, yZero);
    context.stroke();
    context.fillStyle = "#33413c";
    context.fillText("0", plot.x0 - 8, yZero);
  }
  context.save();
  context.translate(18, (plot.y0 + plot.y1) / 2);
  context.rotate(-Math.PI / 2);
  context.textAlign = "center";
  context.fillText(yLabel, 0, 0);
  context.restore();

  context.textBaseline = "top";
  for (const tick of calculateDateTicks(context, plot, minTime, maxTime)) {
    context.save();
    context.strokeStyle = "#cfd9d4";
    context.lineWidth = 1;
    context.beginPath();
    context.moveTo(tick.x, plot.y1);
    context.lineTo(tick.x, plot.y1 + 5);
    context.stroke();
    context.restore();
    context.textAlign = tick.align;
    context.fillText(tick.label, tick.x, plot.y1 + 14 + tick.row * 15);
  }
}

function drawUncertainty(context, item, xScale, yScale) {
  const upper = item.parsed.filter((point) => Number.isFinite(point.sigma));
  if (upper.length < 2) return;
  context.beginPath();
  upper.forEach((point, index) => {
    const x = xScale(point.time);
    const y = yScale(point.value + point.sigma);
    if (index === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
  [...upper].reverse().forEach((point) => {
    context.lineTo(xScale(point.time), yScale(point.value - point.sigma));
  });
  context.closePath();
  context.fillStyle = "rgba(80, 85, 82, 0.18)";
  context.fill();
}

function drawLine(context, item, xScale, yScale) {
  context.beginPath();
  item.parsed.forEach((point, index) => {
    const x = xScale(point.time);
    const y = yScale(point.value);
    if (index === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
  context.strokeStyle = item.color;
  context.lineWidth = 2.4;
  context.setLineDash(item.dash || []);
  context.stroke();
  context.setLineDash([]);
}

function drawLegend(context, series, plot) {
  let x = plot.x0 + 8;
  const y = plot.y0 + 2;
  context.font = "12px system-ui, sans-serif";
  context.textAlign = "left";
  context.textBaseline = "middle";
  for (const item of series) {
    context.strokeStyle = item.color;
    context.lineWidth = 2.4;
    context.setLineDash(item.dash || []);
    context.beginPath();
    context.moveTo(x, y + 8);
    context.lineTo(x + 28, y + 8);
    context.stroke();
    context.setLineDash([]);
    context.fillStyle = "#17201d";
    context.fillText(item.kind === "gps" ? `GPS ${item.label}` : "InSAR pixel", x + 34, y + 8);
    x += item.kind === "gps" ? 120 : 112;
  }
}

function updateDetails(series) {
  const lines = series.map((item) => {
    const extra = item.kind === "gps"
      ? `Projected with incidence ${formatNumber(item.incidence_angle_deg)} deg and azimuth ${formatNumber(item.azimuth_angle_deg)} deg.`
      : `Raster row ${item.row}, col ${item.col}.`;
    return `<p><strong>${item.label}</strong><br>${item.dates.length} points. ${extra}</p>`;
  });
  const reference = state.pixelSeries?.display_reference_point;
  const referenceNote = reference
    ? `<p>InSAR values are relative to temporary reference row ${reference.row}, col ${reference.col}, then zeroed at the first selected epoch.</p>`
    : "";
  els.details.innerHTML = `${lines.join("")}${referenceNote}<p>LOS convention: positive values are toward the satellite.</p>`;
}

function updateCombinedDetails(details, series, component) {
  const label = component === "up" ? "vertical" : "east-west";
  const rows = series.map((item) => {
    if (item.kind === "gps") {
      return `<p><strong>GPS ${item.label}</strong><br>${item.dates.length} ${label} epochs, re-referenced to first selected GPS epoch.</p>`;
    }
    const parent = state.combinedPixelSeries || {};
    return `<p><strong>InSAR pixel</strong><br>${item.dates.length} paired ASC/DESC epochs. ASC row ${parent.asc_row}, col ${parent.asc_col}; DESC row ${parent.desc_row}, col ${parent.desc_col}.</p>`;
  });
  const reference = state.combinedPixelSeries?.display_reference_point;
  const referenceNote = reference
    ? `<p>InSAR values are relative to the temporary reference at ${reference.latitude.toFixed(5)}, ${reference.longitude.toFixed(5)}, then zeroed at the first matched epoch.</p>`
    : "";
  details.innerHTML = `${rows.join("")}${referenceNote}<p>ASC/DESC decomposition assumes north displacement is zero.</p>`;
}

function formatNumber(value) {
  return Number.isFinite(value) ? Number(value).toFixed(2) : "n/a";
}

function safeId(value) {
  return String(value).replace(/[^a-zA-Z0-9_-]/g, "_");
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function setMode(mode) {
  viewGroups.clear();
  state.mode = mode;
  els.losTab.classList.toggle("active", mode === "los");
  els.combinedTab.classList.toggle("active", mode === "combined");
  els.losWorkspace.classList.toggle("hidden", mode !== "los");
  els.combinedWorkspace.classList.toggle("hidden", mode !== "combined");
  state.currentMap = null;
  state.currentCombinedMap = null;
  state.pixelSeries = null;
  state.gpsSeries = null;
  state.selectedPixel = null;
  state.combinedPixelSeries = null;
  state.combinedGpsUpSeries = null;
  state.combinedGpsEastSeries = null;
  state.combinedSelectedPixel = null;
  state.gpsVectors = null;
  state.displayReference = null;
  state.selectingReference = false;
  paintEmpty("Render an ERA5 interval map to begin.");
  clearCombinedMaps("Render an ASC/DESC decomposition to begin.");
  clearGpsVectorOverlays();
  drawSeries();
  drawCombinedSeries("up");
  drawCombinedSeries("east");
  updateReferenceControls();
  populateProjectSelect();
}

els.threshold.addEventListener("input", () => {
  els.thresholdValue.textContent = Number(els.threshold.value).toFixed(2);
});
els.render.addEventListener("click", renderInterval);
els.setReference.addEventListener("click", beginReferenceSelection);
els.resetReference.addEventListener("click", resetTemporaryReference);
els.project.addEventListener("change", async () => {
  applyProjectDates();
  viewGroups.clear();
  state.currentMap = null;
  state.currentCombinedMap = null;
  state.pixelSeries = null;
  state.gpsSeries = null;
  state.selectedPixel = null;
  state.combinedPixelSeries = null;
  state.combinedGpsUpSeries = null;
  state.combinedGpsEastSeries = null;
  state.combinedSelectedPixel = null;
  state.gpsVectors = null;
  state.displayReference = null;
  state.selectingReference = false;
  clearGpsVectorOverlays();
  if (state.mode === "combined") {
    clearCombinedMaps("Render an ASC/DESC decomposition to begin.");
  } else {
    paintEmpty("Render an ERA5 interval map to begin.");
  }
  await loadStations();
  drawSeries();
  drawCombinedSeries("up");
  drawCombinedSeries("east");
  updateReferenceControls();
});
els.gps.addEventListener("change", onGpsChange);
els.combinedUpGps.addEventListener("change", () => onCombinedGpsChange("up"));
els.combinedEastGps.addEventListener("change", () => onCombinedGpsChange("east"));
els.gpsVectorsToggle.addEventListener("change", refreshGpsVectors);
els.losTab.addEventListener("click", () => setMode("los"));
els.combinedTab.addEventListener("click", () => setMode("combined"));
let chartResizeFrame = null;
function scheduleChartRedraw() {
  if (chartResizeFrame !== null) cancelAnimationFrame(chartResizeFrame);
  chartResizeFrame = requestAnimationFrame(() => {
    chartResizeFrame = null;
    drawSeries();
    drawCombinedSeries("up");
    drawCombinedSeries("east");
  });
}

window.addEventListener("resize", () => {
  scheduleChartRedraw();
  if (state.mode === "combined") {
    if (!state.currentCombinedMap) return;
    addDemContoursToFrame(els.combinedUpFrame, state.currentCombinedMap);
    addDemContoursToFrame(els.combinedEastFrame, state.currentCombinedMap);
    addCombinedReferenceMarkers(els.combinedUpFrame, state.currentCombinedMap);
    addCombinedReferenceMarkers(els.combinedEastFrame, state.currentCombinedMap);
    addDisplayReferenceMarker(els.combinedUpFrame, state.currentCombinedMap);
    addDisplayReferenceMarker(els.combinedEastFrame, state.currentCombinedMap);
    addStationMarkersToFrame(els.combinedUpFrame, state.currentCombinedMap);
    addStationMarkersToFrame(els.combinedEastFrame, state.currentCombinedMap);
    addGpsVectorsToFrame(els.combinedUpFrame, state.currentCombinedMap);
    addGpsVectorsToFrame(els.combinedEastFrame, state.currentCombinedMap);
    addCombinedPixelMarker(els.combinedUpFrame);
    addCombinedPixelMarker(els.combinedEastFrame);
  } else {
    if (!state.currentMap) return;
    addDemContoursToFrame(els.mapFrame, state.currentMap);
    addReferenceMarker();
    addDisplayReferenceMarker(els.mapFrame, state.currentMap);
    addStationMarkers();
    addGpsVectorsToFrame(els.mapFrame, state.currentMap);
    addPixelMarker();
  }
});

if ("ResizeObserver" in window) {
  const chartResizeObserver = new ResizeObserver(scheduleChartRedraw);
  chartResizeObserver.observe(els.canvas);
  chartResizeObserver.observe(els.combinedUpCanvas);
  chartResizeObserver.observe(els.combinedEastCanvas);
}

loadProjects().catch((error) => {
  setStatus(error.message, "error");
});
drawSeries();
drawCombinedSeries("up");
drawCombinedSeries("east");
updateReferenceControls();
