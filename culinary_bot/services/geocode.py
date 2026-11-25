"""
Адрес по точке на карте («обратное геокодирование»): координаты → «ул. …, Будва».

Используем бесплатный сервис OpenStreetMap Nominatim. Его правила:
не чаще 1 запроса в секунду и обязательно представиться (User-Agent).
У лавки заказов немного, так что лимит не страшен.

Адрес — только ПОДСКАЗКА: по Черногории OSM иногда знает адрес приблизительно.
Главное для доставки — сама точка (ссылка на карту). Поэтому любая ошибка
(нет интернета, сервис не ответил) просто даёт None, и заказ идёт дальше.
"""

import logging
import re

import aiohttp

from culinary_bot.config import settings

log = logging.getLogger(__name__)

URL = "https://nominatim.openstreetmap.org/reverse"
HEADERS = {"User-Agent": "culinary-bot (Telegram order bot for a home kitchen)"}


def format_address(data: dict) -> str | None:
    """
    Собрать короткий адрес из ответа Nominatim: «ул. Морская 5, Будва».

    Ответ содержит словарь address с частями: road, house_number, city/town/village.
    Полное display_name слишком длинное (с регионом, индексом и страной).
    """
    parts = data.get("address") or {}
    street = parts.get("road") or parts.get("pedestrian") or parts.get("neighbourhood")
    house = parts.get("house_number")
    place = parts.get("city") or parts.get("town") or parts.get("village") or parts.get("suburb")
    first = " ".join(x for x in (street, house) if x)
    result = ", ".join(x for x in (first, place) if x)
    return result or None


async def reverse(latitude: float, longitude: float) -> str | None:
    """Адрес по координатам или None, если не получилось."""
    if not settings.geocoding_enabled:
        return None
    params = {"lat": latitude, "lon": longitude, "format": "json", "accept-language": "ru", "zoom": 18}
    try:
        timeout = aiohttp.ClientTimeout(total=5)  # клиент не должен ждать дольше 5 секунд
        async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as http:
            async with http.get(URL, params=params) as response:
                if response.status != 200:
                    return None
                return format_address(await response.json())
    except Exception as error:  # сеть, таймаут, неожиданный ответ — всё не критично
        log.warning("Не удалось определить адрес по точке: %s", error)
        return None


# ---------------------------------------------------------------------------
# Ссылки Google Maps → координаты
#
# Клиент может не отправлять точку, а вставить ссылку из приложения Карт
# («Поделиться» → «Копировать ссылку»). Координаты в длинных ссылках записаны
# прямо в тексте — достаём их регулярными выражениями (шаблонами поиска):
#   https://www.google.com/maps/@42.28,18.84,17z
#   https://maps.google.com/?q=42.28,18.84
#   https://www.google.com/maps/place/.../data=!3d42.28!4d18.84
# Короткие ссылки (maps.app.goo.gl/…) ведут на длинную — её узнаём, пройдя
# по переадресации (redirect).
# ---------------------------------------------------------------------------

MAPS_LINK_RE = re.compile(r"https?://(?:www\.)?(?:google\.[a-z.]+/maps|maps\.google\.[a-z.]+|maps\.app\.goo\.gl|goo\.gl/maps)\S*")
COORD_PATTERNS = [
    re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)"),  # точное место в ссылке на «place»
    re.compile(r"[?&](?:q|query|ll|destination)=(-?\d+\.\d+),\s*(-?\d+\.\d+)"),
    re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)"),  # центр карты
]


def find_maps_link(text: str) -> str | None:
    """Найти в тексте ссылку на Google Maps (или None)."""
    match = MAPS_LINK_RE.search(text)
    return match.group(0) if match else None


def coords_from_url(url: str) -> tuple[float, float] | None:
    """Достать координаты из длинной ссылки Google Maps."""
    from urllib.parse import unquote

    url = unquote(url)  # %2C → «,» и т.п.
    for pattern in COORD_PATTERNS:
        match = pattern.search(url)
        if match:
            lat, lon = float(match.group(1)), float(match.group(2))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
    return None


async def coords_from_link(url: str) -> tuple[float, float] | None:
    """
    Координаты из любой ссылки Google Maps, в том числе короткой.

    Короткую ссылку открываем, не скачивая страницу целиком, и смотрим,
    куда она переадресует. Ошибка сети — просто None.
    """
    coords = coords_from_url(url)
    if coords or "goo.gl" not in url:
        return coords
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as http:
            async with http.get(url, allow_redirects=True) as response:
                return coords_from_url(str(response.url))
    except Exception as error:
        log.warning("Не удалось открыть короткую ссылку карт: %s", error)
        return None
