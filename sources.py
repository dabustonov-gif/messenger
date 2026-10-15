"""
Поставщик слоёв на GDAL: перепроецирует локальные GeoTIFF в любую целевую сетку (WGS-84 или ГК СК-42).

Структура данных на сервере (по умолчанию $MSG_DATA_DIR/layers):
    layers/satellite/*.tif   — спутниковые снимки (обычно EPSG:3857 после предзагрузки)
    layers/genshtab/*.tif    — листы Генштаба (обычно СК-42, ГК; см. gis/preload.py --import)

Файлы одного слоя могут быть в РАЗНЫХ системах координат: gdal.Warp принимает список
и сам пересчитывает каждый. Палитровые сканы (1 канал + таблица цветов) разворачиваются в RGBA.
Пересчёт датума СК-42 задаётся явным +towgs84 (gis.sk42.PROJ_SK42_TOWGS84), чтобы GDAL
и наш собственный расчёт сетки давали одинаковый результат.
"""
from __future__ import annotations

import glob
import os
import threading

import numpy as np

from . import sk42
from .raster import Grid

try:
    from osgeo import gdal, osr
    gdal.UseExceptions()
    gdal.SetConfigOption("GDAL_NUM_THREADS", "ALL_CPUS")
    gdal.SetCacheMax(512 * 1024 * 1024)
    HAVE_GDAL = True
except ImportError:          # сервер работает и без ГИС-модуля (мессенджер), карты будут недоступны
    gdal = osr = None
    HAVE_GDAL = False

LAYERS = ("satellite", "genshtab")
_vrt_lock = threading.Lock()


def layer_files(root: str, layer: str) -> list[str]:
    files = []
    for ext in ("*.tif", "*.tiff", "*.vrt", "*.jp2"):
        files += glob.glob(os.path.join(root, layer, ext))
    return sorted(files)


def _rgba_source(path: str) -> str:
    """Возвращает путь к источнику с 3–4 каналами: палитру/градации серого заворачиваем в VRT."""
    ds = gdal.Open(path)
    b1 = ds.GetRasterBand(1)
    if ds.RasterCount >= 3:
        return path
    vrt = f"/vsimem/rgba_{abs(hash(path))}.vrt"
    with _vrt_lock:
        if gdal.VSIStatL(vrt) is None:
            if b1.GetColorTable() is not None:
                gdal.Translate(vrt, ds, format="VRT", rgbExpand="rgba")
            else:
                gdal.Translate(vrt, ds, format="VRT", bandList=[1, 1, 1])
    return vrt


def _dst_srs(grid: Grid) -> str:
    return sk42.PROJ4_WGS84 if grid.is_wgs84 else sk42.proj4_sk42_gk(int(grid.crs))


def warp_to_grid(files: list[str], grid: Grid, dst_srs: str | None = None, resample: str = "bilinear") -> np.ndarray | None:
    """Перепроецирует набор файлов в сетку grid; возвращает RGBA (H, W, 4) uint8 или None, если пусто."""
    if not files:
        return None
    srcs = [_rgba_source(f) for f in files]
    ds = gdal.Warp("", srcs, format="MEM", dstSRS=dst_srs or _dst_srs(grid), outputBounds=grid.bounds,
                   width=grid.width, height=grid.height, resampleAlg=resample, dstAlpha=True,
                   multithread=True, warpOptions=["NUM_THREADS=ALL_CPUS", "SKIP_NOSOURCE=YES"])
    arr = ds.ReadAsArray()                                   # (bands, H, W)
    rgb, alpha = arr[:3], arr[-1]
    if not alpha.any():
        return None                                          # область вне покрытия слоя
    return np.dstack([rgb[0], rgb[1], rgb[2], alpha]).astype(np.uint8)


def gdal_provider(root: str):
    """Фабрика поставщика слоёв для gis.engine."""
    if not HAVE_GDAL:
        raise RuntimeError("GDAL не установлен: apt install python3-gdal")

    def provide(layer: str, grid: Grid):
        return warp_to_grid(layer_files(root, layer), grid,
                            resample="cubic" if layer == "satellite" else "bilinear")
    return provide


# ---------------------------------------------------------------------------------------------
# Тайлы для предпросмотра в приложении (XYZ, EPSG:3857, 256×256)
# ---------------------------------------------------------------------------------------------
WEB_MERC = "+proj=merc +a=6378137 +b=6378137 +lat_ts=0 +lon_0=0 +x_0=0 +y_0=0 +k=1 +units=m +nadgrids=@null +no_defs"
ORIGIN = 20037508.342789244


def render_tile(root: str, layer: str, z: int, x: int, y: int) -> bytes | None:
    """PNG-тайл слоя (или «mix» — Генштаб 50 % поверх спутника). None — если в тайле нет данных."""
    from io import BytesIO
    from PIL import Image
    from .raster import blend

    size = 2 * ORIGIN / (2 ** z)
    g = Grid("webmerc", -ORIGIN + x * size, ORIGIN - y * size, size / 256, size / 256, 256, 256)
    get = lambda name: warp_to_grid(layer_files(root, name), g, dst_srs=WEB_MERC)  # noqa: E731
    if layer == "mix":
        sat, gen = get("satellite"), get("genshtab")
        if sat is None and gen is None:
            return None
        rgb = blend(sat, gen, 0.5 if sat is not None else 1.0, (256, 256))
        img = Image.fromarray(rgb)
    else:
        arr = get(layer)
        if arr is None:
            return None
        img = Image.fromarray(arr, "RGBA")
    buf = BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def availability(root: str) -> dict:
    return {layer: len(layer_files(root, layer)) for layer in LAYERS} | {"gdal": HAVE_GDAL}
