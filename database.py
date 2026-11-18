"""Работа с базой данных пользователей v6.5"""

import sqlite3
import nutrients as nutrient_utils
import json
from datetime import datetime, timedelta

import pytz

from tiers import DEFAULT_TIER, PAID_PERIOD_DAYS
from timezones import get_tz
from norms import CALORIE_FLOOR, min_daily_calories
from config import DB_PATH, TRIAL_DAYS, REFERRAL_BONUS_DAYS


# Дни для логов и серий считаем по московскому времени — так же, как работает планировщик.
_TZ = pytz.timezone("Europe/Moscow")


def user_tz(user: dict):
    """Часовой пояс пользователя (по умолчанию Москва)."""
    return get_tz((user or {}).get('tz'))


def user_now(user: dict) -> datetime:
    """Текущее местное время пользователя (без tzinfo)."""
    return datetime.now(user_tz(user)).replace(tzinfo=None)


def user_today(user: dict):
    return user_now(user).date()


def _today_str() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d")


def _column_exists(cursor, table, column):
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


OLD_DEFAULT_HABITS = {
    "Выпить 2 литра воды", "Съесть 5 порций овощей/фруктов", "10 минут растяжки",
    "Завтрак в течение часа после пробуждения",
}


def _migrate_old_default_habits(cursor):
    """Один раз: убирает у существующих пользователей прежние стандартные привычки (оставляем свои и три новые)."""
    if cursor.execute("PRAGMA user_version").fetchone()[0] >= 1:
        return
    cursor.execute("SELECT user_id, habits, habits_completed FROM users")
    for user_id, habits_json, completed_json in cursor.fetchall():
        try:
            habits = json.loads(habits_json or "[]")
            completed = json.loads(completed_json or "[]")
        except ValueError:
            continue
        keep = [i for i, h in enumerate(habits) if h not in OLD_DEFAULT_HABITS]
        if len(keep) == len(habits):
            continue
        new_index = {old: new for new, old in enumerate(keep)}
        cursor.execute("UPDATE users SET habits = ?, habits_completed = ? WHERE user_id = ?",
                       (json.dumps([habits[i] for i in keep], ensure_ascii=False),
                        json.dumps([new_index[i] for i in completed if i in new_index]), user_id))
    cursor.execute("PRAGMA user_version = 1")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    import os
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TEXT,
            name TEXT,
            age INTEGER,
            weight REAL,
            height REAL,
            goal TEXT DEFAULT 'похудение',
            diet_type TEXT DEFAULT 'обычное',
            allergies TEXT DEFAULT '',
            body_fat REAL DEFAULT 20,
            activity_level TEXT DEFAULT 'легкий',
            subscription_end TEXT,
            water_today INTEGER DEFAULT 0,
            water_goal INTEGER DEFAULT 2000,
            habits TEXT DEFAULT '[]',
            habits_completed TEXT DEFAULT '[]',
            last_menu_date TEXT,
            is_active INTEGER DEFAULT 1,
            referral_code TEXT,
            referred_by INTEGER,
            referrals_count INTEGER DEFAULT 0,
            reminder_water_enabled INTEGER DEFAULT 1,
            reminder_water_interval INTEGER DEFAULT 2,
            reminder_weight_day INTEGER DEFAULT 1,
            reminder_weight_time TEXT DEFAULT '08:00',
            reminder_habits_time TEXT DEFAULT '20:00'
        )
    """)

    migrations = [
        ("referral_code", "TEXT"), ("referred_by", "INTEGER"),
        ("referrals_count", "INTEGER DEFAULT 0"), ("diet_type", "TEXT DEFAULT 'обычное'"),
        ("allergies", "TEXT DEFAULT ''"), ("reminder_water_enabled", "INTEGER DEFAULT 1"),
        ("reminder_water_interval", "INTEGER DEFAULT 2"), ("reminder_weight_day", "INTEGER DEFAULT 1"),
        ("reminder_weight_time", "TEXT DEFAULT '08:00'"), ("reminder_habits_time", "TEXT DEFAULT '20:00'"),
        ("body_fat", "REAL DEFAULT 20"), ("activity_level", "TEXT DEFAULT 'легкий'"),
        ("calorie_adjustment", "INTEGER DEFAULT 0"),
        ("target_weight", "REAL"), ("weekly_pace_percent", "REAL"),
        ("last_slip_at", "TEXT"),
        ("reminder_plan_enabled", "INTEGER DEFAULT 1"),
        ("paused_until", "TEXT"), ("pause_notified", "INTEGER DEFAULT 1"),
        ("pause_available", "INTEGER DEFAULT 0"),
        ("tz", "TEXT DEFAULT 'Europe/Moscow'"), ("consent_at", "TEXT"), ("source", "TEXT"),
        ("expiry_stage", "INTEGER DEFAULT 0"), ("expiry_for", "TEXT"),
        ("subscription_tier", f"TEXT DEFAULT '{DEFAULT_TIER}'"),
        ("reminder_diary_enabled", "INTEGER DEFAULT 1"), ("diary_cheer_day", "TEXT"),
        ("pending_promo", "TEXT"),
    ]
    backfill_pause = False
    for col_name, col_type in migrations:
        if not _column_exists(cursor, "users", col_name):
            cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
            print(f"✅ Миграция: добавлена колонка {col_name}")
            if col_name == "pause_available":
                backfill_pause = True  # заполняется ниже, когда появится таблица payments

    _migrate_old_default_habits(cursor)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_foods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            kcal REAL,
            protein REAL,
            fat REAL,
            carb REAL,
            portion REAL,
            created_at TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_foods_user ON user_foods (user_id)")

    # промокоды и награды за приглашения; записи о наградах/использовании хранят только ID и не удаляются вместе с профилем:
    # иначе «удалить данные и зайти снова» давало бы бонус повторно
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY,
            days INTEGER,
            tier TEXT,
            max_uses INTEGER DEFAULT 0,
            used INTEGER DEFAULT 0,
            expires_at TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS promo_uses (
            code TEXT,
            user_id INTEGER,
            used_at TEXT,
            PRIMARY KEY (code, user_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS referral_rewards (
            friend_id INTEGER PRIMARY KEY,
            referrer_id INTEGER,
            days INTEGER,
            rewarded_at TEXT
        )
    """)
    # кэш ответов Open Food Facts по штрихкодам (общий для всех, без данных пользователей)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS barcode_cache (
            code TEXT PRIMARY KEY,
            status TEXT,
            data TEXT,
            fetched_at TEXT
        )
    """)
    # запросы, которых нет в базе еды: без user_id, только текст и счётчик по дням
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS food_misses (
            day TEXT,
            query TEXT,
            cnt INTEGER DEFAULT 1,
            PRIMARY KEY (day, query)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS food_diary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            day TEXT,
            title TEXT,
            kcal INTEGER,
            created_at TEXT
        )
    """)
    if not _column_exists(cursor, "food_diary", "nutrients"):
        cursor.execute("ALTER TABLE food_diary ADD COLUMN nutrients TEXT")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_food_diary_user_day ON food_diary (user_id, day)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS deleted_users (
            user_id INTEGER PRIMARY KEY,
            subscription_end TEXT,
            subscription_tier TEXT,
            deleted_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS weights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            weight REAL,
            recorded_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            currency TEXT DEFAULT 'RUB',
            provider TEXT DEFAULT 'manual',
            provider_payment_id TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            paid_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    """)

    for col_name, col_type in [("currency", "TEXT DEFAULT 'RUB'"), ("provider", "TEXT DEFAULT 'manual'"), ("provider_payment_id", "TEXT"), ("paid_at", "TEXT"), ("tier", f"TEXT DEFAULT '{DEFAULT_TIER}'"), ("days", "INTEGER")]:
        if not _column_exists(cursor, "payments", col_name):
            cursor.execute(f"ALTER TABLE payments ADD COLUMN {col_name} {col_type}")
            print(f"✅ Миграция: добавлена колонка payments.{col_name}")

    if backfill_pause:
        # те, кто уже платил, получают право на одну паузу в текущем периоде
        cursor.execute("UPDATE users SET pause_available = 1 WHERE user_id IN "
                       "(SELECT user_id FROM payments WHERE status = 'completed')")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            log_date TEXT,
            water INTEGER DEFAULT 0,
            habits_done INTEGER DEFAULT 0,
            habits_total INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    """)

    if not _column_exists(cursor, "daily_logs", "paused"):
        cursor.execute("ALTER TABLE daily_logs ADD COLUMN paused INTEGER DEFAULT 0")

    # Без уникального индекса INSERT OR REPLACE в log_day() не заменяет запись,
    # а плодит дубли за один день. Сначала чистим старые дубли, потом ставим индекс.
    cursor.execute("""
        DELETE FROM daily_logs
        WHERE id NOT IN (SELECT MAX(id) FROM daily_logs GROUP BY user_id, log_date)
    """)
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_logs_user_date ON daily_logs (user_id, log_date)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS food_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            photo_file_id TEXT,
            analysis_text TEXT,
            estimated_calories INTEGER,
            logged_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    """)

    conn.commit()
    conn.close()


def find_user(identifier: str):
    """Пользователь по числовому ID или по @username (регистр не важен). None, если не найден."""
    identifier = identifier.strip()
    if identifier.isdigit():
        return get_user(int(identifier))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (identifier.lstrip("@"),))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def add_user(user_id: int, username: str, first_name: str, referral_code: str = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    trial_end = (datetime.now() + timedelta(days=TRIAL_DAYS)).isoformat()
    cursor.execute("""
        INSERT OR IGNORE INTO users 
        (user_id, username, first_name, created_at, subscription_end, habits, referral_code)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, username, first_name, datetime.now().isoformat(), trial_end, json.dumps([]), referral_code))
    created = cursor.rowcount == 1
    if created:
        # вернулся после удаления данных: сохраняем оплаченный срок и не выдаём повторный пробный период
        cursor.execute("SELECT subscription_end, subscription_tier FROM deleted_users WHERE user_id = ?", (user_id,))
        old = cursor.fetchone()
        if old:
            cursor.execute("UPDATE users SET subscription_end = ?, subscription_tier = COALESCE(?, subscription_tier) WHERE user_id = ?",
                           (old[0], old[1], user_id))
            cursor.execute("DELETE FROM deleted_users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def update_user(user_id: int, **kwargs):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    for key, value in kwargs.items():
        cursor.execute(f"UPDATE users SET {key} = ? WHERE user_id = ?", (value, user_id))
    conn.commit()
    conn.close()


def add_water(user_id: int, amount: int = 250):
    user = get_user(user_id)
    if not user:
        return 0
    new_amount = user.get('water_today', 0) + amount
    update_user(user_id, water_today=new_amount)
    return new_amount


def reset_water(user_id: int):
    update_user(user_id, water_today=0)


def get_habits(user_id: int):
    user = get_user(user_id)
    if not user:
        return []
    return json.loads(user.get('habits', '[]'))


def add_habit(user_id: int, habit: str):
    habits = get_habits(user_id)
    habits.append(habit)
    update_user(user_id, habits=json.dumps(habits))


def remove_habit(user_id: int, index: int):
    """Удаляет привычку по номеру; отметки «выполнено» пересчитываются. Возвращает название или None."""
    habits = get_habits(user_id)
    if not 0 <= index < len(habits):
        return None
    removed = habits.pop(index)
    completed = [i - 1 if i > index else i for i in get_completed_habits(user_id) if i != index]
    update_user(user_id, habits=json.dumps(habits, ensure_ascii=False), habits_completed=json.dumps(completed))
    return removed


def get_completed_habits(user_id: int):
    user = get_user(user_id)
    if not user:
        return []
    return json.loads(user.get('habits_completed', '[]'))


def complete_habit(user_id: int, habit_index: int):
    completed = get_completed_habits(user_id)
    if habit_index not in completed:
        completed.append(habit_index)
        update_user(user_id, habits_completed=json.dumps(completed))


def reset_habits(user_id: int):
    update_user(user_id, habits_completed='[]')


def is_subscribed(user_id: int):
    user = get_user(user_id)
    if not user:
        return False
    sub_end = user.get('subscription_end')
    if not sub_end:
        return False
    return datetime.now() < datetime.fromisoformat(sub_end)


def extend_subscription(user_id: int, days: int = 30, tier: str = None):
    """Продлевает подписку на days дней от текущего конца (или от сейчас). Если указан tier — меняет тариф."""
    user = get_user(user_id)
    current_end = datetime.now()
    if user and user.get('subscription_end'):
        end_date = datetime.fromisoformat(user['subscription_end'])
        if end_date > current_end:
            current_end = end_date
    new_end = (current_end + timedelta(days=days)).isoformat()
    update_user(user_id, subscription_end=new_end)
    if days >= PAID_PERIOD_DAYS:
        update_user(user_id, pause_available=1)  # новый оплаченный (или подаренный) период: пауза снова доступна
    if tier:
        update_user(user_id, subscription_tier=tier)


def add_weight(user_id: int, weight: float):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO weights (user_id, weight, recorded_at) VALUES (?, ?, ?)",
                   (user_id, weight, datetime.now().isoformat()))
    weight_id = cursor.lastrowid
    conn.commit()
    conn.close()
    update_user(user_id, weight=weight)
    return weight_id


def remove_last_weight(user_id: int, weight_id: int, restore_weight: float) -> bool:
    """Отмена записи веса: только если это ещё последняя запись пользователя (старые кнопки «Отменить» ничего не сломают)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM weights WHERE user_id = ? ORDER BY recorded_at DESC, id DESC LIMIT 1", (user_id,))
    row = cursor.fetchone()
    ok = bool(row) and row[0] == weight_id
    if ok:
        cursor.execute("DELETE FROM weights WHERE id = ?", (weight_id,))
    conn.commit()
    conn.close()
    if ok:
        update_user(user_id, weight=restore_weight)
    return ok


def get_weight_history(user_id: int, limit: int = 10):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT weight, recorded_at FROM weights WHERE user_id = ? ORDER BY recorded_at DESC LIMIT ?",
                   (user_id, limit))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_weight_change(user_id: int):
    history = get_weight_history(user_id, limit=2)
    if len(history) < 2:
        return None
    current = history[0]['weight']
    previous = history[1]['weight']
    return round(current - previous, 1)


def set_referrer(user_id: int, referrer_id: int) -> bool:
    """Запоминает, кто пригласил. Дни пригласившему начисляются позже, когда друг завершит регистрацию
    (grant_referral_reward), — иначе бонус можно получить, просто нажав /start по ссылке."""
    user = get_user(user_id)
    if not user or user.get('referred_by') or referrer_id == user_id:
        return False
    referrer = get_user(referrer_id)
    if not referrer or not referrer.get('name'):
        return False
    update_user(user_id, referred_by=referrer_id)
    return True


def grant_referral_reward(user_id: int):
    """После регистрации друга начисляет REFERRAL_BONUS_DAYS пригласившему. Один раз на друга (даже если тот
    удалит данные и зайдёт снова) и не больше REFERRAL_MAX_REWARDS наград на одного пригласившего.
    Возвращает (ID пригласившего, дней) или None."""
    import config as _cfg
    max_rewards = getattr(_cfg, "REFERRAL_MAX_REWARDS", 12)
    user = get_user(user_id)
    referrer_id = user.get('referred_by') if user else None
    if not referrer_id or not get_user(referrer_id):
        return None
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM referral_rewards WHERE friend_id = ?", (user_id,))
    if cursor.fetchone():
        conn.close()
        return None
    cursor.execute("SELECT COUNT(*) FROM referral_rewards WHERE referrer_id = ? AND days > 0", (referrer_id,))
    capped = cursor.fetchone()[0] >= max_rewards
    days = 0 if capped else REFERRAL_BONUS_DAYS
    cursor.execute("INSERT OR IGNORE INTO referral_rewards (friend_id, referrer_id, days, rewarded_at) VALUES (?, ?, ?, ?)",
                   (user_id, referrer_id, days, datetime.now().isoformat()))
    claimed = cursor.rowcount == 1
    conn.commit()
    conn.close()
    if not claimed:
        return None
    referrer = get_user(referrer_id)
    update_user(referrer_id, referrals_count=(referrer.get('referrals_count') or 0) + 1)
    if days:
        extend_subscription(referrer_id, days)
    return referrer_id, days


# === Промокоды ===

def normalize_promo(code: str) -> str:
    return (code or "").strip().upper()


def create_promo(code: str, days: int, max_uses: int = 0, valid_days: int = None, tier: str = None) -> bool:
    """Создаёт промокод. max_uses=0 — без ограничения; valid_days — сколько дней код можно активировать."""
    code = normalize_promo(code)
    expires = (datetime.now() + timedelta(days=valid_days)).isoformat() if valid_days else None
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO promo_codes (code, days, tier, max_uses, used, expires_at, active, created_at) VALUES (?, ?, ?, ?, 0, ?, 1, ?)",
                   (code, days, tier, max_uses, expires, datetime.now().isoformat()))
    created = cursor.rowcount == 1
    conn.commit()
    conn.close()
    return created


def list_promos() -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM promo_codes ORDER BY created_at DESC")]
    conn.close()
    return rows


def set_promo_active(code: str, active: bool) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE promo_codes SET active = ? WHERE code = ?", (1 if active else 0, normalize_promo(code)))
    changed = cursor.rowcount == 1
    conn.commit()
    conn.close()
    return changed


def redeem_promo(user_id: int, code: str):
    """Активирует промокод. Возвращает (статус, дней): 'ok', 'unknown', 'inactive', 'expired', 'used_up', 'already'.
    Использование и счётчик меняются одной транзакцией, поэтому лимит не превысить даже при одновременных нажатиях."""
    code = normalize_promo(code)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT days, tier, max_uses, used, expires_at, active FROM promo_codes WHERE code = ?", (code,)).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return "unknown", 0
        days, tier, max_uses, used, expires_at, active = row
        if not active:
            status = "inactive"
        elif expires_at and datetime.fromisoformat(expires_at) < datetime.now():
            status = "expired"
        elif max_uses and used >= max_uses:
            status = "used_up"
        elif conn.execute("SELECT 1 FROM promo_uses WHERE code = ? AND user_id = ?", (code, user_id)).fetchone():
            status = "already"
        else:
            status = "ok"
        if status != "ok":
            conn.execute("ROLLBACK")
            return status, 0
        conn.execute("INSERT INTO promo_uses (code, user_id, used_at) VALUES (?, ?, ?)", (code, user_id, datetime.now().isoformat()))
        conn.execute("UPDATE promo_codes SET used = used + 1 WHERE code = ?", (code,))
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()
    extend_subscription(user_id, days, tier=tier)
    return "ok", days


# === Кэш штрихкодов ===

def get_barcode_cache(code: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT status, data, fetched_at FROM barcode_cache WHERE code = ?", (code,)).fetchone()
    conn.close()
    if not row:
        return None
    try:
        data = json.loads(row[1]) if row[1] else None
    except ValueError:
        data = None
    return {"status": row[0], "data": data, "fetched_at": row[2]}


def save_barcode_cache(code: str, status: str, data):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO barcode_cache (code, status, data, fetched_at) VALUES (?, ?, ?, ?)",
                 (code, status, json.dumps(data, ensure_ascii=False) if data else None, datetime.now().isoformat()))
    conn.commit()
    conn.close()


# === Запросы, которых не нашлось в базе еды ===

def log_food_miss(text: str):
    """Считает ненайденный запрос (анонимно, без ID пользователя)."""
    query = " ".join((text or "").lower().split())[:60]
    if not query:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO food_misses (day, query, cnt) VALUES (?, ?, 1) ON CONFLICT(day, query) DO UPDATE SET cnt = cnt + 1",
                 (_today_str(), query))
    conn.commit()
    conn.close()


def get_top_misses(days: int = 7, limit: int = 20) -> list:
    """[(запрос, сколько раз)] за последние days дней."""
    since = (datetime.now(_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT query, SUM(cnt) AS c FROM food_misses WHERE day >= ? GROUP BY query ORDER BY c DESC, query LIMIT ?",
                        (since, limit)).fetchall()
    conn.close()
    return rows


def purge_old_misses(keep_days: int = 90):
    since = (datetime.now(_TZ) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM food_misses WHERE day < ?", (since,))
    conn.commit()
    conn.close()


def create_payment(user_id: int, amount: int, provider: str, provider_payment_id: str, tier: str = DEFAULT_TIER, days: int = None):
    """days — на сколько дней продлит подписку этот платёж (30 для месяца, 365 для года)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO payments (user_id, amount, provider, provider_payment_id, status, created_at, tier, days) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (user_id, amount, provider, provider_payment_id, 'pending', datetime.now().isoformat(), tier, days or PAID_PERIOD_DAYS))
    payment_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return payment_id


def get_payment_by_provider_id(provider: str, provider_payment_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM payments WHERE provider = ? AND provider_payment_id = ?", (provider, provider_payment_id))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_source_stats() -> list:
    """Воронка по источникам рекламы (метка из ссылки ?start=src_метка): пришли → зарегистрировались → оплатили → выручка."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COALESCE(u.source, '') AS source,
               COUNT(*) AS started,
               SUM(CASE WHEN u.weight IS NOT NULL THEN 1 ELSE 0 END) AS registered,
               SUM(CASE WHEN p.user_id IS NOT NULL THEN 1 ELSE 0 END) AS payers,
               COALESCE(SUM(p.total), 0) AS revenue
        FROM users u
        LEFT JOIN (SELECT user_id, SUM(amount) AS total FROM payments WHERE status = 'completed' GROUP BY user_id) p
               ON p.user_id = u.user_id
        GROUP BY COALESCE(u.source, '')
        ORDER BY started DESC
    """)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_admin_stats() -> dict:
    """Сводка для администратора: пользователи, выручка, конверсия, активность.
    Все даты в базе — местное время сервера без часового пояса, окна считаем так же."""
    now = datetime.now()
    now_iso = now.isoformat()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    ago7, ago30 = (now - timedelta(days=7)).isoformat(), (now - timedelta(days=30)).isoformat()
    trial_cut = (now - timedelta(days=TRIAL_DAYS)).isoformat()
    paid_date = "COALESCE(paid_at, created_at)"
    paid = "(SELECT user_id FROM payments WHERE status = 'completed')"

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    def one(sql, params=()):
        cursor.execute(sql, params)
        return cursor.fetchone()[0] or 0

    def revenue(since=None):
        where = f" AND {paid_date} >= ?" if since else ""
        cursor.execute(f"SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM payments WHERE status = 'completed'{where}",
                       (since,) if since else ())
        return tuple(cursor.fetchone())

    stats = {
        "users_total": one("SELECT COUNT(*) FROM users"),
        "users_7d": one("SELECT COUNT(*) FROM users WHERE created_at >= ?", (ago7,)),
        "users_30d": one("SELECT COUNT(*) FROM users WHERE created_at >= ?", (ago30,)),
        "registered": one("SELECT COUNT(*) FROM users WHERE weight IS NOT NULL"),
        "subscribed": one("SELECT COUNT(*) FROM users WHERE subscription_end > ?", (now_iso,)),
        # пробный период: подписка действует, регистрация свежая, оплат не было
        "in_trial": one(f"SELECT COUNT(*) FROM users WHERE subscription_end > ? AND created_at >= ? AND user_id NOT IN {paid}",
                        (now_iso, trial_cut)),
        "revenue_today": revenue(day_start), "revenue_7d": revenue(ago7), "revenue_30d": revenue(ago30), "revenue_total": revenue(),
        "referrals": one("SELECT SUM(referrals_count) FROM users"),
        "paused_now": one("SELECT COUNT(*) FROM users WHERE paused_until > ?", (_now_msk().isoformat(),)),
        "plan_disabled": one("SELECT COUNT(*) FROM users WHERE reminder_plan_enabled = 0 AND subscription_end > ?", (now_iso,)),
        "payers_30d": one(f"SELECT COUNT(DISTINCT user_id) FROM payments WHERE status = 'completed' AND {paid_date} >= ?", (ago30,)),
    }

    # подписки с оплатой или подарком (без пробных) по тарифам
    cursor.execute(f"""SELECT COALESCE(subscription_tier, ?), COUNT(*) FROM users
                       WHERE subscription_end > ? AND (created_at < ? OR user_id IN {paid}) GROUP BY 1""",
                   (DEFAULT_TIER, now_iso, trial_cut))
    stats["subscribed_by_tier"] = dict(cursor.fetchall())

    # выручка за 30 дней по тарифам
    cursor.execute(f"""SELECT COALESCE(tier, ?), COUNT(*), COALESCE(SUM(amount), 0) FROM payments
                       WHERE status = 'completed' AND {paid_date} >= ? GROUP BY 1""", (DEFAULT_TIER, ago30))
    stats["revenue_30d_by_tier"] = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}

    # конверсия: из тех, у кого пробный период уже закончился, сколько хоть раз оплатили
    for key, extra, params in (("trial_all", "", (trial_cut,)), ("trial_30d", " AND created_at >= ?", (trial_cut, ago30))):
        passed = one(f"SELECT COUNT(*) FROM users WHERE created_at <= ?{extra}", params)
        converted = one(f"SELECT COUNT(*) FROM users WHERE created_at <= ?{extra} AND user_id IN {paid}", params)
        stats[key] = (converted, passed)

    # удержание: из платившего хоть раз, сколько платили повторно
    stats["repeat"] = (one("SELECT COUNT(*) FROM (SELECT user_id FROM payments WHERE status = 'completed' GROUP BY user_id HAVING COUNT(*) >= 2)"),
                       one("SELECT COUNT(DISTINCT user_id) FROM payments WHERE status = 'completed'"))

    # активность за 7 дней: взвешивание, запись еды, отметки воды или привычек (итоги дней — только с отметками)
    since_date = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    active = f"""SELECT user_id FROM weights WHERE recorded_at >= ?
                 UNION SELECT user_id FROM food_logs WHERE logged_at >= ?
                 UNION SELECT user_id FROM daily_logs WHERE log_date >= ? AND (habits_done > 0 OR water > 0)
                 UNION SELECT user_id FROM users WHERE water_today > 0
                                                    OR (habits_completed IS NOT NULL AND habits_completed NOT IN ('', '[]'))"""
    stats["active_7d"] = one(f"SELECT COUNT(*) FROM ({active})", (ago7, ago7, since_date))
    stats["active_7d_subscribed"] = one(
        f"SELECT COUNT(*) FROM ({active}) WHERE user_id IN (SELECT user_id FROM users WHERE subscription_end > ?)",
        (ago7, ago7, since_date, now_iso))
    conn.close()
    return stats


def get_recent_payments(limit: int = 15) -> list:
    """Последние оплаченные платежи с данными пользователя."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""SELECT p.id, p.user_id, u.username, p.amount, p.provider, COALESCE(p.tier, ?) AS tier,
                             COALESCE(p.paid_at, p.created_at) AS paid
                      FROM payments p LEFT JOIN users u ON u.user_id = p.user_id
                      WHERE p.status = 'completed' ORDER BY paid DESC LIMIT ?""", (DEFAULT_TIER, limit))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_payments_export() -> list:
    """Все платежи (любого статуса) для выгрузки в таблицу."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""SELECT p.id, p.created_at, p.paid_at, p.status, p.user_id, u.username, p.amount, p.currency, p.provider,
                             COALESCE(p.tier, ?) AS tier, p.provider_payment_id
                      FROM payments p LEFT JOIN users u ON u.user_id = p.user_id ORDER BY p.created_at""", (DEFAULT_TIER,))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def update_payment_status(payment_id: int, status: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    paid_at = datetime.now().isoformat() if status == 'completed' else None
    cursor.execute("UPDATE payments SET status = ?, paid_at = ? WHERE id = ?", (status, paid_at, payment_id))
    conn.commit()
    conn.close()


def complete_payment(payment: dict) -> bool:
    """Зачисляет оплаченный платёж ровно один раз: отмечает его выполненным и продлевает подписку
    на срок платежа (payments.days, по умолчанию PAID_PERIOD_DAYS) по тарифу платежа. Возвращает True, если подписка продлена сейчас, и False,
    если платёж уже был зачтён раньше (повторное нажатие «Проверить оплату», гонка с автопроверкой)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE payments SET status = 'completed', paid_at = ? WHERE id = ? AND status != 'completed'",
                   (datetime.now().isoformat(), payment['id']))
    claimed = cursor.rowcount == 1
    conn.commit()
    conn.close()
    if not claimed:
        return False
    try:
        extend_subscription(payment['user_id'], payment.get('days') or PAID_PERIOD_DAYS, tier=payment.get('tier') or DEFAULT_TIER)
    except Exception:
        # не оставляем платёж «зачтённым» без продления — автопроверка повторит попытку
        update_payment_status(payment['id'], 'pending')
        raise
    return True


def has_food_analysis(user_id: int) -> bool:
    """Анализ еды по фото доступен при действующей подписке тарифа «Полная»."""
    user = get_user(user_id)
    return bool(user) and is_subscribed(user_id) and (user.get('subscription_tier') or DEFAULT_TIER) == 'full'


def get_user_payments(user_id: int, limit: int = 10):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM payments WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_pending_payments(provider: str = None):
    """Все платежи со статусом 'pending' — для фоновой автопроверки без ручной кнопки."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if provider:
        cursor.execute("SELECT * FROM payments WHERE status = 'pending' AND provider = ?", (provider,))
    else:
        cursor.execute("SELECT * FROM payments WHERE status = 'pending'")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def log_food_analysis(user_id: int, photo_file_id: str, analysis_text: str, estimated_calories: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO food_logs (user_id, photo_file_id, analysis_text, estimated_calories, logged_at) VALUES (?, ?, ?, ?, ?)",
                   (user_id, photo_file_id, analysis_text, estimated_calories, user_now(get_user(user_id)).isoformat()))
    conn.commit()
    conn.close()


def get_today_food_calories(user_id: int) -> int:
    """Калории за сегодня: анализ фото + записи дневника."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    today = user_today(get_user(user_id)).strftime("%Y-%m-%d")
    cursor.execute("SELECT COALESCE(SUM(estimated_calories), 0) FROM food_logs WHERE user_id = ? AND DATE(logged_at) = ?", (user_id, today))
    result = cursor.fetchone()[0]
    conn.close()
    return result + get_diary_kcal(user_id, today)


def add_diary_entry(user_id: int, title: str, kcal: int, nutrients: dict = None) -> int:
    """Запись в дневник еды за сегодняшний день (по местному времени пользователя)."""
    user = get_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO food_diary (user_id, day, title, kcal, created_at, nutrients) VALUES (?, ?, ?, ?, ?, ?)",
                   (user_id, user_today(user).strftime("%Y-%m-%d"), title[:80], int(kcal), user_now(user).isoformat(),
                    nutrient_utils.dumps(nutrients) if nutrients else None))
    entry_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return entry_id


MAX_USER_FOODS = 100


def add_user_food(user_id: int, name: str, kcal: float, protein=None, fat=None, carb=None, portion=None) -> str:
    """Сохраняет личный продукт (значения на 100 г). Одноимённый продукт обновляется.
    Возвращает 'added' / 'updated' / 'limit'."""
    name = " ".join((name or "").split())[:40]
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM user_foods WHERE user_id = ?", (user_id,))
    # сравнение без учёта регистра делаем в Python: lower() в SQLite не понимает кириллицу
    row = next((r for r in cursor.fetchall() if r[1].casefold() == name.casefold()), None)
    if row:
        cursor.execute("UPDATE user_foods SET kcal=?, protein=?, fat=?, carb=?, portion=? WHERE id = ?",
                       (kcal, protein, fat, carb, portion, row[0]))
        result = "updated"
    else:
        cursor.execute("SELECT COUNT(*) FROM user_foods WHERE user_id = ?", (user_id,))
        if cursor.fetchone()[0] >= MAX_USER_FOODS:
            conn.close()
            return "limit"
        cursor.execute("INSERT INTO user_foods (user_id, name, kcal, protein, fat, carb, portion, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (user_id, name, kcal, protein, fat, carb, portion, datetime.now().isoformat()))
        result = "added"
    conn.commit()
    conn.close()
    return result


def get_user_foods(user_id: int) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, kcal, protein, fat, carb, portion FROM user_foods WHERE user_id = ? ORDER BY name COLLATE NOCASE", (user_id,))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def delete_user_food(user_id: int, food_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM user_foods WHERE id = ? AND user_id = ?", (food_id, user_id))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def get_diary(user_id: int, day: str = None) -> list:
    day = day or user_today(get_user(user_id)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, kcal, nutrients FROM food_diary WHERE user_id = ? AND day = ? ORDER BY id", (user_id, day))
    rows = [dict(r) for r in cursor.fetchall()]
    for row in rows:
        row["nutrients"] = nutrient_utils.loads(row["nutrients"])
    conn.close()
    return rows


def get_diary_day_totals(user_id: int, since_day: str) -> dict:
    """{'ГГГГ-ММ-ДД': {'kcal', 'count', 'protein', 'fat', 'carb'}} для дней с записями, начиная с since_day."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT day, kcal, nutrients FROM food_diary WHERE user_id = ? AND day >= ?", (user_id, since_day))
    rows = cursor.fetchall()
    conn.close()
    days = {}
    for day, kcal, raw in rows:
        d = days.setdefault(day, {"kcal": 0, "count": 0, "protein": 0.0, "fat": 0.0, "carb": 0.0})
        d["kcal"] += kcal or 0
        d["count"] += 1
        for key, value in (nutrient_utils.loads(raw) or {}).items():
            if key in ("protein", "fat", "carb"):
                d[key] += value
    return days


def get_diary_streak(user_id: int):
    """(серия, записано_сегодня): сколько дней подряд есть записи в дневнике. Если сегодня записей ещё нет,
    серия считается от вчерашнего дня (она «жива» до конца сегодняшнего дня)."""
    user = get_user(user_id)
    if not user:
        return 0, False
    today = user_today(user)
    since = (today - timedelta(days=400)).strftime("%Y-%m-%d")
    days = set(get_diary_day_totals(user_id, since))
    done_today = today.strftime("%Y-%m-%d") in days
    cursor_day = today if done_today else today - timedelta(days=1)
    streak = 0
    while cursor_day.strftime("%Y-%m-%d") in days:
        streak += 1
        cursor_day -= timedelta(days=1)
    return streak, done_today


def used_diary_recently(user_id: int, days: int = 10) -> bool:
    """Были ли записи в дневнике за последние days дней, не считая сегодняшний."""
    user = get_user(user_id)
    if not user:
        return False
    today = user_today(user)
    since = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    return any(d < today.strftime("%Y-%m-%d") for d in get_diary_day_totals(user_id, since))


def get_diary_kcal(user_id: int, day: str = None) -> int:
    return sum(row['kcal'] for row in get_diary(user_id, day))


def delete_diary_entry(user_id: int, entry_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM food_diary WHERE id = ? AND user_id = ?", (entry_id, user_id))
    deleted = cursor.rowcount == 1
    conn.commit()
    conn.close()
    return deleted


def get_food_history(user_id: int, limit: int = 10):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM food_logs WHERE user_id = ? ORDER BY logged_at DESC LIMIT ?", (user_id, limit))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def log_day(user_id: int, water: int, habits_done: int, habits_total: int, paused: bool = False, day: str = None):
    """day — дата дня по местному времени пользователя (ГГГГ-ММ-ДД); по умолчанию «сегодня по Москве»."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    today = day or _today_str()
    cursor.execute("INSERT OR REPLACE INTO daily_logs (user_id, log_date, water, habits_done, habits_total, paused) VALUES (?, ?, ?, ?, ?, ?)",
                   (user_id, today, water, habits_done, habits_total, 1 if paused else 0))
    conn.commit()
    conn.close()


def get_weekly_stats(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    cursor.execute("SELECT * FROM daily_logs WHERE user_id = ? AND log_date >= ? AND COALESCE(paused, 0) = 0 ORDER BY log_date DESC", (user_id, week_ago))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


# === Пауза ===
# Пауза молчит напоминания и «План дня», не сжигает серию, исключается из статистики и не даёт автокоррекции
# пересчитать норму. Подписка при этом идёт как обычно. Время паузы хранится по Москве (как расписание бота).

def _now_msk() -> datetime:
    return datetime.now(_TZ).replace(tzinfo=None)


def _paused_until(user: dict):
    value = (user or {}).get('paused_until')
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def is_paused(user: dict) -> bool:
    """Пауза сейчас включена."""
    until = _paused_until(user)
    return bool(until) and until > _now_msk()


def pause_touches_today(user: dict) -> bool:
    """Пауза действует сейчас или закончилась сегодня (по местному дню пользователя): такой день не считаем «проваленным»."""
    until = _paused_until(user)
    if not until:
        return False
    local_midnight = user_now(user).replace(hour=0, minute=0, second=0, microsecond=0)
    shift = _now_msk() - user_now(user)  # местное время -> московское
    return until >= local_midnight + shift


def paused_recently(user: dict, days: int = 14) -> bool:
    """Пауза включена или закончилась меньше days дней назад."""
    until = _paused_until(user)
    return bool(until) and until >= _now_msk() - timedelta(days=days)


PAUSE_MAX_DAYS = 30  # максимум одной паузы; потом возобновление автоматически


def pause_available(user: dict) -> bool:
    """Можно ли поставить паузу: один раз за оплаченный период, пока подписка активна."""
    if not user or is_paused(user) or not user.get('pause_available'):
        return False
    end = user.get('subscription_end')
    return bool(end) and datetime.fromisoformat(end) > datetime.now()


def start_pause(user_id: int):
    """Ставит паузу. Дни подписки замораживаются: конец подписки сдвигается на максимальную длину паузы,
    а при возобновлении неиспользованная часть сдвига снимается (см. end_pause).
    Возвращает дату автовозобновления или None, если пауза недоступна."""
    user = get_user(user_id)
    if not pause_available(user):
        return None
    until = _now_msk() + timedelta(days=PAUSE_MAX_DAYS)
    new_end = datetime.fromisoformat(user['subscription_end']) + timedelta(days=PAUSE_MAX_DAYS)
    update_user(user_id, paused_until=until.isoformat(), pause_notified=0,
                pause_available=0, subscription_end=new_end.isoformat())
    return until


def end_pause(user_id: int) -> bool:
    """Снимает паузу сейчас: подписка продолжается с того же места. True, если пауза была включена."""
    user = get_user(user_id)
    if not is_paused(user):
        return False
    unused = _paused_until(user) - _now_msk()
    end = datetime.fromisoformat(user['subscription_end']) - unused
    update_user(user_id, paused_until=_now_msk().isoformat(), pause_notified=1,
                subscription_end=max(end, datetime.now()).isoformat())
    return True


def frozen_subscription_end(user: dict):
    """Дата окончания подписки, которая «заморожена» на время паузы (для показа пользователю)."""
    if not is_paused(user):
        return None
    return datetime.fromisoformat(user['subscription_end']) - (_paused_until(user) - _now_msk())


# === Серии (streaks) по привычкам ===
# День засчитывается в серию, если выполнены ВСЕ привычки дня.
# Чтобы засчитывать день хотя бы при одной выполненной привычке — поставьте False.
STREAK_REQUIRE_ALL_HABITS = True


def _day_counts(done: int, total: int) -> bool:
    if not total:
        return False
    return done >= total if STREAK_REQUIRE_ALL_HABITS else done > 0


def get_habit_streak(user_id: int) -> int:
    """Сколько дней подряд пользователь выполняет привычки.

    Прошлые дни берём из daily_logs, сегодняшний — "вживую" (habits_completed
    против habits), поэтому не важно, когда именно вызывается log_day().
    Если сегодня привычки ещё не закрыты, серия не обрывается: считаем от вчера.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT log_date, MAX(habits_done), MAX(habits_total), MAX(COALESCE(paused, 0)) "
        "FROM daily_logs WHERE user_id = ? GROUP BY log_date",
        (user_id,),
    )
    days = {row[0]: (row[1] or 0, row[2] or 0, row[3] or 0) for row in cursor.fetchall()}
    conn.close()

    user = get_user(user_id)
    today = user_today(user)
    today_ok = not pause_touches_today(user) and _day_counts(len(get_completed_habits(user_id)), len(get_habits(user_id)))

    streak = 1 if today_ok else 0
    day = today - timedelta(days=1)
    while True:
        done, total, paused = days.get(day.strftime("%Y-%m-%d"), (0, 0, 0))
        if paused:  # день паузы: серию не растит и не рвёт
            day -= timedelta(days=1)
            continue
        if not _day_counts(done, total):
            break
        streak += 1
        day -= timedelta(days=1)
    return streak


def _days_word(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "дней"
    return {1: "день", 2: "дня", 3: "дня", 4: "дня"}.get(n % 10, "дней")


def format_streak_message(streak: int) -> str:
    """Короткая строка про серию для показа в боте. Пустая, если серии нет."""
    if streak <= 0:
        return ""
    text = f"🔥 {streak} {_days_word(streak)} подряд выполняете привычки!"
    if streak in (7, 14, 30, 60, 100, 200, 365):
        text += "\n🏆 Отличный рубеж, так держать!"
    return text


def get_monthly_report_data(user_id: int, days: int = 30) -> dict:
    """Данные для отчёта за последние `days` дней: вес, привычки, вода, серии.
    Привычки и вода берутся из daily_logs (пишется при ночном закрытии дня)."""
    today = user_today(get_user(user_id))
    start = today - timedelta(days=days - 1)
    start_str = start.strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT log_date, water, habits_done, habits_total, COALESCE(paused, 0) FROM daily_logs "
        "WHERE user_id = ? AND log_date >= ? ORDER BY log_date",
        (user_id, start_str),
    )
    all_logs = cursor.fetchall()
    paused_dates = {row[0] for row in all_logs if row[4]}
    logs = [row for row in all_logs if not row[4]]
    cursor.execute(
        "SELECT weight, recorded_at FROM weights WHERE user_id = ? AND recorded_at >= ? ORDER BY recorded_at",
        (user_id, start_str),
    )
    weights = [(datetime.fromisoformat(row[1]), row[0]) for row in cursor.fetchall()]
    conn.close()

    total_done = sum(row[2] or 0 for row in logs)
    total_habits = sum(row[3] or 0 for row in logs)
    water_values = [row[1] or 0 for row in logs]

    ok_days = {row[0] for row in logs if _day_counts(row[2] or 0, row[3] or 0)}
    best_streak = run = 0
    day = start
    while day <= today:
        key = day.strftime("%Y-%m-%d")
        if key not in paused_dates:  # дни паузы пропускаем: серию они не рвут и не растят
            run = run + 1 if key in ok_days else 0
            best_streak = max(best_streak, run)
        day += timedelta(days=1)
    current_streak = get_habit_streak(user_id)
    best_streak = max(best_streak, min(current_streak, days))  # сегодняшний день в логах появится только ночью

    return {
        "start": start,
        "end": today,
        "weights": weights,  # [(datetime, кг), ...] по возрастанию даты
        "days_logged": len(logs),
        "habits_percent": round(100 * total_done / total_habits) if total_habits else None,
        "avg_water": None,  # воду в боте больше не отслеживаем
        "current_streak": current_streak,
        "best_streak": best_streak,
    }


def format_streak_at_risk(streak: int) -> str:
    """Строка для вечернего напоминания: серия есть, но сегодня привычки ещё не закрыты."""
    if streak <= 0:
        return ""
    return f"🔥 Ваша серия — {streak} {_days_word(streak)} подряд. Отметьте оставшиеся привычки, чтобы не потерять её!"


def get_stats():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE subscription_end > ?", (datetime.now().isoformat(),))
    active_subs = cursor.fetchone()[0]
    cursor.execute("SELECT SUM(amount) FROM payments WHERE status = 'completed'")
    total_revenue = cursor.fetchone()[0] or 0
    cursor.execute("SELECT SUM(referrals_count) FROM users")
    total_referrals = cursor.fetchone()[0] or 0
    conn.close()
    return {"total_users": total_users, "active_subscriptions": active_subs, "total_revenue": total_revenue, "total_referrals": total_referrals}


def delete_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    cursor.execute("DELETE FROM weights WHERE user_id = ?", (user_id,))
    # платежи не удаляем: это бухгалтерские записи, по ним считается выручка
    cursor.execute("DELETE FROM daily_logs WHERE user_id = ?", (user_id,))
    cursor.execute("DELETE FROM food_logs WHERE user_id = ?", (user_id,))
    cursor.execute("DELETE FROM food_diary WHERE user_id = ?", (user_id,))
    cursor.execute("DELETE FROM user_foods WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def set_consent(user_id: int):
    update_user(user_id, consent_at=datetime.now().isoformat())


def erase_user_data(user_id: int):
    """Полное удаление персональных данных по просьбе пользователя. Остаются записи об оплатах (бухучёт)
    и служебная отметка со сроком подписки, чтобы оплаченные дни не пропали и пробный период не выдавался повторно."""
    user = get_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if user:
        cursor.execute("INSERT OR REPLACE INTO deleted_users (user_id, subscription_end, subscription_tier, deleted_at) VALUES (?, ?, ?, ?)",
                       (user_id, user.get('subscription_end'), user.get('subscription_tier'), datetime.now().isoformat()))
    conn.commit()
    conn.close()
    delete_user(user_id)


# === Еженедельный адаптивный пересчёт калорийности ===
# Идея (вместо разового "поймали полный застой"): каждую неделю сравниваем
# СРЕДНИЙ вес за последние 7 дней со средним весом за предыдущие 7 дней —
# это сглаживает суточные скачки от воды/еды/гликогена, на которые нельзя
# полагаться при одиночном взвешивании. Полученный фактический недельный
# темп сравниваем с целевым (заданным при регистрации: target_weight +
# weekly_pace_percent) и плавно подруливаем calorie_adjustment:
#   - идёт заметно медленнее плана (или в обратную сторону) → усиливаем
#     дефицit/профицит на шаг
#   - идёт заметно быстрее плана (> в 1.5 раза) → немного отпускаем, чтобы
#     не терять мышцы при похудении / не набирать лишний жир при наборе
#   - в пределах плана (50–150% от целевого темпа) → не трогаем
# Для цели "поддержание" логика не применяется.
WEEKLY_STEP_KCAL = 100
WEEKLY_MAX_ADJUSTMENT = 500
WEIGHT_RISE_TOLERANCE_KG = 0.3   # изменение среднего веса за неделю до этого значения в «не ту» сторону считаем шумом, а не ростом
SLOW_PROGRESS_RATIO = 0.5   # фактический темп меньше этой доли от плана — слишком медленно
FAST_PROGRESS_RATIO = 1.5   # фактический темп больше этой доли от плана — слишком быстро


def _avg_weight_in_window(history, days_ago_start: int, days_ago_end: int):
    """Средний вес по записям, чей возраст (в днях от сейчас) попадает в
    полуинтервал (days_ago_end, days_ago_start] — т.е. days_ago_end < возраст <= days_ago_start."""
    now = datetime.now()
    values = []
    for h in history:
        age_days = (now - datetime.fromisoformat(h['recorded_at'])).total_seconds() / 86400
        if days_ago_end < age_days <= days_ago_start:
            values.append(h['weight'])
    return (sum(values) / len(values)) if values else None


def mark_slip(user_id: int):
    """Запоминает, что человек открывал раздел «Если сорвался». Нужно только для того, чтобы
    в ближайшую неделю не срезать ему норму калорий (см. weekly_adaptive_adjustment)."""
    update_user(user_id, last_slip_at=datetime.now().isoformat())


def _recent_slip(user: dict, days: int = 7) -> bool:
    value = user.get('last_slip_at')
    if not value:
        return False
    try:
        return datetime.now() - datetime.fromisoformat(value) < timedelta(days=days)
    except ValueError:
        return False


def _progress_ratio(goal: str, pace_percent: float, avg_now: float, avg_before: float):
    """(фактическое изменение, плановое изменение, доля плана) за неделю между двумя средними весами."""
    actual_change = avg_now - avg_before  # кг за неделю, со знаком
    target_change = (pace_percent / 100) * avg_now
    target_change = -target_change if goal == 'похудение' else target_change
    # Соотношение факта к плану имеет смысл только когда знаки совпадают —
    # если идёт в обратную сторону, это всегда "слишком медленно" (< 0 от плана).
    same_direction = (actual_change < 0) == (target_change < 0)
    ratio = (actual_change / target_change) if (same_direction and target_change != 0) else 0
    return actual_change, target_change, ratio


def _moving_wrong_way(goal: str, actual_change: float) -> bool:
    """Вес за неделю заметно движется в сторону, обратную цели (растёт при похудении, падает при наборе)."""
    if goal == 'похудение':
        return actual_change > WEIGHT_RISE_TOLERANCE_KG
    return actual_change < -WEIGHT_RISE_TOLERANCE_KG


def _below_calorie_floor(user: dict, goal: str, new_adjustment: int) -> bool:
    """True, если после коррекции норма станет ниже max(CALORIE_FLOOR, базовый обмен)."""
    from menu_generator import calculate_daily_calories  # локальный импорт: без циклов при загрузке модулей
    weight = user.get('weight')
    if not weight:
        return False
    body_fat = user.get('body_fat') or 20
    base = calculate_daily_calories(weight, body_fat, goal, user.get('activity_level') or 'легкий')
    return base + new_adjustment < min_daily_calories(weight, body_fat)


def weekly_adaptive_adjustment(user_id: int):
    """
    Раз в неделю сверяет фактический тренд веса (среднее за последние 7 дней
    против среднего за предыдущие 7 дней) с целевым темпом пользователя и
    корректирует calorie_adjustment. Возвращает текст уведомления для
    пользователя, если что-то изменилось или достигнута цель, иначе None.
    Ничего не делает для пользователей без target_weight/weekly_pace_percent
    (например, цель "поддержание", или анкета заполнялась до введения этой
    механики).
    """
    user = get_user(user_id)
    if not user:
        return None

    goal = user.get('goal', 'похудение')
    target_weight = user.get('target_weight')
    pace_percent = user.get('weekly_pace_percent')
    if goal not in ('похудение', 'набор') or not target_weight or not pace_percent:
        return None
    if paused_recently(user):
        return None  # после перерыва вес и привычки несравнимы с обычными неделями — норму не трогаем

    history = get_weight_history(user_id, limit=60)
    if len(history) < 2:
        return None

    avg_this_week = _avg_weight_in_window(history, 7, 0)
    avg_last_week = _avg_weight_in_window(history, 14, 7)
    if avg_this_week is None or avg_last_week is None:
        return None  # нет хотя бы одного взвешивания в каждой из двух последних недель

    # Достигнута ли цель?
    if goal == 'похудение' and avg_this_week <= target_weight:
        if user.get('calorie_adjustment'):
            update_user(user_id, calorie_adjustment=0)
        return f"🎉 Поздравляем! Ваш средний вес за неделю достиг цели ({target_weight} кг). Можно закрепить результат — переключитесь на цель «Поддержание» в /update."
    if goal == 'набор' and avg_this_week >= target_weight:
        if user.get('calorie_adjustment'):
            update_user(user_id, calorie_adjustment=0)
        return f"🎉 Поздравляем! Ваш средний вес за неделю достиг цели ({target_weight} кг). Можно закрепить результат — переключитесь на цель «Поддержание» в /update."

    actual_change, target_change, ratio = _progress_ratio(goal, pace_percent, avg_this_week, avg_last_week)
    current_adjustment = user.get('calorie_adjustment') or 0

    if ratio < SLOW_PROGRESS_RATIO:
        step = -WEEKLY_STEP_KCAL if goal == 'похудение' else WEEKLY_STEP_KCAL
        new_adjustment = max(-WEEKLY_MAX_ADJUSTMENT, min(WEEKLY_MAX_ADJUSTMENT, current_adjustment + step))
        if new_adjustment == current_adjustment:
            return None

        # 1. Одна медленная неделя — это шум (вода, соль, срыв), а не причина менять норму:
        #    корректируем только если предыдущая неделя тоже шла медленно.
        avg_before_last = _avg_weight_in_window(history, 21, 14)
        if avg_before_last is None:
            return None
        previous_change, _, previous_ratio = _progress_ratio(goal, pace_percent, avg_last_week, avg_before_last)
        if previous_ratio >= SLOW_PROGRESS_RATIO:
            return None

        # 2. Если за последнюю неделю человек открывал «Если сорвался» — норму не трогаем.
        if _recent_slip(user):
            return ("📊 Эта неделя вышла непростой, поэтому норму калорий не меняем. "
                    "Вернитесь к обычному режиму — дальше посмотрим на динамику 💚")

        # 3. Вес идёт в обратную сторону — это не «норма слишком велика», а сбой плана или колебания:
        #    срезать норму тут бессмысленно, поэтому не трогаем её и поддерживаем человека.
        if _moving_wrong_way(goal, actual_change):
            if goal == 'похудение':
                return (f"📊 Средний вес за неделю вырос на {abs(actual_change):.1f} кг — такое бывает: вода, соль, "
                        "нерегулярное питание. Норму калорий не меняем, сначала вернёмся к плану. "
                        "Если срыв повторяется, загляните в «💚 Если сорвался».")
            return (f"📊 Средний вес за неделю снизился на {abs(actual_change):.1f} кг. Норму калорий не меняем: "
                    "возможно, её не удаётся съедать полностью. Попробуйте добавить калорийные перекусы из меню.")
        if _moving_wrong_way(goal, previous_change):
            return None  # неделю назад вес шёл не туда — это ещё не застой, подождём

        # 4. Нижняя граница: срез не должен опускать норму ниже безопасного минимума.
        if step < 0 and _below_calorie_floor(user, goal, new_adjustment):
            return ("📊 Норма калорий уже на нижней безопасной границе, дальше не снижаем. "
                    "Если вес стоит на месте, стоит обсудить план питания с врачом или диетологом.")

        update_user(user_id, calorie_adjustment=new_adjustment)
        return (f"📊 За неделю средний вес изменился на {actual_change:+.1f} кг — медленнее плана "
                f"({target_change:+.1f} кг/нед). Корректируем норму калорий на {step:+d} ккал.")
    elif ratio > FAST_PROGRESS_RATIO:
        step = WEEKLY_STEP_KCAL if goal == 'похудение' else -WEEKLY_STEP_KCAL
        new_adjustment = current_adjustment + step
        new_adjustment = max(-WEEKLY_MAX_ADJUSTMENT, min(WEEKLY_MAX_ADJUSTMENT, new_adjustment))
        if new_adjustment != current_adjustment:
            update_user(user_id, calorie_adjustment=new_adjustment)
            reason = "чтобы не терять мышцы" if goal == 'похудение' else "чтобы не набирать лишний жир"
            return (f"📊 За неделю средний вес изменился на {actual_change:+.1f} кг — быстрее плана "
                    f"({target_change:+.1f} кг/нед). Немного смягчаем норму калорий на {step:+d} ккал, {reason}.")

    return None


def estimate_weeks_to_goal(current_weight: float, target_weight: float, pace_percent: float) -> int:
    """Грубая оценка числа недель до достижения цели при заданном темпе (% веса/неделю)."""
    weekly_kg = current_weight * (pace_percent / 100)
    if weekly_kg <= 0:
        return 0
    return max(1, round(abs(target_weight - current_weight) / weekly_kg))
