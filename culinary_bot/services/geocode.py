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
