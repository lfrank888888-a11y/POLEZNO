"""Список покупок на неделю: собирается из ингредиентов в recipes.py"""

import math
import random
import re
from collections import Counter, defaultdict

from dietfilter import expand_banned as _expand_banned, find_dish as _find_dish, ingredients_allowed as _is_allowed
from recipes import RECIPES

CATEGORIES = ["завтрак", "обед", "перекус", "ужин"]
CATEGORY_EMOJI = {"завтрак": "🌅", "обед": "☀️", "перекус": "🌤", "ужин": "🌙"}

# Сколько разных блюд брать в каждой категории на неделю. Блюда повторяются по
# кругу — так готовить и закупаться проще, чем 7 разных завтраков.
WEEKLY_VARIETY = 3

# Не покупаем
SKIP = {"Вода", "Вода/бульон"}

# Одинаковые продукты, записанные в рецептах по-разному
ALIASES = {
    "Огурец свежий": "Огурец",
    "Яйцо": "Яйца", "Яйца куриные": "Яйца",
    "Куриная грудка": "Куриное филе (грудка)", "Куриное филе": "Куриное филе (грудка)",
    "Индейка варёная": "Филе индейки",
    "Филе белой рыбы": "Рыба белая (филе)", "Филе рыбы": "Рыба белая (филе)",
    "Гречневая крупа": "Гречка",
    "Орехи грецкие": "Грецкие орехи",
    "Арахисовая паста натуральная": "Арахисовая паста",
    "Паста цельнозерновая": "Паста",
    "Молоко 1,5%": "Молоко", "Молоко 2%": "Молоко",
    "Масло": "Масло растительное", "Масло оливковое": "Оливковое масло",
    "Ржаной хлеб": "Хлеб ржаной",
    "Цукини": "Кабачок",
    "Соус томатный": "Томатный соус",
    "Укроп": "Зелень",
}

# Жидкости, которые удобнее считать в мл (1 ст.л. ≈ 15 мл, 1 ч.л. ≈ 5 мл)
LIQUIDS = {"Молоко", "Сливки 10%", "Соевый соус", "Оливковое масло",
           "Масло растительное", "Лимонный сок", "Уксус яблочный"}

UNIT_ALIASES = {"зубчик": "зубч.", "зубчика": "зубч.", "ломтик": "ломт.", "ломтика": "ломт.",
                "кусочек": "кус.", "листа": "лист."}
UNIT_ORDER = ["г", "мл", "шт", "зубч.", "ломт.", "кус.", "лист.", "ст.л.", "ч.л."]

# Разделы списка (как в магазине). Всё, чего нет в списках, попадёт в «Прочее».
SECTIONS = [
    ("🥩 Белковые продукты", ["Куриное филе (грудка)", "Куриные бёдра", "Филе индейки", "Говядина",
                            "Говядина на кости", "Говядина (стейк)", "Свинина", "Бекон", "Фарш мясной", "Лосось", "Треска",
                            "Форель", "Рыба белая (филе)",
                            "Креветки", "Тунец в собственном соку", "Тофу"]),
    ("🥛 Молочное, яйца, напитки", ["Яйца", "Творог 5%", "Творог 9%", "Молоко", "Сливки 10%", "Сметана 15%",
                            "Греческий йогурт 2%", "Йогурт натуральный 2%", "Кефир 1%", "Соевый напиток", "Творог 2%", "Сыр твёрдый",
                            "Пармезан", "Масло сливочное", "Сгущённое молоко"]),
    ("🥦 Овощи, фрукты, зелень", ["Авокадо", "Баклажан", "Банан", "Брокколи", "Кабачок", "Капуста",
                                  "Картофель", "Лимон", "Лук", "Лук красный", "Морковь", "Огурец",
                                  "Перец болгарский", "Помидор", "Салат овощной", "Салатный лист",
                                  "Свёкла", "Тыква", "Чеснок", "Шпинат свежий", "Стручковая фасоль", "Яблоко", "Груша", "Апельсин",
                                  "Виноград", "Ягоды", "Зелень"]),
    ("🌾 Крупы, хлеб, макароны", ["Овсяные хлопья", "Гречка", "Рис", "Киноа", "Манка", "Паста",
                                  "Булгур", "Манка", "Нут", "Чечевица красная", "Лапша рисовая", "Хлеб ржаной", "Хлеб цельнозерновой", "Хлебцы", "Тост", "Мука"]),
    ("🥜 Орехи, масла, соусы", ["Миндаль", "Грецкие орехи", "Орехи", "Тыквенные семечки", "Изюм", "Курага", "Арахисовая паста",
                                "Оливковое масло", "Масло растительное", "Соевый соус", "Томатный соус",
                                "Томатная паста", "Горчица", "Майонез лёгкий", "Уксус яблочный",
                                "Лимонный сок", "Мёд", "Сахар", "Хумус", "Протеиновый порошок"]),
]
OTHER_SECTION = "🍽 Прочее"
_SECTION_OF = {name: title for title, names in SECTIONS for name in names}

_FRACTIONS = {"½": 0.5, "¼": 0.25, "¾": 0.75}
_QTY_RE = re.compile(
    r"^\s*(?P<num>\d+(?:[.,]\d+)?)?\s*(?P<frac>[½¼¾])?\s*(?P<unit>[^\d(]*?)\s*(?:\((?P<note>[^)]*)\))?\s*$"
)


def parse_ingredient(line: str):
    """'Куриная грудка — 150 г' -> ('Куриное филе (грудка)', 150.0, 'г').
    Для 'по вкусу' / 'щепотка' количество и единица равны None."""
    if " — " not in line:
        return line.strip(), None, None
    raw_name, raw_qty = (part.strip() for part in line.split(" — ", 1))
    name = ALIASES.get(raw_name, raw_name)
    m = _QTY_RE.match(raw_qty)
    if not m or (m.group("num") is None and m.group("frac") is None):
        return name, None, None

    qty = float((m.group("num") or "0").replace(",", ".")) + _FRACTIONS.get(m.group("frac"), 0)
    unit = m.group("unit").strip() or "шт"
    unit = UNIT_ALIASES.get(unit, unit)
    note = m.group("note") or ""

    weight = re.search(r"(\d+(?:[.,]\d+)?)\s*(г|мл)\b", note)
    if unit not in ("г", "мл") and weight:
        # '2 ст.л. (32 г)' -> 32 г: точнее, чем считать ложки
        qty, unit = float(weight.group(1).replace(",", ".")), weight.group(2)
    elif unit == "дольки":
        qty, unit = qty / 8, "шт"  # 1 лимон ≈ 8 долек
    elif name in LIQUIDS and unit in ("ст.л.", "ч.л."):
        qty, unit = qty * (15 if unit == "ст.л." else 5), "мл"
    return name, qty, unit


def make_weekly_plan(menus: dict, days: int = 7, variety: int = WEEKLY_VARIETY, banned=()):
    """menus — отфильтрованное меню {категория: [строки блюд]}.
    Возвращает список дней [{категория: название блюда}, ...] или [] если рецептов не нашлось."""
    banned = _expand_banned(banned)
    chosen = {}
    for category in CATEGORIES:
        dishes = list(dict.fromkeys(
            dish for dish in (_find_dish(item) for item in menus.get(category, []))
            if dish and _is_allowed(dish, banned)
        ))
        if dishes:
            chosen[category] = random.sample(dishes, min(variety, len(dishes)))
    if not chosen:
        return []
    return [{cat: options[day % len(options)] for cat, options in chosen.items()} for day in range(days)]


def build_shopping_list(plan, portion_factor: float = 1.0):
    """По плану недели суммирует ингредиенты (с учётом множителя порции). Возвращает (разделы, из_запасов).
    разделы: [(заголовок, [(продукт, {единица: количество}), ...]), ...]"""
    counts = Counter(dish for day in plan for dish in day.values())
    totals = defaultdict(lambda: defaultdict(float))
    pantry = set()
    for dish, times in counts.items():
        recipe = RECIPES[dish]
        factor = times / (recipe.get("portions") or 1) * portion_factor  # в рецепте могло быть несколько порций
        for line in recipe["ingredients"]:
            name, qty, unit = parse_ingredient(line)
            if name in SKIP:
                continue
            if qty is None:
                pantry.update(part.strip().lower() for part in name.split(","))
                continue
            totals[name][unit] += qty * factor
    pantry -= {name.lower() for name in totals}

    grouped = defaultdict(list)
    for name in sorted(totals):
        grouped[_SECTION_OF.get(name, OTHER_SECTION)].append((name, dict(totals[name])))
    titles = [title for title, _ in SECTIONS] + [OTHER_SECTION]
    return [(title, grouped[title]) for title in titles if grouped.get(title)], sorted(pantry)


def _trim(x: float) -> str:
    text = f"{x:.1f}".replace(".", ",")
    return text[:-2] if text.endswith(",0") else text


def format_amount(total: float, unit: str) -> str:
    total = round(total, 6)  # без этого 150.00000000000003 превращается в лишние 5 г
    if unit in ("г", "мл"):
        total = math.ceil(total / 5) * 5
        if total >= 1000:
            return f"{_trim(total / 1000)} {'кг' if unit == 'г' else 'л'}"
        return f"{total} {unit}"
    return f"{math.ceil(total)} {unit}"


def _format_item(name: str, amounts: dict) -> str:
    order = sorted(amounts, key=lambda u: UNIT_ORDER.index(u) if u in UNIT_ORDER else len(UNIT_ORDER))
    return f"• {name} — " + " + ".join(format_amount(amounts[u], u) for u in order)


def format_shopping_list(sections, pantry, days: int = 7, portion_phrase: str = None) -> str:
    lines = [f"🛒 *Список покупок на {days} дней*", ""]
    for title, items in sections:
        lines.append(f"*{title}*")
        lines.extend(_format_item(name, amounts) for name, amounts in items)
        lines.append("")
    if pantry:
        lines.append("🧂 *Проверьте дома:* " + ", ".join(pantry))
        lines.append("")
    if portion_phrase:
        lines.append(f"Количества рассчитаны на 1 человека с учётом ваших порций ({portion_phrase}, чем в рецептах).")
    else:
        lines.append("Количества рассчитаны на 1 человека.")
    return "\n".join(lines)


def format_plan(plan) -> str:
    lines = ["📅 *Меню на неделю*", ""]
    for number, day in enumerate(plan, 1):
        lines.append(f"*День {number}*")
        lines.extend(f"{CATEGORY_EMOJI[cat]} {dish}" for cat, dish in day.items())
        lines.append("")
    missing = [cat for cat in CATEGORIES if cat not in plan[0]]
    if missing:
        names = ", ".join(missing)
        lines.append(f"⚠️ Для категорий ({names}) не нашлось блюд с рецептами под ваши ограничения — "
                     "в список они не вошли.")
    return "\n".join(lines).rstrip()


def split_message(text: str, limit: int = 4000):
    """Telegram не принимает сообщения длиннее 4096 символов — режем по строкам."""
    chunks, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def build_weekly_shopping(menus: dict, banned=(), days: int = 7, portion_factor: float = 1.0, portion_phrase: str = None):
    """Главная функция для бота: (текст_списка, текст_меню_на_неделю) или None, если рецептов нет."""
    plan = make_weekly_plan(menus, days=days, banned=banned)
    if not plan:
        return None
    sections, pantry = build_shopping_list(plan, portion_factor)
    shopping_text = format_shopping_list(sections, pantry, days, portion_phrase)
    missing = [cat for cat in CATEGORIES if cat not in plan[0]]
    if missing:
        # при строгой диете или аллергиях рецептов может не хватать — не делаем вид, что список полный
        shopping_text += f"\n⚠️ В списке нет блюд на: {', '.join(missing)}. Под ваши ограничения для этого пока нет рецептов."
    return shopping_text, format_plan(plan)
