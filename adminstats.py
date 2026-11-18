"""Оформление статистики для администратора: сводка, список оплат и выгрузка в CSV."""

import csv
import io
from datetime import datetime

from tiers import TIERS, name as tier_name

# Подсказка по командам: без символов «_» и «[», иначе Telegram не примет сообщение в режиме Markdown
ADMIN_COMMANDS = (
    "/extend ID или @username ДНЕЙ (по желанию тариф basic или full) — подарить или продлить подписку\n"
    "/payments — последние оплаты\n"
    "/export — выгрузка всех платежей в CSV (откроется в Excel)\n"
    "/link МЕТКА — ссылка на бота для рекламы (например /link telega1)\n"
    "/sources — какие рекламные источники приводят людей и оплаты\n"
    "/broadcast ТЕКСТ — рассылка"
)


def _money(value) -> str:
    return f"{int(value):,}".replace(",", " ") + "₽"


def _payments_word(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "оплат"
    return {1: "оплата", 2: "оплаты", 3: "оплаты", 4: "оплаты"}.get(n % 10, "оплат")


def _pct(part, whole) -> str:
    return f"{round(100 * part / whole)}%" if whole else "—"


def _tier(key) -> str:
    return tier_name(key) if key in TIERS else str(key)


def _md_escape(text) -> str:
    out = str(text)
    for ch in ("\\", "_", "*", "`", "["):
        out = out.replace(ch, "\\" + ch)
    return out


def build_admin_report(s: dict) -> str:
    lines = ["📊 *Админ-панель*", "", "👥 *Пользователи*",
             f"Всего: {s['users_total']} (за 7 дней +{s['users_7d']}, за 30 дней +{s['users_30d']})",
             f"Прошли регистрацию: {s['registered']} из {s['users_total']} ({_pct(s['registered'], s['users_total'])})",
             f"Подписка действует: {s['subscribed']}, из них в пробном периоде: {s['in_trial']}"]
    if s["subscribed_by_tier"]:
        parts = ", ".join(f"{_tier(k)} {v}" for k, v in sorted(s["subscribed_by_tier"].items()))
        lines.append(f"Без пробных, по тарифам: {parts}")

    lines += ["", "💰 *Выручка*"]
    for title, key in (("Сегодня", "revenue_today"), ("7 дней", "revenue_7d"), ("30 дней", "revenue_30d"), ("Всего", "revenue_total")):
        count, total = s[key]
        lines.append(f"{title}: {_money(total)} ({count} {_payments_word(count)})")
    if s["revenue_30d_by_tier"]:
        parts = "; ".join(f"{_tier(k)} — {c} {_payments_word(c)} на {_money(t)}" for k, (c, t) in sorted(s["revenue_30d_by_tier"].items()))
        lines.append(f"За 30 дней по тарифам: {parts}")

    conv30, conv_all = s["trial_30d"], s["trial_all"]
    repeat = s["repeat"]
    lines += ["", "🔁 *Конверсия и удержание*",
              f"Пробный → оплата, пришедшие за 30 дней: {conv30[0]} из {conv30[1]} ({_pct(*conv30)})",
              f"Пробный → оплата, за всё время: {conv_all[0]} из {conv_all[1]} ({_pct(*conv_all)})",
              f"Платили повторно: {repeat[0]} из {repeat[1]} ({_pct(*repeat)})"]

    lines += ["", "📈 *Активность*",
              f"За 7 дней были записи или отметки: {s['active_7d']} (из подписанных: {s['active_7d_subscribed']} из {s['subscribed']})",
              f"Отключили план дня: {s['plan_disabled']} из {s['subscribed']} подписанных",
              f"Сейчас на паузе: {s['paused_now']}",
              "", f"🎁 Рефералов: {s['referrals']}", "", ADMIN_COMMANDS]
    return "\n".join(lines)


def _user_label(row: dict) -> str:
    return f"@{row['username']}" if row.get("username") else str(row["user_id"])


def _short_date(value) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return "—"


def format_payments(rows: list) -> str:
    if not rows:
        return "Оплаченных платежей пока нет."
    lines = ["💳 *Последние оплаты*", ""]
    for row in rows:
        lines.append(f"{_short_date(row['paid'])} · {_md_escape(_user_label(row))} · {_tier(row['tier'])} · "
                     f"{_money(row['amount'])} · {row['provider']}")
    return "\n".join(lines)


def payments_csv(rows: list) -> bytes:
    """CSV в кодировке utf-8-sig с разделителем «;» — Excel открывает такой файл без «кракозябр»."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["ID", "Создан", "Оплачен", "Статус", "ID пользователя", "Username", "Сумма", "Валюта", "Провайдер", "Тариф",
                     "ID платежа у провайдера"])
    for r in rows:
        writer.writerow([r["id"], r["created_at"] or "", r["paid_at"] or "", r["status"], r["user_id"], r["username"] or "",
                         r["amount"], r["currency"] or "RUB", r["provider"], _tier(r["tier"]), r["provider_payment_id"] or ""])
    return buffer.getvalue().encode("utf-8-sig")


def format_sources(rows: list) -> str:
    """Таблица источников: пришли / зарегистрировались / оплатили / выручка."""
    if not rows:
        return "Пока нет данных."
    lines = ["📣 *Источники*", "(пришли → зарегистрировались → оплатили, выручка)", ""]
    for r in rows:
        name = _md_escape(r["source"]) if r["source"] else "без метки (прямой заход, друзья)"
        lines.append(f"*{name}*: {r['started']} → {r['registered']} → {r['payers']}, {_money(r['revenue'])}"
                     f" (оплатили {_pct(r['payers'], r['started'])})")
    lines += ["", "Ссылку с меткой даёт команда /link МЕТКА."]
    return "\n".join(lines)
