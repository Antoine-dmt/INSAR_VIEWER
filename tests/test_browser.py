"""Opt-in real browser checks against examples/make_demo.py products."""

import os
import pytest


@pytest.mark.skipif(not os.environ.get("INSAR_VIEWER_TEST_URL"), reason="opt-in running demo server")
@pytest.mark.parametrize("viewport", [{"width": 1440, "height": 1000}, {"width": 390, "height": 844}])
def test_maps_charts_downloads_and_navigation(viewport, tmp_path):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport=viewport, device_scale_factor=2, has_touch=True,
                                      base_url=os.environ["INSAR_VIEWER_TEST_URL"])
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(os.environ["INSAR_VIEWER_TEST_URL"])
        page.wait_for_function("document.querySelector('#startDate').value !== ''")
        assert page.locator("#startDate").input_value() == "2026-01-01"
        assert page.locator("#endDate").input_value() == "2026-06-01"
        page.locator("#renderButton").click()
        page.wait_for_function("state.currentMap !== null", timeout=60000)
        page.wait_for_function("document.querySelector('#mapFrame img')?.naturalWidth > 0")
        assert page.locator("#losGeotiffDownload").get_attribute("href").endswith(".tif")
        response = context.request.get(page.locator("#losJpegDownload").get_attribute("href"))
        assert response.ok and response.headers["content-type"] == "image/jpeg"
        grid = page.evaluate("state.currentMap.raster_grid")
        assert grid["width"] > 0 and grid["height"] > 0
        frame = page.locator("#mapFrame")
        width_before = frame.locator("img").evaluate("element => element.style.width")
        frame.get_by_role("button", name="Zoom in", exact=True).click()
        assert frame.locator("img").evaluate("element => element.style.width") != width_before
        frame.press("ArrowRight")
        frame.get_by_role("button", name="Reset zoom to displayed area", exact=True).click()
        frame.click(position={"x": frame.bounding_box()["width"] * .55,
                              "y": frame.bounding_box()["height"] * .55})
        page.wait_for_function("state.pixelSeries !== null", timeout=30000)
        assert page.evaluate("state.pixelSeries.values_cm[0]") == 0
        pixels = page.evaluate("""() => {
          const c = document.querySelector('#seriesCanvas');
          return Array.from(c.getContext('2d').getImageData(0,0,c.width,c.height).data)
            .filter((v,i) => i % 4 !== 3 && v < 200).length;
        }""")
        assert pixels > 100
        page.locator("#gpsSelect").select_option("DEMO")
        page.wait_for_function("state.gpsSeries !== null")
        page.locator("#gpsVectorsToggle").check()
        page.wait_for_function("state.gpsVectors !== null")
        page.locator("#setReferenceButton").click()
        frame.click(position={"x": frame.bounding_box()["width"] * .55,
                              "y": frame.bounding_box()["height"] * .55})
        page.wait_for_function("state.currentMap?.display_reference_point !== null && state.displayReference !== null")
        page.locator("#resetReferenceButton").click()
        page.wait_for_function("state.currentMap?.display_reference_point === null && state.displayReference === null")
        page.locator("#combinedTabButton").click()
        page.locator("#renderButton").click()
        page.wait_for_function("state.currentCombinedMap !== null", timeout=60000)
        page.wait_for_function("document.querySelector('#combinedUpFrame img')?.naturalWidth > 0")
        east = page.locator("#combinedEastFrame")
        east.click(position={"x": east.bounding_box()["width"] * .55,
                             "y": east.bounding_box()["height"] * .55})
        page.wait_for_function("state.combinedPixelSeries !== null", timeout=30000)
        assert page.evaluate("state.combinedPixelSeries.series.up.values_cm[0]") == 0
        assert page.locator("#combinedUpGeotiffDownload").get_attribute("href").endswith(".tif")
        assert context.request.get("/viewer.example.yaml").status == 404
        assert context.request.get("/%2e%2e/server.py").status == 404
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        page.screenshot(path=str(tmp_path / f"viewer-{viewport['width']}.png"), full_page=True)
        assert not errors
        context.close()
        browser.close()
