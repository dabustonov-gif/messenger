"""
Километровая сетка СК-42 поверх растра.

* Если растр в проекции Гаусса–Крюгера той же зоны — линии прямые (как на бумажной карте Генштаба).
* Если растр в WGS-84 (для Garmin KMZ) — линии сетки СК-42 слегка изогнуты и повёрнуты
  (сближение меридианов + сдвиг датума); каждая линия строится как ломаная по 200 точкам,
  пересчитанным через gis.sk42, поэтому точность определяется только точностью пересчёта датума (~3 м).

Подписи — по традиции топокарт: две последние цифры километра («77» для X = 6177 км),
у первой линии — полное значение.
"""
from __future__ import annotations

import os

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import sk42
from .raster import Grid

FONT_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]


def font(size: int):
    for p in FONT_PATHS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size=size)


def default_step(scale: int | None) -> int:
    """Шаг сетки как на топокартах: 1 км до 1:50 000, 2 км для 1:100 000, 4 км для 1:200 000."""
    if not scale or scale <= 50_000:
        return 1000
    return 2000 if scale <= 100_000 else 4000


def _gk_extent(grid: Grid, zone: int):
    """Охват растра в координатах СК-42 (X север, Y восток) по плотной выборке его границ."""
    if not grid.is_wgs84:
        minx, miny, maxx, maxy = grid.bounds
        return miny, maxy, minx, maxx           # Xmin, Xmax, Ymin, Ymax
    t = np.linspace(0, 1, 50)
    w, s, e, n = grid.bounds
    lons = np.concatenate([w + (e - w) * t, np.full(50, e), w + (e - w) * t, np.full(50, w)])
    lats = np.concatenate([np.full(50, n), s + (n - s) * t, np.full(50, s), s + (n - s) * t])
    x, y = sk42.wgs84_to_sk42_gk(lats, lons, zone)
    return x.min(), x.max(), y.min(), y.max()


def _label(draw, xy, text, fnt, color, anchor):
    draw.text(xy, text, font=fnt, fill=color, anchor=anchor, stroke_width=max(2, fnt.size // 7), stroke_fill=(255, 255, 255))


def draw_sk42_grid(img: Image.Image, grid: Grid, zone: int, step_m: int = 1000,
                   color=(20, 40, 160), line_px: int | None = None, labels: bool = True) -> Image.Image:
    """Рисует сетку на копии изображения img (размер img должен совпадать с grid.width × grid.height)."""
    assert img.size == (grid.width, grid.height)
    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out)
    line_px = line_px or max(1, round(min(grid.width, grid.height) / 900))
    fnt = font(max(12, min(grid.width, grid.height) // 55))
    xmin, xmax, ymin, ymax = _gk_extent(grid, zone)
    first_x = True
    first_y = True

    def to_px(xs, ys):
        """Точки СК-42 (X, Y) -> пиксели растра."""
        if grid.is_wgs84:
            lat, lon = sk42.sk42_gk_to_wgs84(xs, ys, zone)
            return grid.to_pixel(lon, lat)
        return grid.to_pixel(ys, xs)            # в ГК-растре x_geo = Y (восток), y_geo = X (север)

    # Линии постоянного X (абсциссы, идут с запада на восток)
    samples = 2 if not grid.is_wgs84 else 200
    for x_line in np.arange(np.ceil(xmin / step_m) * step_m, xmax + 1e-6, step_m):
        ys = np.linspace(ymin, ymax, samples)
        c, r = to_px(np.full_like(ys, x_line), ys)
        d.line(list(zip(c, r)), fill=color, width=line_px)
        if labels:
            km = int(round(x_line / 1000))
            text = str(km) if first_x else f"{km % 100:02d}"
            first_x = False
            idx = int(np.argmin(np.abs(c - 4)))  # точка у левого края
            if fnt.size * 2.2 <= r[idx] < grid.height:    # не налезать на подписи верхнего края
                _label(d, (4, r[idx] - 2), text, fnt, color, "ls")

    # Линии постоянного Y (ординаты, идут с юга на север)
    for y_line in np.arange(np.ceil(ymin / step_m) * step_m, ymax + 1e-6, step_m):
        xs = np.linspace(xmin, xmax, samples)
        c, r = to_px(xs, np.full_like(xs, y_line))
        d.line(list(zip(c, r)), fill=color, width=line_px)
        if labels:
            km = int(round(y_line / 1000))
            text = str(km) if first_y else f"{km % 100:02d}"
            first_y = False
            idx = int(np.argmin(np.abs(r - 4)))  # точка у верхнего края
            if 0 <= c[idx] < grid.width - fnt.size * 1.5:
                _label(d, (c[idx] + 3, 4), text, fnt, color, "lt")
    return out
