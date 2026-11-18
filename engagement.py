"""Вовлечение: серия дней с дневником, итоги недели по питанию, вечернее напоминание о дневнике."""

from datetime import timedelta

from database import (
    get_diary_day_totals, get_diary_streak, used_diary_recently, get_diary, update_user,
    user_today, _days_word,
)

# На каких днях серии поздравляем (один раз за день)
MILESTONES = (3, 7, 14, 30, 60, 100, 200, 365)


def streak_line(user_id: int) -> str:
    """Строка для экрана дневника: «🔥 Серия: 5 дней подряд». Пусто, если серии нет."""
    streak, done_today = get_diary_streak(user_id)
    if streak < 2:
        return ""
    if done_today:
        return f"🔥 Серия: {streak} {_days_word(streak)} подряд"
    return f"🔥 Серия: {streak} {_days_word(streak)} подряд — запишите что-нибудь сегодня, чтобы продолжить"


def milestone_message(user: dict):
    """Поздравление, если сегодня серия достигла круглого числа. Сообщает не чаще раза в день."""
    streak, done_today = get_diary_streak(user["user_id"])
    if not done_today or streak not in MILESTONES:
        return None
    today = user_today(user).strftime("%Y-%m-%d")
    if user.get("diary_cheer_day") == today:
        return None
    update_user(user["user_id"], diary_cheer_day=today)
    return f"🔥 *{streak} {_days_word(streak)} подряд ведёте дневник — отличный результат!*\nПривычка, которая действительно работает. Так держать 💚"


def weekly_food_block(user: dict, norm: int) -> str:
    """Блок итогов недели по питанию для воскресного сообщения. Пусто, если записей не было."""
    today = user_today(user)
    since = (today - timedelta(days=6)).strftime("%Y-%m-%d")
    days = get_diary_day_totals(user["user_id"], since)
    if not days:
        return ""
    logged = len(days)
    avg_kcal = round(sum(d["kcal"] for d in days.values()) / logged)
    lines = [f"📓 Дневник: {logged} из 7 дней", f"🍽 В среднем {avg_kcal} ккал в день (норма {norm})"]
    if all(d["protein"] or d["fat"] or d["carb"] for d in days.values()):
        p = round(sum(d["protein"] for d in days.values()) / logged)
        f = round(sum(d["fat"] for d in days.values()) / logged)
        c = round(sum(d["carb"] for d in days.values()) / logged)
        lines.append(f"Б {p} · Ж {f} · У {c} г в день")
    within = sum(1 for d in days.values() if abs(d["kcal"] - norm) <= norm * 0.1)
    if logged >= 3:
        lines.append(f"🎯 В пределах нормы (±10%): {within} из {logged} дней")
    streak, _ = get_diary_streak(user["user_id"])
    if streak >= 2:
        lines.append(f"🔥 Серия дневника: {streak} {_days_word(streak)} подряд")
    if logged < 7:
        lines.append("_Дней без записей данные неполные: средние посчитаны по тем дням, что вы записали._")
    return "\n".join(lines)


def diary_reminder(user: dict):
    """(текст, нужно_ли_отправлять) вечернего напоминания о дневнике."""
    user_id = user["user_id"]
    if not user.get("reminder_diary_enabled", 1):
        return None
    if get_diary(user_id):  # сегодня уже есть записи
        return None
    if not used_diary_recently(user_id):  # не навязываемся тем, кто дневником не пользуется
        return None
    streak, _ = get_diary_streak(user_id)
    if streak >= 2:
        return (f"📓 *Серия {streak} {_days_word(streak)} под угрозой!*\n\n"
                "Сегодня в дневнике пусто. Запишите хотя бы один приём пищи — это минута.")
    return "📓 *Что сегодня ели?*\n\nЗапишите в дневник — так итоги дня и недели будут точными. Это займёт минуту."
