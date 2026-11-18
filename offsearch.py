"""Поиск товаров по названию и бренду в локальной базе off_ru.db (российские товары из Open Food Facts).

Базу строит скрипт build_ru_products.py. Если файла нет, бот работает как раньше, а функция available() вернёт False.
Путь по умолчанию — off_ru.db рядом с базой бота; изменить можно в config.py:  OFF_DB_PATH = "/путь/off_ru.db"
"""

import logging
import os
import re
import sqlite3

logger = logging.getLogger(__name__)

# Слова, которые не помогают искать: предлоги, единицы измерения, служебные слова
_STOP = {"и", "с", "со", "в", "на", "из", "для", "без", "по", "от", "до", "г", "гр", "мл", "шт", "кг", "л", "ккал",
         "порция", "порции", "ст", "ч", "ложка", "ложки", "кусок", "куска", "штука", "штуки", "штук", "съел", "съела"}


def db_path() -> str:
    try:
        import config
        custom = getattr(config, "OFF_DB_PATH", None)
        if custom:
            return custom
        return os.path.join(os.path.dirname(os.path.abspath(config.DB_PATH)), "off_ru.db")
    except ImportError:
        return "off_ru.db"


def available() -> bool:
    return os.path.exists(db_path())


def _tokens(query: str) -> list:
    text = (query or "").lower().replace("ё", "е")
    words = re.findall(r"[a-zа-я]+", text)
    return [w for w in words if w not in _STOP and len(w) >= 2]


def _term(word: str) -> str:
    """Основа слова для поиска по началу: «сырников» и «сырники» дают одну и ту же основу.
    В SQLite нет русской морфологии, поэтому слово обрезаем и ищем по префиксу."""
    if len(word) >= 10:
        return word[:-3]
    if len(word) >= 7:
        return word[:-2]
    if len(word) >= 5:
        return word[:-1]
    return word


def _fts_query(tokens: list) -> str:
    return " AND ".join(f'"{_term(t)}"*' for t in tokens)


def _row_to_product(row) -> dict:
    code, name, brand, kcal, protein, fat, carb, fiber, sugar, sat, sodium, serving, package = row[:13]
    n = {}
    for key, value in (("protein", protein), ("fat", fat), ("carb", carb), ("fiber", fiber), ("sugar", sugar),
                       ("sat", sat), ("sodium", sodium)):
        if value is not None:
            n[key] = value
    return {"code": code, "name": name, "brand": brand or "", "kcal": round(kcal), "n": n,
            "serving_g": serving or None, "package_g": package or None, "warn": ""}


def search(query: str, limit: int = 5) -> list:
    """Лучшие совпадения по названию и бренду: список товаров в том же виде, что у поиска по штрихкоду."""
    tokens = _tokens(query)
    if not tokens or not available():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT p.code, p.name, p.brand, p.kcal, p.protein, p.fat, p.carb, p.fiber, p.sugar, p.sat, p.sodium_mg, "
            "p.serving_g, p.package_g, bm25(off_fts, 5.0, 2.0) AS rank "
            "FROM off_fts JOIN off_products p ON p.rowid = off_fts.rowid WHERE off_fts MATCH ? ORDER BY rank LIMIT 60",
            (_fts_query(tokens),)).fetchall()
        conn.close()
    except sqlite3.Error as e:
        logger.warning(f"Поиск в off_ru.db не удался: {e}")
        return []

    scored = []
    for row in rows:
        product = _row_to_product(row)
        name = product["name"]
        score = row[13]  # bm25: чем меньше, тем лучше
        if all(k in product["n"] for k in ("protein", "fat", "carb")):
            score -= 1.0  # полные БЖУ предпочтительнее
        if re.search(r"[А-Яа-яЁё]", name):
            score -= 1.0
        score += len(name) / 80.0  # короткое название обычно точнее («Кефир 2,5%» лучше, чем длинный состав)
        scored.append((score, product))
    scored.sort(key=lambda item: item[0])

    result, seen = [], set()
    for _, product in scored:
        key = (re.sub(r"\W+", "", product["name"].lower()), re.sub(r"\W+", "", product["brand"].lower()))
        if key in seen:
            continue
        seen.add(key)
        result.append(product)
        if len(result) >= limit:
            break
    return result
