"""Модуль напоминаний (APScheduler)"""

import logging
import sqlite3
import time
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from telebot import types
import pytz
from config import DB_PATH
from database import is_paused, pause_touches_today, user_now, get_top_misses, purge_old_misses

logger = logging.getLogger(__name__)


def init_reminders(bot):
    # Задачи запускаются каждый час (или каждые полчаса) и сами выбирают тех, у кого по МЕСТНОМУ времени
    # сейчас подходящий момент: так у каждого пользователя свой часовой пояс.
    scheduler = BackgroundScheduler(timezone="Europe/Moscow")
    # план дня в 08:30 местного времени, чтобы не совпадать со взвешиванием в 08:00
    scheduler.add_job(lambda: send_daily_plans(bot), trigger='cron', minute=30, id='daily_plans', replace_existing=True)
    scheduler.add_job(lambda: send_weight_reminders(bot), trigger='cron', minute='0,30', id='weight_reminders', replace_existing=True)
    scheduler.add_job(lambda: send_habits_reminders(bot), trigger='cron', minute='0,30', id='habits_reminders', replace_existing=True)
    scheduler.add_job(lambda: send_weekly_reports(bot), trigger='cron', minute=0, id='weekly_reports', replace_existing=True)
    scheduler.add_job(lambda: send_misses_digest(bot), trigger='cron', day_of_week='mon', hour=10, minute=15, id='misses_digest', replace_existing=True)
    scheduler.add_job(purge_old_misses, trigger='cron', hour=3, minute=40, id='purge_misses', replace_existing=True)
    scheduler.add_job(lambda: __import__('backup').run_scheduled(bot), trigger='cron', hour=4, minute=10, id='db_backup', replace_existing=True)
    scheduler.add_job(lambda: send_diary_reminders(bot), trigger='cron', minute='0,30', id='diary_reminders', replace_existing=True)
    scheduler.add_job(lambda: send_expiry_notices(bot), trigger='cron', minute=20, id='expiry_notices', replace_existing=True)
    scheduler.add_job(lambda: send_pause_endings(bot), trigger='cron', minute=5, id='pause_endings', replace_existing=True)
    scheduler.add_job(daily_rollover, trigger='cron', minute=59, id='daily_rollover', replace_existing=True)
    scheduler.add_job(lambda: check_pending_payments(bot), trigger='interval', minutes=5, id='payment_autocheck', replace_existing=True)
    scheduler.start()
    logger.info("✅ Планировщик напоминаний запущен")
    return scheduler


def _local_slot(user, now=None):
    """(местное время пользователя, «слот» (час, 0 или 30)) — слот соответствует запуску задачи в :00 или :30."""
    local = user_now(user) if now is None else now
    return local, (local.hour, 0 if local.minute < 30 else 30)


def _setting_slot(value, default):
    """'08:30' -> (8, 30); значения вида 08:15 округляются вниз до получаса."""
    try:
        h, m = str(value or default).split(":")
        return int(h), 0 if int(m) < 30 else 30
    except ValueError:
        h, m = default.split(":")
        return int(h), int(m)


def _pause_kb():
    """Кнопка «Пауза» под напоминаниями: открывает выбор срока."""
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⏸ Пауза", callback_data="pause_menu"))
    return kb


def _habits_kb(habits, completed):
    """Кнопки под вечерним чек-ином: невыполненные привычки (нажатие отмечает) и вход в полный список."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    pending = [(i, h) for i, h in enumerate(habits) if i not in completed]
    for i, habit in pending[:4]:
        label = habit if len(habit) <= 40 else habit[:39] + "…"
        kb.add(types.InlineKeyboardButton(f"✅ {label}", callback_data=f"habit_{i}"))
    if len(pending) > 4:
        kb.add(types.InlineKeyboardButton(f"📋 Все привычки (ещё {len(pending) - 4})", callback_data="habits_open"))
    kb.add(types.InlineKeyboardButton("⏸ Пауза", callback_data="pause_menu"))
    return kb


def _moscow_hour() -> int:
    return datetime.now(pytz.timezone("Europe/Moscow")).hour


def _get_active_users():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE is_active = 1")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def send_pause_endings(bot):
    """Раз в час: пишем «пауза закончилась» тем, чья пауза истекла сама (не ночью, с 8 до 22 по Москве)."""
    from database import update_user, get_habit_streak, _days_word
    now_msk = datetime.now(pytz.timezone("Europe/Moscow")).replace(tzinfo=None)
    for user in _get_active_users():
        until = user.get('paused_until')
        if not until or user.get('pause_notified', 1) != 0:
            continue
        if not 8 <= user_now(user).hour < 22:  # не пишем ночью по местному времени
            continue
        try:
            if datetime.fromisoformat(until) > now_msk:
                continue
        except ValueError:
            continue
        try:
            streak = get_habit_streak(user['user_id'])
            streak_line = f"\n🏅 Серия привычек сохранена: {streak} {_days_word(streak)}." if streak > 0 else ""
            bot.send_message(user['user_id'], f"👋 *Пауза закончилась — с возвращением!*\n\nНапоминания и план дня снова приходят.{streak_line}")
        except Exception as e:
            logger.error(f"Ошибка сообщения об окончании паузы {user['user_id']}: {e}")
        finally:
            update_user(user['user_id'], pause_notified=1)


def _renew_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💳 Продлить подписку", callback_data="renew_open"))
    return kb


def send_expiry_notices(bot):
    """Предупреждения об окончании подписки (за 3 дня, за 1 день) и сообщение после окончания — по одному разу
    на каждый срок подписки, днём по местному времени. Кнопка ведёт сразу к оплате."""
    from datetime import timedelta
    from database import update_user
    now = datetime.now()
    for user in _get_active_users():
        end_raw = user.get('subscription_end')
        if not end_raw or is_paused(user) or not 10 <= user_now(user).hour < 21:
            continue
        try:
            end = datetime.fromisoformat(end_raw)
            created = datetime.fromisoformat(user['created_at']) if user.get('created_at') else end
        except ValueError:
            continue
        stage = user.get('expiry_stage') or 0
        if user.get('expiry_for') != end_raw:  # срок изменился (оплата, подарок, пауза): начинаем отсчёт заново
            stage = 0
        left = end - now
        text, new_stage = None, stage
        if left <= timedelta(0):
            if stage < 3 and left > timedelta(days=-3):
                text = "⏳ *Подписка закончилась*\n\nЧтобы вернуть меню, план дня и напоминания, продлите подписку 👇"
                new_stage = 3
        elif left <= timedelta(days=1):
            if stage < 2:
                text, new_stage = "⏳ *Подписка заканчивается завтра*\n\nПродлите заранее — оплаченные дни добавятся к текущему сроку, ничего не потеряется.", 2
        elif left <= timedelta(days=3):
            if stage < 1 and (end - created) >= timedelta(days=5):  # короткий пробный период не пугаем сразу после старта
                text, new_stage = f"⏳ *Подписка заканчивается через {left.days + 1} дн.*\n\nМожно продлить уже сейчас — оплаченные дни добавятся к текущему сроку.", 1
        if text is None:
            if user.get('expiry_for') != end_raw:
                update_user(user['user_id'], expiry_for=end_raw, expiry_stage=stage)
            continue
        try:
            bot.send_message(user['user_id'], text, reply_markup=_renew_kb())
            time.sleep(0.05)
        except Exception as e:
            logger.error(f"Ошибка напоминания об окончании подписки {user['user_id']}: {e}")
        update_user(user['user_id'], expiry_for=end_raw, expiry_stage=new_stage)


def daily_rollover():
    """Каждый час в :59 (закрывает день у тех, у кого по местному времени 23:59): записывает итоги дня в daily_logs и обнуляет
    привычки. Без этого статистика, итоги недели и серии не копятся,
    а отметки привычек переходят на следующий день."""
    logger.info("🌙 Закрытие дня: запись итогов и сброс...")
    import json
    from database import log_day, reset_habits
    for user in _get_active_users():
        local = user_now(user)
        if local.hour != 23:  # закрываем день в 23:59 по местному времени пользователя
            continue
        try:
            habits = json.loads(user.get('habits') or '[]')
            completed = json.loads(user.get('habits_completed') or '[]')
            log_day(user['user_id'], 0, len(completed), len(habits), paused=pause_touches_today(user), day=local.strftime('%Y-%m-%d'))
            reset_habits(user['user_id'])
        except Exception as e:
            logger.error(f"Ошибка daily rollover {user['user_id']}: {e}")


def send_daily_plans(bot):
    """Утреннее сообщение «План дня» подписчикам, у которых оно не отключено."""
    logger.info("☀️ Отправка «Плана дня»...")
    from database import is_subscribed
    from dailyplan import build_daily_plan, plan_keyboard
    sent = 0
    for user in _get_active_users():
        if not user.get('reminder_plan_enabled', 1) or is_paused(user):
            continue
        if _local_slot(user)[1] != (8, 30):
            continue
        try:
            if not is_subscribed(user['user_id']):
                continue
            plan = build_daily_plan(user)
            if not plan:
                continue
            kb = plan_keyboard(plan)
            bot.send_message(user['user_id'], plan['text'], reply_markup=kb)
            sent += 1
            time.sleep(0.05)  # не упираемся в лимиты Telegram
        except Exception as e:
            logger.error(f"Ошибка плана дня {user['user_id']}: {e}")
    logger.info(f"☀️ План дня отправлен: {sent}")


def send_weight_reminders(bot):
    logger.info("⚖️ Отправка напоминаний о взвешивании...")
    for user in _get_active_users():
        if is_paused(user):
            continue
        local, slot = _local_slot(user)
        if local.weekday() != 0 or slot != _setting_slot(user.get('reminder_weight_time'), "08:00"):
            continue
        try:
            bot.send_message(user['user_id'], """⚖️ *Доброе утро!*

Сегодня день взвешивания. Взвесьтесь утром натощак и обновите вес в боте! 📉""")
        except Exception as e:
            logger.error(f"Ошибка weight reminder {user['user_id']}: {e}")


def send_habits_reminders(bot):
    logger.info("✅ Отправка напоминаний о привычках...")
    import json
    from database import get_habit_streak, format_streak_at_risk
    for user in _get_active_users():
        if is_paused(user):
            continue
        if _local_slot(user)[1] != _setting_slot(user.get('reminder_habits_time'), "20:00"):
            continue
        habits = json.loads(user.get('habits', '[]'))
        completed = json.loads(user.get('habits_completed', '[]'))
        remaining = len(habits) - len(completed)
        if remaining > 0:
            try:
                streak_warning = format_streak_at_risk(get_habit_streak(user['user_id']))
                streak_block = f"\n\n{streak_warning}" if streak_warning else ""
                bot.send_message(user['user_id'], f"""✅ *Вечерний чек-ин*

Сегодня выполнено привычек: {len(completed)} из {len(habits)}.
Осталось: {remaining}. Нажмите на привычку, чтобы отметить! 💪{streak_block}""", reply_markup=_habits_kb(habits, completed))
            except Exception as e:
                logger.error(f"Ошибка habits reminder {user['user_id']}: {e}")


def send_diary_reminders(bot):
    """Вечером (19:00 по местному времени), если в дневнике за сегодня пусто, а человек им пользуется."""
    from database import is_subscribed
    from engagement import diary_reminder
    for user in _get_active_users():
        if is_paused(user) or _local_slot(user)[1] != (19, 0):
            continue
        try:
            if not is_subscribed(user['user_id']):
                continue
            text = diary_reminder(user)
            if not text:
                continue
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("✏️ Написать, что съел(а)", callback_data="dman"),
                   types.InlineKeyboardButton("📓 Открыть дневник", callback_data="dback"),
                   types.InlineKeyboardButton("🔕 Не напоминать", callback_data="diary_rem_off"))
            bot.send_message(user['user_id'], text, reply_markup=kb)
            time.sleep(0.05)
        except Exception as e:
            logger.error(f"Ошибка напоминания о дневнике {user['user_id']}: {e}")


def misses_text(days: int = 7, limit: int = 20) -> str:
    rows = get_top_misses(days, limit)
    if not rows:
        return f"🔎 За {days} дн. всё нашлось: ненайденных запросов нет."
    lines = [f"{i}. {q} — {n}" for i, (q, n) in enumerate(rows, 1)]
    return (f"🔎 *Чего не нашлось в базе еды* (за {days} дн., топ-{len(rows)})\n\n" + "\n".join(lines) +
            "\n\nЧисло — сколько раз искали. Самые частые стоит добавить в базу.")


def send_misses_digest(bot):
    """По понедельникам присылает админам топ ненайденных запросов за неделю."""
    from alerts import admin_ids
    rows = get_top_misses(7, 20)
    if not rows:
        return
    text = misses_text(7)
    for admin in admin_ids():
        try:
            bot.send_message(admin, text)
        except Exception as e:
            logger.error(f"Не удалось отправить сводку «не нашёл» админу {admin}: {e}")


def send_weekly_reports(bot):
    logger.info("📈 Отправка еженедельных отчётов...")
    from database import get_weekly_stats, get_weight_history, weekly_adaptive_adjustment
    from menu_generator import get_progress_bar
    for user in _get_active_users():
        if is_paused(user):
            continue
        local = user_now(user)
        if local.weekday() != 6 or local.hour != 21:  # воскресенье, 21:00 по местному времени
            continue
        try:
            from engagement import weekly_food_block
            from menu_generator import user_calorie_norm
            weekly = get_weekly_stats(user['user_id'])
            food_block = weekly_food_block(user, user_calorie_norm(user))
            if not weekly and not food_block:
                continue
            weekly = weekly or []
            total_habits_done = sum(day['habits_done'] for day in weekly)
            total_habits_total = sum(day['habits_total'] for day in weekly)
            days_count = len(weekly)
            habits_percent = int(100 * total_habits_done / total_habits_total) if total_habits_total > 0 else 0
            weight_history = get_weight_history(user['user_id'], limit=2)
            weight_text = ""
            if len(weight_history) >= 2:
                first = weight_history[-1]['weight']
                last = weight_history[0]['weight']
                diff = round(last - first, 1)
                arrow = "📉" if diff < 0 else "📈" if diff > 0 else "➡️"
                weight_text = f"""
{arrow} Изменение веса: {diff:+.1f} кг"""
            habits_block = (f"✅ Привычки: {total_habits_done}/{total_habits_total} ({habits_percent}%)\n"
                            f"{get_progress_bar(total_habits_done, total_habits_total)}") if total_habits_total else ""
            parts = [p for p in (habits_block + weight_text if habits_block else weight_text.strip("\n"), food_block) if p]
            bot.send_message(user['user_id'], "📊 *Итоги недели*\n\n" + "\n\n".join(parts) + "\n\nОтличная работа! Продолжайте в том же духе 💚")
            adjustment_message = weekly_adaptive_adjustment(user['user_id'])
            if adjustment_message:
                bot.send_message(user['user_id'], adjustment_message)
        except Exception as e:
            logger.error(f"Ошибка weekly report {user['user_id']}: {e}")


def check_pending_payments(bot):
    """
    Фоновая автопроверка платежей ЮKassa и CryptoCloud — без неё пользователю
    пришлось бы самому нажимать «Проверить оплату». Работает через те же
    функции, что и ручная кнопка (payments.check_payment), просто вызывает их
    по расписанию для всех ещё не подтверждённых платежей. Поднимать отдельный
    веб-сервер для приёма вебхуков ради этого не требуется.
    """
    from database import get_pending_payments, complete_payment
    from payments import check_payment as check_provider_payment
    from tiers import name as tier_name, PAID_PERIOD_DAYS, DEFAULT_TIER, period_text

    for provider in ("yookassa", "cryptocloud"):
        for payment in get_pending_payments(provider):
            try:
                result = check_provider_payment(provider, payment['provider_payment_id'])
                if result.get("error"):
                    continue  # временная ошибка API — попробуем на следующем цикле
                if result.get("paid") and complete_payment(payment):  # False — платёж уже зачтён вручную
                    tier = payment.get('tier') or DEFAULT_TIER
                    bot.send_message(payment['user_id'], f"✅ *Оплата прошла успешно!*\n\nТариф «{tier_name(tier)}» активирован на {period_text(payment.get('days') or PAID_PERIOD_DAYS)}.\nСпасибо за поддержку! 💚")
                    logger.info(f"Автоподтверждён платёж {payment['id']} ({provider}) для пользователя {payment['user_id']}")
            except Exception as e:
                logger.error(f"Ошибка автопроверки платежа {payment['id']} ({provider}): {e}")
