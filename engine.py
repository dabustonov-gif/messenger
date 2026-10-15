"""
Сборка карты по заказу клиента: область (bbox WGS-84) -> слои -> сетка СК-42 -> KMZ (Garmin) или PDF.

Порядок для KMZ:
  1. рассчитать растровую сетку в WGS-84 с нужной детальностью (м/пиксель);
  2. при необходимости уменьшить её под лимит тайлов Garmin;
  3. запросить у поставщика слои (GDAL перепроецирует GeoTIFF спутника и листы Генштаба в эту сетку);
  4. наложить слой Генштаба поверх спутника с заданной прозрачностью;
  5. нарисовать километровую сетку СК-42 (изогнутые линии, пересчёт через gis.sk42);
  6. нарезать на тайлы ≤ 1 Мп и упаковать в .kmz.

Порядок для PDF: то же, но растр строится сразу в проекции Гаусса–Крюгера (прямая сетка)
с размером пикселя под заданный масштаб и DPI; большая область делится на листы.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from . import grid as gridmod
from . import kmz as kmzmod
from . import pdf as pdfmod
from . import sk42
from .raster import Grid, LayerProvider, blend

MAX_SIDE_KM = float(os.environ.get("GIS_MAX_SIDE_KM", "60"))
MAX_PDF_SHEETS = 24


class MapError(Exception):
    """Ошибка, понятная пользователю (выводится в приложении как есть)."""


@dataclass
class MapRequest:
    bbox: tuple[float, float, float, float]          # west, south, east, north (WGS-84, градусы)
    fmt: str = "kmz"                                 # "kmz" | "pdf"
    satellite: bool = True
    genshtab: bool = True
    opacity: float = 0.55                            # прозрачность слоя Генштаба над спутником
    grid: bool = True
    name: str = "Карта"
    # KMZ
    detail_m: float = 4.0                            # желаемый размер пикселя на местности
    max_tiles: int = 100
    tile_px: int = 1024
    # PDF
    scale: int = 50_000
    paper: str = "A4"
    dpi: int = 300
    sources_note: str = field(default="")

    @staticmethod
    def from_json(d: dict) -> "MapRequest":
        try:
            bbox = tuple(float(v) for v in d["bbox"])
            r = MapRequest(
                bbox=bbox, fmt=str(d.get("format", "kmz")), satellite=bool(d.get("satellite", True)),
                genshtab=bool(d.get("genshtab", True)), opacity=float(d.get("opacity", 0.55)),
                grid=bool(d.get("grid", True)), name=str(d.get("name", "Карта"))[:60],
                detail_m=float(d.get("detail_m", 4.0)), max_tiles=int(d.get("max_tiles", 100)),
                tile_px=int(d.get("tile_px", 1024)), scale=int(d.get("scale", 50_000)),
                paper=str(d.get("paper", "A4")), dpi=int(d.get("dpi", 300)))
        except (KeyError, TypeError, ValueError):
            raise MapError("Некорректные параметры карты")
        r.validate()
        return r

    def validate(self):
        w, s, e, n = self.bbox
        if not (-180 <= w < e <= 180 and -85 <= s < n <= 85):
            raise MapError("Неверно задана область")
        mid = math.radians((s + n) / 2)
        side_x = (e - w) * 111.32 * math.cos(mid)
        side_y = (n - s) * 111.32
        if max(side_x, side_y) > MAX_SIDE_KM:
            raise MapError(f"Область слишком большая: {side_x:.0f}×{side_y:.0f} км, максимум {MAX_SIDE_KM:.0f} км по стороне")
        if self.fmt not in ("kmz", "pdf"):
            raise MapError("Формат должен быть kmz или pdf")
        if not (self.satellite or self.genshtab):
            raise MapError("Не выбран ни один слой")
        if not (1 <= self.max_tiles <= 500 and 256 <= self.tile_px <= 1024):
            raise MapError("Лимиты тайлов: 1–500 тайлов, 256–1024 пикселя")
        if not (0.5 <= self.detail_m <= 100):
            raise MapError("Детальность: 0,5–100 м на пиксель")
        if self.paper not in pdfmod.PAPER_MM or not (5_000 <= self.scale <= 500_000) or not (100 <= self.dpi <= 600):
            raise MapError("Неверные параметры печати")


# ---------------------------------------------------------------------------------------------
def _render(req: MapRequest, g: Grid, provider: LayerProvider) -> Image.Image:
    """Запрашивает слои и смешивает их в одно RGB-изображение размером сетки."""
    sat = provider("satellite", g) if req.satellite else None
    gen = provider("genshtab", g) if req.genshtab else None
    missing = [n for n, want, arr in (("спутник", req.satellite, sat), ("Генштаб", req.genshtab, gen)) if want and arr is None]
    if len(missing) == int(req.satellite) + int(req.genshtab):
        raise MapError("На сервере нет данных для этой области: " + ", ".join(missing))
    opacity = req.opacity if sat is not None else 1.0       # без подложки Генштаб — непрозрачный
    return Image.fromarray(blend(sat, gen, opacity, (g.height, g.width)))


def zone_for_bbox(bbox) -> int:
    w, s, e, n = bbox
    _, lon, _ = sk42.wgs84_to_sk42_geo((s + n) / 2, (w + e) / 2)
    return sk42.zone_of(float(lon))


def build_kmz(req: MapRequest, provider: LayerProvider, out_path: str) -> dict:
    w, s, e, n = req.bbox
    px_lat = req.detail_m / 111_320
    px_lon = req.detail_m / (111_320 * math.cos(math.radians((s + n) / 2)))
    width0, height0 = math.ceil((e - w) / px_lon), math.ceil((n - s) / px_lat)
    width, height, k = kmzmod.fit_to_tile_budget(width0, height0, req.max_tiles, req.tile_px)
    g = Grid("wgs84", w, n, (e - w) / width, (n - s) / height, width, height)
    img = _render(req, g, provider)
    zone = zone_for_bbox(req.bbox)
    if req.grid:
        img = gridmod.draw_sk42_grid(img, g, zone, step_m=1000)
    info = kmzmod.write_kmz(img, g, out_path, req.name, tile_px=req.tile_px, max_tiles=req.max_tiles)
    info.update(zone=zone, detail_m=round(req.detail_m / k, 2))
    return info


def build_pdf(req: MapRequest, provider: LayerProvider, out_path: str) -> dict:
    zone = zone_for_bbox(req.bbox)
    w, s, e, n = req.bbox
    # Охват области в СК-42 (по плотной выборке границ — края в ГК не прямые)
    t = np.linspace(0, 1, 40)
    lons = np.concatenate([w + (e - w) * t, np.full(40, e), w + (e - w) * t, np.full(40, w)])
    lats = np.concatenate([np.full(40, n), s + (n - s) * t, np.full(40, s), s + (n - s) * t])
    xs, ys = sk42.wgs84_to_sk42_gk(lats, lons, zone)
    x_min, x_max, y_min, y_max = xs.min(), xs.max(), ys.min(), ys.max()

    # Ориентация листа: та, при которой листов меньше
    m_per_mm = req.scale / 1000.0
    best = None
    for landscape in (False, True):
        aw, ah = pdfmod.map_area_mm(req.paper, landscape)
        cols = math.ceil((y_max - y_min) / (aw * m_per_mm))
        rows = math.ceil((x_max - x_min) / (ah * m_per_mm))
        if best is None or cols * rows < best[0]:
            best = (cols * rows, landscape, cols, rows, aw, ah)
    total, landscape, cols, rows, aw, ah = best
    if total > MAX_PDF_SHEETS:
        raise MapError(f"Нужно {total} листов — уменьшите область, возьмите мельче масштаб или A3")

    px_m = req.scale * 0.0254 / req.dpi                     # метров на местности в одном пикселе печати
    wpx, hpx = round(aw / 25.4 * req.dpi), round(ah / 25.4 * req.dpi)
    step = gridmod.default_step(req.scale)
    doc = pdfmod.MapPdf(out_path, req.paper, landscape)
    # Центруем набор листов на области
    span_y, span_x = cols * aw * m_per_mm, rows * ah * m_per_mm
    left = (y_min + y_max) / 2 - span_y / 2
    top = (x_min + x_max) / 2 + span_x / 2
    for r in range(rows):
        for c in range(cols):
            g = Grid(zone, left + c * aw * m_per_mm, top - r * ah * m_per_mm, px_m, px_m, wpx, hpx)
            img = _render(req, g, provider)
            if req.grid:
                img = gridmod.draw_sk42_grid(img, g, zone, step_m=step)
            gx0, gy0, gx1, gy1 = g.bounds
            la, lo = sk42.sk42_gk_to_wgs84(np.array([gy1, gy0]), np.array([gx0, gx1]), zone)
            corners = f"СЗ {la[0]:.5f}, {lo[0]:.5f}   ЮВ {la[1]:.5f}, {lo[1]:.5f}"
            doc.add_page(img, title=req.name, scale=req.scale, zone=zone, step_m=step,
                         sheet=f"{r + 1}-{c + 1} из {rows}×{cols}", corners_wgs84=corners,
                         sources=req.sources_note or "спутниковые снимки, карты Генштаба")
    doc.save()
    return {"sheets": total, "paper": req.paper, "landscape": landscape, "zone": zone, "px_m": round(px_m, 2)}


def build(req: MapRequest, provider: LayerProvider, out_path: str) -> dict:
    return build_kmz(req, provider, out_path) if req.fmt == "kmz" else build_pdf(req, provider, out_path)
