"""
Описание растровой сетки (геопривязки) и работа с RGBA-массивами.

Сетка задаётся так же, как GeoTransform в GDAL (без поворота):
    x_geo = x0 + col * px          y_geo = y0 - row * py
где для crs="wgs84" x — долгота, y — широта (градусы),
а для crs=<номер зоны> x — восточная координата (ордината Y СК-42 c номером зоны),
y — северная (абсцисса X СК-42), в метрах.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Union

import numpy as np

Crs = Union[str, int]   # "wgs84" или номер зоны Гаусса–Крюгера СК-42


@dataclass(frozen=True)
class Grid:
    crs: Crs
    x0: float          # левый край
    y0: float          # верхний край
    px: float          # размер пикселя по X (в единицах crs)
    py: float          # размер пикселя по Y (положительный)
    width: int
    height: int

    @property
    def bounds(self):
        """(minX, minY, maxX, maxY) — порядок, который ожидает GDAL в outputBounds."""
        return self.x0, self.y0 - self.height * self.py, self.x0 + self.width * self.px, self.y0

    def to_pixel(self, x, y):
        return (np.asarray(x) - self.x0) / self.px, (self.y0 - np.asarray(y)) / self.py

    def to_geo(self, col, row):
        return self.x0 + np.asarray(col) * self.px, self.y0 - np.asarray(row) * self.py

    @property
    def is_wgs84(self) -> bool:
        return self.crs == "wgs84"


# Поставщик слоя: по имени слоя и целевой сетке возвращает RGBA uint8 (H, W, 4) или None, если данных нет.
# В боевом режиме — gis.sources.gdal_provider (GDAL warp), в тестах — синтетический.
LayerProvider = Callable[[str, Grid], Optional[np.ndarray]]


def blend(base: Optional[np.ndarray], overlay: Optional[np.ndarray], opacity: float, shape) -> np.ndarray:
    """
    Наложение слоя overlay на base с заданной прозрачностью (учитывается и альфа-канал overlay).
    Пустые области (альфа = 0) не затирают подложку. Результат — RGB uint8.
    """
    h, w = shape
    out = np.full((h, w, 3), 255.0) if base is None else base[..., :3].astype(np.float32)
    if base is not None:   # прозрачные места подложки — белые (как бумага)
        a = base[..., 3:4].astype(np.float32) / 255.0
        out = out * a + 255.0 * (1 - a)
    if overlay is not None:
        a = overlay[..., 3:4].astype(np.float32) / 255.0 * float(np.clip(opacity, 0, 1))
        out = out * (1 - a) + overlay[..., :3].astype(np.float32) * a
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)
