#!/usr/bin/env python3
"""Отбирает российские товары из выгрузки Open Food Facts и складывает их в небольшую базу SQLite.

Запуск (на сервере, из папки бота):
    nice -n 19 venv/bin/python build_ru_products.py offdata/en.openfoodfacts.org.products.csv.gz data/off_ru.db

Файл читается построчно прямо из архива (распаковывать 9 ГБ не нужно), памяти нужно немного. На одном ядре
разбор занимает порядка 10–25 минут; nice не даёт ему мешать боту. Бот эту базу пока не использует: сначала смотрим,
сколько товаров получилось, и решаем, стоит ли подключать поиск по названию.

Берутся товары, у которых страна продажи включает Россию ИЛИ штрихкод из российского диапазона GS1 (460–469),
есть название и калорийность (или БЖУ, из которых калории считаются). Правила проверки данных — те же, что при поиске
по штрихкоду (barcode_lookup.parse_product): заведомо неверные значения отбрасываются.
"""

import csv
import gzip
import os
import re
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from barcode_lookup import parse_product, STATUS_OK, checksum_ok  # noqa: E402

csv.field_size_limit(10 ** 8)

# Возможные названия колонок (в разных выгрузках бывают немного разные)
COLUMNS = {
    "code": ["code"],
    "name": ["product_name_ru", "product_name", "generic_name"],
    "brand": ["brands"],
    "countries": ["countries_tags", "countries_en", "countries"],
    "quantity": ["quantity"],
    "serving_quantity": ["serving_quantity"],
    "product_quantity": ["product_quantity"],
}
NUTRIMENTS = ["energy-kcal_100g", "energy-kj_100g", "energy_100g", "proteins_100g", "fat_100g", "carbohydrates_100g",
              "fiber_100g", "sugars_100g", "saturated-fat_100g", "monounsaturated-fat_100g", "polyunsaturated-fat_100g",
              "trans-fat_100g", "cholesterol_100g", "sodium_100g", "salt_100g"]
RUSSIA_WORDS = ("en:russia", "russia", "россия", "ru:россия")


def resolve(header):
    """Находит индексы нужных колонок. Возвращает (индексы, список ненайденных)."""
    index = {name: i for i, name in enumerate(header)}
    found, missing = {}, []
    for key, candidates in COLUMNS.items():
        positions = [index[c] for c in candidates if c in index]
        if positions:
            found[key] = positions
        elif key in ("code", "name"):
            missing.append(key)
    nut = {n: index[n] for n in NUTRIMENTS if n in index}
    if "energy-kcal_100g" not in nut and "energy_100g" not in nut:
        missing.append("energy-kcal_100g")
    return found, nut, missing


def first(row, positions):
    for p in positions:
        if p < len(row) and row[p].strip():
            return row[p].strip()
    return ""


def main(src, dst):
    if not os.path.exists(src):
        sys.exit(f"Файл не найден: {src}")
    if os.path.exists(dst):
        os.remove(dst)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    db = sqlite3.connect(dst)
    db.execute("""CREATE TABLE off_products (
        code TEXT PRIMARY KEY, name TEXT, brand TEXT, kcal REAL, protein REAL, fat REAL, carb REAL, fiber REAL,
        sugar REAL, sat REAL, sodium_mg REAL, serving_g REAL, package_g REAL, russia_tag INTEGER)""")

    opener = gzip.open if src.endswith(".gz") else open
    stats = Counter()
    brands = Counter()
    with opener(src, "rt", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
        header = next(reader)
        cols, nut, missing = resolve(header)
        print(f"Колонок в файле: {len(header)}")
        if missing:
            print("⚠ Не найдены колонки:", ", ".join(missing))
            print("Первые 60 колонок файла:", header[:60])
            sys.exit("Без этих колонок продолжить нельзя — пришлите этот вывод.")
        batch = []
        for row in reader:
            stats["rows"] += 1
            if stats["rows"] % 500_000 == 0:
                print(f"  обработано {stats['rows']:,} строк, отобрано {stats['kept']:,}", flush=True)
            code = first(row, cols["code"])
            if not re.fullmatch(r"\d{8,14}", code):
                continue
            countries = first(row, cols.get("countries", [])).lower()
            has_tag = any(w in countries for w in RUSSIA_WORDS)
            has_prefix = len(code) == 13 and code[:3].isdigit() and 460 <= int(code[:3]) <= 469
            if has_tag:
                stats["russia_tag"] += 1
            if has_prefix:
                stats["prefix_46x"] += 1
            if not (has_tag or has_prefix):
                continue
            stats["candidates"] += 1
            name = first(row, cols["name"])
            if not name:
                stats["no_name"] += 1
                continue
            nutriments = {k: row[i] for k, i in nut.items() if i < len(row) and row[i].strip()}
            product, status = parse_product(code, {
                "product_name": name, "brands": first(row, cols.get("brand", [])), "nutriments": nutriments,
                "serving_quantity": first(row, cols.get("serving_quantity", [])),
                "product_quantity": first(row, cols.get("product_quantity", [])),
            })
            if status != STATUS_OK:
                stats["no_energy_or_bad"] += 1
                continue
            if len(code) in (8, 12, 13) and not checksum_ok(code):
                stats["bad_checksum"] += 1
                continue
            n = product["n"]
            stats["kept"] += 1
            if all(k in n for k in ("protein", "fat", "carb")):
                stats["kept_with_macros"] += 1
            if re.search(r"[А-Яа-яЁё]", product["name"]):
                stats["cyrillic_name"] += 1
            if product["brand"]:
                brands[product["brand"]] += 1
            batch.append((code, product["name"], product["brand"], product["kcal"], n.get("protein"), n.get("fat"),
                          n.get("carb"), n.get("fiber"), n.get("sugar"), n.get("sat"), n.get("sodium"),
                          product["serving_g"], product["package_g"], 1 if has_tag else 0))
            if len(batch) >= 5000:
                db.executemany("INSERT OR REPLACE INTO off_products VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
                batch.clear()
        if batch:
            db.executemany("INSERT OR REPLACE INTO off_products VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
    db.commit()

    try:  # индекс для поиска по названию; если в SQLite нет FTS5, база всё равно пригодна
        db.execute("CREATE VIRTUAL TABLE off_fts USING fts5(name, brand, content='off_products', content_rowid='rowid', "
                   "tokenize='unicode61 remove_diacritics 2')")
        db.execute("INSERT INTO off_fts(off_fts) VALUES('rebuild')")
        db.commit()
        fts = "да"
    except sqlite3.OperationalError as e:
        fts = f"нет ({e})"
    total = db.execute("SELECT COUNT(*) FROM off_products").fetchone()[0]
    db.execute("VACUUM")
    db.close()

    print("\n=== Итог ===")
    print(f"Всего строк в выгрузке:            {stats['rows']:>10,}")
    print(f"С пометкой «Россия» в странах:      {stats['russia_tag']:>10,}")
    print(f"Со штрихкодом 460–469 (РФ, GS1):    {stats['prefix_46x']:>10,}")
    print(f"Кандидатов (одно из двух):          {stats['candidates']:>10,}")
    print(f"  без названия:                     {stats['no_name']:>10,}")
    print(f"  без калорий / неверные данные:    {stats['no_energy_or_bad']:>10,}")
    print(f"  неверная контрольная цифра:       {stats['bad_checksum']:>10,}")
    print(f"ОТОБРАНО в базу:                    {total:>10,}")
    print(f"  из них с полными Б/Ж/У:           {stats['kept_with_macros']:>10,}")
    print(f"  из них название кириллицей:       {stats['cyrillic_name']:>10,}")
    print(f"Поиск по названию (FTS5):           {fts}")
    print(f"Размер базы: {os.path.getsize(dst) / 1024 / 1024:.1f} МБ → {dst}")
    print("\nЧаще всего встречаются бренды:", ", ".join(f"{b} ({c})" for b, c in brands.most_common(15)))
    print("\nПримеры:")
    db = sqlite3.connect(dst)
    for r in db.execute("SELECT code, name, brand, kcal, protein, fat, carb FROM off_products ORDER BY RANDOM() LIMIT 12"):
        print("  ", r)
    db.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
