"""База продуктов и блюд: калории и нутриенты на 100 г (foods_data.json).

Источник: USDA FoodData Central (SR Legacy, общедоступные данные США), названия и синонимы на русском;
блюда российской кухни рассчитаны по типовым рецептам из ингредиентов. Значения ориентировочные.
Файл собирается отдельно и не требует правки руками.
"""
import json
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "foods_data.json")


def _load():
    with open(_PATH, encoding="utf-8") as fh:
        rows = json.load(fh)
    foods = []
    for aliases, name, piece, portion, tbsp, tsp, nutrients in rows:
        if "kcal" not in nutrients:
            continue
        foods.append({
            "aliases": aliases, "name": name, "kcal": nutrients["kcal"],
            "piece": piece, "portion": portion, "tbsp": tbsp, "tsp": tsp, "n": nutrients,
        })
    return foods


FOODS = _load()
