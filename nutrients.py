"""Нутриенты: суммирование по записям дневника и тексты экранов «Итог дня» и «Подробнее»."""
import json

# Ориентировочные суточные значения (как на этикетках, для взрослых, без деления по полу).
NORMS = {
    "fiber": 25, "sugar": 50, "sat": 20, "chol": 300, "sodium": 2000,
    "vita": 800, "vitd": 15, "vite": 12, "vitk": 75, "b1": 1.1, "b2": 1.4, "b3": 16, "b5": 6, "b6": 1.4,
    "b9": 200, "b12": 2.5, "vitc": 80,
    "ca": 1000, "fe": 14, "mg": 375, "p": 700, "k": 3500, "zn": 10, "se": 55, "cu": 1, "mn": 2,
}

# (ключ, название, единица, знаков после запятой)
FATS = [("sat", "Насыщенные", "г", 1), ("mono", "Мононенасыщенные", "г", 1),
        ("poly", "Полиненасыщенные", "г", 1), ("trans", "Трансжиры", "г", 1), ("chol", "Холестерин", "мг", 0)]
VITAMINS = [("vita", "Витамин A", "мкг", 0), ("vitd", "Витамин D", "мкг", 1), ("vite", "Витамин E", "мг", 1),
            ("vitk", "Витамин K", "мкг", 0), ("b1", "B1 (тиамин)", "мг", 2), ("b2", "B2 (рибофлавин)", "мг", 2),
            ("b3", "B3 (ниацин)", "мг", 1), ("b5", "B5 (пантотеновая)", "мг", 1), ("b6", "B6", "мг", 2),
            ("b9", "B9 (фолиевая)", "мкг", 0), ("b12", "B12", "мкг", 1), ("vitc", "Витамин C", "мг", 0)]
MINERALS = [("ca", "Кальций", "мг", 0), ("fe", "Железо", "мг", 1), ("mg", "Магний", "мг", 0),
            ("p", "Фосфор", "мг", 0), ("k", "Калий", "мг", 0), ("zn", "Цинк", "мг", 1),
            ("se", "Селен", "мкг", 0), ("cu", "Медь", "мг", 2), ("mn", "Марганец", "мг", 2)]


def dumps(nut: dict) -> str:
    return json.dumps({k: round(v, 3) for k, v in nut.items()}, separators=(",", ":"))


def loads(raw):
    try:
        data = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) and data else None


def scale(per100: dict, grams: float) -> dict:
    """Нутриенты порции из значений на 100 г."""
    return {k: round(v * grams / 100, 3) for k, v in per100.items() if k != "kcal"}


def total(entries) -> tuple:
    """(суммы, число записей с данными, всего записей)."""
    sums, with_data = {}, 0
    for e in entries:
        nut = e.get("nutrients")
        if nut:
            with_data += 1
            for k, v in nut.items():
                sums[k] = sums.get(k, 0) + v
    return sums, with_data, len(entries)


def _fmt(value, digits):
    text = f"{value:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def macro_line(nut: dict) -> str:
    """Короткая строка: Б · Ж · У · клетчатка · сахар (г)."""
    parts = []
    for key, label in (("protein", "Б"), ("fat", "Ж"), ("carb", "У")):
        if key in nut:
            parts.append(f"{label} {_fmt(nut[key], 0)}")
    line = " · ".join(parts)
    extra = []
    if "fiber" in nut:
        extra.append(f"клетчатка {_fmt(nut['fiber'], 0)}")
    if "sugar" in nut:
        extra.append(f"сахар {_fmt(nut['sugar'], 0)}")
    if extra:
        line += ("\n" if line else "") + " · ".join(extra)
    return (line + " г") if line else ""


def summary_block(entries) -> str:
    """Блок «Итог дня» для экрана дневника; пусто, если данных о нутриентах нет."""
    sums, with_data, count = total(entries)
    if not with_data:
        return ""
    text = "\n\n🥗 *Нутриенты за день*\n" + macro_line(sums)
    if with_data < count:
        text += f"\n_Учтено записей: {with_data} из {count} (по блюдам из меню и записям вручную данных нет)_"
    return text


def _rows(group, sums):
    lines = []
    for key, label, unit, digits in group:
        if key in sums:
            value = sums[key]
            share = f" — {round(value / NORMS[key] * 100)}%" if key in NORMS and NORMS[key] else ""
            lines.append(f"{label}: {_fmt(value, digits)} {unit}{share}")
        else:
            lines.append(f"{label}: нет данных")
    return lines


def details_text(entries, title="🔬 *Подробнее за сегодня*") -> str:
    sums, with_data, count = total(entries)
    if not with_data:
        return ("🔬 *Подробнее*\n\nПока нет записей, по которым известен состав. "
                "Напишите, что съели, через «✏️ Написать, что съел(а)» — бот посчитает витамины и минералы.")
    lines = [title, ""]
    lines.append("*Жиры и другое*")
    lines += _rows(FATS, sums)
    if "sodium" in sums:
        salt = sums["sodium"] * 2.5 / 1000
        lines.append(f"Натрий: {_fmt(sums['sodium'], 0)} мг — {round(sums['sodium'] / NORMS['sodium'] * 100)}% (≈ соли {_fmt(salt, 1)} г)")
    else:
        lines.append("Натрий: нет данных")
    for key, label, unit in (("water", "Вода в продуктах", "г"), ("alcohol", "Алкоголь", "г")):
        if key in sums and sums[key] > 0:
            lines.append(f"{label}: {_fmt(sums[key], 0)} {unit}")
    lines += ["", "*Витамины*"] + _rows(VITAMINS, sums)
    lines += ["", "*Минералы*"] + _rows(MINERALS, sums)
    foot = "\n\nПроценты — доля от ориентировочной суточной нормы для взрослого (как на этикетках)."
    if with_data < count:
        foot += f"\nУчтено записей: {with_data} из {count}."
    foot += "\nДанные ориентировочные; это не медицинская рекомендация."
    return "\n".join(lines) + foot
