"""Порции под норму калорий пользователя.

Блюда в меню рассчитаны на стандартную порцию, и за день они дают заметно меньше суточной нормы
(при похудении примерно половину). Чтобы человек не недоедал, считаем множитель порции: во сколько
раз увеличить порции из рецептов, чтобы типичный день вышел на норму. Множитель не меньше 1
(уменьшать порции не советуем) и не больше PORTION_MAX, шаг 0,25.
"""

import re

from dietfilter import expand_banned, find_dish, ingredients_allowed
from menu_generator import filter_menu, get_all_menus, get_ingredient_banned_keywords, user_calorie_norm
from norms import PORTION_MAX, factor_phrase

MEAL_CATEGORIES = ("завтрак", "обед", "перекус", "ужин")


def kcal_of(line: str):
    match = re.search(r"~(\d+)\s*ккал", line)
    return int(match.group(1)) if match else None


def candidate_lines(user: dict) -> dict:
    """Блюда меню, подходящие пользователю по цели, типу питания и аллергиям (с проверкой состава рецепта)."""
    goal = user.get("goal") or "похудение"
    diet = user.get("diet_type") or "обычное"
    allergies = user.get("allergies") or ""
    menus = filter_menu(get_all_menus(goal), diet, allergies)
    banned = expand_banned(get_ingredient_banned_keywords(diet, allergies))
    result = {}
    for category, items in menus.items():
        good = [item for item in items if (dish := find_dish(item)) and ingredients_allowed(dish, banned)]
        if good:
            result[category] = good
    return result


def typical_day_kcal(user: dict):
    """Сколько ккал в среднем даёт день из подходящих пользователю блюд (по стандартным порциям).
    None, если для какого-то приёма пищи нет блюд с известной калорийностью."""
    candidates = candidate_lines(user)
    total = 0.0
    for category in MEAL_CATEGORIES:
        values = [k for k in (kcal_of(line) for line in candidates.get(category, [])) if k]
        if not values:
            return None
        total += sum(values) / len(values)
    return total


def portion_info(user: dict) -> dict:
    """{"factor": 1.0–2.0, "phrase": "вдвое больше" | None, "norm": ккал, "typical": ккал, "capped": bool}."""
    info = {"factor": 1.0, "phrase": None, "norm": None, "typical": None, "capped": False}
    if not user or not user.get("weight"):
        return info
    typical = typical_day_kcal(user)
    norm = user_calorie_norm(user)
    info.update(norm=norm, typical=typical)
    if not typical:
        return info
    raw = norm / typical
    factor = round(min(max(raw, 1.0), PORTION_MAX) * 4) / 4
    info.update(factor=factor, phrase=factor_phrase(factor), capped=raw > PORTION_MAX + 0.125)
    return info


def portion_factor(user: dict) -> float:
    return portion_info(user)["factor"]
