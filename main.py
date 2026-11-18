"""
Telegram-бот для ЗОЖ / похудения на telebot (v6.5)
Автоматическая оплата: ЮKassa + CryptoCloud
AI: Google Gemini для анализа фото еды
Рецепты: пошаговые инструкции для каждого блюда
"""

import logging
import json
import os
import re
import io
import time
import threading
from datetime import datetime, timedelta

import telebot
from telebot import types

from config import (
    BOT_TOKEN, ADMIN_IDS, SUBSCRIPTION_PRICE, TRIAL_DAYS, REFERRAL_BONUS_DAYS,
    YOOKASSA_ENABLED, CRYPTOCLOUD_ENABLED, ACTIVITY_NAMES, PACE_OPTIONS
)
import diary as food_diary
import foodparse as food_parse
import engagement
import barcode_lookup
import offsearch
import nutrients as nutrient_utils
import menu_nutrition
from privacy import support_line, support_text, MIN_AGE, AGE_REFUSAL, CONSENT_TEXT, POLICY_PARTS, consent_kb, delete_confirm_kb
from timezones import ZONES as TZ_ZONES, label as tz_label, parse_offset as tz_parse_offset
from database import (
    add_user_food, get_user_foods, delete_user_food, MAX_USER_FOODS,
    init_db, get_user, find_user, add_user, update_user, mark_slip,
    get_habits, add_habit, remove_habit, get_completed_habits, complete_habit,
    reset_habits, is_subscribed, extend_subscription, get_stats, get_admin_stats, get_source_stats, get_recent_payments, get_payments_export,
    is_paused, start_pause, end_pause, pause_available, frozen_subscription_end, PAUSE_MAX_DAYS, get_habit_streak,
    add_weight, remove_last_weight, get_weight_history, get_weight_change, get_weekly_stats,
    set_referrer, log_day, delete_user, erase_user_data, set_consent, user_now,
    create_payment as db_create_payment, get_payment_by_provider_id,
    update_payment_status, get_user_payments, complete_payment, has_food_analysis,
    log_food_analysis, get_today_food_calories, add_diary_entry, delete_diary_entry, get_diary, estimate_weeks_to_goal,
    get_habit_streak, format_streak_message,
    grant_referral_reward, redeem_promo, create_promo, list_promos, set_promo_active, normalize_promo,
    log_food_miss, get_top_misses
)
from menu_generator import (
    calculate_bmi, calculate_daily_calories, get_default_habits,
    get_progress_bar,
    get_all_menus, filter_menu, get_menu_with_recipe_kb, sample_menu,
    calculate_macros, format_macros, get_bmi_recommendation, get_ingredient_banned_keywords,
    calorie_norm_info, user_calorie_norm
)
from payments import create_payment, check_payment
from charts import generate_weight_chart, generate_monthly_report
from reminders import init_reminders
from recipes import get_recipe_by_hash, format_recipe
from shopping import build_weekly_shopping, split_message
from support import SLIP_INTRO, SLIP_OPTIONS, SLIP_ANSWERS
from norms import FLOOR_NOTE
from adminstats import format_sources, build_admin_report, format_payments, payments_csv
from portions import portion_info
from tiers import (FULL_TIER_ENABLED, TIERS, PAID_PERIOD_DAYS, sellable_tiers, tier_of, plans_overview,
                   label as tier_label, name as tier_name, price as tier_price,
                   ANNUAL_ENABLED, ANNUAL_DAYS, annual_price, annual_saving, annual_button_label, period_text)
from food_analyzer import analyze_food_photo, FOOD_ANALYSIS_ENABLED

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _parse_admin_ids(raw) -> set:
    """ADMIN_IDS из config.py -> множество чисел. Терпит ID, записанные строкой ("123"), и пробелы."""
    items = raw if isinstance(raw, (list, tuple, set)) else [raw]
    ids = set()
    for item in items:
        text = str(item).strip()
        if text.lstrip("-").isdigit():
            ids.add(int(text))
        else:
            logger.warning(f"⚠️ В ADMIN_IDS должно быть число (ваш Telegram ID), а не «{item}» — пропущено")
    return ids


ADMIN_IDS = _parse_admin_ids(ADMIN_IDS)

if not BOT_TOKEN:
    logger.error("❌ Укажите BOT_TOKEN!")
    exit(1)

init_db()
logger.info("✅ База данных инициализирована")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown", num_threads=8)


def _plain_text_fallback(method):
    """Telegram отклоняет сообщение в режиме Markdown, если в тексте одиночное _ или * (например, в имени
    пользователя или ID). Вместо тишины повторяем отправку обычным текстом."""
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Exception as e:
            if "can't parse entities" not in str(e).lower():
                raise
            logger.warning(f"⚠️ Telegram отклонил Markdown, отправляю обычным текстом: {e}")
            kwargs["parse_mode"] = ""
            return method(*args, **kwargs)
    return wrapper


bot.send_message = _plain_text_fallback(bot.send_message)
bot.edit_message_text = _plain_text_fallback(bot.edit_message_text)


def _md_escape(text) -> str:
    """Экранирует символы, ломающие Markdown, в подставляемых значениях (имена, @username)."""
    out = str(text)
    for ch in ("\\", "_", "*", "`", "["):
        out = out.replace(ch, "\\" + ch)
    return out

def portion_line(user) -> str:
    """Строка про порции: блюда в рецептах стандартные и дают меньше нормы."""
    phrase = portion_info(user)["phrase"] if user else None
    return f"\n📏 Порции в рецептах стандартные: для вашей нормы делайте {phrase}." if phrase else ""


user_states = {}

def set_state(user_id, step, data=None):
    user_states[user_id] = {"step": step, "data": data or {}}

def get_state(user_id):
    return user_states.get(user_id, {"step": None, "data": {}})

def clear_state(user_id):
    user_states.pop(user_id, None)


def main_menu_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        types.KeyboardButton("📅 План на сегодня"),
        types.KeyboardButton("📓 Дневник еды"),
        types.KeyboardButton("📋 Моё меню"),
        types.KeyboardButton("✅ Привычки"),
        types.KeyboardButton("📊 Прогресс"),
        types.KeyboardButton("⚖️ Вес"),
        types.KeyboardButton("📈 Статистика"),
        types.KeyboardButton("👤 Профиль"),
        types.KeyboardButton("⭐ Подписка"),
        types.KeyboardButton("📸 Анализ еды"),
        types.KeyboardButton("🛒 Список покупок"),
        types.KeyboardButton("💚 Если сорвался"),
    )
    return kb


def goal_inline_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔥 Похудение", callback_data="goal_похудение"),
        types.InlineKeyboardButton("⚖️ Поддержание", callback_data="goal_поддержание"),
        types.InlineKeyboardButton("💪 Набор массы", callback_data="goal_набор"),
    )
    return kb


def pace_inline_kb(goal):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for key, option in PACE_OPTIONS.get(goal, {}).items():
        kb.add(types.InlineKeyboardButton(option["label"], callback_data=f"pace_{key}"))
    return kb


def diet_inline_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🍖 Обычное", callback_data="diet_обычное"),
        types.InlineKeyboardButton("🥗 Вегетарианское", callback_data="diet_вегетарианское"),
        types.InlineKeyboardButton("🌱 Веган", callback_data="diet_веган"),
    )
    return kb


def activity_inline_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(ACTIVITY_NAMES["сидячий"], callback_data="activity_сидячий"),
        types.InlineKeyboardButton(ACTIVITY_NAMES["легкий"], callback_data="activity_легкий"),
        types.InlineKeyboardButton(ACTIVITY_NAMES["средний"], callback_data="activity_средний"),
        types.InlineKeyboardButton(ACTIVITY_NAMES["высокий"], callback_data="activity_высокий"),
        types.InlineKeyboardButton(ACTIVITY_NAMES["очень_высокий"], callback_data="activity_очень_высокий"),
    )
    return kb


def habits_inline_kb(user_id):
    habits = get_habits(user_id)
    completed = get_completed_habits(user_id)
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, habit in enumerate(habits):
        status = "✅" if i in completed else "⬜"
        kb.add(types.InlineKeyboardButton(f"{status} {habit}", callback_data=f"habit_{i}"))
    kb.add(types.InlineKeyboardButton("➕ Добавить привычку", callback_data="habit_add"))
    if habits:
        kb.add(types.InlineKeyboardButton("🗑 Удалить привычку", callback_data="habits_del_menu"))
    return kb


def habits_delete_kb(user_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, habit in enumerate(get_habits(user_id)):
        kb.add(types.InlineKeyboardButton(f"🗑 {habit}", callback_data=f"hdel_{i}"))
    kb.add(types.InlineKeyboardButton("← Назад к привычкам", callback_data="habits_open"))
    return kb


def habits_screen_text(user_id):
    """Текст экрана привычек: прогресс за сегодня + серия дней подряд."""
    total = len(get_habits(user_id))
    completed_count = len(get_completed_habits(user_id))
    progress = get_progress_bar(completed_count, total)
    streak_text = format_streak_message(get_habit_streak(user_id))
    streak_line = f"\n{streak_text}" if streak_text else ""
    if not total:
        return "✅ *Ваши привычки*\n\nПривычек пока нет. Добавьте свою — например, «Прогулка 30 минут»."
    return f"✅ *Ваши привычки*\nВыполнено: {completed_count} из {total}\n{progress}{streak_line}\n\nНажмите, чтобы отметить:"


def provider_buttons(kb, tier, annual=False):
    """Кнопки способов оплаты для выбранного тарифа (annual — для годовой подписки)."""
    suffix = "_y" if annual else ""
    if YOOKASSA_ENABLED:
        kb.add(types.InlineKeyboardButton("💳 Картой / СБП (ЮKassa)", callback_data=f"pay_yookassa_{tier}{suffix}"))
    if CRYPTOCLOUD_ENABLED:
        kb.add(types.InlineKeyboardButton("₿ Криптовалютой", callback_data=f"pay_cryptocloud_{tier}{suffix}"))


def year_button(kb, tier):
    if ANNUAL_ENABLED and (YOOKASSA_ENABLED or CRYPTOCLOUD_ENABLED):
        kb.add(types.InlineKeyboardButton(annual_button_label(tier), callback_data=f"year_{tier}"))


def sub_inline_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    available = sellable_tiers()
    if len(available) == 1:
        provider_buttons(kb, available[0])  # один тариф — сразу способы оплаты
        year_button(kb, available[0])
    else:
        for tier in available:
            kb.add(types.InlineKeyboardButton(tier_label(tier), callback_data=f"plan_{tier}"))
    kb.add(
        types.InlineKeyboardButton("🎁 Приведи друга", callback_data="sub_ref"),
        types.InlineKeyboardButton("🎟 Ввести промокод", callback_data="promo_ask"),
        types.InlineKeyboardButton("📋 Что входит?", callback_data="sub_info"),
    )
    return kb


def _tier_warning(user_id, tier) -> str:
    """Предупреждение, если действующая подписка меняет тариф."""
    if not is_subscribed(user_id):
        return ""
    current = tier_of(get_user(user_id))
    if current == "full" and tier == "basic":
        return "\n\n⚠️ Тариф сменится сразу после оплаты: анализ еды станет недоступен."
    if current == "basic" and tier == "full":
        return "\n\nℹ️ Полная подписка включится сразу, остаток базовой добавится к новому периоду."
    return ""


@bot.callback_query_handler(func=lambda call: call.data.startswith("plan_"))
def choose_plan(call):
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)
    tier = call.data.replace("plan_", "")
    if tier not in sellable_tiers():
        bot.send_message(user_id, "❌ Этот тариф сейчас недоступен. Откройте «⭐ Подписка» ещё раз.")
        return
    kb = types.InlineKeyboardMarkup(row_width=1)
    provider_buttons(kb, tier)
    year_button(kb, tier)
    bot.send_message(user_id, f"{tier_label(tier)}/мес\n\nВыберите способ оплаты:{_tier_warning(user_id, tier)}", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("year_"))
def choose_year(call):
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)
    tier = call.data.replace("year_", "")
    if not ANNUAL_ENABLED or tier not in sellable_tiers():
        bot.send_message(user_id, "❌ Годовая подписка сейчас недоступна. Откройте «⭐ Подписка» ещё раз.")
        return
    kb = types.InlineKeyboardMarkup(row_width=1)
    provider_buttons(kb, tier, annual=True)
    bot.send_message(user_id, f"📆 *Годовая подписка — тариф «{tier_name(tier)}»*\n\n12 месяцев ({ANNUAL_DAYS} дней) за *{annual_price(tier)}₽* вместо {tier_price(tier) * 12}₽ — выгода *{annual_saving(tier)}₽*.\n\nВыберите способ оплаты:{_tier_warning(user_id, tier)}", reply_markup=kb)


def access_line(user_id) -> str:
    """Что сказать про доступ: новый пробный период или уже существующая подписка (например, после удаления данных)."""
    user = get_user(user_id) or {}
    try:
        end = datetime.fromisoformat(user['subscription_end'])
        created = datetime.fromisoformat(user['created_at'])
    except (KeyError, TypeError, ValueError):
        return ""
    if abs((end - created) - timedelta(days=TRIAL_DAYS)) < timedelta(minutes=2):
        return f"🎁 У вас *{TRIAL_DAYS} дня бесплатного доступа* ко всем функциям!"
    if end > datetime.now():
        return f"✅ Ваша подписка сохранена: действует до {end.strftime('%d.%m.%Y')}."
    return "⚠️ Подписка закончилась — оформить её можно в разделе «⭐ Подписка»."


def clean_source_tag(tag: str):
    """Метка источника: латиница, цифры, _ и -, до 32 символов (так требует Telegram для параметра start)."""
    tag = re.sub(r"[^a-z0-9_\-]", "", (tag or "").lower())[:32]
    return tag or None


PROMO_ANSWERS = {
    "unknown": "❌ Такого промокода нет. Проверьте написание.",
    "inactive": "❌ Этот промокод больше не действует.",
    "expired": "❌ Срок действия промокода истёк.",
    "used_up": "❌ Этот промокод уже использовали максимальное число раз.",
    "already": "ℹ️ Вы уже активировали этот промокод.",
}
_promo_fails = {}  # user_id -> [время неудачных попыток]: защита от подбора кодов


def _promo_blocked(user_id) -> bool:
    now = time.time()
    fails = [t for t in _promo_fails.get(user_id, []) if now - t < 3600]
    _promo_fails[user_id] = fails
    return len(fails) >= 5


def _apply_promo(user_id, code) -> bool:
    """Активирует промокод и сообщает результат. True, если подписка продлена."""
    if _promo_blocked(user_id):
        bot.send_message(user_id, "⏳ Слишком много неверных кодов. Попробуйте через час.")
        return False
    status, days = redeem_promo(user_id, code)
    if status == "ok":
        end = datetime.fromisoformat(get_user(user_id)['subscription_end']).strftime("%d.%m.%Y")
        bot.send_message(user_id, f"🎟 *Промокод активирован!*\n\nДобавлено {days} дн. подписки. Она действует до {end}.")
        return True
    if status in ("unknown", "inactive", "expired"):
        _promo_fails.setdefault(user_id, []).append(time.time())
    bot.send_message(user_id, PROMO_ANSWERS[status])
    return False


def _after_registration(user_id):
    """Когда регистрация завершена: начисляем дни пригласившему и применяем отложенный промокод."""
    try:
        reward = grant_referral_reward(user_id)
        if reward and reward[1]:
            referrer_id, days = reward
            end = datetime.fromisoformat(get_user(referrer_id)['subscription_end']).strftime("%d.%m.%Y")
            try:
                bot.send_message(referrer_id, f"🎉 *Ваш друг присоединился к боту!*\n\nВам начислено +{days} дн. подписки. Она действует до {end}.")
            except Exception as e:
                logger.error(f"Не удалось уведомить {referrer_id} о награде за друга: {e}")
    except Exception as e:
        logger.error(f"Ошибка награды за приглашение ({user_id}): {e}")
    user = get_user(user_id)
    if user and user.get('pending_promo'):
        code = user['pending_promo']
        update_user(user_id, pending_promo=None)
        try:
            _apply_promo(user_id, code)
        except Exception as e:
            logger.error(f"Ошибка отложенного промокода {code} ({user_id}): {e}")


@bot.message_handler(commands=['promo'])
def cmd_promo(message):
    user_id = message.from_user.id
    parts = message.text.split(maxsplit=1)
    if not get_user(user_id):
        bot.send_message(user_id, "Сначала пройдите регистрацию: /start")
        return
    if len(parts) > 1:
        _apply_promo(user_id, parts[1])
        return
    set_state(user_id, "promo_enter")
    bot.send_message(user_id, "🎟 Введите промокод одним сообщением. Отмена — /start")


@bot.callback_query_handler(func=lambda call: call.data == "promo_ask")
def promo_ask(call):
    bot.answer_callback_query(call.id)
    set_state(call.from_user.id, "promo_enter")
    bot.send_message(call.from_user.id, "🎟 Введите промокод одним сообщением. Отмена — /start")


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "promo_enter" and not (m.text or "").startswith("/"))
def promo_enter(message):
    user_id = message.from_user.id
    clear_state(user_id)
    _apply_promo(user_id, message.text or "")


@bot.message_handler(commands=['backup'])
def cmd_backup(message):
    """Админ: /backup — сделать копию базы прямо сейчас; /backup send — ещё и прислать файл сюда."""
    if message.from_user.id not in ADMIN_IDS:
        _deny_admin(message)
        return
    admin_id = message.from_user.id
    import backup
    try:
        path = backup.make_backup()
    except Exception as e:
        logger.error(f"Ошибка ручного резервного копирования: {e}")
        bot.send_message(admin_id, f"❌ Копия не создана: {e}")
        return
    count = len(backup.list_backups())
    bot.send_message(admin_id, f"✅ Копия создана: {os.path.basename(path)} ({os.path.getsize(path) // 1024} КБ).\nХранится копий: {count}. Папка: {os.path.dirname(path)}")
    if "send" in message.text.split()[1:]:
        with open(path, "rb") as f:
            bot.send_document(admin_id, f, caption="💾 Копия базы (внутри личные данные пользователей)")


@bot.message_handler(commands=['misses'])
def cmd_misses(message):
    """Админ: что люди искали в дневнике, а в базе не нашлось. /misses [ДНЕЙ], по умолчанию 7."""
    if message.from_user.id not in ADMIN_IDS:
        _deny_admin(message)
        return
    parts = message.text.split()
    days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() and 1 <= int(parts[1]) <= 90 else 7
    from reminders import misses_text
    bot.send_message(message.from_user.id, misses_text(days))


@bot.message_handler(commands=['promo_add'])
def cmd_promo_add(message):
    """Админ: /promo_add КОД ДНЕЙ [МАКС_АКТИВАЦИЙ] [ДНЕЙ_ДЕЙСТВИЯ_КОДА]."""
    if message.from_user.id not in ADMIN_IDS:
        _deny_admin(message)
        return
    admin_id = message.from_user.id
    parts = message.text.split()
    try:
        code = normalize_promo(parts[1])
        if not re.fullmatch(r"[A-Z0-9_\-]{3,32}", code):
            raise ValueError
        days = int(parts[2])
        max_uses = int(parts[3]) if len(parts) > 3 else 0
        valid_days = int(parts[4]) if len(parts) > 4 else None
        if not 1 <= days <= 3650 or max_uses < 0 or (valid_days is not None and valid_days < 1):
            raise ValueError
    except (IndexError, ValueError):
        bot.send_message(admin_id, "Использование: /promo_add КОД ДНЕЙ [МАКС_АКТИВАЦИЙ] [ДНЕЙ_ДЕЙСТВИЯ_КОДА]\n\n"
                                   "Код — латиница, цифры, _ и -, 3–32 символа.\n"
                                   "Пример: /promo_add SALE15 15 100 30 — 15 дней подписки, не более 100 активаций, код действует 30 дней.\n"
                                   "Без третьего и четвёртого числа — без лимитов.")
        return
    if not create_promo(code, days, max_uses, valid_days):
        bot.send_message(admin_id, f"❌ Код {code} уже существует. Отключить: /promo_off {code}")
        return
    try:
        username = bot.get_me().username
    except Exception:
        username = "ваш_бот"
    limit = f"до {max_uses} активаций" if max_uses else "без лимита активаций"
    valid = f", действует {valid_days} дн." if valid_days else "."
    bot.send_message(admin_id, f"✅ Промокод `{code}`: +{days} дн., {limit}{valid}\n\nСсылка: https://t.me/{username}?start=promo_{code}")


@bot.message_handler(commands=['promo_list'])
def cmd_promo_list(message):
    if message.from_user.id not in ADMIN_IDS:
        _deny_admin(message)
        return
    promos = list_promos()
    if not promos:
        bot.send_message(message.from_user.id, "Промокодов пока нет. Создать: /promo_add КОД ДНЕЙ")
        return
    lines = []
    for p in promos[:30]:
        state = "🟢" if p['active'] else "⚫"
        if p['expires_at'] and datetime.fromisoformat(p['expires_at']) < datetime.now():
            state = "⌛"
        limit = f"/{p['max_uses']}" if p['max_uses'] else ""
        until = f", до {datetime.fromisoformat(p['expires_at']).strftime('%d.%m.%Y')}" if p['expires_at'] else ""
        lines.append(f"{state} `{p['code']}` — +{p['days']} дн., использовано {p['used']}{limit}{until}")
    bot.send_message(message.from_user.id, "🎟 *Промокоды*\n\n" + "\n".join(lines) + "\n\nОтключить: /promo_off КОД")


@bot.message_handler(commands=['promo_off'])
def cmd_promo_off(message):
    if message.from_user.id not in ADMIN_IDS:
        _deny_admin(message)
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.from_user.id, "Использование: /promo_off КОД")
        return
    ok = set_promo_active(parts[1], False)
    bot.send_message(message.from_user.id, f"⚫ Код {normalize_promo(parts[1])} отключён." if ok else "❌ Такого кода нет.")


def _start_registration(user_id):
    """После согласия: приветствие и первый вопрос регистрации."""
    bot.send_message(user_id, f"👋 Добро пожаловать в *ПолезноБот*!\n\n{access_line(user_id)}\n\n⚠️ *Важно:* бот даёт общие рекомендации по питанию и не заменяет консультацию врача или диетолога. Перед изменением рациона или физических нагрузок, особенно при хронических заболеваниях, аллергиях или беременности, проконсультируйтесь со специалистом.\n\nДавайте познакомимся. Как вас зовут?")
    set_state(user_id, "reg_name")


@bot.message_handler(commands=['start'])
def cmd_start(message):
    user_id = message.from_user.id
    user = get_user(user_id)
    ref_id = None
    source = None
    promo_param = None
    if message.text and len(message.text.split()) > 1:
        param = message.text.split()[1]
        if param.startswith("src_"):  # метка рекламного источника: t.me/бот?start=src_telega1
            source = clean_source_tag(param[4:])
        elif param.startswith("promo_"):  # промокод в ссылке: t.me/бот?start=promo_SALE15
            promo_param = re.sub(r"[^A-Za-z0-9_\-]", "", param[6:])[:32] or None
        else:
            try:
                ref_id = int(param)
            except ValueError:
                ref_id = None

    if user and user.get('name'):
        if promo_param:
            _apply_promo(user_id, promo_param)
        sub_status = "✅ Активна" if is_subscribed(user_id) else "❌ Неактивна"
        bot.send_message(user_id, f"👋 С возвращением, {user['name']}!\n\n📅 Подписка: {sub_status}\nВыберите действие в меню ниже:", reply_markup=main_menu_keyboard())
        if not user.get('consent_at'):  # пользователи, пришедшие до появления политики, подтверждают согласие один раз
            bot.send_message(user_id, CONSENT_TEXT, reply_markup=consent_kb(types))
        return

    if not user:
        add_user(user_id, message.from_user.username, message.from_user.first_name, str(user_id))
        if source:  # источник запоминаем только при первом приходе
            update_user(user_id, source=source)
    # приглашение учитываем после создания записи (раньше оно терялось, потому что пользователя ещё не было)
    if ref_id and ref_id != user_id:
        set_referrer(user_id, ref_id)
    if promo_param:  # применится, когда человек завершит регистрацию
        update_user(user_id, pending_promo=normalize_promo(promo_param))
    if get_user(user_id).get('consent_at'):
        _start_registration(user_id)
    else:
        set_state(user_id, "reg_consent")
        bot.send_message(user_id, CONSENT_TEXT, reply_markup=consent_kb(types))


@bot.callback_query_handler(func=lambda call: call.data == "consent_yes")
def consent_yes(call):
    user_id = call.from_user.id
    bot.answer_callback_query(call.id, "Спасибо!")
    user = get_user(user_id)
    if not user:
        bot.send_message(user_id, "Нажмите /start")
        return
    set_consent(user_id)
    if get_state(user_id)["step"] == "reg_consent":
        _start_registration(user_id)
    else:
        bot.send_message(user_id, "✅ Спасибо, согласие сохранено.")


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "reg_consent" and not (m.text or "").startswith("/"))
def consent_reminder(message):
    bot.send_message(message.from_user.id, "Чтобы продолжить, нажмите «✅ Принимаю» под сообщением выше (или прочитайте политику: /privacy).",
                     reply_markup=consent_kb(types))


def _send_privacy(user_id):
    for part in POLICY_PARTS:
        bot.send_message(user_id, part)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🗑 Удалить мои данные", callback_data="erase_ask"))
    bot.send_message(user_id, "Вы можете удалить свои данные в любой момент:", reply_markup=kb)


@bot.message_handler(commands=['support', 'contact'])
def cmd_support(message):
    bot.send_message(message.from_user.id, support_text())


@bot.message_handler(commands=['privacy'])
def cmd_privacy(message):
    _send_privacy(message.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data == "privacy_show")
def privacy_show(call):
    bot.answer_callback_query(call.id)
    _send_privacy(call.from_user.id)


@bot.message_handler(commands=['delete_data'])
def cmd_delete_data(message):
    _ask_erase(message.from_user.id)


def _ask_erase(user_id):
    user = get_user(user_id)
    sub = ""
    if user and is_subscribed(user_id):
        end = datetime.fromisoformat(user['subscription_end']).strftime("%d.%m.%Y")
        sub = f"\n\nОплаченная подписка (до {end}) сохранится: если вернётесь, дни не пропадут."
    bot.send_message(user_id, "🗑 *Удалить мои данные?*\n\nБудут удалены профиль, история веса, привычки, заметки о еде и статистика. "
                              "Останутся только записи об оплатах (их требует закон) и срок подписки." + sub +
                              "\n\nЭто нельзя отменить.", reply_markup=delete_confirm_kb(types))


@bot.callback_query_handler(func=lambda call: call.data == "erase_ask")
def erase_ask(call):
    bot.answer_callback_query(call.id)
    _ask_erase(call.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data == "erase_cancel")
def erase_cancel(call):
    bot.answer_callback_query(call.id, "Хорошо, ничего не удалено")


@bot.callback_query_handler(func=lambda call: call.data == "erase_confirm")
def erase_confirm(call):
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)
    erase_user_data(user_id)
    clear_state(user_id)
    bot.send_message(user_id, "✅ Данные удалены. Напоминания больше приходить не будут.\n\nЕсли захотите вернуться — отправьте /start.",
                     reply_markup=types.ReplyKeyboardRemove())


# Админ-команды зарегистрированы до обработчиков шагов диалога: иначе /extend, отправленный
# посреди регистрации или редактирования профиля, перехватывался бы обработчиком шага.
def _deny_admin(message):
    """Отвечаем, а не молчим: так сразу видно, что бот работает и дело в ID."""
    bot.send_message(message.from_user.id, f"⛔ Команда только для администратора.\n\nВаш ID: `{message.from_user.id}`")


@bot.message_handler(commands=['admin'])
def cmd_admin(message):
    if message.from_user.id not in ADMIN_IDS:
        _deny_admin(message)
        return
    bot.send_message(message.from_user.id, build_admin_report(get_admin_stats()))


@bot.message_handler(commands=['link'])
def cmd_link(message):
    if message.from_user.id not in ADMIN_IDS:
        _deny_admin(message)
        return
    parts = (message.text or "").split()
    tag = clean_source_tag(parts[1]) if len(parts) > 1 else None
    if not tag:
        bot.send_message(message.from_user.id, "Напишите метку латиницей, например: /link telega1\n\nМетка — это название площадки или объявления, чтобы потом видеть, откуда пришли люди (только a-z, 0-9, _ и -).")
        return
    try:
        username = bot.get_me().username
    except Exception:
        bot.send_message(message.from_user.id, f"Не удалось узнать имя бота. Ссылка: t.me/ИМЯ_БОТА?start=src\\_{tag}")
        return
    bot.send_message(message.from_user.id, f"🔗 Ссылка для источника «{tag}»:\n\nhttps://t.me/{username}?start=src_{tag}\n\nСтатистика по источникам — /sources",
                     disable_web_page_preview=True)


@bot.message_handler(commands=['sources'])
def cmd_sources(message):
    if message.from_user.id not in ADMIN_IDS:
        _deny_admin(message)
        return
    bot.send_message(message.from_user.id, format_sources(get_source_stats()))


@bot.message_handler(commands=['payments'])
def cmd_payments(message):
    if message.from_user.id not in ADMIN_IDS:
        _deny_admin(message)
        return
    bot.send_message(message.from_user.id, format_payments(get_recent_payments(15)))


@bot.message_handler(commands=['export'])
def cmd_export(message):
    """Выгрузка всех платежей в CSV для сверки с ЮKassa и учёта."""
    if message.from_user.id not in ADMIN_IDS:
        _deny_admin(message)
        return
    rows = get_payments_export()
    if not rows:
        bot.send_message(message.from_user.id, "Платежей пока нет.")
        return
    document = io.BytesIO(payments_csv(rows))
    document.name = f"payments_{datetime.now().strftime('%Y-%m-%d')}.csv"
    bot.send_document(message.from_user.id, document, caption=f"Платежей в выгрузке: {len(rows)}")


@bot.message_handler(commands=['myid'])
def cmd_myid(message):
    """Пользователь может узнать свой ID и сообщить его админу для подарочной подписки."""
    bot.send_message(message.from_user.id, f"🆔 Ваш ID: `{message.from_user.id}`")


def _pause_text_and_kb(user):
    """Экран паузы: возобновить (если идёт), поставить (если доступна) или объяснение, почему нельзя."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    if is_paused(user):
        auto = datetime.fromisoformat(user['paused_until']).strftime("%d.%m")
        left = frozen_subscription_end(user)
        left_line = f"Подписка заморожена: останется до {left.strftime('%d.%m.%Y')} (если возобновить сегодня).\n\n" if left else ""
        kb.add(types.InlineKeyboardButton("▶️ Возобновить", callback_data="pause_resume"))
        return (f"⏸ *Пауза включена*\n\n{left_line}Дни подписки не сгорают, напоминания и план дня не приходят, "
                f"серия привычек сохранена. Возобновить можно в любой день — или я сама включу всё {auto}.", kb)
    if pause_available(user):
        kb.add(types.InlineKeyboardButton("⏸ Поставить на паузу", callback_data="pause_confirm"))
        return ("⏸ *Пауза подписки*\n\nОтпуск, болезнь или просто нужен перерыв? На паузе:\n"
                "• дни подписки не сгорают\n"
                "• не приходят напоминания и план дня\n"
                "• серия привычек не сгорает\n"
                "• норму калорий мы не пересчитываем\n\n"
                f"Возобновить можно в любой день, максимум пауза длится {PAUSE_MAX_DAYS} дней. "
                "Пауза доступна один раз за оплаченный период.", kb)
    reason = ("Пауза доступна для оплаченной подписки — один раз за оплаченный период. "
              "Если вы уже использовали её, она снова станет доступна после следующей оплаты.")
    return (f"⏸ *Пауза подписки*\n\n{reason}", kb)


def _send_pause_menu(user_id):
    user = get_user(user_id)
    if not user:
        bot.send_message(user_id, "Сначала пройдите регистрацию: /start")
        return
    text, kb = _pause_text_and_kb(user)
    bot.send_message(user_id, text, reply_markup=kb)


def _resume_and_report(user_id):
    user = get_user(user_id)
    if not user:
        bot.send_message(user_id, "Сначала пройдите регистрацию: /start")
        return
    if not end_pause(user_id):
        bot.send_message(user_id, "Пауза сейчас не включена. Поставить на паузу: /pause")
        return
    streak = get_habit_streak(user_id)
    streak_line = f"\n🏅 Серия привычек сохранена: {streak}." if streak > 0 else ""
    end = datetime.fromisoformat(get_user(user_id)['subscription_end']).strftime("%d.%m.%Y")
    bot.send_message(user_id, f"▶️ *Пауза снята. С возвращением!*\n\nПодписка продолжается до {end}. Напоминания и план дня снова приходят.{streak_line}")


@bot.message_handler(commands=['pause'])
def cmd_pause(message):
    _send_pause_menu(message.from_user.id)


@bot.message_handler(commands=['resume'])
def cmd_resume(message):
    _resume_and_report(message.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data == "pause_menu")
def pause_menu(call):
    bot.answer_callback_query(call.id)
    _send_pause_menu(call.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data == "pause_confirm")
def pause_confirm(call):
    bot.answer_callback_query(call.id)
    user = get_user(call.from_user.id)
    if not pause_available(user):
        _send_pause_menu(call.from_user.id)
        return
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("✅ Да, на паузу", callback_data="pause_start"),
           types.InlineKeyboardButton("Отмена", callback_data="pause_cancel"))
    bot.send_message(call.from_user.id, "Поставить подписку на паузу? Это можно сделать один раз за оплаченный период.", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data == "pause_cancel")
def pause_cancel(call):
    bot.answer_callback_query(call.id, "Хорошо, всё как было")


@bot.callback_query_handler(func=lambda call: call.data == "pause_start")
def pause_start(call):
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)
    if not get_user(user_id):
        bot.send_message(user_id, "Сначала пройдите регистрацию: /start")
        return
    until = start_pause(user_id)
    if not until:
        _send_pause_menu(user_id)
        return
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("▶️ Возобновить", callback_data="pause_resume"))
    bot.send_message(user_id, f"⏸ *Пауза включена.* Дни подписки не сгорают.\n\n"
                              "Напоминания и план дня не приходят, серия привычек сохранена. Возобновить можно в любой день "
                              f"кнопкой ниже или командой /resume. Если не вернётесь сами, пауза закончится {until.strftime('%d.%m')}.", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data == "pause_resume")
def pause_resume(call):
    bot.answer_callback_query(call.id)
    _resume_and_report(call.from_user.id)


@bot.message_handler(commands=['extend'])
def cmd_extend(message):
    """Админ дарит или продлевает подписку: /extend USER_ID или @username ДНЕЙ.
    Отрицательное число сокращает подписку. Получателю приходит уведомление."""
    if message.from_user.id not in ADMIN_IDS:
        _deny_admin(message)
        return
    admin_id = message.from_user.id
    parts = message.text.split()
    try:
        target, days = parts[1], int(parts[2])
        tier = parts[3].lower() if len(parts) > 3 else None
        if tier is not None and tier not in TIERS:
            raise ValueError
    except (IndexError, ValueError):
        bot.send_message(admin_id, "Использование: /extend ID или @username ДНЕЙ (по желанию тариф basic или full)\n\nПример: /extend @ivan 30 — подарить 30 дней, /extend @ivan 30 full — с анализом еды.\nБез тарифа остаётся текущий. Отрицательное число сокращает подписку.")
        return
    user = find_user(target)
    if not user:
        bot.send_message(admin_id, "❌ Пользователь не найден. Он должен хотя бы раз запустить бота (/start).")
        return
    target_id = user['user_id']
    extend_subscription(target_id, days, tier=tier)
    end = datetime.fromisoformat(get_user(target_id)['subscription_end']).strftime("%d.%m.%Y")
    label = _md_escape(f"@{user['username']}") if user.get('username') else str(target_id)
    action = f"продлена на {days} дн." if days >= 0 else f"сокращена на {-days} дн."
    note = ""
    if days > 0:
        try:
            bot.send_message(target_id, f"🎁 Вам подарили {days} дн. подписки!\n\nПодписка активна до {end}.")
        except Exception as e:
            logger.error(f"Не удалось уведомить {target_id} о подарке: {e}")
            note = "\n⚠️ Уведомление не доставлено (пользователь мог заблокировать бота)."
    tier_line = f"\nТариф: «{tier_name(tier_of(get_user(target_id)))}»"
    bot.send_message(admin_id, f"✅ Подписка {label} (ID {target_id}) {action}\nДействует до: {end}{tier_line}{note}")


@bot.message_handler(commands=['revoke'])
def cmd_revoke(message):
    """Админ обнуляет подписку: /revoke ID или @username. Подписка заканчивается сразу, уведомление не отправляется."""
    if message.from_user.id not in ADMIN_IDS:
        _deny_admin(message)
        return
    admin_id = message.from_user.id
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(admin_id, "Использование: /revoke ID или @username\n\nПодписка закончится сразу. Платежи и данные пользователя не трогаются, деньги не возвращаются.")
        return
    user = find_user(parts[1])
    if not user:
        bot.send_message(admin_id, "❌ Пользователь не найден. Он должен хотя бы раз запустить бота (/start).")
        return
    target_id = user['user_id']
    was = user.get('subscription_end')
    update_user(target_id, subscription_end=(datetime.now() - timedelta(seconds=1)).isoformat())
    label = _md_escape(f"@{user['username']}") if user.get('username') else str(target_id)
    was_text = datetime.fromisoformat(was).strftime("%d.%m.%Y") if was else "не было"
    bot.send_message(admin_id, f"🚫 Подписка {label} (ID {target_id}) обнулена.\nБыло до: {was_text}")


@bot.message_handler(commands=['broadcast'])
def cmd_broadcast(message):
    if message.from_user.id not in ADMIN_IDS:
        _deny_admin(message)
        return
    text = message.text.replace("/broadcast ", "", 1)
    if not text or text == "/broadcast":
        bot.send_message(message.from_user.id, "Использование: /broadcast ТЕКСТ")
        return

    import sqlite3
    from config import DB_PATH
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE is_active = 1")
    user_ids = [row['user_id'] for row in cursor.fetchall()]
    conn.close()

    sent, failed = 0, 0
    for uid in user_ids:
        try:
            bot.send_message(uid, f"📢 {text}")
            sent += 1
        except Exception as e:
            failed += 1
            logger.error(f"Ошибка рассылки для {uid}: {e}")
        time.sleep(0.05)  # не упереться в лимиты Telegram (~30 сообщений/сек)

    bot.send_message(message.from_user.id, f"📢 Рассылка завершена!\n\n✅ Доставлено: {sent}\n❌ Не доставлено: {failed}")


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "reg_name")
def process_name(message):
    user_id = message.from_user.id
    set_state(user_id, "reg_age", {"name": message.text})
    bot.send_message(user_id, f"Отлично, {message.text}! Сколько вам лет?")


@bot.message_handler(commands=['disclaimer'])
def cmd_disclaimer(message):
    bot.send_message(message.from_user.id, "⚠️ *Дисклеймер*\n\nПолезноБот предоставляет общую информацию о питании, воде, физической активности и подсчёте калорий в справочных и просветительских целях. Бот *не является* медицинским, диетологическим или иным профессиональным изданием и *не заменяет* консультацию врача, диетолога или другого квалифицированного специалиста.\n\nРекомендации, расчёты калорий, БЖУ и ИМТ носят ориентировочный характер и могут не учитывать индивидуальные особенности здоровья.\n\nПеред изменением рациона питания, началом новых физических нагрузок или в случае хронических заболеваний, аллергий, беременности или кормления грудью — обязательно проконсультируйтесь со специалистом.\n\nАвтор бота не несёт ответственности за последствия использования предоставленной информации без консультации с врачом.")


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "reg_age")
def process_age(message):
    user_id = message.from_user.id
    try:
        age = int(message.text)
        if not 1 <= age <= 100:
            raise ValueError
        if age < MIN_AGE:  # бот не для детей: данные не сохраняем
            delete_user(user_id)
            clear_state(user_id)
            bot.send_message(user_id, AGE_REFUSAL, reply_markup=types.ReplyKeyboardRemove())
            return
        data = get_state(user_id)["data"]
        data["age"] = age
        set_state(user_id, "reg_weight", data)
        bot.send_message(user_id, "Какой у вас вес (в кг)? Например: 70")
    except ValueError:
        bot.send_message(user_id, "Пожалуйста, введите корректный возраст (числом).")


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "reg_weight")
def process_weight(message):
    user_id = message.from_user.id
    try:
        weight = float(message.text.replace(",", "."))
        if not 30 <= weight <= 300:
            raise ValueError
        data = get_state(user_id)["data"]
        data["weight"] = weight
        set_state(user_id, "reg_height", data)
        bot.send_message(user_id, "Какой у вас рост (в см)? Например: 170")
    except ValueError:
        bot.send_message(user_id, "Пожалуйста, введите корректный вес (30–300 кг).")


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "reg_height")
def process_height(message):
    user_id = message.from_user.id
    try:
        height = float(message.text.replace(",", "."))
        if not 100 <= height <= 250:
            raise ValueError
        data = get_state(user_id)["data"]
        data["height"] = height
        set_state(user_id, "reg_body_fat", data)
        bot.send_message(user_id, "Какой у вас примерный процент жира в организме? Например: 20\n\n💡 Если не знаете — напишите 20 (среднее для женщин) или 15 (среднее для мужчин).")
    except ValueError:
        bot.send_message(user_id, "Пожалуйста, введите корректный рост (100–250 см).")


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "reg_body_fat")
def process_body_fat(message):
    user_id = message.from_user.id
    try:
        body_fat = float(message.text.replace(",", "."))
        if not 3 <= body_fat <= 60:
            raise ValueError
        data = get_state(user_id)["data"]
        data["body_fat"] = body_fat
        set_state(user_id, "reg_activity", data)
        bot.send_message(user_id, "Выберите ваш уровень физической активности:", reply_markup=activity_inline_kb())
    except ValueError:
        bot.send_message(user_id, "Пожалуйста, введите корректный процент жира (3–60%).")


@bot.callback_query_handler(func=lambda call: call.data.startswith("activity_") and get_state(call.from_user.id)["step"] == "reg_activity")
def process_activity_callback(call):
    user_id = call.from_user.id
    activity = call.data.replace("activity_", "")
    data = get_state(user_id)["data"]
    data["activity_level"] = activity
    set_state(user_id, "reg_goal", data)
    bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id, text=f"🏃 Активность: *{activity}*\n\nТеперь выберите цель:", reply_markup=goal_inline_kb())
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("goal_") and get_state(call.from_user.id)["step"] == "reg_goal")
def process_goal(call):
    user_id = call.from_user.id
    goal = call.data.replace("goal_", "")
    data = get_state(user_id)["data"]
    data["goal"] = goal
    if goal in ("похудение", "набор"):
        set_state(user_id, "reg_target_weight", data)
        direction = "хотите похудеть до" if goal == "похудение" else "хотите набрать до"
        bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id,
                               text=f"🎯 Цель: *{goal}*\n\nДо какого веса (в кг) вы {direction}?\nНапример: {data['weight'] - 5 if goal == 'похудение' else data['weight'] + 5}")
    else:
        set_state(user_id, "reg_diet", data)
        bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id, text=f"🎯 Цель: *{goal}*\n\nТеперь выберите тип питания:", reply_markup=diet_inline_kb())
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "reg_target_weight")
def process_target_weight(message):
    user_id = message.from_user.id
    data = get_state(user_id)["data"]
    try:
        target_weight = float(message.text.replace(",", "."))
        if not 30 <= target_weight <= 300:
            raise ValueError
        current_weight = data['weight']
        goal = data['goal']
        if goal == "похудение" and target_weight >= current_weight:
            bot.send_message(user_id, f"Цель похудения должна быть меньше текущего веса ({current_weight} кг). Введите корректный целевой вес:")
            return
        if goal == "набор" and target_weight <= current_weight:
            bot.send_message(user_id, f"Цель набора должна быть больше текущего веса ({current_weight} кг). Введите корректный целевой вес:")
            return
        data["target_weight"] = target_weight
        set_state(user_id, "reg_pace", data)
        bot.send_message(user_id, f"🎯 Целевой вес: *{target_weight} кг*\n\nКак хотите двигаться к цели?", reply_markup=pace_inline_kb(goal))
    except ValueError:
        bot.send_message(user_id, "Пожалуйста, введите корректный вес в кг (30–300).")


@bot.callback_query_handler(func=lambda call: call.data.startswith("pace_") and get_state(call.from_user.id)["step"] == "reg_pace")
def process_pace(call):
    user_id = call.from_user.id
    pace_key = call.data.replace("pace_", "")
    data = get_state(user_id)["data"]
    goal = data['goal']
    pace_option = PACE_OPTIONS.get(goal, {}).get(pace_key)
    if not pace_option:
        bot.answer_callback_query(call.id, "Не удалось распознать темп, попробуйте снова")
        return
    data["weekly_pace_percent"] = pace_option["percent"]
    weeks = estimate_weeks_to_goal(data['weight'], data['target_weight'], pace_option["percent"])
    set_state(user_id, "reg_diet", data)
    bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id,
                           text=f"{pace_option['label']}\n\n📅 Ориентировочно до цели: *~{weeks} нед.*\n(темп пересчитывается каждую неделю по факту)\n\nТеперь выберите тип питания:", reply_markup=diet_inline_kb())
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("diet_"))
def process_diet(call):
    user_id = call.from_user.id
    diet = call.data.replace("diet_", "")
    data = get_state(user_id)["data"]
    data["diet_type"] = diet
    set_state(user_id, "reg_allergies", data)
    bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id, text=f"🥗 Тип питания: *{diet}*\n\nУкажите ваши аллергии через запятую.\nНапример: *молоко, орехи, яйцо*\n\nЕсли нет аллергий — напишите *нет*:")
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "reg_allergies")
def process_allergies(message):
    user_id = message.from_user.id
    text = message.text.strip()
    allergies = "" if text.lower() in ("нет", "no", "-", "—") else text.lower()
    data = get_state(user_id)["data"]
    update_user(user_id, name=data['name'], age=data['age'], weight=data['weight'], height=data['height'],
                 body_fat=data.get('body_fat', 20), activity_level=data.get('activity_level', 'легкий'),
                 goal=data['goal'], diet_type=data['diet_type'], allergies=allergies,
                 target_weight=data.get('target_weight'), weekly_pace_percent=data.get('weekly_pace_percent'),
                 habits=json.dumps(get_default_habits()))
    add_weight(user_id, data['weight'])
    bmi_data = calculate_bmi(data['weight'], data['height'])
    calories, floored = calorie_norm_info(data['weight'], data.get('body_fat', 20), data['goal'], data.get('activity_level', 'легкий'))
    floor_line = f"\n{FLOOR_NOTE}" if floored else ""
    macros = calculate_macros(calories, data['weight'], data['goal'])
    clear_state(user_id)
    goal_line = f"🎯 Цель: {data['goal']}"
    if data.get('target_weight'):
        weeks = estimate_weeks_to_goal(data['weight'], data['target_weight'], data['weekly_pace_percent'])
        goal_line = f"🎯 Цель: {data['goal']} → {data['target_weight']} кг (~{weeks} нед.)"
    recommendation = get_bmi_recommendation(bmi_data['status'], data['goal'])
    _after_registration(user_id)
    bot.send_message(user_id, f"🎉 Регистрация завершена, {data['name']}!\n\n📊 Ваш ИМТ: {bmi_data['bmi']} ({bmi_data['status']})\n🍽 Тип питания: {data['diet_type']}\n🍽 Рекомендуемая норма: {calories} ккал/день{floor_line}\n{format_macros(macros)}\n{goal_line}\n\n💡 {recommendation}\n\n{access_line(user_id)}")
    menus = filter_menu(get_all_menus(data['goal']), data['diet_type'], allergies)
    menu_blocks = get_menu_with_recipe_kb(sample_menu(menus))
    bot.send_message(user_id, f"📋 *Ваше персонализированное меню на сегодня*\n🎯 Цель: {data['goal']} | 🥗 {data['diet_type']}\n🔥 Норма калорий: {calories} ккал/день\n{format_macros(macros)}{portion_line(get_user(user_id))}\n💡 Нажмите 📖 Рецепт под любым блюдом:\n\n*Выберите по одному варианту из каждой категории:*", reply_markup=main_menu_keyboard())
    for category, text, kb in menu_blocks:
        bot.send_message(user_id, text, reply_markup=kb)
    tz_kb = types.InlineKeyboardMarkup()
    tz_kb.add(types.InlineKeyboardButton("🌍 Я не в Москве — выбрать пояс", callback_data="tz_menu"))
    bot.send_message(user_id, "⏰ Напоминания и план дня приходят по московскому времени. Если вы в другом часовом поясе — настройте, чтобы они приходили в удобное время.", reply_markup=tz_kb)


# === Дневник еды (без ИИ) ===
def _diary_user(user_id):
    """Пользователь с действующей подпиской или None (с ответом об ошибке)."""
    user = get_user(user_id)
    if not user:
        bot.send_message(user_id, "Сначала пройдите регистрацию: /start")
        return None
    if not is_subscribed(user_id):
        bot.send_message(user_id, "❌ Дневник еды доступен только по подписке.\n\n🎁 Ваш бесплатный период закончился.", reply_markup=sub_inline_kb())
        return None
    return user


def _show_diary(user_id, message_id=None):
    user = get_user(user_id)
    text, kb = food_diary.screen(user, types)
    if message_id:
        try:
            bot.edit_message_text(chat_id=user_id, message_id=message_id, text=text, reply_markup=kb)
            return
        except Exception:
            pass  # сообщение нельзя изменить (устарело или без изменений) — просто пришлём новое
    bot.send_message(user_id, text, reply_markup=kb)


def _cheer_streak(user_id):
    """Поздравление с круглой серией дней дневника (раз в день)."""
    try:
        text = engagement.milestone_message(get_user(user_id))
        if text:
            bot.send_message(user_id, text)
    except Exception as e:
        logger.error(f"Ошибка поздравления с серией {user_id}: {e}")


@bot.callback_query_handler(func=lambda call: call.data == "diary_rem_off")
def diary_rem_off(call):
    update_user(call.from_user.id, reminder_diary_enabled=0)
    bot.answer_callback_query(call.id, "🔕 Напоминания о дневнике отключены")
    bot.send_message(call.from_user.id, "🔕 Вечерние напоминания о дневнике отключены. Включить обратно можно в настройках напоминаний.")


@bot.message_handler(func=lambda m: m.text == "📓 Дневник еды")
def show_diary(message):
    if _diary_user(message.from_user.id):
        _show_diary(message.from_user.id)


@bot.message_handler(commands=['diary'])
def cmd_diary(message):
    show_diary(message)


@bot.callback_query_handler(func=lambda call: call.data == "dback")
def diary_back(call):
    bot.answer_callback_query(call.id)
    clear_state(call.from_user.id)
    if _diary_user(call.from_user.id):
        _show_diary(call.from_user.id, call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data == "dm")
def diary_meals(call):
    bot.answer_callback_query(call.id)
    if _diary_user(call.from_user.id):
        bot.edit_message_text(chat_id=call.from_user.id, message_id=call.message.message_id,
                              text="📓 Что вы ели? Выберите приём пищи:", reply_markup=food_diary.meal_kb(types))


@bot.callback_query_handler(func=lambda call: call.data.startswith("dl_"))
def diary_dishes(call):
    bot.answer_callback_query(call.id)
    user = _diary_user(call.from_user.id)
    if not user:
        return
    try:
        _, ci, page = call.data.split("_")
        ci, page = int(ci), int(page)
        title = food_diary.MEALS[ci][2]
    except (ValueError, IndexError):
        return
    kb, page, pages = food_diary.dishes_kb(user, ci, page, types)
    bot.edit_message_text(chat_id=call.from_user.id, message_id=call.message.message_id,
                          text=f"📓 *{title}* — выберите блюдо (стр. {page + 1}/{pages}):", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("dp_"))
def diary_pick_dish(call):
    bot.answer_callback_query(call.id)
    user = _diary_user(call.from_user.id)
    if not user:
        return
    try:
        _, ci, index = call.data.split("_")
        ci, index = int(ci), int(index)
        line = food_diary.category_dishes(user, ci)[index]
    except (ValueError, IndexError):
        bot.send_message(call.from_user.id, "Этот список обновился, откройте дневник заново: /diary")
        return
    kcal = food_diary.kcal_of(line)
    bot.edit_message_text(chat_id=call.from_user.id, message_id=call.message.message_id,
                          text=f"Сколько вы съели?\n\n*{food_diary.find_dish(line)}*" + (f" — {kcal} ккал за порцию из рецепта" if kcal else ""),
                          reply_markup=food_diary.portion_kb(ci, index, types))


@bot.callback_query_handler(func=lambda call: call.data.startswith("dq_"))
def diary_add_dish(call):
    user = _diary_user(call.from_user.id)
    if not user:
        bot.answer_callback_query(call.id)
        return
    try:
        _, ci, index, percent = call.data.split("_")
        ci, index, percent = int(ci), int(index), int(percent)
        line = food_diary.category_dishes(user, ci)[index]
        label = dict(food_diary.PORTIONS)[percent]
    except (ValueError, IndexError, KeyError):
        bot.answer_callback_query(call.id)
        return
    kcal = food_diary.kcal_of(line)
    if not kcal:
        bot.answer_callback_query(call.id, "У этого блюда нет калорийности — запишите вручную")
        return
    dish_nut = menu_nutrition.dish_scaled(food_diary.find_dish(line), percent / 100)
    add_diary_entry(call.from_user.id, f"{food_diary.find_dish(line)} ({label})", round(kcal * percent / 100), dish_nut)
    bot.answer_callback_query(call.id, "Записано ✅")
    _show_diary(call.from_user.id, call.message.message_id)
    _cheer_streak(call.from_user.id)


BARCODE_HELP = ("📷 Пришлите *фото штрихкода* на упаковке (чтобы полоски были чёткими и крупными) "
                "или напишите *цифры* под ним.\n\nОтмена — /diary")


def _bc_start(user_id):
    if not _diary_user(user_id):
        return
    set_state(user_id, "bc_wait")
    bot.send_message(user_id, BARCODE_HELP)


@bot.callback_query_handler(func=lambda call: call.data == "dbar")
def diary_barcode_ask(call):
    bot.answer_callback_query(call.id)
    _bc_start(call.from_user.id)


@bot.message_handler(commands=["barcode"])
def cmd_barcode(message):
    _bc_start(message.from_user.id)


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "bc_wait" and (m.text or "") != "" and not (m.text or "").startswith("/"))
def barcode_digits(message):
    user_id = message.from_user.id
    code = barcode_lookup.clean_code(message.text)
    if not code:
        bot.send_message(user_id, "Не похоже на штрихкод: нужны только цифры (обычно 13, иногда 8) — они напечатаны под полосками. Попробуйте ещё раз или пришлите фото. Отмена — /diary")
        return
    _barcode_flow(user_id, code)


def _bc_kb(product):
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    if product.get("serving_g"):
        buttons.append(types.InlineKeyboardButton(f"1 порция ({product['serving_g']:g} г)", callback_data=f"bcg_{product['serving_g']:g}"))
    if product.get("package_g") and product["package_g"] != product.get("serving_g"):
        buttons.append(types.InlineKeyboardButton(f"Вся упаковка ({product['package_g']:g} г)", callback_data=f"bcg_{product['package_g']:g}"))
    for grams in (50, 100, 150, 200):
        buttons.append(types.InlineKeyboardButton(f"{grams} г", callback_data=f"bcg_{grams}"))
    kb.add(*buttons)
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data="bcx"))
    return kb


def _bc_card(product):
    title = product["name"] + (f" ({product['brand']})" if product.get("brand") and product["brand"].lower() not in product["name"].lower() else "")
    n = product.get("n") or {}
    macro = " · ".join(f"{label} {n[key]:g}" for key, label in (("protein", "Б"), ("fat", "Ж"), ("carb", "У")) if key in n)
    lines = [f"🏷 *{_md_escape(title)}*", f"{product['kcal']} ккал на 100 г" + (f" · {macro}" if macro else "")]
    if product.get("serving_g"):
        lines.append(f"Порция на упаковке: {product['serving_g']:g} г")
    if product.get("package_g"):
        lines.append(f"Вся упаковка: {product['package_g']:g} г")
    if product.get("warn"):
        lines.append(f"⚠️ _{_md_escape(product['warn'])}_")
    lines.append("\n*Сколько вы съели?* Нажмите кнопку или напишите вес в граммах (например `130`), либо число порций (`2 порции`).")
    lines.append("_Данные из Open Food Facts — их вносят сами пользователи, поэтому изредка бывают неточными._")
    return "\n".join(lines)


def _barcode_flow(user_id, code):
    """Ищет товар по штрихкоду и спрашивает, сколько съели."""
    if not _diary_user(user_id):
        return
    if not barcode_lookup.is_valid(code):
        bot.send_message(user_id, "Цифры введены с ошибкой: не сходится контрольная цифра штрихкода. Проверьте и введите ещё раз (или пришлите фото). Отмена — /diary")
        set_state(user_id, "bc_wait")
        return
    code = barcode_lookup.normalize(code)
    status, product = barcode_lookup.lookup(code)
    if status == barcode_lookup.STATUS_UNAVAILABLE:
        bot.send_message(user_id, "😕 База штрихкодов сейчас не отвечает. Попробуйте через пару минут или добавьте продукт вручную — "
                                  "напишите его одним сообщением, например:\n`йогурт 150 г 78 ккал на 100 г Б 4 Ж 3 У 10`")
        set_state(user_id, "diary_manual")
        return
    if status != barcode_lookup.STATUS_OK:
        try:
            log_food_miss(f"штрихкод {code}")
        except Exception as e:
            logger.error(f"Не удалось записать ненайденный штрихкод: {e}")
        name = (product or {}).get("name")
        reason = ("В базе есть этот товар, но без калорийности." if status == barcode_lookup.STATUS_INCOMPLETE
                  else "Такого штрихкода нет в базе.")
        bot.send_message(user_id, f"😕 {reason}" + (f" ({_md_escape(name)})" if name else "") +
                         "\n\nНапишите продукт одной строкой по данным с упаковки, например:\n"
                         "`йогурт 150 г 78 ккал на 100 г Б 4 Ж 3 У 10`\n"
                         "и нажмите «💾 Запомнить» — в следующий раз он найдётся сам.")
        set_state(user_id, "diary_manual")
        return
    set_state(user_id, "bc_grams", {"product": product})
    bot.send_message(user_id, _bc_card(product), reply_markup=_bc_kb(product))


def _bc_item(product, grams):
    """Позиция дневника из товара по штрихкоду и съеденному весу."""
    per100 = {"kcal": product["kcal"], **(product.get("n") or {})}
    title = product["name"] + (f" ({product['brand']})" if product.get("brand") and product["brand"].lower() not in product["name"].lower() else "")
    item = {"title": title, "grams": round(grams), "kcal": round(product["kcal"] * grams / 100),
            "note": ("из базы товаров" if product.get("from_search") else "по штрихкоду") + (f"; {product['warn']}" if product.get("warn") else ""),
            "n": {k: round(v * grams / 100, 3) for k, v in (product.get("n") or {}).items()}}
    n = product.get("n") or {}
    if all(k in n for k in ("protein", "fat", "carb")):  # можно запомнить в «Мои продукты»
        item["per100"] = {"kcal": product["kcal"], "protein": n["protein"], "fat": n["fat"], "carb": n["carb"],
                          "grams": product.get("serving_g")}
    return item


def _bc_finish(user_id, grams):
    state = get_state(user_id)
    product = state["data"].get("product") if state["step"] == "bc_grams" else None
    if not product:
        bot.send_message(user_id, "Сессия устарела — начните заново: /barcode")
        return
    if not 1 <= grams <= 5000:
        bot.send_message(user_id, "Вес должен быть от 1 до 5000 г. Напишите ещё раз.")
        return
    data = state["data"]
    items = list(data.get("items") or [])
    at = data.get("at")
    new_item = _bc_item(product, grams)
    if isinstance(at, int) and 0 <= at <= len(items):
        items.insert(at, new_item)
    else:
        items.append(new_item)
    unknown = list(data.get("unknown") or [])
    set_state(user_id, "diary_manual", {"items": items, "unknown": unknown})
    bot.send_message(user_id, _diary_preview(items, unknown), reply_markup=_diary_preview_kb(items, unknown))


@bot.callback_query_handler(func=lambda call: call.data.startswith("bcg_"))
def barcode_grams_button(call):
    bot.answer_callback_query(call.id)
    try:
        grams = float(call.data[4:])
    except ValueError:
        return
    _bc_finish(call.from_user.id, grams)


@bot.callback_query_handler(func=lambda call: call.data == "bcx")
def barcode_cancel(call):
    clear_state(call.from_user.id)
    bot.answer_callback_query(call.id, "Отменено")
    try:
        bot.delete_message(call.from_user.id, call.message.message_id)
    except Exception:
        pass


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "bc_grams" and (m.text or "") != "" and not (m.text or "").startswith("/"))
def barcode_grams_text(message):
    user_id = message.from_user.id
    product = get_state(user_id)["data"].get("product") or {}
    match = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(г|гр|грамм\w*|мл|шт\w*|порц\w*|упак\w*)?\.?\s*", message.text.lower())
    if not match:
        bot.send_message(user_id, "Напишите вес числом, например `130`, или число порций: `2 порции`. Отмена — /diary")
        return
    value, unit = float(match.group(1).replace(",", ".")), match.group(2) or "г"
    if unit.startswith(("порц", "шт")):
        if not product.get("serving_g"):
            bot.send_message(user_id, "Размер порции для этого товара неизвестен — напишите вес в граммах.")
            return
        grams = value * product["serving_g"]
    elif unit.startswith("упак"):
        if not product.get("package_g"):
            bot.send_message(user_id, "Вес упаковки для этого товара неизвестен — напишите вес в граммах.")
            return
        grams = value * product["package_g"]
    else:
        grams = value
    _bc_finish(user_id, grams)


FIND_HELP = ("🔎 Напишите название товара из магазина — например `сырники вкусвилл` или `йогурт простоквашино`. "
             "Можно сразу с весом: `кефир домик в деревне 200 г`.\n\nОтмена — /diary")
_QTY_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:г|гр|грамм\w*|мл)\b", re.I)


def _find_results_text(query, results):
    lines = []
    for i, p in enumerate(results, 1):
        brand = f" ({p['brand']})" if p.get("brand") and p["brand"].lower() not in p["name"].lower() else ""
        lines.append(f"{i}. {_md_escape(p['name'] + brand)} — {p['kcal']} ккал/100 г")
    return (f"🔎 По запросу «{_md_escape(query)}» нашёл:\n\n" + "\n".join(lines) +
            "\n\nВыберите нужный. _Данные: Open Food Facts — их вносят сами пользователи, возможны неточности._")


def _find_run(user_id, query, items=None, unknown=None, seg=None, at=None):
    """Ищет товары; при успехе показывает варианты и ждёт выбор."""
    match = _QTY_RE.search(query)
    grams = float(match.group(1).replace(",", ".")) if match else None
    results = offsearch.search(query, limit=5)
    if not results:
        try:
            log_food_miss(("поиск " + query.lower())[:60])
        except Exception as e:
            logger.error(f"Не удалось записать пустой поиск: {e}")
        bot.send_message(user_id, "😕 Среди товаров магазинов такого не нашёл. Попробуйте написать короче (только название и бренд) "
                                  "или добавьте продукт вручную по данным с упаковки: `йогурт 150 г 78 ккал на 100 г Б 4 Ж 3 У 10`")
        set_state(user_id, "diary_manual", {"items": items or [], "unknown": unknown or []}) if items else \
            set_state(user_id, "find_wait", {"items": [], "unknown": unknown or [], "seg": seg})
        return
    set_state(user_id, "find_pick", {"results": results, "items": items or [], "unknown": unknown or [], "seg": seg,
                                     "grams": grams, "query": query, "at": at})
    kb = types.InlineKeyboardMarkup(row_width=5)
    kb.row(*[types.InlineKeyboardButton(str(i + 1), callback_data=f"fnd_{i}") for i in range(len(results))])
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data="bcx"))
    bot.send_message(user_id, _find_results_text(query, results), reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data == "dfind")
def diary_find_ask(call):
    bot.answer_callback_query(call.id)
    if not _diary_user(call.from_user.id):
        return
    if not offsearch.available():
        bot.send_message(call.from_user.id, "Поиск по товарам магазинов пока не подключён.")
        return
    set_state(call.from_user.id, "find_wait", {"items": [], "unknown": [], "seg": None})
    bot.send_message(call.from_user.id, FIND_HELP)


@bot.message_handler(commands=["find"])
def cmd_find(message):
    if not _diary_user(message.from_user.id):
        return
    if not offsearch.available():
        bot.send_message(message.from_user.id, "Поиск по товарам магазинов пока не подключён.")
        return
    set_state(message.from_user.id, "find_wait", {"items": [], "unknown": [], "seg": None})
    bot.send_message(message.from_user.id, FIND_HELP)


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] in ("find_wait", "find_pick") and (m.text or "") != "" and not (m.text or "").startswith("/"))
def find_text(message):
    user_id = message.from_user.id
    data = get_state(user_id)["data"]
    _find_run(user_id, message.text.strip()[:100], data.get("items"), data.get("unknown"), data.get("seg"), data.get("at"))


@bot.callback_query_handler(func=lambda call: call.data.startswith("fns_"))
def find_from_unknown(call):
    """Кнопка «Найти …» под непонятым продуктом в предпросмотре."""
    user_id = call.from_user.id
    state = get_state(user_id)
    try:
        seg = state["data"]["unknown"][int(call.data[4:])]
        items = state["data"]["items"]
    except (KeyError, IndexError, ValueError, TypeError):
        bot.answer_callback_query(call.id, "Список устарел — введите заново")
        return
    bot.answer_callback_query(call.id)
    _find_run(user_id, seg, items, state["data"].get("unknown"), seg)


@bot.callback_query_handler(func=lambda call: call.data.startswith("fni_"))
def find_from_item(call):
    """Кнопка под позицией, где часть слов не учтена: ищем всю фразу среди товаров магазинов, найденное заменит позицию."""
    user_id = call.from_user.id
    state = get_state(user_id)
    try:
        index = int(call.data[4:])
        items = state["data"]["items"]
        seg = items[index]["seg"]
    except (KeyError, IndexError, ValueError, TypeError):
        bot.answer_callback_query(call.id, "Список устарел — введите заново")
        return
    bot.answer_callback_query(call.id)
    _find_run(user_id, seg, items, state["data"].get("unknown"), None, at=index)


@bot.callback_query_handler(func=lambda call: call.data.startswith("fnd_"))
def find_pick(call):
    user_id = call.from_user.id
    state = get_state(user_id)
    try:
        data = state["data"]
        product = dict(data["results"][int(call.data[4:])])
    except (KeyError, IndexError, ValueError, TypeError):
        bot.answer_callback_query(call.id, "Список устарел — найдите заново")
        return
    bot.answer_callback_query(call.id)
    product["from_search"] = True
    unknown = list(data.get("unknown") or [])
    if data.get("seg") in unknown:
        unknown.remove(data["seg"])
    items = list(data.get("items") or [])
    at = data.get("at")
    if isinstance(at, int) and 0 <= at < len(items):
        items.pop(at)  # товар из магазина заменяет приблизительную позицию
    else:
        at = None
    keep = {"product": product, "items": items, "unknown": unknown, "at": at}
    set_state(user_id, "bc_grams", keep)
    if data.get("grams"):  # вес уже был в запросе
        _bc_finish(user_id, data["grams"])
        return
    bot.send_message(user_id, _bc_card(product), reply_markup=_bc_kb(product))


@bot.callback_query_handler(func=lambda call: call.data == "dman")
def diary_manual_ask(call):
    bot.answer_callback_query(call.id)
    if not _diary_user(call.from_user.id):
        return
    set_state(call.from_user.id, "diary_manual")
    bot.send_message(call.from_user.id,
                     "✏️ Напишите, что вы съели, одним сообщением — я сам посчитаю калории.\n\n"
                     "Например:\n`гречка 150 г, 2 яйца, кофе с молоком 200 мл`\n"
                     "`борщ 300 г, хлеб 2 куска, сметана 1 ст.л.`\n\n"
                     "Если продукта нет в моей таблице — укажите калории сами: `пирожное 300 ккал` "
                     "или с упаковки: `йогурт 150 г 78 ккал на 100 г Б 4 Ж 3 У 10` — потом его можно запомнить кнопкой.\n"
                     "Отмена — /diary")


def _diary_preview(items, unknown):
    lines = []
    for i, it in enumerate(items, 1):
        qty = f"{it['grams']} г — " if it["grams"] else ""
        line = f"{i}. {_md_escape(it['title'])}: {qty}*{it['kcal']} ккал*"
        if it["note"]:
            line += f"\n    _{_md_escape(it['note'])}_"
        lines.append(line)
    text = "🍽 *Вот что я понял:*\n\n" + "\n".join(lines)
    text += f"\n\n🔥 Итого: *{sum(it['kcal'] for it in items)} ккал*"
    sums = {}
    for it in items:
        for k, v in (it.get("n") or {}).items():
            sums[k] = sums.get(k, 0) + v
    macro = nutrient_utils.macro_line(sums)
    if macro:
        text += "\n" + macro
    if unknown:
        text += ("\n\n❓ Не нашёл в таблице: " + ", ".join(_md_escape(u) for u in unknown) +
                 "\nНапишите такие продукты ещё раз с калориями, например `шашлык 250 ккал`.")
    text += "\n\nЗаписать в дневник? Калорийность ориентировочная — по средним справочным данным."
    return text


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "diary_manual" and not (m.text or "").startswith("/"))
def diary_manual_save(message):
    user_id = message.from_user.id
    code = barcode_lookup.clean_code(message.text)
    if code:  # в дневник прислали только цифры штрихкода
        _barcode_flow(user_id, code)
        return
    items, unknown = food_parse.parse_text(message.text, extra=food_parse.user_pool(get_user_foods(user_id)))
    for segment in unknown:  # копим запросы, которых нет в базе, чтобы дополнять её по реальным запросам
        try:
            log_food_miss(food_parse.miss_key(segment))
        except Exception as e:
            logger.error(f"Не удалось записать ненайденный запрос: {e}")
    if not items:
        bot.send_message(user_id, "Не смог разобрать 🤔 Пишите продукты через запятую с количеством, например:\n"
                                  "`гречка 150 г, 2 яйца, чай`\nЕсли продукт редкий — добавьте калории: `пирожное 300 ккал` "
                                  "или с упаковки: `йогурт 150 г 78 ккал на 100 г`.")
        return
    set_state(user_id, "diary_manual", {"items": items, "unknown": unknown})
    bot.send_message(user_id, _diary_preview(items, unknown), reply_markup=_diary_preview_kb(items, unknown))


def _diary_preview_kb(items, unknown=None):
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(types.InlineKeyboardButton("✅ Записать", callback_data="dtok"),
           types.InlineKeyboardButton("❌ Отмена", callback_data="dtno"))
    sized = [(i, it) for i, it in enumerate(items) if it.get("base")][:3]
    for i, it in sized:
        # порция «на глаз»: подпись с названием, если позиций с выбором несколько
        short = f" {it['title'].split('(')[0].strip()[:12]}" if len(sized) > 1 else ""
        current = it.get("size") or "m"
        kb.row(*[types.InlineKeyboardButton(("✅ " if current == key else "") + label + short, callback_data=f"dpz_{i}_{key}")
                 for key, label in (("s", "🤏 Мало"), ("m", "⚖️ Обычно"), ("l", "🍽 Много"))])
    if offsearch.available():
        shown = 0
        for i, seg in enumerate((unknown or [])[:2]):
            kb.add(types.InlineKeyboardButton(f"🔎 Найти «{seg[:22]}» среди товаров", callback_data=f"fns_{i}"))
            shown += 1
        for i, it in enumerate(items):  # «йогурт простоквашино»: бот взял общий йогурт, но это может быть конкретный товар
            if shown >= 3:
                break
            if it.get("seg") and it.get("extra_words"):
                words = " ".join(it["extra_words"])[:18]
                kb.add(types.InlineKeyboardButton(f"🔎 «{words}» — это товар из магазина?", callback_data=f"fni_{i}"))
                shown += 1
    for i, it in enumerate(items):
        if it.get("per100"):
            kb.add(types.InlineKeyboardButton(f"💾 Запомнить «{it['title'][:24]}»", callback_data=f"dsv_{i}"))
    return kb


@bot.callback_query_handler(func=lambda call: call.data.startswith("dpz_"))
def diary_resize(call):
    """Выбор размера порции «на глаз» в предпросмотре записи."""
    user_id = call.from_user.id
    state = get_state(user_id)
    items = state["data"].get("items") if state["step"] == "diary_manual" else None
    try:
        _, i, size = call.data.split("_")
        i = int(i)
        new_item = food_parse.resize_item(items[i], size)
    except (TypeError, ValueError, IndexError, KeyError, AttributeError):
        bot.answer_callback_query(call.id, "Список устарел — введите заново")
        return
    items[i] = new_item
    set_state(user_id, "diary_manual", {"items": items, "unknown": state["data"].get("unknown", [])})
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id,
                              text=_diary_preview(items, state["data"].get("unknown", [])),
                              reply_markup=_diary_preview_kb(items, state["data"].get("unknown", [])))
    except Exception:
        pass  # «сообщение не изменилось» — повторное нажатие на тот же размер


@bot.callback_query_handler(func=lambda call: call.data.startswith("dsv_"))
def diary_save_food(call):
    user_id = call.from_user.id
    state = get_state(user_id)
    items = state["data"].get("items") if state["step"] == "diary_manual" else None
    try:
        item = items[int(call.data[4:])]
        per100 = item["per100"]
    except (TypeError, ValueError, IndexError, KeyError):
        bot.answer_callback_query(call.id, "Список устарел — введите продукт заново")
        return
    result = _save_my_food(user_id, item["title"], per100)
    bot.answer_callback_query(call.id, _MYFOOD_TOAST[result], show_alert=(result == "limit"))


_MYFOOD_TOAST = {"added": "Запомнил ✅ Теперь можно писать просто название", "updated": "Обновил ✅",
                 "limit": f"Достигнут лимит: {MAX_USER_FOODS} своих продуктов. Удалите ненужные в /myfoods"}


def _save_my_food(user_id, title, per100):
    return add_user_food(user_id, title, per100["kcal"], per100.get("protein"), per100.get("fat"), per100.get("carb"),
                         per100.get("grams"))


def _myfoods_screen(user_id):
    rows = get_user_foods(user_id)
    lines = ["🧺 *Мои продукты*", ""]
    kb = types.InlineKeyboardMarkup()
    if rows:
        for r in rows:
            macro = " · ".join(f"{label} {r[key]:g}" for key, label in (("protein", "Б"), ("fat", "Ж"), ("carb", "У")) if r.get(key) is not None)
            lines.append(f"• {_md_escape(r['name'])} — {r['kcal']:g} ккал/100 г" + (f" ({macro})" if macro else "") +
                         (f", порция {r['portion']:g} г" if r.get("portion") else ""))
            kb.add(types.InlineKeyboardButton(f"🗑 {r['name'][:28]}", callback_data=f"mfd_{r['id']}"))
        lines.append("\nПишите в дневнике просто название (`йогурт`) или с количеством (`йогурт 2 шт`) — калории возьму отсюда.")
    else:
        lines.append("Пока пусто. Здесь хранятся продукты с упаковки, которые вы едите часто.")
    kb.add(types.InlineKeyboardButton("➕ Добавить продукт", callback_data="mfa"))
    return "\n".join(lines), kb


@bot.message_handler(commands=["myfoods"])
def cmd_myfoods(message):
    if not _diary_user(message.from_user.id):
        return
    text, kb = _myfoods_screen(message.from_user.id)
    bot.send_message(message.from_user.id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("mfd_"))
def myfood_delete(call):
    try:
        delete_user_food(call.from_user.id, int(call.data[4:]))
    except ValueError:
        pass
    bot.answer_callback_query(call.id, "Удалено")
    text, kb = _myfoods_screen(call.from_user.id)
    try:
        bot.edit_message_text(chat_id=call.from_user.id, message_id=call.message.message_id, text=text, reply_markup=kb)
    except Exception:
        bot.send_message(call.from_user.id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data == "mfa")
def myfood_add_ask(call):
    bot.answer_callback_query(call.id)
    if not _diary_user(call.from_user.id):
        return
    set_state(call.from_user.id, "myfood_new")
    bot.send_message(call.from_user.id,
                     "➕ Напишите продукт с упаковки (значения на 100 г), например:\n"
                     "`Йогурт Простоквашино 78 ккал на 100 г, Б 4 Ж 3 У 10, 150 г`\n\n"
                     "Б, Ж, У можно не указывать. Количество в конце («150 г») станет вашей порцией по умолчанию.\n"
                     "Отмена — /myfoods")


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "myfood_new" and not (m.text or "").startswith("/"))
def myfood_add_save(message):
    user_id = message.from_user.id
    items, _ = food_parse.parse_text(message.text)
    item = next((it for it in items if it.get("per100")), None)
    if not item:
        bot.send_message(user_id, "Не получилось 🤔 Нужны калории на 100 г, например: `Йогурт 78 ккал на 100 г Б 4 Ж 3 У 10, 150 г`")
        return
    result = _save_my_food(user_id, item["title"], item["per100"])
    clear_state(user_id)
    bot.send_message(user_id, _MYFOOD_TOAST[result])
    text, kb = _myfoods_screen(user_id)
    bot.send_message(user_id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data in ("dtok", "dtno"))
def diary_text_confirm(call):
    user_id = call.from_user.id
    state = get_state(user_id)
    items = state["data"].get("items") if state["step"] == "diary_manual" else None
    if call.data == "dtno" or not items:
        clear_state(user_id)
        bot.answer_callback_query(call.id, "Отменено" if call.data == "dtno" else "Список устарел — введите заново")
        try:
            bot.delete_message(user_id, call.message.message_id)
        except Exception:  # сообщение старше 48 ч или уже удалено — хотя бы уберём кнопки
            try:
                bot.edit_message_reply_markup(chat_id=user_id, message_id=call.message.message_id, reply_markup=None)
            except Exception:
                pass
        return
    if not _diary_user(user_id):
        bot.answer_callback_query(call.id)
        return
    for it in items:
        title = f"{it['title']}, {it['grams']} г" if it["grams"] else it["title"]
        add_diary_entry(user_id, title, it["kcal"], it.get("n") or None)
    clear_state(user_id)
    bot.answer_callback_query(call.id, "Записано ✅")
    try:
        bot.edit_message_reply_markup(chat_id=user_id, message_id=call.message.message_id, reply_markup=None)
    except Exception:
        pass
    _show_diary(user_id)
    _cheer_streak(user_id)


@bot.callback_query_handler(func=lambda call: call.data == "dnut")
def diary_nutrients(call):
    bot.answer_callback_query(call.id)
    if not _diary_user(call.from_user.id):
        return
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ К дневнику", callback_data="dback"))
    text = nutrient_utils.details_text(get_diary(call.from_user.id))
    try:
        bot.edit_message_text(chat_id=call.from_user.id, message_id=call.message.message_id, text=text, reply_markup=kb)
    except Exception:
        bot.send_message(call.from_user.id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data == "ddm")
def diary_delete_menu(call):
    bot.answer_callback_query(call.id)
    if _diary_user(call.from_user.id):
        bot.edit_message_text(chat_id=call.from_user.id, message_id=call.message.message_id,
                              text="🗑 Какую запись удалить?", reply_markup=food_diary.delete_kb(call.from_user.id, types))


@bot.callback_query_handler(func=lambda call: call.data.startswith("dd_"))
def diary_delete(call):
    try:
        entry_id = int(call.data.split("_")[1])
    except ValueError:
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id, "Удалено" if delete_diary_entry(call.from_user.id, entry_id) else "Уже удалено")
    _show_diary(call.from_user.id, call.message.message_id)


def send_today_plan(user_id):
    """«План дня» по запросу: то же сообщение, что приходит утром, в любое время и даже при отключённой рассылке."""
    from dailyplan import build_daily_plan, plan_keyboard
    user = get_user(user_id)
    if not user:
        bot.send_message(user_id, "Сначала пройдите регистрацию: /start")
        return
    if not is_subscribed(user_id):
        bot.send_message(user_id, "❌ План дня доступен только по подписке.\n\n🎁 Ваш бесплатный период закончился.", reply_markup=sub_inline_kb())
        return
    plan = build_daily_plan(user)
    if not plan:
        bot.send_message(user_id, "Пока не могу составить план: не хватает данных профиля. Обновите их: /update")
        return
    bot.send_message(user_id, plan['text'], reply_markup=plan_keyboard(plan, bool(user.get('reminder_plan_enabled', 1))))


@bot.message_handler(func=lambda m: m.text == "📅 План на сегодня")
def show_today_plan(message):
    send_today_plan(message.from_user.id)


@bot.message_handler(commands=['plan'])
def cmd_plan(message):
    send_today_plan(message.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data == "plan_on")
def plan_on(call):
    update_user(call.from_user.id, reminder_plan_enabled=1)
    bot.answer_callback_query(call.id, "🔔 Включено")
    bot.send_message(call.from_user.id, "🔔 Буду присылать план дня каждое утро в 08:30 по вашему времени.")


@bot.message_handler(func=lambda m: m.text == "📋 Моё меню")
def show_menu(message):
    user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        bot.send_message(user_id, "Сначала пройдите регистрацию: /start")
        return
    if not is_subscribed(user_id):
        bot.send_message(user_id, "❌ Доступ к меню только по подписке.\n\n🎁 Ваш бесплатный период закончился.", reply_markup=sub_inline_kb())
        return
    goal = user.get('goal', 'похудение')
    diet = user.get('diet_type', 'обычное')
    allergies = user.get('allergies', '')
    calories = user_calorie_norm(user, goal)
    macros = calculate_macros(calories, user['weight'], goal)
    menus = filter_menu(get_all_menus(goal), diet, allergies)
    menu_blocks = get_menu_with_recipe_kb(sample_menu(menus))
    bot.send_message(user_id, f"📋 *Ваше персонализированное меню*\n🎯 Цель: {goal} | 🥗 {diet}\n🔥 Норма калорий: {calories} ккал/день\n{format_macros(macros)}{portion_line(user)}\n💡 Нажмите 📖 Рецепт под любым блюдом:\n\n*Выберите по одному варианту из каждой категории:*")
    for category, text, kb in menu_blocks:
        bot.send_message(user_id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("recipe_"))
def show_recipe(call):
    short_hash = call.data.replace("recipe_", "")
    dish_name, recipe = get_recipe_by_hash(short_hash)
    if recipe:
        text = format_recipe(recipe, dish_name)
        text += menu_nutrition.recipe_block(dish_name)
        phrase = portion_info(get_user(call.from_user.id))["phrase"]
        if phrase:
            text += f"\n\n📏 Для вашей нормы калорий делайте порцию {phrase}, чем в рецепте (умножьте количества ингредиентов)."
        nut_kb = types.InlineKeyboardMarkup()
        nut_kb.add(types.InlineKeyboardButton("🔬 Витамины и минералы", callback_data=f"rn_{short_hash}"))
        bot.send_message(call.from_user.id, text, reply_markup=nut_kb)
    else:
        bot.send_message(call.from_user.id, "📖 Рецепт для этого блюда пока в разработке.\nСкоро добавим! 🍳")
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("rn_"))
def show_recipe_nutrients(call):
    bot.answer_callback_query(call.id)
    dish_name, recipe = get_recipe_by_hash(call.data[3:])
    if recipe:
        bot.send_message(call.from_user.id, menu_nutrition.dish_details(dish_name))


@bot.callback_query_handler(func=lambda call: call.data.startswith("reshuffle_"))
def reshuffle_category(call):
    user_id = call.from_user.id
    category = call.data.replace("reshuffle_", "")
    user = get_user(user_id)
    if not user:
        bot.answer_callback_query(call.id, "Сначала пройдите регистрацию: /start")
        return
    goal = user.get('goal', 'похудение')
    diet = user.get('diet_type', 'обычное')
    allergies = user.get('allergies', '')
    menus = filter_menu(get_all_menus(goal), diet, allergies)
    new_options = sample_menu(menus).get(category, [])
    new_menu_blocks = get_menu_with_recipe_kb({category: new_options})
    matching = [block for block in new_menu_blocks if block[0] == category]
    if not matching:
        bot.answer_callback_query(call.id, "Не удалось обновить варианты")
        return
    _, text, kb = matching[0]
    try:
        bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id, text=text, reply_markup=kb)
    except Exception:
        pass  # Telegram иногда отклоняет edit, если новый текст совпал со старым — не критично
    bot.answer_callback_query(call.id, "Обновили варианты 🔄")


def shopping_inline_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔄 Составить заново", callback_data="shop_regen"))
    return kb


def send_shopping_list(user_id):
    """Составляет меню на неделю из блюд с рецептами и присылает список покупок."""
    user = get_user(user_id)
    if not user:
        bot.send_message(user_id, "Сначала пройдите регистрацию: /start")
        return
    if not is_subscribed(user_id):
        bot.send_message(user_id, "❌ Список покупок доступен только по подписке.\n\n🎁 Ваш бесплатный период закончился.", reply_markup=sub_inline_kb())
        return
    goal = user.get('goal', 'похудение')
    diet = user.get('diet_type', 'обычное')
    allergies = user.get('allergies', '')
    menus = filter_menu(get_all_menus(goal), diet, allergies)
    portions = portion_info(user)
    result = build_weekly_shopping(menus, banned=get_ingredient_banned_keywords(diet, allergies),
                                   portion_factor=portions['factor'], portion_phrase=portions['phrase'])
    if not result:
        bot.send_message(user_id, "🛒 Не получилось составить список: для подходящих вам блюд пока нет рецептов с ингредиентами.")
        return
    shopping_text, plan_text = result
    for chunk in split_message(shopping_text):
        bot.send_message(user_id, chunk)
    plan_chunks = split_message(plan_text)
    for i, chunk in enumerate(plan_chunks):
        is_last = i == len(plan_chunks) - 1
        bot.send_message(user_id, chunk, reply_markup=shopping_inline_kb() if is_last else None)


@bot.message_handler(func=lambda m: m.text == "🛒 Список покупок")
def show_shopping_list(message):
    send_shopping_list(message.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data == "shop_regen")
def shopping_regen(call):
    bot.answer_callback_query(call.id, "Составляем заново 🔄")
    send_shopping_list(call.from_user.id)


def slip_options_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    for key, label in SLIP_OPTIONS:
        kb.add(types.InlineKeyboardButton(label, callback_data=f"slip_{key}"))
    return kb


def slip_back_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="slip_back"))
    return kb


# Поддержка при срыве доступна всем, без подписки и регистрации: прятать её за оплату нельзя.
@bot.message_handler(func=lambda m: m.text == "💚 Если сорвался")
def show_slip_help(message):
    mark_slip(message.from_user.id)
    bot.send_message(message.from_user.id, SLIP_INTRO, reply_markup=slip_options_kb())


@bot.callback_query_handler(func=lambda call: call.data.startswith("slip_"))
def slip_answer(call):
    user_id = call.from_user.id
    key = call.data.replace("slip_", "")
    bot.answer_callback_query(call.id)
    if key == "back":
        text, kb = SLIP_INTRO, slip_options_kb()
    elif key in SLIP_ANSWERS:
        mark_slip(user_id)
        text, kb = SLIP_ANSWERS[key], slip_back_kb()
    else:
        return
    try:
        bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id, text=text, reply_markup=kb)
    except Exception:
        bot.send_message(user_id, text, reply_markup=kb)


@bot.message_handler(func=lambda m: m.text == "✅ Привычки")
def show_habits(message):
    user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        bot.send_message(user_id, "Сначала пройдите регистрацию: /start")
        return
    bot.send_message(user_id, habits_screen_text(user_id), reply_markup=habits_inline_kb(user_id))


@bot.callback_query_handler(func=lambda call: call.data == "habits_del_menu")
def habits_del_menu(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    if get_user(user_id):
        bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id,
                              text="🗑 *Удаление привычек*\n\nНажмите на привычку, которую хотите удалить:",
                              reply_markup=habits_delete_kb(user_id))


@bot.callback_query_handler(func=lambda call: call.data.startswith("hdel_"))
def habit_delete(call):
    user_id = call.from_user.id
    try:
        index = int(call.data.split("_", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id)
        return
    removed = remove_habit(user_id, index) if get_user(user_id) else None
    bot.answer_callback_query(call.id, "Удалено" if removed else "Эта привычка уже удалена")
    if get_habits(user_id):
        bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id,
                              text="🗑 *Удаление привычек*\n\nНажмите на привычку, которую хотите удалить:",
                              reply_markup=habits_delete_kb(user_id))
    else:
        bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id,
                              text=habits_screen_text(user_id), reply_markup=habits_inline_kb(user_id))


@bot.callback_query_handler(func=lambda call: call.data == "habits_open")
def habits_open(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    if get_user(user_id):
        bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id, text=habits_screen_text(user_id), reply_markup=habits_inline_kb(user_id))


@bot.callback_query_handler(func=lambda call: call.data.startswith("habit_"))
def process_habit(call):
    action = call.data.replace("habit_", "")
    user_id = call.from_user.id
    if action == "add":
        bot.send_message(user_id, "✏️ Напишите название новой привычки:")
        set_state(user_id, "add_habit")
        bot.answer_callback_query(call.id)
        return
    try:
        habit_index = int(action)
        complete_habit(user_id, habit_index)
        bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id, text=habits_screen_text(user_id), reply_markup=habits_inline_kb(user_id))
        bot.answer_callback_query(call.id, "Привычка отмечена! 💪")
    except ValueError:
        bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "add_habit")
def add_new_habit(message):
    user_id = message.from_user.id
    habit_text = message.text.strip()
    if len(habit_text) < 3:
        bot.send_message(user_id, "Слишком коротко. Попробуйте ещё раз:")
        return
    add_habit(user_id, habit_text)
    clear_state(user_id)
    bot.send_message(user_id, f"✅ Привычка «{habit_text}» добавлена!")
    show_habits(message)


@bot.message_handler(func=lambda m: m.text == "⚖️ Вес")
def show_weight(message):
    user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        bot.send_message(user_id, "Сначала пройдите регистрацию: /start")
        return
    history = get_weight_history(user_id, limit=5)
    current_weight = user.get('weight', 0)
    change = get_weight_change(user_id)
    history_text = ""
    if history:
        history_text = "\n\n📉 *История веса (последние 5):*\n"
        for record in history:
            date = datetime.fromisoformat(record['recorded_at']).strftime("%d.%m")
            history_text += f"   • {date}: {record['weight']} кг\n"
    change_text = ""
    if change is not None:
        arrow = "📉" if change < 0 else "📈" if change > 0 else "➡️"
        change_text = f"\n\n{arrow} Изменение: {change:+.1f} кг"
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("📝 Обновить вес", callback_data="weight_update"), types.InlineKeyboardButton("📊 График", callback_data="weight_chart"))
    kb.add(types.InlineKeyboardButton("🗓 Отчёт за месяц", callback_data="month_report"))
    bot.send_message(user_id, f"⚖️ *Трекер веса*\n\nТекущий вес: *{current_weight} кг*{change_text}{history_text}\n\n💡 Рекомендуется взвешиваться 1 раз в неделю утром натощак.", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data == "weight_update")
def weight_update(call):
    user_id = call.from_user.id
    bot.send_message(user_id, "✏️ Введите ваш текущий вес (в кг):")
    set_state(user_id, "update_weight")
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "weight_chart")
def weight_chart_handler(call):
    user_id = call.from_user.id
    chart = generate_weight_chart(user_id)
    if chart:
        bot.send_photo(user_id, chart, caption="📊 Ваша динамика веса")
    else:
        bot.send_message(user_id, "Недостаточно данных для графика (нужно минимум 2 записи).")
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "month_report")
def monthly_report_handler(call):
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)
    if not is_subscribed(user_id):
        bot.send_message(user_id, "❌ Отчёт за месяц доступен только по подписке.\n\n🎁 Ваш бесплатный период закончился.", reply_markup=sub_inline_kb())
        return
    report = generate_monthly_report(user_id)
    if report:
        bot.send_photo(user_id, report, caption="🗓 Ваш отчёт за 30 дней")
    else:
        bot.send_message(user_id, "Пока мало данных для отчёта: нужно хотя бы 2 взвешивания за месяц или 3 дня с отметками привычек.")


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "update_weight")
def process_weight_update(message):
    user_id = message.from_user.id
    try:
        weight = float(message.text.replace(",", "."))
        if not 30 <= weight <= 300:
            raise ValueError
        add_weight(user_id, weight)
        change = get_weight_change(user_id)
        change_text = ""
        if change is not None:
            arrow = "📉" if change < 0 else "📈" if change > 0 else "➡️"
            change_text = f"\n{arrow} Изменение: {change:+.1f} кг"
        clear_state(user_id)
        bot.send_message(user_id, f"✅ Вес обновлён: *{weight} кг*{change_text}\n\nПродолжайте в том же духе!")
        show_weight(message)
    except ValueError:
        bot.send_message(user_id, "Пожалуйста, введите корректный вес (30–300 кг).")


def food_locked_message(user_id):
    """Что ответить, если анализ еды недоступен по подписке или тарифу."""
    if not is_subscribed(user_id):
        bot.send_message(user_id, "🔒 Анализ еды доступен по подписке.", reply_markup=sub_inline_kb())
    elif FULL_TIER_ENABLED:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(tier_label("full"), callback_data="plan_full"))
        bot.send_message(user_id, f"🔒 Анализ еды входит в тариф «{tier_name('full')}».", reply_markup=kb)
    else:
        bot.send_message(user_id, "🔒 Анализ еды пока недоступен.")


@bot.message_handler(func=lambda m: m.text == "📸 Анализ еды")
def show_food_analysis_info(message):
    user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        bot.send_message(user_id, "Сначала пройдите регистрацию: /start")
        return
    if not FOOD_ANALYSIS_ENABLED:
        bot.send_message(user_id, "🔒 *Фото-анализ еды*\n\nЭта функция временно недоступна.\nАдминистратор настраивает AI...")
        return
    if not has_food_analysis(user_id):
        food_locked_message(user_id)
        return
    today_cals = get_today_food_calories(user_id)
    daily_goal = user_calorie_norm(user)
    bot.send_message(user_id, f"📸 *Анализ еды по фото*\n\nОтправьте мне фото вашей еды, и я проанализирую:\n• Что на фото\n• Примерную калорийность\n• Состав (Б/Ж/У)\n• Рекомендацию\n\n📊 Сегодня съедено: ~{today_cals} ккал из {daily_goal} ккал\n{get_progress_bar(today_cals, daily_goal)}")


@bot.message_handler(content_types=['photo'])
def handle_food_photo(message):
    user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        bot.send_message(user_id, "Сначала пройдите регистрацию: /start")
        return
    # Сначала ищем штрихкод: это быстро (локально) и доступно всем, у кого есть дневник
    try:
        photo_file = bot.get_file(message.photo[-1].file_id)
        code = barcode_lookup.decode_photo(bot.download_file(photo_file.file_path))
    except Exception as e:
        logger.error(f"Ошибка чтения штрихкода с фото: {e}")
        code = None
    if code:
        _barcode_flow(user_id, code)
        return
    barcode_waiting = get_state(user_id)["step"] == "bc_wait"
    if barcode_waiting or not FOOD_ANALYSIS_ENABLED or not has_food_analysis(user_id):
        if barcode_waiting or not FOOD_ANALYSIS_ENABLED:
            hint = ("" if barcode_lookup.decoder_available() else "\n\n(На сервере не установлено распознавание штрихкодов с фото — "
                                                                  "пока введите цифры под полосками.)")
            bot.send_message(user_id, "Штрихкод на фото не нашёл 🤔 Сфотографируйте ближе и ровно, чтобы полоски заняли почти весь кадр, "
                                      "или напишите цифры под ними." + hint)
            return
    if not FOOD_ANALYSIS_ENABLED:
        bot.send_message(user_id, "🔒 Фото-анализ отключён.")
        return
    if not has_food_analysis(user_id):
        food_locked_message(user_id)
        return
    bot.send_message(user_id, "🔍 Анализирую фото... Это займёт несколько секунд.")
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        photo_bytes = bot.download_file(file_info.file_path)
        result = analyze_food_photo(photo_bytes)
        if result.get("error"):
            bot.send_message(user_id, f"❌ {result['error']}")
            return
        estimated_cals = result.get('calories') or 0
        log_food_analysis(user_id, message.photo[-1].file_id, result['text'], estimated_cals)
        today_cals = get_today_food_calories(user_id)
        daily_goal = user_calorie_norm(user)
        bot.send_message(user_id, f"📸 *Результат анализа*\n\n{result['text']}\n\n📊 Сегодня всего: ~{today_cals} ккал из {daily_goal} ккал\n{get_progress_bar(today_cals, daily_goal)}")
    except Exception as e:
        logger.error(f"Ошибка обработки фото: {e}")
        bot.send_message(user_id, "❌ Не удалось обработать фото. Попробуйте ещё раз.")


@bot.message_handler(func=lambda m: m.text == "📈 Статистика")
def show_stats(message):
    user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        bot.send_message(user_id, "Сначала пройдите регистрацию: /start")
        return
    if not is_subscribed(user_id):
        bot.send_message(user_id, "❌ Статистика доступна только по подписке.\n\n🎁 Ваш бесплатный период закончился.", reply_markup=sub_inline_kb())
        return
    weekly = get_weekly_stats(user_id)
    if not weekly:
        bot.send_message(user_id, "📈 *Ваша статистика*\n\nПока недостаточно данных. Продолжайте отмечать привычки — статистика появится через несколько дней!")
        return
    total_habits_done = sum(day['habits_done'] for day in weekly)
    total_habits_total = sum(day['habits_total'] for day in weekly)
    days_count = len(weekly)
    habits_percent = int(100 * total_habits_done / total_habits_total) if total_habits_total > 0 else 0
    weight_history = get_weight_history(user_id, limit=2)
    weight_text = ""
    if len(weight_history) >= 2:
        first = weight_history[-1]['weight']
        last = weight_history[0]['weight']
        diff = round(last - first, 1)
        arrow = "📉" if diff < 0 else "📈" if diff > 0 else "➡️"
        weight_text = f"\n{arrow} Изменение веса: {diff:+.1f} кг"
    bot.send_message(user_id, f"📈 *Ваша статистика за {days_count} дней*\n\n✅ Привычек выполнено: {total_habits_done} из {total_habits_total}\n✅ Процент выполнения: {habits_percent}%\n{get_progress_bar(total_habits_done, total_habits_total)}{weight_text}\n\n💡 Регулярность — ключ к результату!")


@bot.message_handler(func=lambda m: m.text == "📊 Прогресс")
def show_progress(message):
    user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        bot.send_message(user_id, "Сначала пройдите регистрацию: /start")
        return
    bmi_data = calculate_bmi(user['weight'], user['height'])
    adjustment = user.get('calorie_adjustment') or 0
    calories, floored = calorie_norm_info(user['weight'], user.get('body_fat', 20), user['goal'], user.get('activity_level', 'легкий'), adjustment)
    floor_text = f"\n{FLOOR_NOTE}" if floored else ""
    macros = calculate_macros(calories, user['weight'], user['goal'])
    habits = get_habits(user_id)
    completed = get_completed_habits(user_id)
    change = get_weight_change(user_id)
    weight_change_text = ""
    if change is not None:
        arrow = "📉" if change < 0 else "📈" if change > 0 else "➡️"
        weight_change_text = f"\n{arrow} Вес: {change:+.1f} кг"
    today_food = get_today_food_calories(user_id)
    food_text = f"\n🍽 Еда сегодня: ~{today_food}/{calories} ккал\n{get_progress_bar(today_food, calories)}" if (FOOD_ANALYSIS_ENABLED or today_food) else ""
    adjustment_text = f"\n🔧 Норма скорректирована из-за плато веса ({adjustment:+d} ккал)" if adjustment else ""
    streak_text = format_streak_message(get_habit_streak(user_id))
    streak_line = f"\n{streak_text.splitlines()[0]}" if streak_text else ""
    bot.send_message(user_id, f"📊 *Ваш прогресс*\n\n👤 {user['name']}, {user['age']} лет\n⚖️ {user['weight']} кг | {user['height']} см{weight_change_text}\n📈 ИМТ: {bmi_data['bmi']} ({bmi_data['status']})\n🎯 Цель: {user['goal']}\n🥗 Питание: {user.get('diet_type', 'обычное')}\n🔥 Калории: {calories} ккал/день{adjustment_text}{floor_text}{food_text}\n\n🥗 *Норма БЖУ на день:*\n{format_macros(macros)}\n\n✅ Привычки: {len(completed)}/{len(habits)}\n{get_progress_bar(len(completed), len(habits))}{streak_line}")


@bot.message_handler(func=lambda m: m.text == "👤 Профиль")
def show_profile(message):
    user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        bot.send_message(user_id, "Сначала пройдите регистрацию: /start")
        return
    sub_status = "✅ Активна" if is_subscribed(user_id) else "❌ Неактивна"
    sub_end = user.get('subscription_end', '—')
    if sub_end != '—':
        frozen = frozen_subscription_end(user)
        if frozen:
            sub_status = "⏸ На паузе"
            sub_end = frozen.strftime("%d.%m.%Y") + " (дни заморожены)"
        else:
            sub_end = datetime.fromisoformat(sub_end).strftime("%d.%m.%Y")
    referrals = user.get('referrals_count', 0)
    ref_text = f"\n👥 Приглашено друзей: {referrals}" if referrals > 0 else ""
    allergies = user.get('allergies', '')
    allergy_text = f"\n🚫 Аллергии: {allergies}" if allergies else ""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("✏️ Обновить данные", callback_data="profile_update"),
           types.InlineKeyboardButton("🗑 Сбросить всё", callback_data="profile_reset"),
           types.InlineKeyboardButton("🔔 Напоминания", callback_data="reminder_settings"),
           types.InlineKeyboardButton("▶️ Возобновить после паузы" if is_paused(user) else "⏸ Пауза", callback_data="pause_menu"))
    bot.send_message(user_id, f"👤 *Профиль*\n\nИмя: {user['name']}\nВозраст: {user['age']}\nВес: {user['weight']} кг\nРост: {user['height']} см\nЦель: {user['goal']}\n🥗 Питание: {user.get('diet_type', 'обычное')}{allergy_text}\n\n⭐ Подписка: {sub_status}\n📅 До: {sub_end}{ref_text}\n\n💡 Команды: /update — изменить данные, /reset — начать заново", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data == "profile_update")
def profile_update_btn(call):
    cmd_update(call.message, user_id=call.from_user.id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "profile_reset")
def profile_reset_btn(call):
    cmd_reset(call.message, user_id=call.from_user.id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "reminder_settings")
def reminder_settings(call):
    user_id = call.from_user.id
    user = get_user(user_id)
    plan_status = "✅ Вкл" if user.get('reminder_plan_enabled', 1) else "❌ Выкл"
    weight_time = user.get('reminder_weight_time', '08:00')
    habits_time = user.get('reminder_habits_time', '20:00')
    kb = types.InlineKeyboardMarkup(row_width=1)
    diary_status = "✅ Вкл" if user.get('reminder_diary_enabled', 1) else "❌ Выкл"
    kb.add(types.InlineKeyboardButton(f"☀️ План дня по утрам: {plan_status}", callback_data="toggle_plan"),
           types.InlineKeyboardButton(f"📓 Напоминание о дневнике: {diary_status}", callback_data="toggle_diary_rem"),
           types.InlineKeyboardButton(f"⚖️ Время взвешивания: {weight_time}", callback_data="set_weight_time"),
           types.InlineKeyboardButton(f"✅ Время привычек: {habits_time}", callback_data="set_habits_time"),
           types.InlineKeyboardButton(f"🌍 Часовой пояс: {tz_label(user.get('tz'))}", callback_data="tz_menu"),
           types.InlineKeyboardButton("▶️ Возобновить после паузы" if is_paused(user) else "⏸ Пауза подписки", callback_data="pause_menu"))
    pause_line = "⏸ *Сейчас пауза подписки*\n\n" if is_paused(user) else ""
    bot.send_message(user_id, f"🔔 *Настройки напоминаний*\n\n{pause_line}☀️ План дня: каждое утро в 08:30\n⚖️ Взвешивание: понедельник в {weight_time}\n✅ Привычки: каждый день в {habits_time}\n📓 Дневник: в 19:00, если за день нет записей\n\nВсё по вашему местному времени: {tz_label(user.get('tz'))}.\n\nНажмите, чтобы изменить:", reply_markup=kb)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "toggle_plan")
def toggle_plan(call):
    user_id = call.from_user.id
    user = get_user(user_id)
    new_val = 0 if user.get('reminder_plan_enabled', 1) else 1
    update_user(user_id, reminder_plan_enabled=new_val)
    bot.answer_callback_query(call.id, "✅ Настройки обновлены")
    reminder_settings(call)


@bot.callback_query_handler(func=lambda call: call.data == "toggle_diary_rem")
def toggle_diary_rem(call):
    user_id = call.from_user.id
    user = get_user(user_id)
    update_user(user_id, reminder_diary_enabled=0 if user.get('reminder_diary_enabled', 1) else 1)
    bot.answer_callback_query(call.id, "✅ Настройки обновлены")
    reminder_settings(call)


@bot.callback_query_handler(func=lambda call: call.data == "plan_off")
def plan_off(call):
    update_user(call.from_user.id, reminder_plan_enabled=0)
    bot.answer_callback_query(call.id, "🔕 План дня отключён")
    bot.send_message(call.from_user.id, "🔕 Утренний план дня отключён. Вернуть его можно кнопкой «📅 План на сегодня» или в настройках напоминаний.")


@bot.callback_query_handler(func=lambda call: call.data == "set_weight_time")
def set_weight_time(call):
    user_id = call.from_user.id
    bot.send_message(user_id, "✏️ Введите время для напоминания о взвешивании (ЧЧ:ММ), например 08:00:")
    set_state(user_id, "set_weight_time")
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "set_weight_time")
def process_weight_time(message):
    user_id = message.from_user.id
    text = message.text.strip()
    try:
        datetime.strptime(text, "%H:%M")
        update_user(user_id, reminder_weight_time=text)
        clear_state(user_id)
        bot.send_message(user_id, f"✅ Время взвешивания обновлено: {text}")
    except ValueError:
        bot.send_message(user_id, "Неверный формат. Введите время как ЧЧ:ММ (например, 08:00)")


@bot.callback_query_handler(func=lambda call: call.data == "set_habits_time")
def set_habits_time(call):
    user_id = call.from_user.id
    bot.send_message(user_id, "✏️ Введите время для напоминания о привычках (ЧЧ:ММ), например 20:00:")
    set_state(user_id, "set_habits_time")
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "set_habits_time")
def process_habits_time(message):
    user_id = message.from_user.id
    text = message.text.strip()
    try:
        datetime.strptime(text, "%H:%M")
        update_user(user_id, reminder_habits_time=text)
        clear_state(user_id)
        bot.send_message(user_id, f"✅ Время привычек обновлено: {text}")
    except ValueError:
        bot.send_message(user_id, "Неверный формат. Введите время как ЧЧ:ММ (например, 20:00)")


def _tz_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(*[types.InlineKeyboardButton(label, callback_data=f"tz_set:{i}") for i, (label, _) in enumerate(TZ_ZONES)])
    kb.add(types.InlineKeyboardButton("✏️ Другой (ввести смещение от UTC)", callback_data="tz_other"))
    return kb


def _tz_text(user):
    return (f"🌍 *Часовой пояс*\n\nСейчас: {tz_label(user.get('tz'))}. У вас сейчас {user_now(user).strftime('%H:%M')}.\n\n"
            "План дня, напоминания и итоги дня приходят по вашему местному времени. Выберите пояс:")


@bot.message_handler(commands=['timezone'])
def cmd_timezone(message):
    user = get_user(message.from_user.id)
    if not user:
        bot.send_message(message.from_user.id, "Сначала пройдите регистрацию: /start")
        return
    bot.send_message(message.from_user.id, _tz_text(user), reply_markup=_tz_kb())


@bot.callback_query_handler(func=lambda call: call.data == "tz_menu")
def tz_menu(call):
    bot.answer_callback_query(call.id)
    user = get_user(call.from_user.id)
    if user:
        bot.send_message(call.from_user.id, _tz_text(user), reply_markup=_tz_kb())


def _tz_saved(user_id, name):
    update_user(user_id, tz=name)
    user = get_user(user_id)
    bot.send_message(user_id, f"✅ Часовой пояс: {tz_label(name)}. У вас сейчас {user_now(user).strftime('%H:%M')}.\n"
                              "План дня придёт в 08:30 по вашему времени.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("tz_set:"))
def tz_set(call):
    bot.answer_callback_query(call.id)
    try:
        name = TZ_ZONES[int(call.data.split(":")[1])][1]
    except (ValueError, IndexError):
        return
    if get_user(call.from_user.id):
        _tz_saved(call.from_user.id, name)


@bot.callback_query_handler(func=lambda call: call.data == "tz_other")
def tz_other(call):
    bot.answer_callback_query(call.id)
    set_state(call.from_user.id, "set_tz_text")
    bot.send_message(call.from_user.id, "Введите смещение от UTC целым числом, например `+5` или `-3`. "
                                        "Москва — это +3.")


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "set_tz_text")
def process_tz_text(message):
    user_id = message.from_user.id
    name = tz_parse_offset(message.text or "")
    if not name:
        bot.send_message(user_id, "Не понял. Введите число от -12 до +14, например `+5` (Москва — `+3`).")
        return
    clear_state(user_id)
    _tz_saved(user_id, name)


@bot.message_handler(func=lambda m: m.text == "⭐ Подписка")
def show_subscription(message):
    user_id = message.from_user.id
    if is_subscribed(user_id):
        user = get_user(user_id)
        sub_end = datetime.fromisoformat(user['subscription_end']).strftime("%d.%m.%Y")
        tier = tier_of(user)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("💳 Продлить подписку", callback_data="renew_open"))
        if tier != "full" and FULL_TIER_ENABLED:
            kb.add(types.InlineKeyboardButton(f"Перейти на тариф «{tier_name('full')}» — {tier_price('full')}₽", callback_data="plan_full"))
        bot.send_message(user_id, f"⭐ *Подписка активна*\n\nТариф: «{tier_name(tier)}»\n📅 До: {sub_end}\n\nСпасибо, что с нами! 💚" + support_line(), reply_markup=kb)
    else:
        bot.send_message(user_id, f"⭐ *Оформление подписки*\n\n{plans_overview()}\n⚡ Оплата подтверждается автоматически в течение нескольких минут\n\n🎁 Или пригласите друга и получите *{REFERRAL_BONUS_DAYS} дней бесплатно*!\n\n⚠️ Бот не заменяет консультацию врача — используйте /disclaimer, чтобы прочитать подробнее." + support_line(), reply_markup=sub_inline_kb())


@bot.callback_query_handler(func=lambda call: call.data == "renew_open")
def renew_open(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    user = get_user(user_id)
    if not user:
        return
    extra = ""
    if is_subscribed(user_id):
        end = datetime.fromisoformat(user['subscription_end']).strftime("%d.%m.%Y")
        extra = f"Сейчас подписка действует до {end}; оплаченные дни добавятся к этому сроку.\n\n"
    bot.send_message(user_id, f"💳 *Продление подписки*\n\n{extra}{plans_overview()}\n⚡ Оплата подтверждается автоматически в течение нескольких минут" + support_line(), reply_markup=sub_inline_kb())


@bot.callback_query_handler(func=lambda call: call.data == "sub_info")
def sub_info(call):
    bot.send_message(call.from_user.id, f"📋 *О подписке*\n\n{plans_overview()}\n\nℹ️ Еженедельная коррекция калорий работает при цели «похудение» или «набор» и взвешивании раз в неделю.\n\n🎁 Пробный период — {TRIAL_DAYS} дн. бесплатно!\n🎁 Пригласите друга и получите {REFERRAL_BONUS_DAYS} дней бесплатно.")
    bot.answer_callback_query(call.id)


def _tier_from_callback(data: str, prefix: str) -> str:
    """'pay_yookassa_full' -> 'full'. Старые кнопки без тарифа ведут на старший из доступных."""
    suffix = data[len(prefix):].lstrip("_")
    if suffix.endswith("_y"):  # годовая подписка: 'pay_yookassa_basic_y'
        suffix = suffix[:-2]
    return suffix if suffix in TIERS else sellable_tiers()[-1]


def _is_annual(data: str) -> bool:
    return data.endswith("_y")


def _activate_paid(user_id: int, provider: str, provider_payment_id: str, crypto: bool = False):
    """Зачисляет оплаченный платёж ровно один раз; повторные нажатия «Проверить оплату» подписку не продлевают."""
    payment = get_payment_by_provider_id(provider, provider_payment_id)
    if not payment:
        bot.send_message(user_id, "❌ Платёж оплачен, но не найден в базе. Напишите администратору — всё исправим.")
        return
    if payment['user_id'] != user_id:
        bot.send_message(user_id, "❌ Этот платёж оформлен другим пользователем.")
        return
    if complete_payment(payment):
        tier = payment.get('tier') or tier_of({})
        title = "Крипто-оплата прошла успешно!" if crypto else "Оплата прошла успешно!"
        bot.send_message(user_id, f"✅ *{title}*\n\nТариф «{tier_name(tier)}» активирован на {period_text(payment.get('days') or PAID_PERIOD_DAYS)}.\nСпасибо за поддержку! 💚", reply_markup=main_menu_keyboard())
    else:
        end = datetime.fromisoformat(get_user(user_id)['subscription_end']).strftime("%d.%m.%Y")
        bot.send_message(user_id, f"✅ Эта оплата уже зачтена. Подписка активна до {end}.")


@bot.callback_query_handler(func=lambda call: call.data == "pay_yookassa" or call.data.startswith("pay_yookassa_"))
def pay_yookassa(call):
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)
    if not YOOKASSA_ENABLED:
        bot.send_message(user_id, "❌ Оплата картой/СБП не настроена администратором.")
        return
    tier = _tier_from_callback(call.data, "pay_yookassa")
    annual = _is_annual(call.data)
    if tier not in sellable_tiers() or (annual and not ANNUAL_ENABLED):
        bot.send_message(user_id, "❌ Этот тариф сейчас недоступен. Откройте «⭐ Подписка» ещё раз.")
        return
    amount, days = (annual_price(tier), ANNUAL_DAYS) if annual else (tier_price(tier), PAID_PERIOD_DAYS)
    bot.send_message(user_id, "⏳ Создаём счёт на оплату...")
    result = create_payment(user_id, "yookassa", amount, tier_name(tier) + (", год" if annual else ""))
    if result.get("error"):
        bot.send_message(user_id, f"❌ {result['error']}")
        return
    payment_id = result["payment_id"]
    confirmation_url = result["confirmation_url"]
    db_create_payment(user_id, amount, "yookassa", payment_id, tier, days)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💳 Перейти к оплате", url=confirmation_url))
    kb.add(types.InlineKeyboardButton("🔄 Проверить оплату", callback_data=f"check_yookassa_{payment_id}"))
    bot.send_message(user_id, f"💳 *Оплата картой или через СБП*\n\nТариф: *{tier_name(tier)}*\nСумма: *{amount}₽*\nПериод: *{period_text(days)}*\n\n1. Нажмите «Перейти к оплате»\n2. Выберите карту или СБП на странице ЮKassa\n3. Подтвердите платёж\n\nПодписка активируется автоматически в течение нескольких минут после оплаты — можно просто закрыть страницу и вернуться в бот. Либо нажмите «Проверить оплату», чтобы увидеть результат сразу.", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("check_yookassa_"))
def check_yookassa_payment_handler(call):
    payment_id = call.data.replace("check_yookassa_", "")
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(user_id, "⏳ Проверяем статус платежа...")
    result = check_payment("yookassa", payment_id)
    if result.get("error"):
        bot.send_message(user_id, f"❌ {result['error']}")
        return
    if result.get("paid"):
        _activate_paid(user_id, "yookassa", payment_id)
    else:
        status = result.get("status", "unknown")
        bot.send_message(user_id, f"⏳ *Платёж в обработке*\n\nТекущий статус: {status}\n\nМы также проверяем оплату автоматически — подписка активируется сама, как только платёж пройдёт.")


@bot.callback_query_handler(func=lambda call: call.data == "pay_cryptocloud" or call.data.startswith("pay_cryptocloud_"))
def pay_cryptocloud(call):
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)
    if not CRYPTOCLOUD_ENABLED:
        bot.send_message(user_id, "❌ CryptoCloud не настроен администратором.")
        return
    tier = _tier_from_callback(call.data, "pay_cryptocloud")
    annual = _is_annual(call.data)
    if tier not in sellable_tiers() or (annual and not ANNUAL_ENABLED):
        bot.send_message(user_id, "❌ Этот тариф сейчас недоступен. Откройте «⭐ Подписка» ещё раз.")
        return
    amount, days = (annual_price(tier), ANNUAL_DAYS) if annual else (tier_price(tier), PAID_PERIOD_DAYS)
    bot.send_message(user_id, "⏳ Создаём крипто-счёт...")
    result = create_payment(user_id, "cryptocloud", amount, tier_name(tier) + (", год" if annual else ""))
    if result.get("error"):
        bot.send_message(user_id, f"❌ {result['error']}")
        return
    invoice_id = result["invoice_id"]
    pay_url = result["pay_url"]
    db_create_payment(user_id, amount, "cryptocloud", invoice_id, tier, days)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("₿ Перейти к оплате", url=pay_url))
    kb.add(types.InlineKeyboardButton("🔄 Проверить оплату", callback_data=f"check_cryptocloud_{invoice_id}"))
    bot.send_message(user_id, f"₿ *Оплата криптовалютой*\n\nТариф: *{tier_name(tier)}*\nСумма: *{amount}₽*\nПериод: *{period_text(days)}*\n\n1. Нажмите кнопку «Перейти к оплате»\n2. Выберите криптовалюту (USDT, BTC, ETH и др.)\n3. Отправьте указанную сумму на адрес\n4. Вернитесь в бот и нажмите «Проверить оплату»\n\n⚠️ ID счёта: `{invoice_id}`", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("check_cryptocloud_"))
def check_cryptocloud_payment(call):
    invoice_id = call.data.replace("check_cryptocloud_", "")
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(user_id, "⏳ Проверяем статус крипто-платежа...")
    result = check_payment("cryptocloud", invoice_id)
    if result.get("error"):
        bot.send_message(user_id, f"❌ {result['error']}")
        return
    if result.get("paid"):
        _activate_paid(user_id, "cryptocloud", invoice_id, crypto=True)
    else:
        status = result.get("status", "unknown")
        bot.send_message(user_id, f"⏳ *Крипто-платёж в обработке*\n\nТекущий статус: {status}\n\nКриптовалютные транзакции могут занимать 5–30 минут.\nПожалуйста, подождите и проверьте снова позже.")


@bot.callback_query_handler(func=lambda call: call.data == "sub_ref")
def sub_ref(call):
    user_id = call.from_user.id
    user = get_user(user_id)
    try:
        bot_info = bot.get_me()
        bot_username = bot_info.username
    except:
        bot_username = "poleznobot"
    ref_link = f"https://t.me/{bot_username}?start={user_id}"
    bot.send_message(user_id, f"🎁 *Приведи друга — получи {REFERRAL_BONUS_DAYS} дней бесплатно!*\n\nВаша реферальная ссылка:\n`{ref_link}`\n\n👥 Приглашено: {user.get('referrals_count', 0)}\n💚 Каждый друг = +{REFERRAL_BONUS_DAYS} дней подписки. Дни начисляются, когда друг перейдёт по ссылке и завершит регистрацию.")
    bot.answer_callback_query(call.id)


@bot.message_handler(commands=['reset'])
def cmd_reset(message, user_id=None):
    user_id = user_id or message.from_user.id
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("✅ Да, удалить всё", callback_data="reset_confirm"),
           types.InlineKeyboardButton("❌ Нет, оставить", callback_data="reset_cancel"))
    bot.send_message(user_id, "⚠️ *Внимание!*\n\nВы хотите полностью удалить все свои данные и начать заново?\n\nЭто удалит:\n• Профиль\n• Историю веса\n• Привычки\n• Статистику\n• Историю фото-анализа\n\n*Это действие нельзя отменить!*", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data == "reset_confirm")
def reset_confirmed(call):
    user_id = call.from_user.id
    old_user = get_user(user_id)
    # /reset обещает удалить профиль, историю веса, привычки и статистику —
    # но НЕ подписку. Сохраняем эти поля, чтобы delete_user+add_user не
    # выдавал новый бесплатный период поверх уже оплаченного или использованного.
    old_subscription_end = old_user.get('subscription_end') if old_user else None
    old_referral_code = old_user.get('referral_code') if old_user else None
    old_referrals_count = old_user.get('referrals_count') if old_user else 0
    delete_user(user_id)
    clear_state(user_id)
    bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id, text="✅ Все данные удалены. Начинаем заново...")
    bot.answer_callback_query(call.id)
    # add_user() всегда выдаёт новый пробный период — это нужно только для
    # действительно новых пользователей. Здесь восстанавливаем реальный
    # статус подписки сразу после создания записи, чтобы /reset не выглядел
    # как лазейка для получения второго бесплатного периода или сброса
    # отсчёта уже истёкшей подписки.
    add_user(user_id, call.from_user.username, call.from_user.first_name, old_referral_code or str(user_id))
    if old_user and old_user.get('consent_at'):
        update_user(user_id, consent_at=old_user['consent_at'])  # согласие уже было дано
    if old_subscription_end:
        update_user(user_id, subscription_end=old_subscription_end)
    if old_referrals_count:
        update_user(user_id, referrals_count=old_referrals_count)
    if old_subscription_end and is_subscribed(user_id):
        intro_line = f"✅ Ваша подписка активна — профиль просто обнулён, доступ сохранён."
    elif old_subscription_end:
        intro_line = "⚠️ Профиль обнулён. Ваш пробный период или подписка уже закончились — чтобы продолжить пользоваться ботом, потребуется оформить подписку."
    else:
        intro_line = access_line(user_id)
    bot.send_message(user_id, f"👋 Добро пожаловать в *ПолезноБот*!\n\n{intro_line}\n\n⚠️ *Важно:* бот даёт общие рекомендации по питанию и не заменяет консультацию врача или диетолога. Перед изменением рациона или физических нагрузок, особенно при хронических заболеваниях, аллергиях или беременности, проконсультируйтесь со специалистом.\n\nДавайте познакомимся. Как вас зовут?")
    set_state(user_id, "reg_name")


@bot.callback_query_handler(func=lambda call: call.data == "reset_cancel")
def reset_cancelled(call):
    bot.edit_message_text(chat_id=call.from_user.id, message_id=call.message.message_id, text="❌ Сброс отменён. Ваши данные сохранены.")
    bot.answer_callback_query(call.id)


@bot.message_handler(commands=['update'])
def cmd_update(message, user_id=None):
    user_id = user_id or message.from_user.id
    user = get_user(user_id)
    if not user:
        bot.send_message(user_id, "Сначала пройдите регистрацию: /start")
        return
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("⚖️ Вес", callback_data="upd_weight"),
           types.InlineKeyboardButton("📏 Рост", callback_data="upd_height"),
           types.InlineKeyboardButton("🎂 Возраст", callback_data="upd_age"),
           types.InlineKeyboardButton("🎯 Цель", callback_data="upd_goal"),
           types.InlineKeyboardButton("👤 Имя", callback_data="upd_name"),
           types.InlineKeyboardButton("🥗 Питание", callback_data="upd_diet"),
           types.InlineKeyboardButton("🚫 Аллергии", callback_data="upd_allergies"),
           types.InlineKeyboardButton("🥩 % жира", callback_data="upd_body_fat"),
           types.InlineKeyboardButton("🏃 Активность", callback_data="upd_activity"))
    bot.send_message(user_id, "✏️ *Что хотите обновить?*\n\nВыберите пункт, который нужно изменить:", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("upd_"))
def update_field(call):
    field = call.data.replace("upd_", "")
    user_id = call.from_user.id
    field_names = {"weight": "вес (в кг)", "height": "рост (в см)", "age": "возраст", "name": "имя"}
    if field == "goal":
        bot.send_message(user_id, "Выберите новую цель:", reply_markup=goal_inline_kb())
        set_state(user_id, "update_goal")
        bot.answer_callback_query(call.id)
        return
    if field == "diet":
        bot.send_message(user_id, "Выберите тип питания:", reply_markup=diet_inline_kb())
        set_state(user_id, "update_diet")
        bot.answer_callback_query(call.id)
        return
    if field == "allergies":
        bot.send_message(user_id, "✏️ Укажите аллергии через запятую (или 'нет'):")
        set_state(user_id, "update_allergies")
        bot.answer_callback_query(call.id)
        return
    if field == "body_fat":
        bot.send_message(user_id, "✏️ Введите новый процент жира (3–60%):")
        set_state(user_id, "update_body_fat")
        bot.answer_callback_query(call.id)
        return
    if field == "activity":
        bot.send_message(user_id, "Выберите новый уровень активности:", reply_markup=activity_inline_kb())
        set_state(user_id, "update_activity")
        bot.answer_callback_query(call.id)
        return
    bot.send_message(user_id, f"✏️ Введите новый {field_names.get(field, field)}:")
    set_state(user_id, f"update_{field}")
    bot.answer_callback_query(call.id)


def _finish_goal_update(user_id, message_id, goal, target_weight=None, pace_percent=None):
    """Сохраняет новую цель (с целевым весом и темпом для похудения/набора) и показывает новую норму."""
    update_user(user_id, goal=goal, target_weight=target_weight, weekly_pace_percent=pace_percent,
                calorie_adjustment=0)  # корректировка считалась под старую цель
    user = get_user(user_id)
    calories, floored = calorie_norm_info(user['weight'], user.get('body_fat', 20), goal, user.get('activity_level', 'легкий'))
    floor_line = f"\n{FLOOR_NOTE}" if floored else ""
    macros = calculate_macros(calories, user['weight'], goal)
    goal_line = ""
    if target_weight and pace_percent:
        weeks = estimate_weeks_to_goal(user['weight'], target_weight, pace_percent)
        goal_line = f"\n🎯 Целевой вес: {target_weight} кг (~{weeks} нед.)"
    clear_state(user_id)
    text = f"✅ Цель обновлена: *{goal}*{goal_line}\n🔥 Новая норма калорий: {calories} ккал/день{floor_line}\n{format_macros(macros)}"
    if message_id:
        bot.edit_message_text(chat_id=user_id, message_id=message_id, text=text)
    else:
        bot.send_message(user_id, text)


@bot.callback_query_handler(func=lambda call: call.data.startswith("goal_") and get_state(call.from_user.id)["step"] == "update_goal")
def update_goal_handler(call):
    user_id = call.from_user.id
    goal = call.data.replace("goal_", "")
    bot.answer_callback_query(call.id)
    user = get_user(user_id)
    if not user:
        return
    if goal in ("похудение", "набор"):
        set_state(user_id, "update_target_weight", {"goal": goal})
        direction = "похудеть до" if goal == "похудение" else "набрать до"
        example = user['weight'] - 5 if goal == "похудение" else user['weight'] + 5
        bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id,
                              text=f"🎯 Цель: *{goal}*\n\nДо какого веса (в кг) вы хотите {direction}? Сейчас у вас {user['weight']} кг.\nНапример: {example}")
        return
    _finish_goal_update(user_id, call.message.message_id, goal)


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "update_target_weight")
def update_target_weight_handler(message):
    user_id = message.from_user.id
    user = get_user(user_id)
    data = get_state(user_id)["data"]
    goal = data.get("goal")
    try:
        target_weight = float(message.text.replace(",", "."))
        if not 30 <= target_weight <= 300:
            raise ValueError
    except (ValueError, AttributeError):
        bot.send_message(user_id, "Пожалуйста, введите корректный вес в кг (30–300).")
        return
    if goal == "похудение" and target_weight >= user['weight']:
        bot.send_message(user_id, f"Цель похудения должна быть меньше текущего веса ({user['weight']} кг). Введите корректный целевой вес:")
        return
    if goal == "набор" and target_weight <= user['weight']:
        bot.send_message(user_id, f"Цель набора должна быть больше текущего веса ({user['weight']} кг). Введите корректный целевой вес:")
        return
    data["target_weight"] = target_weight
    set_state(user_id, "update_pace", data)
    bot.send_message(user_id, f"🎯 Целевой вес: *{target_weight} кг*\n\nКак хотите двигаться к цели?", reply_markup=pace_inline_kb(goal))


@bot.callback_query_handler(func=lambda call: call.data.startswith("pace_") and get_state(call.from_user.id)["step"] == "update_pace")
def update_pace_handler(call):
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)
    data = get_state(user_id)["data"]
    option = PACE_OPTIONS.get(data.get("goal"), {}).get(call.data.replace("pace_", ""))
    if not option:
        return
    _finish_goal_update(user_id, call.message.message_id, data["goal"], data["target_weight"], option["percent"])


@bot.callback_query_handler(func=lambda call: call.data.startswith("diet_") and get_state(call.from_user.id)["step"] == "update_diet")
def update_diet_handler(call):
    user_id = call.from_user.id
    diet = call.data.replace("diet_", "")
    update_user(user_id, diet_type=diet)
    clear_state(user_id)
    bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id, text=f"✅ Тип питания обновлён: *{diet}*")
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "update_allergies")
def update_allergies_handler(message):
    user_id = message.from_user.id
    text = message.text.strip()
    allergies = "" if text.lower() in ("нет", "no", "-", "—") else text.lower()
    update_user(user_id, allergies=allergies)
    clear_state(user_id)
    bot.send_message(user_id, f"✅ Аллергии обновлены: {allergies or 'нет'}")


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "update_height")
def update_height_handler(message):
    user_id = message.from_user.id
    try:
        height = float(message.text.replace(",", "."))
        if not 100 <= height <= 250:
            raise ValueError
        update_user(user_id, height=height)
        clear_state(user_id)
        bot.send_message(user_id, f"✅ Рост обновлён: *{height} см*")
    except ValueError:
        bot.send_message(user_id, "Пожалуйста, введите корректный рост (100–250 см).")


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "update_age")
def update_age_handler(message):
    user_id = message.from_user.id
    try:
        age = int(message.text)
        if not 1 <= age <= 100:
            raise ValueError
        if age < MIN_AGE:
            clear_state(user_id)
            bot.send_message(user_id, AGE_REFUSAL)
            return
        update_user(user_id, age=age)
        clear_state(user_id)
        bot.send_message(user_id, f"✅ Возраст обновлён: *{age} лет*")
    except ValueError:
        bot.send_message(user_id, "Пожалуйста, введите корректный возраст (числом).")


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "update_name")
def update_name_handler(message):
    user_id = message.from_user.id
    name = message.text.strip()
    if len(name) < 1:
        bot.send_message(user_id, "Имя не может быть пустым.")
        return
    update_user(user_id, name=name)
    clear_state(user_id)
    bot.send_message(user_id, f"✅ Имя обновлено: *{name}*")


@bot.message_handler(func=lambda m: get_state(m.from_user.id)["step"] == "update_body_fat")
def update_body_fat_handler(message):
    user_id = message.from_user.id
    try:
        body_fat = float(message.text.replace(",", "."))
        if not 3 <= body_fat <= 60:
            raise ValueError
        update_user(user_id, body_fat=body_fat)
        clear_state(user_id)
        bot.send_message(user_id, f"✅ Процент жира обновлён: *{body_fat}%*")
    except ValueError:
        bot.send_message(user_id, "Пожалуйста, введите корректный процент жира (3–60%).")


@bot.callback_query_handler(func=lambda call: call.data.startswith("activity_") and get_state(call.from_user.id)["step"] == "update_activity")
def update_activity_handler(call):
    user_id = call.from_user.id
    activity = call.data.replace("activity_", "")
    update_user(user_id, activity_level=activity)
    clear_state(user_id)
    bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id, text=f"✅ Активность обновлена: *{activity}*")
    bot.answer_callback_query(call.id)


# === Быстрая запись веса: просто число в чат ===
QUICK_WEIGHT_RE = re.compile(r"^\s*(\d{2,3}(?:[.,]\d{1,2})?)\s*(?:кг|kg)?\s*$", re.IGNORECASE)
QUICK_WEIGHT_MAX_JUMP = 7  # больше — переспрашиваем: вероятна опечатка


def _record_quick_weight(user_id, weight, old_weight):
    weight_id = add_weight(user_id, weight)
    change = round(weight - old_weight, 1) if old_weight else None
    change_text = ""
    if change is not None:
        arrow = "📉" if change < 0 else "📈" if change > 0 else "➡️"
        change_text = f" ({arrow} {change:+.1f} кг)"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("↩️ Отменить", callback_data=f"qw_undo_{weight_id}_{old_weight}"),
           types.InlineKeyboardButton("📊 График", callback_data="weight_chart"))
    bot.send_message(user_id, f"⚖️ Записал вес: *{weight} кг*{change_text}", reply_markup=kb)


@bot.message_handler(func=lambda m: m.content_type == 'text' and get_state(m.from_user.id)["step"] is None
                     and QUICK_WEIGHT_RE.match(m.text or "") is not None)
def quick_weight(message):
    user_id = message.from_user.id
    user = get_user(user_id)
    if not user or not user.get('name') or not user.get('weight'):
        return  # регистрация не пройдена: число не трактуем как вес
    weight = float(QUICK_WEIGHT_RE.match(message.text).group(1).replace(",", "."))
    if not 30 <= weight <= 300:
        bot.send_message(user_id, "Вес должен быть от 30 до 300 кг. Напишите, например: `72.5`")
        return
    old_weight = user['weight']
    if abs(weight - old_weight) > QUICK_WEIGHT_MAX_JUMP:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✅ Да, записать", callback_data=f"qw_ok_{weight}"),
               types.InlineKeyboardButton("Нет, опечатка", callback_data="qw_no"))
        bot.send_message(user_id, f"Вес {weight} кг сильно отличается от прошлого ({old_weight} кг). Записать?", reply_markup=kb)
        return
    _record_quick_weight(user_id, weight, old_weight)


@bot.callback_query_handler(func=lambda call: call.data.startswith("qw_ok_"))
def quick_weight_confirm(call):
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)
    user = get_user(user_id)
    try:
        weight = float(call.data.replace("qw_ok_", ""))
    except ValueError:
        return
    if user and 30 <= weight <= 300:
        bot.edit_message_reply_markup(chat_id=user_id, message_id=call.message.message_id, reply_markup=None)
        _record_quick_weight(user_id, weight, user['weight'])


@bot.callback_query_handler(func=lambda call: call.data == "qw_no")
def quick_weight_no(call):
    bot.answer_callback_query(call.id, "Не записал")
    bot.edit_message_reply_markup(chat_id=call.from_user.id, message_id=call.message.message_id, reply_markup=None)


@bot.callback_query_handler(func=lambda call: call.data.startswith("qw_undo_"))
def quick_weight_undo(call):
    user_id = call.from_user.id
    try:
        weight_id, old_weight = call.data.replace("qw_undo_", "").split("_")
        weight_id, old_weight = int(weight_id), float(old_weight)
    except ValueError:
        bot.answer_callback_query(call.id)
        return
    if get_user(user_id) and 30 <= old_weight <= 300 and remove_last_weight(user_id, weight_id, old_weight):
        bot.answer_callback_query(call.id, "Запись отменена")
        bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id, text=f"↩️ Запись отменена. Вес остаётся: {old_weight} кг")
    else:
        bot.answer_callback_query(call.id, "Нечего отменять")


def _acquire_single_instance_lock():
    """Не даём запустить вторую копию бота (Telegram не позволяет двум процессам читать одни обновления)."""
    import fcntl
    from config import DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)) or ".", exist_ok=True)
    lock_file = open(os.path.join(os.path.dirname(os.path.abspath(DB_PATH)) or ".", "bot.lock"), "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.error("❌ Бот уже запущен в другом процессе. Остановите его или используйте systemctl.")
        raise SystemExit(1)
    return lock_file  # держим ссылку, пока работает процесс


if __name__ == "__main__":
    _instance_lock = _acquire_single_instance_lock()
    logger.info("🚀 Бот запущен!")
    try:  # меню команд в Telegram (кнопка «Меню» у поля ввода): так пауза находится без подсказок
        bot.set_my_commands([
            types.BotCommand("start", "Главное меню"),
            types.BotCommand("plan", "План на сегодня"),
            types.BotCommand("diary", "Дневник еды"),
            types.BotCommand("myfoods", "Мои продукты"),
            types.BotCommand("barcode", "Найти продукт по штрихкоду"),
            types.BotCommand("promo", "Ввести промокод"),
            types.BotCommand("pause", "Пауза подписки"),
            types.BotCommand("timezone", "Часовой пояс"),
            types.BotCommand("resume", "Снять паузу"),
            types.BotCommand("disclaimer", "Важная информация"),
            types.BotCommand("privacy", "Политика конфиденциальности"),
            types.BotCommand("support", "Написать нам"),
            types.BotCommand("delete_data", "Удалить мои данные"),
        ])
    except Exception as e:
        logger.warning(f"Не удалось задать меню команд: {e}")
    import alerts
    alerts.install(bot)  # ошибки (в том числе фоновых задач) приходят админам в Telegram
    init_reminders(bot)
    try:
        from config import DB_PATH as _DB
        alerts.notify_started(bot, os.path.join(os.path.dirname(os.path.abspath(_DB)), "last_start.txt"))
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление о запуске: {e}")
    logger.info(f"👤 Администраторы: {sorted(ADMIN_IDS) or 'не заданы (проверьте ADMIN_IDS в config.py)'}")
    bot.infinity_polling()
