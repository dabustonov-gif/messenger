"""
Нарезка растра WGS-84 в Garmin Custom Maps (.kmz).

Требования Garmin Custom Maps (навигаторы GPSMAP/eTrex/Montana/Oregon, часть часов Fenix/Epix):
  * каждый тайл — JPEG (baseline, НЕ progressive), не больше 1 мегапикселя (обычно 1024×1024);
  * на устройстве одновременно не более 100 тайлов (суммарно по ВСЕМ kmz в Garmin/CustomMaps!);
  * изображения не повёрнуты: GroundOverlay с LatLonBox в WGS-84, без <rotation>;
  * drawOrder 1–50 — растр под векторной картой, >50 — поверх.
Поэтому при превышении бюджета изображение уменьшается целиком, а не обрезается.
"""
from __future__ import annotations

import io
import math
import zipfile
from xml.sax.saxutils import escape

from PIL import Image

from .raster import Grid


def fit_to_tile_budget(width: int, height: int, max_tiles: int, tile_px: int) -> tuple[int, int, float]:
    """Подбирает итоговый размер растра так, чтобы ceil(W/t)·ceil(H/t) ≤ max_tiles. Возвращает (W, H, k)."""
    k = 1.0
    while True:
        w, h = max(1, int(width * k)), max(1, int(height * k))
        if math.ceil(w / tile_px) * math.ceil(h / tile_px) <= max_tiles:
            return w, h, k
        k *= 0.97


def write_kmz(img: Image.Image, grid: Grid, path: str, name: str, tile_px: int = 1024,
              max_tiles: int = 100, draw_order: int = 50, quality: int = 85) -> dict:
    assert grid.is_wgs84, "Garmin принимает только неповёрнутые оверлеи в WGS-84"
    assert tile_px * tile_px <= 1_048_576, "тайл больше 1 мегапикселя Garmin не покажет"
    w, h = img.size
    cols, rows = math.ceil(w / tile_px), math.ceil(h / tile_px)
    if cols * rows > max_tiles:
        raise ValueError(f"{cols * rows} тайлов > лимита {max_tiles}; уменьшите растр через fit_to_tile_budget")

    overlays = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        files = {}
        for r in range(rows):
            for c in range(cols):
                box = (c * tile_px, r * tile_px, min(w, (c + 1) * tile_px), min(h, (r + 1) * tile_px))
                buf = io.BytesIO()
                img.crop(box).convert("RGB").save(buf, "JPEG", quality=quality, progressive=False, optimize=True)
                fname = f"files/t_{r:02d}_{c:02d}.jpg"
                files[fname] = buf.getvalue()
                west, north = grid.to_geo(box[0], box[1])
                east, south = grid.to_geo(box[2], box[3])
                overlays.append(f"""  <GroundOverlay>
    <name>{escape(name)} {r + 1}-{c + 1}</name>
    <drawOrder>{draw_order}</drawOrder>
    <Icon><href>{fname}</href></Icon>
    <LatLonBox>
      <north>{float(north):.8f}</north><south>{float(south):.8f}</south>
      <east>{float(east):.8f}</east><west>{float(west):.8f}</west>
    </LatLonBox>
  </GroundOverlay>""")
        kml = ('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2">\n'
               f'<Folder>\n  <name>{escape(name)}</name>\n' + "\n".join(overlays) + "\n</Folder>\n</kml>\n")
        z.writestr("doc.kml", kml)            # doc.kml должен идти первым
        for fname, data in files.items():
            z.writestr(fname, data, compress_type=zipfile.ZIP_STORED)   # JPEG уже сжат
    return {"tiles": cols * rows, "width": w, "height": h}
