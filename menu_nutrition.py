"""Состав блюд меню: калории и БЖУ по рецептам.

Калории в строках меню в config.py — оценки «на глаз», у многих блюд они расходятся с рецептом на 20–70%.
Поэтому по умолчанию «~N ккал» пересчитывается по ингредиентам рецепта (в config.py можно отключить:
MENU_KCAL_FROM_RECIPES = False). БЖУ показываются всегда.
"""
import re

try:
    import config as _config
except Exception:  # pragma: no cover
    _config = None

import nutrients as nutrient_utils
from dietfilter import find_dish
from dishnutrients import dish_nutrients, dish_scaled

KCAL_FROM_RECIPES = bool(getattr(_config, "MENU_KCAL_FROM_RECIPES", True))
_KCAL_RE = re.compile(r"~\s*(\d+)\s*ккал")
_CACHE = {}


def dish_kcal(dish: str):
    data = dish_nutrients(dish) if dish else None
    return round(data["n"]["kcal"]) if data and "kcal" in data["n"] else None


def rewrite_line(line: str) -> str:
    """Подставляет в строку меню калорийность, посчитанную по рецепту (округление до 10 ккал)."""
    if not KCAL_FROM_RECIPES:
        return line
    kcal = dish_kcal(find_dish(line))
    if not kcal or not _KCAL_RE.search(line):
        return line
    return _KCAL_RE.sub(f"~{int(round(kcal / 10.0) * 10)} ккал", line, count=1)


def rewrite_menus(menus: dict, key: str) -> dict:
    if key not in _CACHE:
        _CACHE[key] = {cat: [rewrite_line(i) for i in items] for cat, items in menus.items()}
    return _CACHE[key]


def macro_for_line(line: str) -> str:
    """«Б 35 · Ж 20 · У 40 г» для блюда из строки меню (на 1 порцию рецепта) или пустая строка."""
    dish = find_dish(line)
    nut = dish_scaled(dish) if dish else None
    if not nut:
        return ""
    parts = [f"{label} {round(nut[key])}" for key, label in (("protein", "Б"), ("fat", "Ж"), ("carb", "У")) if key in nut]
    return (" · ".join(parts) + " г") if parts else ""


def recipe_block(dish: str) -> str:
    """Блок «На 1 порцию» для текста рецепта."""
    data = dish_nutrients(dish)
    if not data:
        return ""
    nut = {k: v for k, v in data["n"].items() if k != "kcal"}
    text = f"\n\n📊 *На 1 порцию по рецепту:* ~{round(data['n']['kcal'])} ккал\n{nutrient_utils.macro_line(nut)}"
    text += "\n_Мясо, рыба, крупы и паста посчитаны в сыром (сухом) виде, как в списке ингредиентов._"
    return text


def dish_details(dish: str) -> str:
    nut = dish_scaled(dish)
    if not nut:
        return "Состав этого блюда пока не рассчитан."
    return nutrient_utils.details_text([{"nutrients": nut}], title="🔬 *Витамины и минералы — 1 порция*")
