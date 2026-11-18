"""Утреннее сообщение «План дня»: норма, привычки, серия и меню на сегодня."""

import json
import random
import re
from datetime import datetime

import pytz

from database import _days_word, get_habit_streak, user_today, user_now
from dietfilter import find_dish
from menu_generator import get_menu_with_recipe_kb, user_calorie_norm
from portions import candidate_lines, kcal_of, portion_info

MSK = pytz.timezone("Europe/Moscow")

MEALS = [("завтрак", "🌅", "Завтрак"), ("обед", "☀️", "Обед"), ("перекус", "🌤", "Перекус"), ("ужин", "🌙", "Ужин")]

# Если блюда дня ближе к норме, чем на столько ккал, считаем, что они её закрывают
NORM_TOLERANCE_KCAL = 150


def _md_escape(text) -> str:
    out = str(text)
    for ch in ("\\", "_", "*", "`", "["):
        out = out.replace(ch, "\\" + ch)
    return out


def pick_daily_menu(user_id: int, candidates: dict, day) -> dict:
    """По одному блюду на приём пищи. У каждого пользователя своя перетасованная очередь блюд на каждый
    приём, по которой идём по дням: блюдо не повторяется два дня подряд, а все варианты показываются
    равномерно. Выбор стабилен в пределах дня и не требует хранить историю."""
    chosen = {}
    for category, _, _ in MEALS:
        options = sorted(candidates.get(category) or [])
        if not options:
            continue
        random.Random(f"{user_id}:{category}").shuffle(options)
        chosen[category] = options[day.toordinal() % len(options)]
    return chosen


def _recipe_callback(category: str, line: str):
    """callback_data кнопки «Рецепт» — тот же, что у кнопок на экране меню."""
    for _, _, keyboard in get_menu_with_recipe_kb({category: [line]}):
        for row in keyboard.keyboard:
            for button in row:
                if (button.callback_data or "").startswith("recipe_"):
                    return button.callback_data
    return None


def build_daily_plan(user: dict, day=None):
    """Текст «Плана дня» и кнопки рецептов: {"text": str, "recipe_buttons": [(подпись, callback_data)]}.
    None, если у пользователя нет данных для плана или подходящих блюд."""
    weight = user.get("weight")
    if not weight:
        return None
    candidates = candidate_lines(user)
    if not candidates:
        return None
    day = day or user_today(user)
    picks = pick_daily_menu(user["user_id"], candidates, day)
    if not picks:
        return None

    norm = user_calorie_norm(user)
    portions = portion_info(user)

    name = _md_escape(user.get("name") or "")
    hour = user_now(user).hour
    greeting = "☀️ Доброе утро" if 5 <= hour < 12 else "🌤 Добрый день" if 12 <= hour < 18 else "🌙 Добрый вечер" if hour >= 18 else "🌙 Доброй ночи"
    lines = [f"*{greeting}{', ' + name if name else ''}!*", "", "*План на сегодня*"]
    lines.append(f"🔥 Норма калорий: {norm} ккал")
    try:
        habits = json.loads(user.get("habits") or "[]")
    except ValueError:
        habits = []
    if habits:
        lines.append(f"✅ Привычки: {len(habits)} на сегодня")
    streak = get_habit_streak(user["user_id"])
    if streak > 0:
        lines.append(f"🏅 Серия привычек: {streak} {_days_word(streak)}")

    lines += ["", "*Меню на сегодня*"]
    total, complete = 0, True
    for category, emoji, title in MEALS:
        line = picks.get(category)
        if not line:
            complete = False
            continue
        kcal = kcal_of(line)
        total += kcal or 0
        lines.append(f"{emoji} {title}: {find_dish(line)}" + (f" (~{kcal} ккал)" if kcal else ""))

    # Блюда в рецептах рассчитаны на стандартную порцию и дают меньше нормы — говорим, во сколько раз порции больше
    if complete:
        lines.append("")
        if portions["phrase"]:
            scaled = int(round(total * portions["factor"], -1))
            text = f"📏 Порции: делайте {portions['phrase']}, чем в рецептах — так день выйдет примерно на {scaled} ккал."
            if portions["capped"]:
                text += " Если остаётся голод, добавьте гарнир, фрукты или молочные продукты."
            lines.append(text)
        elif abs(total - norm) <= NORM_TOLERANCE_KCAL:
            lines.append("Эти блюда примерно закрывают вашу норму.")

    buttons = []
    for category, _, title in MEALS:
        line = picks.get(category)
        callback = _recipe_callback(category, line) if line else None
        if callback:
            buttons.append((f"📖 {title}", callback))
    return {"text": "\n".join(lines), "recipe_buttons": buttons}


def plan_keyboard(plan: dict, plan_enabled: bool = True):
    """Кнопки под «Планом дня»: рецепты, список покупок, включение/отключение утренней рассылки, пауза."""
    from telebot import types
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(*[types.InlineKeyboardButton(label, callback_data=data) for label, data in plan['recipe_buttons']])
    kb.add(types.InlineKeyboardButton("🛒 Список покупок на неделю", callback_data="shop_regen"))
    if plan_enabled:
        kb.add(types.InlineKeyboardButton("🔕 Отключить план дня", callback_data="plan_off"))
    else:
        kb.add(types.InlineKeyboardButton("🔔 Присылать каждое утро", callback_data="plan_on"))
    kb.add(types.InlineKeyboardButton("⏸ Пауза", callback_data="pause_menu"))
    return kb
