"""Фильтр блюд по типу питания и аллергиям.

Проверяет не только название блюда, но и ингредиенты его рецепта: иначе «Борщ» попадал
бы вегетарианцу (говядина в составе), а «Омлет + тост» — человеку с аллергией на молоко.
"""

from recipes import RECIPES

# Если в запрете есть ключевое слово-«якорь», добавляем продукты, которых нет в списках config.py:
# без этого при безглютеновой диете проходили бы булгур и манка, вегетарианцу — бекон,
# при аллергии на молоко — сливочное масло и сгущёнка.
_EXTRA_BANNED = {
    "мука": ("манка", "булгур", "кускус", "блин", "овсян", "ячмен", "пшен"),  # глютен
    "молоко": ("сливочн", "сгущ", "сливк"),                                    # молочное
    "мясо": ("бекон", "фарш", "мясн", "ветчин", "колбас", "сосиск"),            # мясо
}


def expand_banned(banned) -> set:
    extra = {word for trigger, words in _EXTRA_BANNED.items() if trigger in banned for word in words}
    return set(banned) | extra


def find_dish(item: str):
    """Строка меню -> ключ в RECIPES (та же очистка названия, что в recipes.get_recipe_by_name)."""
    clean = item.split("—")[0].strip()
    clean = "".join(c for c in clean if not (0x1F300 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF or ord(c) == 0xFE0F)).strip()
    return clean if clean in RECIPES else None


def ingredients_allowed(dish: str, banned) -> bool:
    """Блюдо не подходит, если в ингредиентах есть запрещённое слово."""
    text = " ".join(RECIPES[dish]["ingredients"]).lower()
    text = text.replace("яйца куриные", "яйца")  # иначе слово \"куриные\" принимает яйца за мясо
    return not any(word in text for word in banned)
