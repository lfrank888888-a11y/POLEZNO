"""Разбор текста «что я съел» → список продуктов с граммами и калориями."""
import re
from foodbase import FOODS

_STOP = {"с", "со", "и", "на", "в", "из", "без", "по"}
_JUNK = {"какая", "какой", "какие", "какое", "то", "что", "это", "еще", "ещё"}
_NUM = r"(\d+(?:[.,]\d+)?(?:/\d+)?)"
_KCAL_RE = re.compile(_NUM + r"\s*(?:ккал|kcal|калорий|калории|калория|кал)\b\.?", re.I)
_PCT_RE = re.compile(r"(\d+)(?:[.,](\d+))?\s*%")
_QTY_RE = re.compile(
    _NUM + r"\s*(?P<unit>кг|килограмм\w*|гр\b|г\b|грамм\w*|мл|миллилитр\w*|л\b|литр\w*|шт\b|штук\w*|штуки|"
    r"ст\.?\s?л\b\.?|столов\w+\s+ложк\w+|ч\.?\s?л\b\.?|чайн\w+\s+ложк\w+|стакан\w*|чашк\w*|кружк\w*|"
    r"кус\w+|ломт\w+|долек|дольк\w*|порци\w+)?", re.I)
_SPLIT_RE = re.compile(r"[;\n+]|,(?!\d)|\s+и\s+|\s+\+\s+", re.I)


def _norm(s: str) -> str:
    return s.lower().replace("ё", "е")


def _tokens(s: str):
    return [t for t in re.findall(r"[a-zа-я0-9]+", _norm(s)) if t not in _JUNK]


def _prep_foods():
    prepared = []
    for food in FOODS:
        for alias in food["aliases"]:
            toks = _tokens(alias)
            if toks:
                prepared.append((toks, food))
    return prepared


_ALIASES = _prep_foods()


def _stem(alias: str) -> str:
    return alias[:-1] if len(alias) > 3 and alias[-1] in "аеёиоуыэюяй" else alias


def _same(a: str, t: str) -> bool:
    """Токен t — словоформа алиаса a (гречку ~ гречка, помидоры ~ помидор)."""
    if a == t:
        return True
    if a.startswith("pct") or t.startswith("pct"):
        return False
    stem = _stem(a)
    if not (len(stem) >= 3 and t.startswith(stem) and len(t) - len(stem) <= 3):
        return False
    # Хвост должен быть настоящим падежным окончанием: «макфа» — не «мак», «простоквашино» — не «простокваша»
    return t[len(stem):] in _ENDINGS


_ENDINGS = {"", "а", "у", "ы", "и", "е", "о", "я", "ю", "ь", "й", "ой", "ом", "ем", "ей", "ов", "ев", "ам", "ям", "ах", "ях",
            "ами", "ями", "ью", "ых", "их", "ым", "им", "ое", "ее", "ая", "яя", "ую", "юю", "ые", "ие", "ый", "ий", "ого",
            "его", "ому", "ему", "ки", "ке", "ку", "ка", "ек", "ок", "ке", "ков", "кой", "кам", "ками", "ках", "ко",
            "ик", "ика", "ики", "ику", "ом", "ами", "ши", "шу", "ше", "ша", "ою", "ею", "ут", "ют", "ат", "ят"}


def prep_pool(foods):
    """Подготовка списка своих продуктов (словари как в FOODS) для поиска."""
    pool = []
    for food in foods or []:
        for alias in food["aliases"]:
            toks = _tokens(alias)
            if toks:
                pool.append((toks, food))
    return pool


# Общие блюда: если к названию добавлено незнакомое уточнение, подставлять их нельзя
_GENERIC_FOODS = {"Суп (куриный, овощной)", "Суп-пюре", "Салат овощной с маслом", "Овощное рагу"}


def find_food(name: str, pool=None):
    """Лучшее совпадение по названию или None. Токены pctNN (жирность) используются, если есть в таблице."""
    toks = _tokens(name)
    if not toks:
        return None

    def best(tokens):
        meaningful = [t for t in tokens if t not in _STOP]
        found = None
        found_key = None
        for alias_toks, food in (_ALIASES if pool is None else pool):
            positions = []
            for a in alias_toks:
                pos = next((i for i, t in enumerate(tokens) if _same(a, t)), None)
                if pos is None:
                    break
                positions.append(pos)
            else:
                covered = len({p for p in positions if tokens[p] not in _STOP})
                exact = sum(1 for a, p in zip(alias_toks, positions) if a == tokens[p])
                key = (len(alias_toks), covered, exact, -min(positions))
                if found_key is None or key > found_key:
                    found, found_key = food, key
        return found

    result = best(toks)
    if result is None or any(t.startswith("pct") for t in toks):
        with_pct = best(toks)
        if with_pct is None:
            without = [t for t in toks if not t.startswith("pct")]
            return best(without)
    return result


def _to_float(raw: str) -> float:
    raw = raw.replace(",", ".")
    if "/" in raw:
        a, b = raw.split("/", 1)
        return float(a) / float(b) if float(b) else float(a)
    return float(raw)


def _grams(food, qty: float, unit: str):
    """(граммы, пояснение) для количества и единицы; unit в нижнем регистре или ''."""
    u = unit.lower()
    piece, portion = food.get("piece"), food.get("portion") or 100
    if u.startswith("кг") or u.startswith("килограмм"):
        return qty * 1000, ""
    if u in ("г", "гр") or u.startswith("грамм"):
        return qty, ""
    if u.startswith("мл") or u.startswith("миллилитр"):
        return qty, ""
    if u == "л" or u.startswith("литр"):
        return qty * 1000, ""
    if u.startswith("стакан"):
        return qty * 250, ""
    if u.startswith("ст") and not u.startswith(("стак",)):
        return qty * (food.get("tbsp") or 15), ""
    if u.startswith("ч") and not u.startswith("чашк"):
        return qty * (food.get("tsp") or 5), ""
    if u.startswith("чашк"):
        return qty * 200, ""
    if u.startswith("кружк"):
        return qty * 300, ""
    if u.startswith("порци"):
        return qty * portion, ""
    if u.startswith(("кус", "ломт", "долек", "дольк")):
        w = piece or 30
        return qty * w, f"1 {u[:3]}. ≈ {w:g} г" if not piece else ""
    if u.startswith(("шт", "штук")):
        if piece:
            return qty * piece, ""
        return qty * portion, f"1 шт ≈ {portion:g} г"
    # без единицы
    if piece and qty < 20:
        return qty * piece, ""
    if qty >= 20:
        return qty, ""
    return qty * portion, f"1 порция ≈ {portion:g} г"


def _fat_token(text: str) -> str:
    m = _PCT_RE.search(text)
    if not m:
        return ""
    return "pct" + m.group(1) + (m.group(2) or "")


def find_food_text(name: str, pool=None):
    """find_food + учёт жирности из текста («творог 9%», «кефир 1%»); без подходящего варианта — базовый продукт.
    pool — подготовленные личные продукты пользователя: они проверяются в первую очередь."""
    if pool:
        mine = find_food(_PCT_RE.sub(" ", name), pool)
        if mine:
            return mine
    fat = _fat_token(name)
    base = _PCT_RE.sub(" ", name)
    food = find_food((base + " " + fat).strip()) if fat else None
    return food or find_food(base)


_PER100_RE = re.compile(r"(?:\bна|/)\s*100\s*(?:г\b|гр\b|грамм\w*|мл\b)?", re.I)
_MACRO_WORDS = r"белк\w*|жир\w*|углевод\w*"
_MACRO_RE = re.compile(r"(?<![а-яёa-z])(" + _MACRO_WORDS + r"|[бжу])\.?\s*[:=]?\s*(\d+(?:[.,]\d+)?)(?![\d%])", re.I)
_BJU_RE = re.compile(r"(?<![а-яёa-z])б\s*/?\s*ж\s*/?\s*у\.?\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*[/\s]+\s*(\d+(?:[.,]\d+)?)\s*[/\s]+\s*(\d+(?:[.,]\d+)?)", re.I)
_MACRO_START = re.compile(r"^\s*(?:" + _MACRO_WORDS + r"|[бжу]\b|бжу|ккал|на\s+100|\d+(?:[.,]\d+)?\s*(?:ккал|kcal))", re.I)
_QTY_ONLY = re.compile(r"^\s*\d+(?:[.,]\d+)?\s*(?:г|гр|мл|грамм\w*)\s*$", re.I)


def _merge_macro_segments(parts):
    """«йогурт 78 ккал/100 г, Б 4, Ж 3, У 10»: куски с БЖУ приклеиваем к предыдущему продукту."""
    merged = []
    for part in parts:
        clean = part.strip(" .,-—:")
        if merged and (_MACRO_START.match(clean) or (_QTY_ONLY.match(clean) and ("ккал" in merged[-1].lower() or _MACRO_RE.search(merged[-1])))):
            merged[-1] += " " + part
        else:
            merged.append(part)
    return merged


def _macros(seg: str):
    """(Б, Ж, У или None, текст без БЖУ)."""
    values = {}
    m = _BJU_RE.search(seg)
    if m:
        values = {"protein": _to_float(m.group(1)), "fat": _to_float(m.group(2)), "carb": _to_float(m.group(3))}
        return values, seg[:m.start()] + " " + seg[m.end():]

    def take(match):
        word = match.group(1).lower()
        key = "protein" if word.startswith(("б", "белк")) else "fat" if word.startswith(("ж", "жир")) else "carb"
        values.setdefault(key, _to_float(match.group(2)))
        return " "

    return values, _MACRO_RE.sub(take, seg)


def _label_item(seg: str):
    """Продукт с упаковки или с явными калориями: «йогурт 150 г 78 ккал/100 г Б 4 Ж 3 У 10», «пирожное 300 ккал».
    Возвращает item или None, если в сегменте нет ккал/БЖУ."""
    km = _KCAL_RE.search(seg)
    macros, rest = _macros(seg)
    if not km and not macros:
        return None
    per100 = bool(_PER100_RE.search(seg)) and (km is not None or macros)
    if km:
        kcal_value = _to_float(km.group(1))
        rest = rest.replace(km.group(0), " ") if km.group(0) in rest else _KCAL_RE.sub(" ", rest)
    else:
        kcal_value = None
    rest = _PER100_RE.sub(" ", rest)
    qm = _QTY_RE.search(rest)
    grams = None
    if qm and per100:
        grams, _ = _grams({"portion": 100, "piece": None}, _to_float(qm.group(1)), qm.group("unit") or "г")
    if qm:
        rest = rest[:qm.start()] + " " + rest[qm.end():]
    title = re.sub(r"\s+", " ", rest).strip(" ,-—:/") or "Запись"
    title = title[:1].upper() + title[1:]
    if kcal_value is None:  # только БЖУ: калории по формуле 4/9/4
        kcal_value = 4 * macros.get("protein", 0) + 9 * macros.get("fat", 0) + 4 * macros.get("carb", 0)
    if per100:
        used = grams or 100
        total = kcal_value * used / 100
        scale = used / 100
        note = f"{kcal_value:g} ккал на 100 г × {used:g} г" + ("" if grams else " (количество не указано — считаю 100 г)")
    else:
        used, total, scale = None, kcal_value, 1
        note = "ккал указаны вами"
    if not (1 <= round(total) <= 5000) or (per100 and kcal_value > 900):
        return {"bad": seg}
    item = {"title": title[:50], "grams": round(used) if used else None, "kcal": round(total), "note": note}
    if macros:
        item["n"] = {k: round(v * scale, 3) for k, v in macros.items()}
    if per100:
        item["per100"] = {"kcal": kcal_value, **{k: v for k, v in macros.items()}, "grams": grams}
    return item


# Порция «на глаз»: множитель к обычной порции из базы
SIZES = {"s": (0.6, "маленькая"), "m": (1.0, "обычная"), "l": (1.5, "большая"), "d": (2.0, "двойная")}
_SIZE_WORDS = [
    (re.compile(r"\b(?:маленьк\w+|небольш\w+|малая|мини)\b", re.I), "s"),
    (re.compile(r"\b(?:больш\w+|огромн\w+)\b", re.I), "l"),
    (re.compile(r"\bдвойн\w+\b", re.I), "d"),
    (re.compile(r"\bсредн\w+\b", re.I), "m"),
]


def _extract_size(name: str):
    """«большая порция борща» -> ('l', «порция борща»); без слова о размере -> (None, name)."""
    for rx, key in _SIZE_WORDS:
        if rx.search(name):
            return key, rx.sub(" ", name)
    return None, name


def resize_item(item: dict, size: str) -> dict:
    """Копия позиции с порцией «маленькая/обычная/большая» от обычной порции. Для позиций без базовой порции — как есть."""
    base = item.get("base")
    if not base or size not in SIZES:
        return item
    mult, label = SIZES[size]
    grams = round(base["grams"] * mult)
    new = dict(item)
    new["grams"] = grams
    new["kcal"] = round(base["kcal"] * mult)
    if base.get("n"):
        new["n"] = {k: round(v * mult, 3) for k, v in base["n"].items()}
    new["size"] = size
    new["note"] = f"{label} порция ≈ {grams} г" + ("; " + base["warn"] if base.get("warn") else "") + ("; ваш продукт" if item.get("mine") else "")
    return new


def miss_key(segment: str) -> str:
    """Запрос для счётчика «не нашёл»: без чисел, единиц и знаков, чтобы «суп с рисом 300 г» и «суп с рисом 250 г» считались вместе."""
    text = re.sub(r"\d+(?:[.,]\d+)?\s*(?:кг|г|гр|мл|л|шт|ккал|kcal|%|ст\.?\s*л\.?|ч\.?\s*л\.?)?", " ", segment or "", flags=re.I)
    text = re.sub(r"[^\w\s\-]", " ", text)
    return " ".join(text.lower().split())[:60]


def parse_text(text: str, extra=None):
    """Возвращает (items, unknown).
    item: {title, grams, kcal, note, n?, per100?}; unknown — список непонятых фрагментов.
    extra — личные продукты пользователя (словари как в FOODS), ищутся раньше общей базы."""
    items, unknown = [], []
    pool = prep_pool(extra)
    for raw in _merge_macro_segments([p for p in _SPLIT_RE.split(text or "") if p and p.strip(" .,-—:")]):
        seg = raw.strip(" .,-—:")
        if not seg:
            continue
        # 1) калории/БЖУ указаны явно (в том числе «на 100 г» с упаковки)
        labeled = _label_item(seg)
        if labeled:
            if "bad" in labeled:
                unknown.append(seg)
            else:
                items.append(labeled)
            continue
        fat = _fat_token(seg)
        seg_nopct = _PCT_RE.sub(" ", seg)
        qm = _QTY_RE.search(seg_nopct)
        qty, unit = None, ""
        if qm:
            qty = _to_float(qm.group(1))
            unit = qm.group("unit") or ""
            name = (seg_nopct[:qm.start()] + " " + seg_nopct[qm.end():]).strip()
        else:
            name = seg_nopct.strip()
        size_word, name = _extract_size(name)
        name = re.sub(r"\b(?:примерно|около|порция|порции|тарелка|тарелку|кусок|кусочек|ломтик|съел\w*|съела)\b", " ", name, flags=re.I)
        food = None
        if pool:
            food = find_food(name, pool)
        if food is None:
            food = find_food((name + " " + fat).strip()) if fat else find_food(name)
        if food is None and fat:
            food = find_food(name)
        if food is None:
            unknown.append(seg)
            continue
        if qty is None:
            grams, note = food.get("portion") or 100, f"порция по умолчанию {food.get('portion') or 100:g} г"
        else:
            grams, note = _grams(food, qty, unit)
        if grams <= 0 or grams > 5000:
            unknown.append(seg)
            continue
        kcal = round(food["kcal"] * grams / 100)
        known = [t for a in food["aliases"] for t in _tokens(a)]
        leftover = [t for t in _tokens(name) if t not in _STOP and not t.startswith("pct")
                    and not any(_same(k, t) for k in known)]
        if leftover and food["name"] in _GENERIC_FOODS and not food.get("mine"):
            # «фасолевый суп» не должен молча стать «куриным супом»: лучше честно сказать, что не нашли
            unknown.append(seg)
            continue
        warn_text = ""
        if leftover:
            warn = "не учтено: " + ", ".join(leftover) + " — добавьте отдельной позицией"
            warn_text = warn
            note = f"{note}; {warn}" if note else warn
        item = {"title": food["name"], "grams": round(grams), "kcal": kcal, "note": note,
                "n": {k: round(v * grams / 100, 3) for k, v in food["n"].items() if k != "kcal"}}
        if leftover and not food.get("mine"):  # возможно, это товар с брендом — бот предложит поискать его в базе магазинов
            item["extra_words"] = leftover
            item["seg"] = seg
        if food.get("mine"):
            item["mine"] = True
            item["note"] = (note + "; ваш продукт") if note else "ваш продукт"
        if qty is None:  # количество не названо — можно выбрать «на глаз»: маленькая / обычная / большая
            item["base"] = {"grams": round(grams), "kcal": kcal, "n": dict(item["n"]), "warn": warn_text if leftover else ""}
            if size_word and size_word != "m":
                item = resize_item(item, size_word)
        items.append(item)
    return items, unknown


def user_food_to_food(row: dict) -> dict:
    """Строка таблицы user_foods -> словарь, как у продуктов общей базы."""
    n = {"kcal": row["kcal"]}
    for key in ("protein", "fat", "carb"):
        if row.get(key) is not None:
            n[key] = row[key]
    return {"aliases": [row["name"]], "name": row["name"], "kcal": row["kcal"], "piece": None,
            "portion": row.get("portion"), "tbsp": None, "tsp": None, "n": n, "mine": True}


def user_pool(rows):
    """Личные продукты пользователя для parse_text(extra=...). Если первое слово названия уникально среди его
    продуктов, по нему тоже находим («йогурт» -> «Йогурт Простоквашино»)."""
    foods = [user_food_to_food(r) for r in rows]
    first = {}
    for food in foods:
        toks = _tokens(food["name"])
        if len(toks) > 1:
            first.setdefault(toks[0], []).append(food)
    for word, group in first.items():
        if len(group) == 1:
            group[0]["aliases"].append(word)
    return foods
