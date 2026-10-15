"""
Подготовка картографических данных ЗАРАНЕЕ, пока есть интернет (на месте он не нужен).

Режимы:

1) Скачать слой из XYZ-источника (sources.json) в GeoTIFF для области:
     python -m gis.preload fetch --source eox_s2cloudless --layer satellite \\
            --bbox 37.40,55.60,37.90,55.90 --zoom 15

   Перед загрузкой показывается число тайлов и объём; без --yes качается только до 2000 тайлов.
   Загрузка идёт через драйвер GDAL WMS/TMS в 2 потока с кешем на диске: повторный запуск докачивает.
   ВАЖНО: соблюдайте условия источника. Многие сервисы (Google, Bing, Яндекс, Esri) запрещают
   массовую выгрузку тайлов; в sources.json указаны лицензии источников по умолчанию.

2) Импортировать готовые растры (сканы листов Генштаба, снимки с дрона и т.п.):
     python -m gis.preload import --layer genshtab листы/*.tif
     python -m gis.preload import --layer genshtab --sk42-zone 7 n-37-004.tif   # если у скана
                                                                                 # «чужая»/пустая СК
   Файл переупаковывается в Cloud-Optimized GeoTIFF (быстрое чтение фрагментов) с обзорами.
   --sk42-zone N принудительно назначает СК-42 ГК зоны N с корректным +towgs84 — частая беда
   скачанных листов: проекция указана как Pulkovo без параметров датума, и карта «уезжает» на 100+ м.

3) Показать, что есть на сервере:
     python -m gis.preload list
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

from . import sk42
from .sources import HAVE_GDAL, LAYERS, gdal, layer_files

DATA_DIR = os.environ.get("MSG_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
LAYER_ROOT = os.path.join(DATA_DIR, "layers")
SOURCES = os.path.join(os.path.dirname(__file__), "sources.json")
ORIGIN = 20037508.342789244


def _tile_range(bbox, z):
    w, s, e, n = bbox

    def tx(lon):
        return int((lon + 180) / 360 * 2 ** z)

    def ty(lat):
        r = math.radians(lat)
        return int((1 - math.asinh(math.tan(r)) / math.pi) / 2 * 2 ** z)
    return tx(w), ty(n), tx(e), ty(s)


def _wms_xml(url: str, z: int, cache: str, user_agent: str) -> str:
    url = url.replace("{z}", "${z}").replace("{x}", "${x}").replace("{y}", "${y}")
    return f"""<GDAL_WMS>
  <Service name="TMS"><ServerUrl>{url.replace('&', '&amp;')}</ServerUrl></Service>
  <DataWindow>
    <UpperLeftX>{-ORIGIN}</UpperLeftX><UpperLeftY>{ORIGIN}</UpperLeftY>
    <LowerRightX>{ORIGIN}</LowerRightX><LowerRightY>{-ORIGIN}</LowerRightY>
    <TileLevel>{z}</TileLevel><TileCountX>1</TileCountX><TileCountY>1</TileCountY><YOrigin>top</YOrigin>
  </DataWindow>
  <Projection>EPSG:3857</Projection>
  <BlockSizeX>256</BlockSizeX><BlockSizeY>256</BlockSizeY><BandsCount>3</BandsCount>
  <MaxConnections>2</MaxConnections>
  <ZeroBlockHttpCodes>204,404</ZeroBlockHttpCodes>
  <UserAgent>{user_agent}</UserAgent>
  <Cache><Path>{cache}</Path></Cache>
</GDAL_WMS>"""


def cmd_fetch(a):
    sources = json.load(open(SOURCES, encoding="utf-8"))
    src = sources.get(a.layer, {}).get(a.source)
    if not src or not src.get("url"):
        sys.exit(f"Источник {a.layer}/{a.source} не задан в {SOURCES} (заполните поле url)")
    if a.zoom > src.get("max_zoom", 19):
        sys.exit(f"У источника максимальный уровень {src['max_zoom']}")
    bbox = tuple(float(v) for v in a.bbox.split(","))
    x0, y0, x1, y1 = _tile_range(bbox, a.zoom)
    count = (x1 - x0 + 1) * (y1 - y0 + 1)
    print(f"Источник: {src.get('title', a.source)}\nЛицензия: {src.get('license', 'не указана')}")
    print(f"Уровень {a.zoom}: {count} тайлов (~{count * 25 / 1024:.0f} МБ)")
    if count > 2000 and not a.yes:
        sys.exit("Больше 2000 тайлов — подтвердите флагом --yes или уменьшите область/уровень")

    cache = os.path.join(DATA_DIR, "tile_cache", a.source)
    os.makedirs(cache, exist_ok=True)
    xml = _wms_xml(src["url"], a.zoom, cache, src.get("user_agent", "secure-messenger-preload/1.0"))
    out_dir = os.path.join(LAYER_ROOT, a.layer)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, a.out or f"{a.source}_z{a.zoom}_{bbox[0]:.3f}_{bbox[1]:.3f}.tif")
    gdal.Translate(out, xml, format="COG", projWin=[bbox[0], bbox[3], bbox[2], bbox[1]], projWinSRS="EPSG:4326",
                   creationOptions=["COMPRESS=JPEG", "QUALITY=85", "BIGTIFF=IF_SAFER", "OVERVIEWS=AUTO"],
                   callback=gdal.TermProgress_nocb)
    print(f"Готово: {out}")


def cmd_import(a):
    out_dir = os.path.join(LAYER_ROOT, a.layer)
    os.makedirs(out_dir, exist_ok=True)
    for path in a.files:
        out = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + ".tif")
        opts = dict(format="COG", creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=2", "OVERVIEWS=AUTO"])
        if a.sk42_zone:
            opts["outputSRS"] = sk42.proj4_sk42_gk(a.sk42_zone)
        src = gdal.Open(path)
        if src.GetRasterBand(1).GetColorTable() is not None and src.RasterCount == 1:
            opts["rgbExpand"] = "rgba"      # COG с палитрой + JPEG несовместимы; разворачиваем
        if not src.GetProjection() and not a.sk42_zone:
            print(f"! {path}: нет системы координат — укажите --sk42-zone"); continue
        gdal.Translate(out, src, callback=gdal.TermProgress_nocb, **opts)
        print(f"Импортирован: {out}")


def cmd_list(_a):
    for layer in LAYERS:
        files = layer_files(LAYER_ROOT, layer)
        print(f"{layer}: {len(files)} файл(ов)")
        for f in files:
            ds = gdal.Open(f)
            gt = ds.GetGeoTransform()
            print(f"   {os.path.basename(f)}  {ds.RasterXSize}×{ds.RasterYSize}  пиксель {abs(gt[1]):.2f}")


def main(argv=None):
    if not HAVE_GDAL:
        sys.exit("Нужен GDAL: sudo apt install gdal-bin python3-gdal")
    p = argparse.ArgumentParser(description="Предзагрузка карт для автономной работы")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="скачать слой из XYZ-источника")
    f.add_argument("--source", required=True)
    f.add_argument("--layer", choices=LAYERS, required=True)
    f.add_argument("--bbox", required=True, help="запад,юг,восток,север в градусах WGS-84")
    f.add_argument("--zoom", type=int, required=True)
    f.add_argument("--out")
    f.add_argument("--yes", action="store_true")
    i = sub.add_parser("import", help="импортировать GeoTIFF/JP2/VRT")
    i.add_argument("--layer", choices=LAYERS, required=True)
    i.add_argument("--sk42-zone", type=int)
    i.add_argument("files", nargs="+")
    sub.add_parser("list", help="показать загруженные слои")
    a = p.parse_args(argv)
    {"fetch": cmd_fetch, "import": cmd_import, "list": cmd_list}[a.cmd](a)


if __name__ == "__main__":
    main()
