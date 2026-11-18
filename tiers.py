"""Тарифы подписки: «Базовая» и «Полная» (с анализом еды по фото).

Цены и названия правятся здесь, больше нигде.
"""

# «Полная» продаётся, только когда анализ еды по фото реально работает (сервис оплачен и проверен).
# Пока False, в боте виден один тариф — «Базовая». Когда анализ заработает, поставьте True.
FULL_TIER_ENABLED = False

PAID_PERIOD_DAYS = 30

# Годовая подписка: 12 месяцев по цене ANNUAL_MONTHS_PAID месяцев. Чтобы отключить, поставьте ANNUAL_ENABLED = False.
ANNUAL_ENABLED = True
ANNUAL_MONTHS_PAID = 10
ANNUAL_DAYS = 365

# Тариф у пробного периода и у тех, кто оформил подписку до введения тарифов
DEFAULT_TIER = "full"

TIERS = {
    "basic": {"name": "Базовая", "emoji": "⭐", "price": 200},
    "full": {"name": "Полная", "emoji": "💎", "price": 350},
}

BASE_FEATURES = [
    "✅ Меню с учётом аллергий и типа питания",
    "🛒 Список покупок на неделю по разделам магазина",
    "📈 Норма калорий корректируется каждую неделю по реальной динамике веса",
    "🔥 Серии привычек: сколько дней подряд вы держитесь",
    "🗓 Отчёт за месяц: итоги и график веса одной картинкой",
    "✅ Пошаговые рецепты для каждого блюда",
    "✅ Трекер веса с графиком",
    "✅ Статистика и итоги недели",
    "✅ Трекер привычек, умные напоминания",
    "✅ Расчёт калорий и ИМТ с рекомендацией под вашу цель",
]
FOOD_FEATURE = "📸 Анализ еды по фото (AI): что на тарелке, калории и БЖУ"


def sellable_tiers() -> list:
    """Тарифы, которые сейчас можно купить."""
    return ["basic", "full"] if FULL_TIER_ENABLED else ["basic"]


def tier_of(user: dict) -> str:
    tier = (user or {}).get("subscription_tier")
    return tier if tier in TIERS else DEFAULT_TIER


def price(tier: str) -> int:
    return TIERS[tier]["price"]


def name(tier: str) -> str:
    return TIERS[tier]["name"]


def label(tier: str) -> str:
    """Подпись для кнопки: «⭐ Базовая — 200₽»."""
    return f"{TIERS[tier]['emoji']} {name(tier)} — {price(tier)}₽"


def plans_overview() -> str:
    """Описание тарифов для экрана подписки, «Что входит?» и напоминания об окончании."""
    base = "\n".join(BASE_FEATURES)
    if not FULL_TIER_ENABLED:
        annual = (f"\n📆 Или год за *{annual_price('basic')}₽* — {free_months()} мес. в подарок (выгода {annual_saving('basic')}₽)"
                  if ANNUAL_ENABLED else "")
        return f"С подпиской вы получаете:\n{base}\n\n💳 Стоимость: *{price('basic')}₽/мес*{annual}"
    annual = (f"\n\n📆 Год — 12 месяцев по цене {ANNUAL_MONTHS_PAID}: «{name('basic')}» {annual_price('basic')}₽, «{name('full')}» {annual_price('full')}₽"
              if ANNUAL_ENABLED else "")
    return (f"{TIERS['basic']['emoji']} *{name('basic')} — {price('basic')}₽/мес*\n{base}\n\n"
            f"{TIERS['full']['emoji']} *{name('full')} — {price('full')}₽/мес*\n"
            f"Всё из «{name('basic')}» и ещё:\n{FOOD_FEATURE}{annual}")


def annual_price(tier: str) -> int:
    return price(tier) * ANNUAL_MONTHS_PAID


def annual_saving(tier: str) -> int:
    return price(tier) * 12 - annual_price(tier)


def free_months() -> int:
    return 12 - ANNUAL_MONTHS_PAID


def annual_button_label(tier: str) -> str:
    """Подпись кнопки: «📆 Год — 2000₽ (2 мес. в подарок)»."""
    return f"📆 Год — {annual_price(tier)}₽ ({free_months()} мес. в подарок)"


def period_text(days: int) -> str:
    return f"год ({days} дней)" if days >= ANNUAL_DAYS else f"{days} дней"
