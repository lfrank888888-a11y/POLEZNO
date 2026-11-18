"""Дневник еды без ИИ: запись блюд из меню бота (с порцией) или вручную, подсчёт калорий за день."""

import re

from database import get_diary
from engagement import streak_line
import nutrients as nutrient_utils
import offsearch
from dailyplan import MEALS, _md_escape
from dietfilter import find_dish
from menu_generator import get_progress_bar, user_calorie_norm
from portions import candidate_lines, kcal_of

PAGE_SIZE = 8
PORTIONS = [(50, "½ порции"), (100, "1 порция"), (150, "1½ порции"), (200, "2 порции")]


def parse_manual(text: str):
    """«Яблоко 80», «омлет, 250 ккал» -> ("Яблоко", 80). None, если калории не найдены или вне 1–5000."""
    text = (text or "").strip()
    match = re.search(r"(\d{1,4})\s*(?:ккал|kcal|калорий|кал)?\.?\s*$", text, re.IGNORECASE)
    if not match:
        return None
    kcal = int(match.group(1))
    if not 1 <= kcal <= 5000:
        return None
    title = text[:match.start()].strip(" ,-—:;") or "Запись"
    return title[:60], kcal


def category_dishes(user: dict, category_index: int) -> list:
    """Строки меню выбранного приёма пищи, подходящие пользователю (порядок стабилен)."""
    category = MEALS[category_index][0]
    return sorted(candidate_lines(user).get(category, []))


def screen(user: dict, types):
    """Текст и клавиатура главного экрана дневника."""
    user_id = user["user_id"]
    entries = get_diary(user_id)
    norm = user_calorie_norm(user)
    total = sum(e["kcal"] for e in entries)
    if entries:
        lines = [f"{i}. {_md_escape(e['title'])} — {e['kcal']} ккал" for i, e in enumerate(entries, 1)]
        body = "\n".join(lines) + f"\n\n🔥 Итого: *{total}* из {norm} ккал\n{get_progress_bar(total, norm)}"
        if total > norm:
            body += f"\nНорма превышена на {total - norm} ккал — это нормально, один день не решает."
        else:
            body += f"\nОсталось: {norm - total} ккал"
        body += nutrient_utils.summary_block(entries)
    else:
        body = "Сегодня записей пока нет. Добавьте блюдо, которое съели 👇"
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("➕ Блюдо из меню бота", callback_data="dm"),
           types.InlineKeyboardButton("✏️ Написать, что съел(а)", callback_data="dman"),
           types.InlineKeyboardButton("📷 Штрихкод упаковки", callback_data="dbar"))
    if offsearch.available():
        kb.add(types.InlineKeyboardButton("🔎 Найти продукт из магазина", callback_data="dfind"))
    if any(e.get("nutrients") for e in entries):
        kb.add(types.InlineKeyboardButton("🔬 Подробнее: витамины и минералы", callback_data="dnut"))
    if entries:
        kb.add(types.InlineKeyboardButton("🗑 Удалить запись", callback_data="ddm"))
    streak = streak_line(user_id)
    streak_block = f"{streak}\n\n" if streak else ""
    return f"📓 *Дневник еды — сегодня*\n\n{streak_block}{body}", kb


def meal_kb(types):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(*[types.InlineKeyboardButton(f"{emoji} {title}", callback_data=f"dl_{i}_0") for i, (_, emoji, title) in enumerate(MEALS)])
    kb.add(types.InlineKeyboardButton("← Назад", callback_data="dback"))
    return kb


def dishes_kb(user: dict, category_index: int, page: int, types):
    dishes = category_dishes(user, category_index)
    pages = max(1, -(-len(dishes) // PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    kb = types.InlineKeyboardMarkup(row_width=1)
    for offset, line in enumerate(dishes[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]):
        index = page * PAGE_SIZE + offset
        kcal = kcal_of(line)
        kb.add(types.InlineKeyboardButton(f"{find_dish(line)}" + (f" · {kcal} ккал" if kcal else ""), callback_data=f"dp_{category_index}_{index}"))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀️", callback_data=f"dl_{category_index}_{page - 1}"))
    if page < pages - 1:
        nav.append(types.InlineKeyboardButton("▶️", callback_data=f"dl_{category_index}_{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.add(types.InlineKeyboardButton("← К приёмам пищи", callback_data="dm"))
    return kb, page, pages


def portion_kb(category_index: int, index: int, types):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(*[types.InlineKeyboardButton(label, callback_data=f"dq_{category_index}_{index}_{percent}") for percent, label in PORTIONS])
    kb.add(types.InlineKeyboardButton("← Назад", callback_data=f"dl_{category_index}_0"))
    return kb


def delete_kb(user_id: int, types):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, e in enumerate(get_diary(user_id), 1):
        kb.add(types.InlineKeyboardButton(f"🗑 {i}. {e['title']} — {e['kcal']} ккал", callback_data=f"dd_{e['id']}"))
    kb.add(types.InlineKeyboardButton("← Назад", callback_data="dback"))
    return kb
