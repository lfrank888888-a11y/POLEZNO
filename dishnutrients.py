"""Нутриенты блюд меню: считаем по ингредиентам рецепта (количества на 1 порцию) и базе продуктов."""
import re
from functools import lru_cache

from foodparse import find_food_text as find_food, find_food as _find_plain, _grams
from recipes import RECIPES

_FRACTIONS = {"½": ".5", "¼": ".25", "¾": ".75", "⅓": ".33", "⅔": ".67"}
_SKIP_QTY = ("по вкусу", "щепотк", "по желанию", "для подачи", "для жарки")
_SKIP_NAMES = ("вода", "соль", "специи", "перец чёрный", "лавровый")
_QTY_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(?P<unit>[а-яА-Я.]+)?")
_GRAIN_STEMS = ("гречк", "рис ", "рис\n", "макарон", "паст", "спагетт", "киноа", "булгур", "кускус", "чечевиц",
                "нут", "фасол", "горох", "лапш", "вермишел", "манк", "перловк", "пшено")
_COOKED_WORDS = ("варён", "варен", "отварн", "готов", "консерв")
_COOKED_MEAT = ("варён", "варен", "отварн", "жарен", "запечён", "запечен", "тушён", "готов", "гриль", "копчён")
# Мясо и рыба в рецептах — в СЫРОМ весе (так их покупают и взвешивают при готовке), если не написано «варёное/жареное».
_RAW_ALIAS = {
    "Курица варёная": "курица сырая", "Курица жареная / запечённая": "курица сырая",
    "Куриная грудка варёная": "куриная грудка сырая", "Куриная грудка жареная": "куриная грудка сырая",
    "Куриное бедро / окорочок": "бедро куриное сырое", "Индейка (филе)": "индейка сырая",
    "Говядина варёная": "говядина сырая", "Говядина тушёная": "говядина сырая", "Стейк говяжий": "говядина сырая",
    "Свинина жареная": "свинина сырая", "Шашлык (свинина)": "свинина сырая", "Фарш жареный": "фарш мясной сырой",
    "Белая рыба (треска, минтай, хек)": "рыба белая сырая", "Рыба жареная": "рыба белая сырая",
    "Лосось / форель": "лосось сырой", "Креветки варёные": "креветки сырые",
}
_PAREN_GRAMS = re.compile(r"\((?:около\s*)?(\d+(?:[.,]\d+)?)\s*(?:г|мл)\)")


def _normalize_fractions(text: str) -> str:
    for sym, val in _FRACTIONS.items():
        text = re.sub(r"(\d)\s*" + sym, lambda m: m.group(1) + val, text)  # 1½ -> 1.5
        text = text.replace(sym, "0" + val)  # ½ -> 0.5
    return text


def _food_for(name: str, hint: str):
    """Продукт по названию ингредиента. Крупы, паста и бобовые в рецептах — в СУХОМ весе, если не сказано «варёный»."""
    low = (name + " " + hint).lower() + " "
    if any(s in low for s in _GRAIN_STEMS) and not any(w in low for w in _COOKED_WORDS):
        for suffix in (" сухая", " сухой", " сухие", " сухое"):
            food = _find_plain(name + suffix)
            if food and "сух" in food["name"].lower():
                return food
    food = find_food(name)
    if food and food["name"] in _RAW_ALIAS and not any(w in low for w in _COOKED_MEAT):
        raw = _find_plain(_RAW_ALIAS[food["name"]])
        if raw:
            return raw
    return food


def parse_ingredient(line: str):
    """«Кабачок — ½ шт» -> (название, граммы|None, пропустить?). Граммы None — не удалось определить."""
    name, _, qty = line.partition("—")
    hint = " ".join(re.findall(r"\((.*?)\)", name + " " + qty))
    name = re.sub(r"\(.*?\)", "", name).strip()
    low_name, low_qty = name.lower(), qty.lower()
    if not qty.strip() or any(low_name.startswith(s) for s in _SKIP_NAMES):
        return name, 0.0, True
    if any(s in low_qty for s in _SKIP_QTY) and not re.search(r"\d", low_qty):
        return name, 0.0, True
    qty = _normalize_fractions(qty)
    food = _food_for(name, hint)
    pg = _PAREN_GRAMS.search(qty)
    if pg:  # «2 ст.л. (30 г)» — граммы указаны явно
        return name, float(pg.group(1).replace(",", ".")), False
    m = _QTY_RE.match(qty)
    if not m or not food:
        return name, None, False
    number = float(m.group(1).replace(",", "."))
    unit = (m.group("unit") or "").lower().rstrip(".")
    if unit.startswith("зубч"):
        return name, number * (food.get("piece") or 4), False
    if unit.startswith("лист"):
        return name, number * (food.get("piece") or 8), False
    if unit.startswith(("дольк", "долек")):
        return name, number * min(30, (food.get("piece") or 60) / 6), False
    if unit.startswith("пучок"):
        return name, number * 30, False
    grams, _ = _grams(food, number, unit)
    return name, grams, False


@lru_cache(maxsize=None)
def dish_nutrients(dish: str):
    """{'n': нутриенты на 1 порцию рецепта (с 'kcal'), 'unmatched': [...], 'grams': общий вес продуктов} или None."""
    recipe = RECIPES.get(dish)
    if not recipe:
        return None
    portions = recipe.get("portions") or 1
    total, unmatched, grams_total, parts = {}, [], 0.0, []
    for line in recipe["ingredients"]:
        name, grams, skip = parse_ingredient(line)
        if skip:
            continue
        hint = " ".join(re.findall(r"\((.*?)\)", line))
        food = _food_for(name, hint)
        if not food or grams is None:
            unmatched.append(name)
            continue
        parts.append((name, food["name"], round(grams / portions)))
        grams_total += grams
        for key, value in food["n"].items():
            total[key] = total.get(key, 0) + value * grams / 100
    if not total:
        return None
    per_portion = {k: round(v / portions, 3) for k, v in total.items()}
    return {"n": per_portion, "unmatched": unmatched, "grams": round(grams_total / portions), "parts": parts}


def dish_scaled(dish: str, factor: float = 1.0):
    """Нутриенты порции блюда с множителем (без kcal) или None, если состав не удалось определить."""
    data = dish_nutrients(dish)
    if not data:
        return None
    return {k: round(v * factor, 3) for k, v in data["n"].items() if k != "kcal"}
