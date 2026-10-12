from __future__ import annotations

from datetime import date
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import h5py
import numpy as np
import rasterio

from insar_viewer import server
from insar_viewer.config import load_projects
from insar_viewer.geometry import normalize_azimuth
from insar_viewer.gnss import filter_to_bbox, load_gps


class BackendTests(unittest.TestCase):
    def setUp(self):
        os.environ["MPLCONFIGDIR"] = "/tmp/insar_viewer_test_mpl"
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.projects = {}
        self.original = (server.PROJECTS_PATH, server.CACHE_ROOT)
        server.PROJECTS_PATH = self.root / "viewer.yaml"
        server.CACHE_ROOT = self.root / "cache"
        server.CACHE_ROOT.mkdir()
        for orbit, az in (("ASC", -90.0), ("DESC", 90.0)):
            self.projects[orbit.lower()] = self.make_project(orbit, az)
        self.write_registry()

    def tearDown(self):
        server.PROJECTS_PATH, server.CACHE_ROOT = self.original
        self.tmp.cleanup()

    def make_project(self, orbit, azimuth):
        prefix = orbit.lower()
        ts = self.root / f"{prefix}.h5"
        stack = self.root / f"{prefix}_ifg.h5"
        geom = self.root / f"{prefix}_geom.h5"
        quality = self.root / f"{prefix}_quality.h5"
        with h5py.File(ts, "w") as h5:
            h5.create_dataset("date", data=np.asarray([b"20260101", b"20260113", b"20260125"]))
            h5.create_dataset("timeseries", data=np.stack([np.zeros((4, 5)), np.ones((4, 5)) * .01, np.ones((4, 5)) * .02]).astype("f4"))
            for key, value in {"EPSG": 4326, "X_FIRST": -84.3, "Y_FIRST": 10.3, "X_STEP": .01, "Y_STEP": -.01, "REF_X": 1, "REF_Y": 1}.items():
                h5.attrs[key] = value
            h5.attrs["UNIT"] = "m"
        with h5py.File(stack, "w") as h5:
            h5.create_dataset("date", data=np.asarray([[b"20260101", b"20260113"], [b"20260113", b"20260125"]]))
            h5.create_dataset("retained", data=np.asarray([True, True], dtype=np.bool_))
            h5.create_dataset("coherence", data=np.ones((2, 4, 5), dtype="f4") * .9)
            for key, value in {"EPSG": 4326, "X_FIRST": -84.3, "Y_FIRST": 10.3, "X_STEP": .01, "Y_STEP": -.01}.items():
                h5.attrs[key] = value
        with h5py.File(geom, "w") as h5:
            h5.create_dataset("incidenceAngle", data=np.ones((4, 5), dtype="f4") * 35)
            h5.create_dataset("azimuthAngle", data=np.ones((4, 5), dtype="f4") * azimuth)
            for key, value in {"EPSG": 4326, "X_FIRST": -84.3, "Y_FIRST": 10.3, "X_STEP": .01, "Y_STEP": -.01}.items():
                h5.attrs[key] = value
        with h5py.File(quality, "w") as h5:
            h5.create_dataset("temporalCoherence", data=np.ones((4, 5), dtype="f4") * .9)
            for key, value in {"EPSG": 4326, "X_FIRST": -84.3, "Y_FIRST": 10.3, "X_STEP": .01, "Y_STEP": -.01}.items():
                h5.attrs[key] = value
        return {"timeseries": ts, "stack": stack, "geometry": geom, "quality": quality}

    def write_registry(self, *, different_refs=False, quality=True, stack=True, geometry=True):
        lines = ["version: 1", "projects:"]
        for orbit, files in self.projects.items():
            ref_x = 1 if not (different_refs and orbit == "desc") else 2
            lines.extend([
                f"  - id: sample_{orbit}", f"    label: Sample {orbit.upper()}", "    group: sample",
                "    mission: sentinel1", f"    orbit: {orbit.upper()}", "    look_direction: right",
                "    aoi: {bbox: [-84.31, 10.25, -84.24, 10.31]}",
                f"    timeseries: {{path: {files['timeseries'].name}, corrections: [era5, ramp]}}",
            ])
            if geometry:
                lines.append(f"    geometry: {{path: {files['geometry'].name}, azimuth_convention: mintpy_ground_to_satellite}}")
            if stack:
                lines.append(f"    ifgram_stack: {{path: {files['stack'].name}, retained_dataset: retained, quality_dataset: coherence}}")
            if quality:
                lines.append(f"    quality: {{path: {files['quality'].name}, dataset: temporalCoherence, label: temporal coherence, type: coherence}}")
        server.PROJECTS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_registry_paths_and_explicit_metadata(self):
        project = load_projects(server.PROJECTS_PATH)["sample_asc"]
        self.assertEqual(project.mission, "sentinel1")
        self.assertEqual(project.corrections, ("era5", "ramp"))
        self.assertEqual(project.timeseries_path, self.projects["asc"]["timeseries"])

    def test_mintpy_first_is_outer_corner_and_reference_is_pixel_center(self):
        data = server.read_era5_timeseries(server.get_project("sample_asc"))
        self.assertEqual(server.point_to_row_col(10.285, -84.285, data["profile"]), (1, 1))
        self.assertAlmostEqual(data["reference_point"][0], 10.285)
        self.assertAlmostEqual(data["reference_point"][1], -84.285)

    def test_pixel_interval_quality_and_network_api_functions(self):
        data = server.read_era5_timeseries(server.get_project("sample_asc"))
        self.assertAlmostEqual(data["reference_point"][0], 10.285)
        self.assertAlmostEqual(data["reference_point"][1], -84.285)
        pixel = server.pixel_timeseries({"project": "sample_asc", "lat": 10.29, "lon": -84.29})
        self.assertEqual(pixel["values_cm"], [0.0, 1.0, 2.0])
        quality, label = server.interval_mean_coherence(server.get_project("sample_asc"), expected_shape=(4, 5), start_date="20260101", end_date="20260125")
        self.assertEqual(quality.shape, (4, 5))
        self.assertIn("temporal coherence", label)
        network = server.network_connectivity(server.get_project("sample_asc"), [date(2026, 1, 1), date(2026, 1, 13), date(2026, 1, 25)])
        self.assertTrue(network["assessed"])
        self.assertEqual(len(network["components"]), 1)
        interval = server.interval_map({"project": "sample_asc", "start_date": "2026-01-01", "end_date": "2026-01-25", "coherence_threshold": 0.5})
        self.assertGreater(interval["valid_pixel_count"], 0)

    def test_time_series_remains_available_without_quality_or_network(self):
        self.write_registry(quality=False, stack=False)
        data = server.read_era5_timeseries(server.get_project("sample_asc"))
        self.assertEqual(len(data["dates"]), 3)
        network = server.network_connectivity(server.get_project("sample_asc"), [date(2026, 1, 1)])
        self.assertFalse(network["assessed"])
        coherence, source = server.interval_mean_coherence(server.get_project("sample_asc"), expected_shape=(4, 5), start_date="20260101", end_date="20260125")
        self.assertIsNone(coherence)
        self.assertIsNone(source)

    def test_combined_pixel_decomposition_with_shared_hdf_reference(self):
        result = server.combined_pixel_timeseries({"project": "sentinel1:sample", "lat": 10.29, "lon": -84.29, "start_date": "2026-01-01", "end_date": "2026-01-25"})
        self.assertEqual(result["unit"], "cm")
        self.assertEqual(result["assumption"], "north displacement is assumed zero; ASC/DESC LOS solve east and up components.")

    def test_network_fails_closed_for_integer_flags(self):
        with h5py.File(self.projects["asc"]["stack"], "a") as h5:
            del h5["retained"]
            h5.create_dataset("retained", data=np.asarray([1, 1], dtype="i1"))
        network = server.network_connectivity(server.get_project("sample_asc"), [date(2026, 1, 1), date(2026, 1, 13)])
        self.assertFalse(network["assessed"])

    def test_pair_decomposition_requires_common_source_reference(self):
        self.write_registry(different_refs=True)
        # Shift the DESC source reference and verify the pair gate blocks unreferenced decomposition.
        with h5py.File(self.projects["desc"]["timeseries"], "a") as h5:
            h5.attrs["REF_X"] = 2
        with self.assertRaisesRegex(ValueError, "reference pixels differ"):
            server.combined_pixel_timeseries({"project": "sentinel1:sample", "lat": 10.29, "lon": -84.29, "start_date": "2026-01-01", "end_date": "2026-01-25"})

    def test_gnss_csv_parsing_bbox_and_units(self):
        path = self.root / "stations.csv"
        path.write_text("site,date,latitude,longitude,east_cm,north_cm,up_cm,east_sigma_cm,north_sigma_cm,up_sigma_cm,corr_east_north,corr_east_up,corr_north_up\nA,20260101,10.28,-84.28,0,0,0,0.1,0.2,0.3,0.1,0.2,0.3\nA,20260113,10.28,-84.28,1,2,3,0.1,0.2,0.3,0.1,0.2,0.3\nB,20260101,11,-84,0,0,0,,,,,,\n", encoding="utf-8")
        found = filter_to_bbox(load_gps(path), (-84.31, 10.25, -84.24, 10.31))
        self.assertEqual([s.label for s in found], ["A"])
        projected = server.project_gps_series(found, server.get_project("sample_asc"), "20260101", "20260113")
        self.assertEqual(len(projected[0].values_cm), 2)
        self.assertEqual(len(projected[0].values_sigma_cm), 2)

    def test_legacy_nisar_azimuth_is_explicit_and_checked(self):
        original = np.asarray([0.0, 1.0])
        corrected, adjustment = normalize_azimuth(original, "mintpy_ground_to_satellite", mission="nisar", orbit="ASC")
        np.testing.assert_array_equal(corrected, original)
        self.assertEqual(adjustment, 0.0)
        with self.assertRaises(ValueError):
            normalize_azimuth(np.asarray([-180.0, -179.0]), "legacy_nisar", mission="sentinel1", orbit="ASC")

    def test_decomposition_blocks_unassessed_network(self):
        with h5py.File(self.projects["asc"]["stack"], "a") as h5:
            del h5["retained"]
        payload = {"project": "sentinel1:sample", "lat": 10.285, "lon": -84.285,
                   "start_date": "2026-01-01", "end_date": "2026-01-25"}
        self.assertTrue(server.combined_interval_map(payload)["blocked"])
        with self.assertRaisesRegex(ValueError, "network"):
            server.combined_pixel_timeseries(payload)

    def test_interleaved_components_are_not_a_connected_interval(self):
        with h5py.File(self.projects["asc"]["stack"], "a") as h5:
            h5["date"][...] = [[b"20260101", b"20260125"], [b"20260113", b"20260113"]]
        project = server.get_project("sample_asc")
        network = server.network_connectivity(project, [date(2026, 1, 1), date(2026, 1, 13), date(2026, 1, 25)])
        self.assertEqual(len(network["components"]), 2)
        self.assertEqual(len(server.disconnected_gaps(network)), 2)
        result = server.combined_interval_map({"project": "sentinel1:sample", "start_date": "2026-01-01", "end_date": "2026-01-25"})
        self.assertTrue(result["blocked"])

    def test_rendering_does_not_modify_source_products(self):
        import hashlib
        paths = [path for files in self.projects.values() for path in files.values()]
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        server.interval_map({"project": "sample_asc", "start_date": "2026-01-01", "end_date": "2026-01-25"})
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})

    def test_known_east_up_solution_and_geo_export_units(self):
        for orbit, sign in (("asc", 1), ("desc", -1)):
            with h5py.File(self.projects[orbit]["timeseries"], "a") as h5:
                rate = np.full((4, 5), sign * np.sin(np.deg2rad(35)) * .01 + np.cos(np.deg2rad(35)) * .02)
                rate[1, 1] = 0
                h5["timeseries"][...] = np.asarray([i * rate for i in range(3)])
        result = server.combined_pixel_timeseries({"project": "sentinel1:sample", "lat": 10.275, "lon": -84.275,
                                                  "start_date": "2026-01-01", "end_date": "2026-01-25"})
        np.testing.assert_allclose(result["series"]["east"]["values_cm"], [0, 1, 2], atol=1e-5)
        np.testing.assert_allclose(result["series"]["up"]["values_cm"], [0, 2, 4], atol=1e-5)

    def test_common_los_reference_precedes_variable_geometry_inversion(self):
        for orbit, offset_cm in (("asc", 2), ("desc", -1)):
            with h5py.File(self.projects[orbit]["timeseries"], "a") as h5:
                h5["timeseries"][...] = np.asarray([np.full((4, 5), i * offset_cm / 200) for i in range(3)])
            with h5py.File(self.projects[orbit]["geometry"], "a") as h5:
                incidence = np.full((4, 5), 40)
                incidence[1, 1] = 30
                h5["incidenceAngle"][...] = incidence
        payload = {"project": "sentinel1:sample", "lat": 10.275, "lon": -84.275,
                   "reference_lat": 10.285, "reference_lon": -84.285,
                   "start_date": "2026-01-01", "end_date": "2026-01-25"}
        series = server.combined_pixel_timeseries(payload)
        for component in ("east", "up"):
            np.testing.assert_allclose(series["series"][component]["values_cm"], 0, atol=1e-6)
        with patch.object(server, "reproject_interval_grid", wraps=server.reproject_interval_grid) as reprojection:
            result = server.combined_interval_map(payload)
            np.testing.assert_allclose(reprojection.call_args.args[0]["displacement_cm"], 0, atol=1e-6)
        self.assertFalse(result["blocked"])
        for component in ("east", "up"):
            path = server.CACHE_ROOT / result["downloads"][component]["geotiff"]["url"].removeprefix("/cache/")
            with rasterio.open(path) as raster:
                values = raster.read(1, masked=True)
                self.assertEqual(values.count(), 20)
                np.testing.assert_allclose(values.compressed(), 0, atol=1e-6)

    def test_combined_mask_requires_coherence_in_both_orbits(self):
        for orbit, cell in (("asc", (0, 0)), ("desc", (0, 1))):
            with h5py.File(self.projects[orbit]["quality"], "a") as h5:
                h5["temporalCoherence"][cell] = np.nan
        result = server.combined_interval_map({"project": "sentinel1:sample",
                                               "start_date": "2026-01-01", "end_date": "2026-01-25"})
        for component in ("east", "up"):
            path = server.CACHE_ROOT / result["downloads"][component]["geotiff"]["url"].removeprefix("/cache/")
            with rasterio.open(path) as raster:
                values = raster.read(1, masked=True)
                self.assertTrue(values.mask[0, 0])
                self.assertTrue(values.mask[0, 1])
                self.assertFalse(values.mask[1, 1])

    def test_retained_quality_mean_preserves_zero_unless_explicit_nodata(self):
        self.write_registry(quality=False)
        with h5py.File(self.projects["asc"]["stack"], "a") as h5:
            h5["coherence"][...] = 0
            h5["coherence"][1, 0, 0] = .9
        project = server.get_project("sample_asc")
        kwargs = {"expected_shape": (4, 5), "start_date": "20260101", "end_date": "20260125"}
        quality, _ = server.interval_mean_coherence(project, **kwargs)
        self.assertAlmostEqual(float(quality[0, 0]), .45)
        self.assertEqual(quality[1, 1], 0)
        with h5py.File(self.projects["asc"]["stack"], "a") as h5:
            h5["coherence"].attrs["_FillValue"] = 0
        quality, _ = server.interval_mean_coherence(project, **kwargs)
        self.assertAlmostEqual(float(quality[0, 0]), .9)
        self.assertTrue(np.isnan(quality[1, 1]))

    def test_los_map_without_geometry_omits_optional_overlays(self):
        self.write_registry(geometry=False)
        project = server.get_project("sample_asc")
        result = server.interval_map({"project": "sample_asc", "start_date": "2026-01-01", "end_date": "2026-01-25"})
        self.assertEqual(result["valid_pixel_count"], 20)
        for field in ("hillshade_source", "dem_contours", "satellite_look_direction"):
            self.assertIsNone(result[field])
        with self.assertRaisesRegex(FileNotFoundError, "geometry"):
            server.read_geometry_arrays(project, expected_shape=(4, 5))
        with self.assertRaisesRegex(FileNotFoundError, "geometry"):
            server.combined_pixel_timeseries({"project": "sentinel1:sample", "lat": 10.275, "lon": -84.275,
                                              "start_date": "2026-01-01", "end_date": "2026-01-25"})

    def test_registry_rejects_cache_escape_project_ids(self):
        original = server.PROJECTS_PATH.read_text()
        for project_id in (".", ".."):
            with self.subTest(project_id=project_id):
                server.PROJECTS_PATH.write_text(original.replace("id: sample_asc", f"id: {project_id}"))
                with self.assertRaisesRegex(ValueError, "project id"):
                    load_projects(server.PROJECTS_PATH)

    def test_file_serving_rejects_unapproved_cache_extensions(self):
        handler = object.__new__(server.ViewerHandler)
        with patch.object(handler, "send_error") as error:
            for name in ("secret.env", "custom.cfg", "index.html", "registry.yaml", "science.h5"):
                with self.subTest(name=name):
                    path = server.CACHE_ROOT / name
                    path.write_text("private")
                    handler._serve_file(path)
                    error.assert_called_with(404, "File not found")


if __name__ == "__main__":
    unittest.main()
