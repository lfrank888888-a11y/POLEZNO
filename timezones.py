"""Часовые пояса пользователей: список для выбора и разбор ручного ввода («+5», «UTC+3»)."""

import re
import pytz

DEFAULT_TZ = "Europe/Moscow"

# (подпись, IANA). Порядок — для кнопок.
ZONES = [
    ("Калининград (МСК−1)", "Europe/Kaliningrad"),
    ("Москва, Минск (МСК)", "Europe/Moscow"),
    ("Самара, Баку (МСК+1)", "Europe/Samara"),
    ("Екатеринбург, Ташкент (МСК+2)", "Asia/Yekaterinburg"),
    ("Омск, Бишкек (МСК+3)", "Asia/Omsk"),
    ("Новосибирск, Красноярск (МСК+4)", "Asia/Krasnoyarsk"),
    ("Иркутск (МСК+5)", "Asia/Irkutsk"),
    ("Якутск (МСК+6)", "Asia/Yakutsk"),
    ("Владивосток (МСК+7)", "Asia/Vladivostok"),
    ("Магадан (МСК+8)", "Asia/Magadan"),
    ("Камчатка (МСК+9)", "Asia/Kamchatka"),
]

_LABELS = {iana: label for label, iana in ZONES}


def get_tz(name):
    """pytz-пояс по имени; при пустом/неверном — Москва."""
    try:
        return pytz.timezone(name or DEFAULT_TZ)
    except pytz.UnknownTimeZoneError:
        return pytz.timezone(DEFAULT_TZ)


def offset_hours(name) -> float:
    off = pytz.utc.localize(__import__("datetime").datetime.utcnow()).astimezone(get_tz(name)).utcoffset()
    return off.total_seconds() / 3600


def label(name) -> str:
    """Человеческое название пояса для показа в настройках."""
    name = name or DEFAULT_TZ
    if name in _LABELS:
        return _LABELS[name]
    off = offset_hours(name)
    return f"UTC{int(off):+d}"


def parse_offset(text: str):
    """«+5», «UTC+5», «gmt -3», «5» -> имя пояса вида Etc/GMT-5 (знак у Etc инвертирован). None, если не разобрали.
    Только целые часы от −12 до +14."""
    m = re.fullmatch(r"\s*(?:utc|gmt)?\s*([+\-−]?)\s*(\d{1,2})(?::00)?\s*", (text or "").lower())
    if not m:
        return None
    hours = int(m.group(2))
    sign = -1 if m.group(1) in ("-", "−") else 1
    value = sign * hours
    if not -12 <= value <= 14:
        return None
    if value == 0:
        return "UTC"
    return f"Etc/GMT{-value:+d}"
