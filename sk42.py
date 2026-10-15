"""
Пересчёт координат СК-42 (Пулково-1942, эллипсоид Красовского, проекция Гаусса–Крюгера) <-> WGS-84.

Источник формул и параметров — ГОСТ Р 51794-2008, с переходом ПЗ-90.02 -> WGS-84 по тому же ГОСТу.
Итоговые параметры СК-42 -> WGS-84 (совпадают с EPSG:5044, точность ~3 м на территории РФ):

    ΔX = +23.57 м, ΔY = −140.95 м, ΔZ = −79.80 м
    ωx = 0″, ωy = −0.35″, ωz = −0.79″          (вращение СИСТЕМЫ координат, как в ГОСТ)
    m  = −0.22·10⁻⁶

ВНИМАНИЕ про знаки. В ГОСТ (и EPSG:5044) используется «Coordinate Frame rotation».
PROJ/GDAL в параметре +towgs84 ожидает «Position Vector» — у поворотов противоположный знак:
    +towgs84=23.57,-140.95,-79.8,0,0.35,0.79,-0.22
Константа PROJ_SK42_TOWGS84 ниже уже приведена к соглашению PROJ; тест tests/test_sk42.py
проверяет, что наша реализация совпадает с формулами Молоденского из ГОСТ.

Соглашения СК-42 / Гаусса–Крюгера:
    * зона 6°, номер зоны n = floor(L/6) + 1, осевой меридиан L0 = 6n − 3;
    * X — на север (абсцисса), Y — на восток (ордината);
    * к Y прибавляется n·10⁶ + 500 000 м («номер зоны в старших разрядах»);
    * масштаб на осевом меридиане k0 = 1.

Все функции векторизованы (принимают скаляры или numpy-массивы).
"""
from __future__ import annotations

import math

import numpy as np

# ---------------------------------------------------------------------------------------------
# Эллипсоиды
# ---------------------------------------------------------------------------------------------
KRASS_A = 6378245.0
KRASS_F = 1.0 / 298.3
WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563

ARCSEC = math.pi / (180.0 * 3600.0)

# Параметры ГОСТ Р 51794 (СК-42 -> WGS-84), повороты — в соглашении ГОСТ (Coordinate Frame)
HELMERT_SK42_TO_WGS84 = dict(dx=23.57, dy=-140.95, dz=-79.80, wx=0.0, wy=-0.35, wz=-0.79, m=-0.22e-6)

# Тот же набор для PROJ/GDAL (Position Vector: знаки поворотов инвертированы)
PROJ_SK42_TOWGS84 = "23.57,-140.95,-79.8,0,0.35,0.79,-0.22"


def proj4_sk42_gk(zone: int) -> str:
    """Строка PROJ для зоны Гаусса–Крюгера СК-42 (используется при вызовах GDAL)."""
    return (f"+proj=tmerc +lat_0=0 +lon_0={6 * zone - 3} +k=1 +x_0={zone * 1_000_000 + 500_000} +y_0=0 "
            f"+ellps=krass +towgs84={PROJ_SK42_TOWGS84} +units=m +no_defs")


PROJ4_WGS84 = "+proj=longlat +datum=WGS84 +no_defs"


# ---------------------------------------------------------------------------------------------
# Геодезические <-> геоцентрические прямоугольные координаты
# ---------------------------------------------------------------------------------------------
def geodetic_to_ecef(lat_deg, lon_deg, h, a, f):
    """B, L (градусы), H (м) -> X, Y, Z (м)."""
    lat, lon = np.radians(lat_deg), np.radians(lon_deg)
    e2 = f * (2 - f)
    n = a / np.sqrt(1 - e2 * np.sin(lat) ** 2)
    x = (n + h) * np.cos(lat) * np.cos(lon)
    y = (n + h) * np.cos(lat) * np.sin(lon)
    z = (n * (1 - e2) + h) * np.sin(lat)
    return x, y, z


def ecef_to_geodetic(x, y, z, a, f):
    """X, Y, Z -> B, L (градусы), H. Итерации по широте сходятся до 1e-12 рад за ≤ 6 шагов."""
    e2 = f * (2 - f)
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1 - e2))
    for _ in range(8):
        n = a / np.sqrt(1 - e2 * np.sin(lat) ** 2)
        h = p / np.cos(lat) - n
        lat = np.arctan2(z, p * (1 - e2 * n / (n + h)))
    n = a / np.sqrt(1 - e2 * np.sin(lat) ** 2)
    h = p / np.cos(lat) - n
    return np.degrees(lat), np.degrees(lon), h


# ---------------------------------------------------------------------------------------------
# 7-параметрическое преобразование Гельмерта по формуле ГОСТ Р 51794 (Coordinate Frame)
#
#   | X |               |  1    ωz  −ωy |   | X' |   | ΔX |
#   | Y | = (1 + m) ·   | −ωz   1    ωx | · | Y' | + | ΔY |
#   | Z |               |  ωy  −ωx   1  |   | Z' |   | ΔZ |
# ---------------------------------------------------------------------------------------------
def _rotation(p):
    wx, wy, wz = p["wx"] * ARCSEC, p["wy"] * ARCSEC, p["wz"] * ARCSEC
    return np.array([[1.0, wz, -wy], [-wz, 1.0, wx], [wy, -wx, 1.0]])


def helmert_forward(x, y, z, p=HELMERT_SK42_TO_WGS84):
    r = _rotation(p) * (1.0 + p["m"])
    v = np.stack([np.asarray(x, float), np.asarray(y, float), np.asarray(z, float)])
    out = np.tensordot(r, v, axes=1)
    return out[0] + p["dx"], out[1] + p["dy"], out[2] + p["dz"]


def helmert_inverse(x, y, z, p=HELMERT_SK42_TO_WGS84):
    """Точное обращение (решение линейной системы), а не приближённая смена знаков."""
    r_inv = np.linalg.inv(_rotation(p) * (1.0 + p["m"]))
    v = np.stack([np.asarray(x, float) - p["dx"], np.asarray(y, float) - p["dy"], np.asarray(z, float) - p["dz"]])
    out = np.tensordot(r_inv, v, axes=1)
    return out[0], out[1], out[2]


def sk42_geo_to_wgs84(lat, lon, h=0.0):
    """Геодезические СК-42 (Красовский) -> геодезические WGS-84."""
    return ecef_to_geodetic(*helmert_forward(*geodetic_to_ecef(lat, lon, h, KRASS_A, KRASS_F)), WGS84_A, WGS84_F)


def wgs84_to_sk42_geo(lat, lon, h=0.0):
    """Геодезические WGS-84 -> геодезические СК-42."""
    return ecef_to_geodetic(*helmert_inverse(*geodetic_to_ecef(lat, lon, h, WGS84_A, WGS84_F)), KRASS_A, KRASS_F)


# ---------------------------------------------------------------------------------------------
# Проекция Гаусса–Крюгера (ряды Крюгера до n⁶ — погрешность < 1 мм в пределах 6° зоны)
# ---------------------------------------------------------------------------------------------
def _kruger_coefficients(a, f):
    n = f / (2 - f)
    n2, n3, n4, n5, n6 = n ** 2, n ** 3, n ** 4, n ** 5, n ** 6
    big_a = a / (1 + n) * (1 + n2 / 4 + n4 / 64 + n6 / 256)
    alpha = [
        n / 2 - 2 * n2 / 3 + 5 * n3 / 16 + 41 * n4 / 180 - 127 * n5 / 288 + 7891 * n6 / 37800,
        13 * n2 / 48 - 3 * n3 / 5 + 557 * n4 / 1440 + 281 * n5 / 630 - 1983433 * n6 / 1935360,
        61 * n3 / 240 - 103 * n4 / 140 + 15061 * n5 / 26880 + 167603 * n6 / 181440,
        49561 * n4 / 161280 - 179 * n5 / 168 + 6601661 * n6 / 7257600,
        34729 * n5 / 80640 - 3418889 * n6 / 1995840,
        212378941 * n6 / 319334400,
    ]
    beta = [
        n / 2 - 2 * n2 / 3 + 37 * n3 / 96 - n4 / 360 - 81 * n5 / 512 + 96199 * n6 / 604800,
        n2 / 48 + n3 / 15 - 437 * n4 / 1440 + 46 * n5 / 105 - 1118711 * n6 / 3870720,
        17 * n3 / 480 - 37 * n4 / 840 - 209 * n5 / 4480 + 5569 * n6 / 90720,
        4397 * n4 / 161280 - 11 * n5 / 504 - 830251 * n6 / 7257600,
        4583 * n5 / 161280 - 108847 * n6 / 3991680,
        20648693 * n6 / 638668800,
    ]
    delta = [
        2 * n - 2 * n2 / 3 - 2 * n3 + 116 * n4 / 45 + 26 * n5 / 45 - 2854 * n6 / 675,
        7 * n2 / 3 - 8 * n3 / 5 - 227 * n4 / 45 + 2704 * n5 / 315 + 2323 * n6 / 945,
        56 * n3 / 15 - 136 * n4 / 35 - 1262 * n5 / 105 + 73814 * n6 / 2835,
        4279 * n4 / 630 - 332 * n5 / 35 - 399572 * n6 / 14175,
        4174 * n5 / 315 - 144838 * n6 / 6237,
        601676 * n6 / 22275,
    ]
    return n, big_a, alpha, beta, delta


_KR = _kruger_coefficients(KRASS_A, KRASS_F)


def zone_of(lon_deg) -> int:
    """Номер 6-градусной зоны СК-42 для долготы (восточное полушарие)."""
    return int(math.floor(float(np.min(lon_deg)) / 6.0)) + 1


def gk_forward(lat_deg, lon_deg, zone: int | None = None):
    """
    Геодезические СК-42 (B, L) -> плоские X (север), Y (восток, с номером зоны) в метрах.
    zone=None: зона определяется по долготе (по западному краю массива — для сетки в одной зоне).
    """
    n, big_a, alpha, _, _ = _KR
    zone = zone or zone_of(lon_deg)
    lat = np.radians(np.asarray(lat_deg, float))
    dl = np.radians(np.asarray(lon_deg, float) - (6 * zone - 3))
    e = 2 * math.sqrt(n) / (1 + n)                       # первый эксцентриситет
    t = np.sinh(np.arctanh(np.sin(lat)) - e * np.arctanh(e * np.sin(lat)))
    xi_p = np.arctan2(t, np.cos(dl))
    eta_p = np.arctanh(np.sin(dl) / np.sqrt(1 + t * t))
    xi, eta = xi_p.copy(), eta_p.copy()
    for j, aj in enumerate(alpha, start=1):
        xi = xi + aj * np.sin(2 * j * xi_p) * np.cosh(2 * j * eta_p)
        eta = eta + aj * np.cos(2 * j * xi_p) * np.sinh(2 * j * eta_p)
    x = big_a * xi
    y = big_a * eta + zone * 1_000_000 + 500_000
    return x, y


def gk_inverse(x, y, zone: int | None = None):
    """Плоские X, Y СК-42 (Y с номером зоны) -> геодезические B, L СК-42 в градусах."""
    _, big_a, _, beta, delta = _KR
    y = np.asarray(y, float)
    zone = zone or int(np.min(y) // 1_000_000)
    xi = np.asarray(x, float) / big_a
    eta = (y - zone * 1_000_000 - 500_000) / big_a
    xi_p, eta_p = xi.copy(), eta.copy()
    for j, bj in enumerate(beta, start=1):
        xi_p = xi_p - bj * np.sin(2 * j * xi) * np.cosh(2 * j * eta)
        eta_p = eta_p - bj * np.cos(2 * j * xi) * np.sinh(2 * j * eta)
    chi = np.arcsin(np.sin(xi_p) / np.cosh(eta_p))         # конформная широта
    lat = chi.copy()
    for j, dj in enumerate(delta, start=1):
        lat = lat + dj * np.sin(2 * j * chi)
    lon = np.degrees(np.arctan2(np.sinh(eta_p), np.cos(xi_p))) + (6 * zone - 3)
    return np.degrees(lat), lon


# ---------------------------------------------------------------------------------------------
# Сквозные функции, которыми пользуется картографический модуль
# ---------------------------------------------------------------------------------------------
def wgs84_to_sk42_gk(lat, lon, zone: int | None = None):
    """WGS-84 (B, L) -> прямоугольные СК-42 (X, Y). Зону берём по долготе СК-42."""
    b, l, _ = wgs84_to_sk42_geo(lat, lon)
    return gk_forward(b, l, zone or zone_of(l))


def sk42_gk_to_wgs84(x, y, zone: int | None = None):
    """Прямоугольные СК-42 (X, Y) -> WGS-84 (B, L) в градусах."""
    b, l = gk_inverse(x, y, zone)
    lat, lon, _ = sk42_geo_to_wgs84(b, l)
    return lat, lon


def format_sk42(x: float, y: float) -> str:
    """Запись в принятом виде: X=6 177 234  Y=7 412 551 (целые метры)."""
    fx = f"{int(round(x)):,}".replace(",", " ")
    fy = f"{int(round(y)):,}".replace(",", " ")
    return f"X={fx}  Y={fy}"
