"""
PDF для полевой печати в точном масштабе.

Карта строится в проекции Гаусса–Крюгера СК-42 (сетка прямая, как на листах Генштаба), растр
рассчитан под заданное разрешение печати, и вставляется в PDF ровно в миллиметрах:
  1 мм на бумаге = scale / 1000 метров на местности.
Печатать нужно с масштабом 100 % («Фактический размер»), без «Подогнать по странице».
Если область не помещается на один лист — она автоматически делится на несколько листов.
"""
from __future__ import annotations

import datetime as dt
import io
import os

from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

PAPER_MM = {"A4": (210, 297), "A3": (297, 420)}
MARGIN, HEADER, FOOTER = 10, 12, 20      # мм


def _register_font():
    for name, path in (("Sans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                       ("SansB", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames() and os.path.exists(path):
            pdfmetrics.registerFont(TTFont(name, path))
    return ("Sans", "SansB") if "Sans" in pdfmetrics.getRegisteredFontNames() else ("Helvetica", "Helvetica-Bold")


def map_area_mm(paper: str, landscape: bool) -> tuple[float, float]:
    w, h = PAPER_MM[paper]
    if landscape:
        w, h = h, w
    return w - 2 * MARGIN, h - 2 * MARGIN - HEADER - FOOTER


def nice_bar_m(scale: int, max_mm: float) -> int:
    """Длина масштабной линейки: «круглое» значение, помещающееся в max_mm."""
    best = 100
    for m in (100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000, 25000, 50000):
        if m / scale * 1000 > max_mm:
            break
        best = m
    return best


class MapPdf:
    """Многостраничный PDF; страницы добавляются по одной (каждая — свой лист карты)."""

    def __init__(self, path: str, paper: str, landscape: bool):
        self.path, self.paper, self.landscape = path, paper, landscape
        w, h = PAPER_MM[paper]
        self.page_w, self.page_h = ((h, w) if landscape else (w, h))
        self.c = canvas.Canvas(path, pagesize=(self.page_w * mm, self.page_h * mm))
        self.c.setTitle("Карта СК-42")
        self.font, self.font_b = _register_font()

    def add_page(self, img, *, title: str, scale: int, zone: int, step_m: int, sheet: str,
                 corners_wgs84: str, sources: str):
        c = self.c
        aw, ah = map_area_mm(self.paper, self.landscape)
        top = self.page_h - MARGIN
        # --- заголовок
        c.setFont(self.font_b, 11)
        c.drawString(MARGIN * mm, (top - 6) * mm, title[:80])
        c.setFont(self.font, 8)
        c.drawRightString((self.page_w - MARGIN) * mm, (top - 6) * mm, f"Лист {sheet}")
        c.drawString(MARGIN * mm, (top - 10.5) * mm,
                     f"Масштаб 1:{scale:,}".replace(",", " ") + f"   ·   СК-42, Гаусс–Крюгер, зона {zone}"
                     f"   ·   сетка {step_m // 1000} км")
        # --- карта: ровно aw × ah мм
        y_map = MARGIN + FOOTER
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=88)
        c.drawImage(ImageReader(io.BytesIO(buf.getvalue())), MARGIN * mm, y_map * mm, width=aw * mm, height=ah * mm)
        c.setLineWidth(0.6)
        c.rect(MARGIN * mm, y_map * mm, aw * mm, ah * mm)
        # --- масштабная линейка
        bar_m = nice_bar_m(scale, 60)
        bar_mm = bar_m / scale * 1000
        x0, y0 = MARGIN, MARGIN + 11
        for i in range(4):
            seg = bar_mm / 4
            c.setFillGray(0 if i % 2 == 0 else 1)
            c.rect((x0 + i * seg) * mm, y0 * mm, seg * mm, 1.6 * mm, fill=1, stroke=1)
        c.setFillGray(0)
        c.setFont(self.font, 7)
        c.drawString(x0 * mm, (y0 - 3.5) * mm, "0")
        c.drawRightString((x0 + bar_mm) * mm, (y0 - 3.5) * mm,
                          f"{bar_m / 1000:g} км" if bar_m >= 1000 else f"{bar_m} м")
        # --- служебная информация
        c.drawString((x0 + 70) * mm, (y0 + 0.2) * mm, f"Углы (WGS-84): {corners_wgs84}")
        c.drawString((x0 + 70) * mm, (y0 - 3.5) * mm, f"Источники: {sources}")
        c.drawString(MARGIN * mm, (MARGIN + 1) * mm,
                     f"Печатать в масштабе 100 % (без «подогнать по странице»). Создано {dt.date.today():%d.%m.%Y}. "
                     "Точность пересчёта датума ≈ 3 м.")
        c.showPage()

    def save(self):
        self.c.save()
