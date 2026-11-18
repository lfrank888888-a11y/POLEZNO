"""Генератор меню и калькуляторы для ЗОЖ v6.5"""

import hashlib
import random
from telebot import types
from config import SAMPLE_MENUS, ALLERGEN_KEYWORDS, DIET_FILTERS

# Привычки по умолчанию (задаются здесь, а не в config.py); пользователь может удалять и добавлять свои
DEFAULT_HABITS = [
    "Прогулка 30 минут",
    "Лечь спать до 23:00",
    "Отказаться от сахара",
]
from menu_extra import merge_extra_menus
from norms import min_daily_calories
from dietfilter import expand_banned, find_dish, ingredients_allowed

# Добавляем веганские и вегетарианские блюда к меню из config.py (повторный вызов безопасен)
merge_extra_menus(SAMPLE_MENUS)


def calculate_bmi(weight: float, height: float) -> dict:
    height_m = height / 100
    bmi = weight / (height_m ** 2)
    if bmi < 18.5:
        status = "Недостаточный вес"
    elif 18.5 <= bmi < 25:
        status = "Норма"
    elif 25 <= bmi < 30:
        status = "Избыточный вес"
    else:
        status = "Ожирение"
    return {"bmi": round(bmi, 1), "status": status}


# Рекомендация зависит от ДВУХ вещей: медицинского статуса по ИМТ и того, чего
# человек хочет добиться. Раньше рекомендация смотрела только на ИМТ и могла
# прямо противоречить цели пользователя — например, говорить "поддерживайте
# текущий вес" тому, кто явно пришёл за набором массы.
BMI_GOAL_RECOMMENDATIONS = {
    ("Недостаточный вес", "похудение"): "Ваш вес уже ниже нормы — перед дальнейшим похудением обязательно проконсультируйтесь со специалистом, дальнейшее снижение веса может быть небезопасным.",
    ("Недостаточный вес", "поддержание"): "Ваш вес ниже нормы — для определения здорового ориентира стоит проконсультироваться со специалистом.",
    ("Недостаточный вес", "набор"): "Рекомендуется консультация диетолога для набора массы.",
    ("Норма", "похудение"): "Вес в норме — худейте аккуратно, небольшими шагами, и следите за самочувствием.",
    ("Норма", "поддержание"): "Отличный результат! Поддерживайте текущий вес.",
    ("Норма", "набор"): "ИМТ в норме — можно постепенно набирать массу, ориентируясь на умеренный профицит калорий.",
    ("Избыточный вес", "похудение"): "Рекомендуется сбалансированное питание и физическая активность для постепенного снижения веса.",
    ("Избыточный вес", "поддержание"): "Рекомендуется сбалансированное питание и физическая активность.",
    ("Избыточный вес", "набор"): "Вес уже выше нормы — перед набором массы стоит проконсультироваться со специалистом, чтобы не набирать лишний жир.",
    ("Ожирение", "похудение"): "Рекомендуется консультация врача для разработки плана снижения веса.",
    ("Ожирение", "поддержание"): "Рекомендуется консультация врача для разработки плана снижения веса.",
    ("Ожирение", "набор"): "При таком ИМТ набор массы обычно не рекомендуется без консультации врача.",
}


def get_bmi_recommendation(bmi_status: str, goal: str) -> str:
    return BMI_GOAL_RECOMMENDATIONS.get((bmi_status, goal), "Рекомендуется консультация специалиста для уточнения плана питания.")


def calculate_daily_calories(*args, **kwargs) -> int:
    """
    Универсальная формула. Поддерживает два формата вызова:
    Старый (Миффлин-Сан Жеор): calculate_daily_calories(age, weight, height, goal, gender="female")
    Новый (Кэтч-МакАрдл):      calculate_daily_calories(weight, body_fat, goal, activity_level)
    """
    # Старый формат: 4+ аргументов, второй аргумент — это вес (обычно 30–300)
    # Новый формат: 4 аргумента, второй — % жира (3–60)
    if len(args) >= 4:
        # Если второй аргумент > 100 — это скорее всего рост (см), значит старый формат
        if isinstance(args[1], (int, float)) and args[1] > 100:
            age, weight, height, goal = args[0], args[1], args[2], args[3]
            gender = kwargs.get('gender', 'female')
            if gender == "male":
                bmr = 10 * weight + 6.25 * height - 5 * age + 5
            else:
                bmr = 10 * weight + 6.25 * height - 5 * age - 161
            tdee = bmr * 1.375
            if goal == "похудение":
                tdee -= 500
            elif goal == "набор":
                tdee += 300
            return int(tdee)
        else:
            # Новый формат Кэтч-МакАрдл
            weight, body_fat, goal = args[0], args[1], args[2]
            activity_level = args[3] if len(args) > 3 else kwargs.get('activity_level', 'легкий')
            lean_mass = weight * (1 - body_fat / 100)
            bmr = 370 + (21.6 * lean_mass)
            from config import ACTIVITY_MULTIPLIERS
            multiplier = ACTIVITY_MULTIPLIERS.get(activity_level, 1.375)
            tdee = bmr * multiplier
            if goal == "похудение":
                tdee -= 500
            elif goal == "набор":
                tdee += 300
            return int(tdee)
    # fallback
    return 2000


def calorie_norm_info(weight, body_fat, goal, activity_level, adjustment=0):
    """Суточная норма с учётом коррекции и нижней границы: (норма, ограничена_ли_границей).
    Граница — базовый обмен, но не меньше 1200 ккал: без неё небольшим малоподвижным людям
    формула с дефицитом давала слишком низкую норму."""
    body_fat = body_fat or 20
    raw = calculate_daily_calories(weight, body_fat, goal, activity_level or 'легкий') + (adjustment or 0)
    floor = min_daily_calories(weight, body_fat)
    return (floor, True) if raw < floor else (int(raw), False)


def daily_calorie_norm(weight, body_fat, goal, activity_level, adjustment=0) -> int:
    return calorie_norm_info(weight, body_fat, goal, activity_level, adjustment)[0]


def user_calorie_norm(user: dict, goal: str = None, with_adjustment: bool = True) -> int:
    """Норма пользователя из его профиля (цель можно переопределить, коррекцию — отключить)."""
    return daily_calorie_norm(user['weight'], user.get('body_fat'), goal or user.get('goal') or 'похудение',
                              user.get('activity_level'), (user.get('calorie_adjustment') or 0) if with_adjustment else 0)


def get_all_menus(goal: str) -> dict:
    key = goal if goal in SAMPLE_MENUS else "похудение"
    base = SAMPLE_MENUS[key]
    try:
        from menu_nutrition import rewrite_menus  # калорийность блюд — по рецептам (см. menu_nutrition.py)
        return rewrite_menus(base, key)
    except Exception:
        return base


def get_banned_keywords(diet_type: str, allergies: str) -> set:
    """Слова-маркеры продуктов, которые не подходят по типу питания и аллергиям."""
    banned = set()
    if diet_type in DIET_FILTERS:
        for key in DIET_FILTERS[diet_type]:
            if key in ALLERGEN_KEYWORDS:
                banned.update(ALLERGEN_KEYWORDS[key])
    if allergies:
        for allergy in allergies.split(","):
            allergy = allergy.strip().lower()
            if allergy in ALLERGEN_KEYWORDS:
                banned.update(ALLERGEN_KEYWORDS[allergy])
    return banned


# Ключевые слова в config стоят в именительном падеже: "рыба" не найдёт "рыбы",
# "овсянка" — "овсяные хлопья", "молоко" — "масло сливочное". Для проверки
# ингредиентов добавляем корни слов к тем группам, которых это касается.
INGREDIENT_STEMS = {
    "молоко": ["сливочн", "пармезан", "молоч"],
    "рыба": ["рыб", "лосос", "треск", "тунец", "креветк", "форел"],
    "мясо": ["фарш", "мясн", "свинин", "говяд", "куриц", "курин", "индей", "бёдр"],
    "глютен": ["манк", "овсян", "хлопья", "макарон", "булгур"],
}


def get_ingredient_banned_keywords(diet_type: str, allergies: str) -> set:
    """То же, что get_banned_keywords, плюс корни слов — для проверки состава блюд (список покупок)."""
    keys = set(DIET_FILTERS.get(diet_type, []))
    if allergies:
        keys.update(a.strip().lower() for a in allergies.split(","))
    banned = get_banned_keywords(diet_type, allergies)
    for key in keys:
        banned.update(INGREDIENT_STEMS.get(key, []))
    return banned


def filter_menu(menu_dict: dict, diet_type: str, allergies: str) -> dict:
    banned = expand_banned(get_banned_keywords(diet_type, allergies))
    filtered = {}
    for category, items in menu_dict.items():
        filtered_items = []
        for item in items:
            item_lower = item.lower()
            if any(b in item_lower for b in banned):
                continue
            dish = find_dish(item)
            if dish and not ingredients_allowed(dish, banned):
                continue  # название чистое, но в рецепте есть запрещённый продукт
            filtered_items.append(item)
        if not filtered_items:
            filtered_items = ["🍽 Нет подходящих вариантов под ваши ограничения"]
        filtered[category] = filtered_items
    return filtered


def _macro_suffix(option: str) -> str:
    """Строка «Б · Ж · У» под блюдом меню (пусто, если состав блюда не рассчитан)."""
    try:
        from menu_nutrition import macro_for_line
        macro = macro_for_line(option)
        return f"_{macro}_" if macro else ""
    except Exception:
        return ""


def format_menu_dict(menus: dict) -> str:
    lines = []
    emoji_map = {"завтрак": "🌅", "обед": "☀️", "перекус": "🌤", "ужин": "🌙"}
    for category in ["завтрак", "обед", "перекус", "ужин"]:
        options = menus.get(category, [])
        emoji = emoji_map.get(category, "🍽")
        lines.append(f"{emoji} *{category.capitalize()}* — выберите один вариант:")
        for i, option in enumerate(options, 1):
            lines.append(f"   {i}. {option}")
            macro = _macro_suffix(option)
            if macro:
                lines.append(f"      {macro}")
        lines.append("")
    return "\n".join(lines)


def format_all_menus(goal: str) -> str:
    menus = get_all_menus(goal)
    return format_menu_dict(menus)


def sample_menu(menus: dict, size: int = 4) -> dict:
    """Берёт до `size` случайных вариантов из каждой категории отфильтрованного
    меню. Пул блюд в SAMPLE_MENUS обычно больше size — это даёт кнопке
    "🔄 Другие варианты" реально из чего выбирать."""
    return {category: random.sample(options, min(size, len(options))) for category, options in menus.items()}


def get_menu_with_recipe_kb(menus: dict) -> list:
    result = []
    emoji_map = {"завтрак": "🌅", "обед": "☀️", "перекус": "🌤", "ужин": "🌙"}
    for category in ["завтрак", "обед", "перекус", "ужин"]:
        options = menus.get(category, [])
        emoji = emoji_map.get(category, "🍽")
        text_lines = [f"{emoji} *{category.capitalize()}* — выберите один вариант:"]
        for i, option in enumerate(options, 1):
            text_lines.append(f"   {i}. {option}")
            macro = _macro_suffix(option)
            if macro:
                text_lines.append(f"      {macro}")
        text = "\n".join(text_lines)
        kb = types.InlineKeyboardMarkup(row_width=2)
        buttons = []
        for option in options:
            clean_name = option.split("—")[0].strip()
            clean_name = "".join(c for c in clean_name if not (0x1F300 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF or ord(c) == 0xFE0F)).strip()
            if clean_name and clean_name != "Нет подходящих вариантов под ваши ограничения":
                short_hash = hashlib.md5(clean_name.encode('utf-8')).hexdigest()[:8]
                label = clean_name if len(clean_name) <= 28 else clean_name[:27] + "…"
                buttons.append(types.InlineKeyboardButton(f"📖 {label}", callback_data=f"recipe_{short_hash}"))
        if buttons:
            kb.add(*buttons)
        kb.add(types.InlineKeyboardButton("🔄 Другие варианты", callback_data=f"reshuffle_{category}"))
        result.append((category, text, kb))
    return result


def get_default_habits() -> list:
    return DEFAULT_HABITS.copy()


def get_progress_bar(current: int, total: int, length: int = 10) -> str:
    filled = int(length * current / total) if total > 0 else 0
    bar = "█" * filled + "░" * (length - filled)
    percent = int(100 * current / total) if total > 0 else 0
    return f"[{bar}] {percent}%"


# === Расчёт целевого распределения БЖУ ===
# Белки и углеводы дают 4 ккал/г, жиры — 9 ккал/г.
#
# Белок считается от веса тела (г/кг), а не от процента калорий — так делают
# в спортивном питании на практике, и это не даёт белку "раздуваться" или
# "сжиматься" неестественно при обычных сочетаниях веса/калорийности:
#   похудение    — 2.2 г/кг (выше, чтобы сохранить мышцы при дефиците калорий)
#   поддержание  — 1.6 г/кг
#   набор        — 1.8 г/кг (умеренно выше поддержания, упор на углеводы для энергии)
#
# Жиры — фиксированная доля от общей калорийности (25%, в рамках общепринятых
# 20–35%). Углеводы получают всё, что остаётся от калорий после белка и жиров.
PROTEIN_PER_KG = {
    "похудение": 2.2,
    "поддержание": 1.6,
    "набор": 1.8,
}
FAT_PERCENT_OF_CALORIES = 0.25

# Потолок белка. Расчёт от общего веса тела завышает белок у тяжёлых людей (110 кг × 2,2 = 242 г) и при
# невысокой калорийности делает его долю в рационе слишком большой. Поэтому белок не больше PROTEIN_MAX_G
# граммов и не больше PROTEIN_MAX_CALORIE_SHARE от калорий (35% — верхняя граница общепринятого
# диапазона 10–35% калорий).
PROTEIN_MAX_G = 200
PROTEIN_MAX_CALORIE_SHARE = 0.35


def calculate_macros(calories: int, weight: float, goal: str) -> dict:
    """Целевая суточная норма БЖУ в граммах на основе калорийности, веса тела и цели."""
    protein_coef = PROTEIN_PER_KG.get(goal, PROTEIN_PER_KG["поддержание"])
    protein_g = min(round(weight * protein_coef), PROTEIN_MAX_G, round(calories * PROTEIN_MAX_CALORIE_SHARE / 4))
    fat_g = round((calories * FAT_PERCENT_OF_CALORIES) / 9)

    remaining_kcal = calories - (protein_g * 4) - (fat_g * 9)
    # Если веса много, а калорийность низкая (жёсткий дефицит) — белок и жиры
    # сами по себе могут занять почти всю калорийность. Не даём углеводам уйти
    # в минус, оставляем разумный минимум.
    carbs_g = max(round(remaining_kcal / 4), 50)

    return {"protein": protein_g, "fat": fat_g, "carbs": carbs_g}


def format_macros(macros: dict) -> str:
    return f"🥩 Белки: {macros['protein']} г  •  🥑 Жиры: {macros['fat']} г  •  🍞 Углеводы: {macros['carbs']} г"
