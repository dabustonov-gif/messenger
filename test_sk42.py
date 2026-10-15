"""
Проверка пересчёта СК-42 двумя независимыми путями из ГОСТ Р 51794:
  1) ряды Крюгера (gis.sk42) против явных полиномиальных формул Гаусса–Крюгера из ГОСТ;
  2) 7-параметрический Гельмерт против формул Молоденского для ΔB, ΔL из ГОСТ
     (это ловит ошибку знаков поворотов, которая даёт сдвиг ~25 м).
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from gis import sk42  # noqa: E402

RHO = 206264.806


def gost_gk(b_deg, l_deg):
    """Полиномиальные формулы Гаусса–Крюгера для эллипсоида Красовского (ГОСТ Р 51794, прил.)."""
    n = int((6 + l_deg) // 6)
    l = (l_deg - (3 + 6 * (n - 1))) / 57.29577951
    b = math.radians(b_deg)
    s2, s4, s6 = math.sin(b) ** 2, math.sin(b) ** 4, math.sin(b) ** 6
    l2 = l * l
    x = 6367558.4968 * b - math.sin(2 * b) * (
        16002.8900 + 66.9607 * s2 + 0.3515 * s4
        - l2 * (1594561.25 + 5336.535 * s2 + 26.790 * s4 + 0.149 * s6
                + l2 * (672483.4 - 811219.9 * s2 + 5420.0 * s4 - 10.6 * s6
                        + l2 * (278194 - 830174 * s2 + 572434 * s4 - 16010 * s6
                                + l2 * (109500 - 574700 * s2 + 863700 * s4 - 398600 * s6)))))
    y = (5 + 10 * n) * 1e5 + l * math.cos(b) * (
        6378245 + 21346.1415 * s2 + 107.1590 * s4 + 0.5977 * s6
        + l2 * (1070204.16 - 2136826.66 * s2 + 17.98 * s4 - 11.99 * s6
                + l2 * (270806 - 1523417 * s2 + 1327645 * s4 - 21701 * s6
                        + l2 * (79690 - 866190 * s2 + 1730360 * s4 - 945460 * s6))))
    return x, y


def gost_molodensky(b_deg, l_deg, h=0.0):
    """ΔB, ΔL (угл. сек.) СК-42 -> WGS-84 по формулам ГОСТ Р 51794 (тот же набор параметров)."""
    p = sk42.HELMERT_SK42_TO_WGS84
    a1, f1, a2, f2 = sk42.KRASS_A, sk42.KRASS_F, sk42.WGS84_A, sk42.WGS84_F
    e2_1, e2_2 = f1 * (2 - f1), f2 * (2 - f2)
    a, e2 = (a1 + a2) / 2, (e2_1 + e2_2) / 2
    da, de2 = a2 - a1, e2_2 - e2_1
    b, l = math.radians(b_deg), math.radians(l_deg)
    sb, cb, sl, cl = math.sin(b), math.cos(b), math.sin(l), math.cos(l)
    n = a / math.sqrt(1 - e2 * sb * sb)
    m = a * (1 - e2) / (1 - e2 * sb * sb) ** 1.5
    dx, dy, dz, wx, wy, wz, ms = p["dx"], p["dy"], p["dz"], p["wx"], p["wy"], p["wz"], p["m"]
    d_b = (RHO / (m + h) * (n / a * e2 * sb * cb * da + (n * n / (a * a) + 1) * n * sb * cb * de2 / 2
                            - (dx * cl + dy * sl) * sb + dz * cb)
           - wx * sl * (1 + e2 * math.cos(2 * b)) + wy * cl * (1 + e2 * math.cos(2 * b))
           - RHO * ms * e2 * sb * cb)
    d_l = RHO / ((n + h) * cb) * (-dx * sl + dy * cl) + math.tan(b) * (1 - e2) * (wx * cl + wy * sl) - wz
    return d_b, d_l


POINTS = [(55.7558, 37.6173), (59.9386, 30.3141), (48.7080, 44.5133), (43.1155, 131.8855),
          (61.7849, 34.3469), (52.0, 39.95), (45.0, 33.1)]


def test_gk_matches_gost_polynomials():
    for b, l in POINTS:
        x1, y1 = sk42.gk_forward(b, l)
        x2, y2 = gost_gk(b, l)
        assert abs(x1 - x2) < 0.1 and abs(y1 - y2) < 0.1, (b, l, x1 - x2, y1 - y2)


def test_gk_round_trip():
    lat = np.random.default_rng(1).uniform(40, 70, 500)
    zone = 7
    lon = np.random.default_rng(2).uniform(36.001, 41.999, 500)
    x, y = sk42.gk_forward(lat, lon, zone)
    b, l = sk42.gk_inverse(x, y, zone)
    assert np.max(np.abs(b - lat)) < 1e-9 and np.max(np.abs(l - lon)) < 1e-9


def test_helmert_matches_gost_molodensky():
    for b, l in POINTS:
        lat, lon, _ = sk42.sk42_geo_to_wgs84(b, l)
        d_b, d_l = gost_molodensky(b, l)
        err_b = ((lat - b) * 3600 - d_b) * 30.9                  # угл. сек -> метры
        err_l = ((lon - l) * 3600 - d_l) * 30.9 * math.cos(math.radians(b))
        assert abs(err_b) < 1.0 and abs(err_l) < 1.0, (b, l, err_b, err_l)


def test_datum_shift_magnitude_and_round_trip():
    # В европейской части РФ сдвиг СК-42 -> WGS-84 порядка 100–150 м
    lat, lon, _ = sk42.sk42_geo_to_wgs84(55.7558, 37.6173)
    shift = math.hypot((lat - 55.7558) * 111_320, (lon - 37.6173) * 111_320 * math.cos(math.radians(55.75)))
    assert 80 < shift < 200, shift
    x, y = sk42.wgs84_to_sk42_gk(55.7558, 37.6173)
    lat2, lon2 = sk42.sk42_gk_to_wgs84(x, y)
    assert abs(lat2 - 55.7558) < 1e-8 and abs(lon2 - 37.6173) < 1e-8
    assert int(y // 1_000_000) == 7      # Москва — 7-я зона


def test_proj_string_has_position_vector_signs():
    s = sk42.proj4_sk42_gk(7)
    assert "+lon_0=39" in s and "+x_0=7500000" in s and "0,0.35,0.79,-0.22" in s
