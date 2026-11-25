"""
Работа с деньгами: разбор цены, которую ввёл шеф, и красивый вывод.

Деньги везде хранятся в центах (целым числом), см. комментарий в models.Dish.
"""

from culinary_bot.services import ServiceError


def parse_price(text: str) -> int:
    """
    Превратить введённую цену в центы.

    Принимает строки вида "10", "10.5", "10,50", "10 €", "10е".
    Возвращает целое число центов: "10,50" → 1050.
    Если ввели не число — ServiceError с подсказкой.
    """
    # Убираем пробелы и обозначения валюты, запятую считаем точкой —
    # так шеф может писать как привык.
    cleaned = (
        text.strip().lower().replace("€", "").replace("eur", "").replace("е", "").replace("e", "")
    )
    cleaned = cleaned.replace(" ", "").replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        raise ServiceError("Введите цену числом, например 10 или 10.5") from None
    if value <= 0 or value > 10_000:
        raise ServiceError("Цена должна быть больше нуля и меньше 10 000 €")
    # round, а не int: float(10.15) * 100 = 1014.999..., int дал бы 1014.
    return round(value * 100)


def format_price(cents: int) -> str:
    """
    Центы → строка для показа: 1000 → "10 €", 1050 → "10.50 €".

    Целые суммы показываем без ".00" — так, как пишет шеф в своих постах.
    """
    euros, rest = divmod(cents, 100)
    if rest == 0:
        return f"{euros} €"
    return f"{euros}.{rest:02d} €"
