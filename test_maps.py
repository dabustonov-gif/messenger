"""
Тесты картографического конвейера без GDAL: слои подменяются синтетическим поставщиком.

«Лист Генштаба» генерируется попиксельно: пиксель красный, если его координаты СК-42
(прямой пересчёт WGS-84 -> СК-42) лежат на километровой линии. Сетка gis.grid рисуется
обратным пересчётом (СК-42 -> WGS-84). Совпадение линий подтверждает согласованность пересчётов.
"""
import io
import os
import re
import sys
import zipfile

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from gis import engine, grid as gridmod, kmz, sk42  # noqa: E402
from gis.raster import Grid  # noqa: E402

BBOX = (37.50, 55.70, 37.62, 55.77)      # ~7.5 × 7.8 км, Москва, 7-я зона


def pixel_gk(g: Grid, zone: int):
    cols, rows = np.meshgrid(np.arange(g.width) + 0.5, np.arange(g.height) + 0.5)
    gx, gy = g.to_geo(cols, rows)
    if g.is_wgs84:
        return sk42.wgs84_to_sk42_gk(gy, gx, zone)
    return gy, gx                                          # ГК: x_geo = Y (восток), y_geo = X (север)


def fake_provider(g: Grid):
    zone = 7
    x, y = pixel_gk(g, zone)
    tol = 1.5 * max(g.px * (111_320 * 0.56 if g.is_wgs84 else 1), g.py * (111_320 if g.is_wgs84 else 1))
    on_line = (np.abs((x + tol / 2) % 1000 - tol / 2) < tol / 2) | (np.abs((y + tol / 2) % 1000 - tol / 2) < tol / 2)

    def provide(layer, grid_):
        assert grid_ == g
        h, w = g.height, g.width
        if layer == "genshtab":
            rgba = np.full((h, w, 4), 255, np.uint8)
            rgba[on_line] = (255, 0, 0, 255)
            return rgba
        return np.dstack([np.full((h, w), 40, np.uint8), np.full((h, w), 120, np.uint8),
                          np.full((h, w), 40, np.uint8), np.full((h, w), 255, np.uint8)])
    return provide, on_line


def test_grid_lines_match_gk_math_wgs84():
    g = Grid("wgs84", BBOX[0], BBOX[3], (BBOX[2] - BBOX[0]) / 1200, (BBOX[3] - BBOX[1]) / 1300, 1200, 1300)
    _, on_line = fake_provider(g)
    drawn = gridmod.draw_sk42_grid(Image.new("RGB", (g.width, g.height), "white"), g, 7, labels=False,
                                   color=(0, 0, 255), line_px=1)
    blue = np.array(drawn)[..., 2] > 200
    blue &= np.array(drawn)[..., 0] < 50
    # каждый нарисованный пиксель линии должен лежать в пределах 2 пикселей от «напечатанной» линии
    near = np.zeros_like(on_line)
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            near |= np.roll(np.roll(on_line, dy, 0), dx, 1)
    assert blue.sum() > 5000
    assert (blue & near).sum() / blue.sum() > 0.98


def test_kmz_respects_garmin_limits(tmp_path):
    req = engine.MapRequest(bbox=BBOX, fmt="kmz", detail_m=2.0, max_tiles=12, tile_px=1024, name="Тест")
    state = {}

    def provider(layer, g):
        if "p" not in state:
            state["p"], _ = fake_provider(g)
        return state["p"](layer, g)

    out = tmp_path / "map.kmz"
    info = engine.build(req, provider, str(out))
    assert info["tiles"] <= 12 and info["zone"] == 7
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert names[0] == "doc.kml"
        kml = z.read("doc.kml").decode()
        jpgs = [n for n in names if n.endswith(".jpg")]
        assert len(jpgs) == info["tiles"] == kml.count("<GroundOverlay>")
        for n in jpgs:
            im = Image.open(io.BytesIO(z.read(n)))
            assert im.format == "JPEG" and im.width * im.height <= 1_048_576
            assert not im.info.get("progressive") and not im.info.get("progression")
    north = max(map(float, re.findall(r"<north>([\d.]+)", kml)))
    south = min(map(float, re.findall(r"<south>([\d.]+)", kml)))
    east = max(map(float, re.findall(r"<east>([\d.]+)", kml)))
    west = min(map(float, re.findall(r"<west>([\d.]+)", kml)))
    assert np.allclose([west, south, east, north], BBOX, atol=1e-6)


def test_tile_budget_math():
    w, h, k = kmz.fit_to_tile_budget(9000, 7000, 100, 1024)
    assert np.ceil(w / 1024) * np.ceil(h / 1024) <= 100 and k == 1.0
    w, h, k = kmz.fit_to_tile_budget(20000, 15000, 100, 1024)
    assert np.ceil(w / 1024) * np.ceil(h / 1024) <= 100 and k < 1


def test_pdf_exact_scale_and_sheets(tmp_path):
    req = engine.MapRequest(bbox=BBOX, fmt="pdf", scale=50_000, paper="A4", dpi=150, name="Тест печати")
    grids = []

    def provider(layer, g):
        grids.append(g)
        p, _ = fake_provider(g)
        return p(layer, g)

    out = tmp_path / "map.pdf"
    info = engine.build(req, provider, str(out))
    assert info["sheets"] == 1                     # 7.5 км × 7.8 км = 15 × 16 см — помещается на A4
    g = grids[0]
    assert g.crs == 7
    assert abs(g.px - 50_000 * 0.0254 / 150) < 1e-9          # 8.4667 м/пикс
    data = out.read_bytes()
    assert data.startswith(b"%PDF") and data.count(b"/Type /Page\n") + data.count(b"/Type /Page ") >= 1
    # охват листа покрывает всю область заказа
    gx0, gy0, gx1, gy1 = g.bounds
    xs, ys = sk42.wgs84_to_sk42_gk(np.array([BBOX[1], BBOX[3]]), np.array([BBOX[0], BBOX[2]]), 7)
    assert gx0 <= ys.min() and gx1 >= ys.max() and gy0 <= xs.min() and gy1 >= xs.max()


def test_pdf_splits_large_area(tmp_path):
    req = engine.MapRequest(bbox=(37.4, 55.70, 37.65, 55.80), fmt="pdf", scale=25_000, paper="A4", dpi=100)
    info = engine.build(req, lambda layer, g: fake_provider(g)[0](layer, g), str(tmp_path / "big.pdf"))
    assert info["sheets"] > 4


def test_validation():
    for bad in ({"bbox": [37, 55, 38.5, 56]}, {"bbox": [38, 55, 37, 56]}, {"bbox": [37, 55, 37.1, 55.1], "format": "img"}):
        try:
            engine.MapRequest.from_json(bad)
            raise AssertionError("должно быть отклонено")
        except engine.MapError:
            pass
