"""Поиск продукта по штрихкоду: распознавание кода на фото и данные из Open Food Facts (openfoodfacts.org).

Open Food Facts — открытая база товаров, которую пополняют сами пользователи, поэтому данные бывают неполными или
неточными. Бот показывает это в подсказке. Найденные товары кэшируются в базе бота (таблица barcode_cache):
повторный запрос по тому же коду не обращается в интернет.

Для распознавания кода с фото нужна библиотека:   pip install zxing-cpp
Без неё бот продолжает работать — штрихкод можно ввести цифрами.
"""

import io
import json
import logging
import re
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
OFF_FIELDS = ("code,product_name,product_name_ru,generic_name,brands,quantity,product_quantity,serving_size,"
              "serving_quantity,nutriments")
TIMEOUT = 6                      # секунд на один запрос в OFF
CACHE_FOUND_DAYS = 90
CACHE_MISSING_DAYS = 3           # «не найден» помним недолго: товар могут добавить в базу

STATUS_OK, STATUS_NOT_FOUND, STATUS_UNAVAILABLE, STATUS_INCOMPLETE = "ok", "not_found", "unavailable", "incomplete"


# ---------- проверка кода ----------

def clean_code(text: str):
    """Только цифры, 8–14 знаков; иначе None. Пробелы и дефисы между цифрами допускаются."""
    digits = re.sub(r"[\s\-]", "", text or "")
    return digits if re.fullmatch(r"\d{8,14}", digits) else None


def checksum_ok(code: str) -> bool:
    """Проверка контрольной цифры EAN-8/UPC-A/EAN-13/GTIN-14: ловит опечатки при вводе цифрами."""
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def normalize(code: str) -> str:
    """UPC-A (12 цифр) и GTIN-14 приводим к EAN-13, как хранит Open Food Facts."""
    if len(code) == 12:
        return "0" + code
    if len(code) == 14 and code.startswith("0"):
        return code[1:]
    return code


def is_valid(code: str) -> bool:
    return bool(code) and checksum_ok(code)


# ---------- распознавание на фото ----------

def decoder_available() -> bool:
    try:
        import zxingcpp  # noqa: F401
        return True
    except ImportError:
        return False


def decode_photo(photo_bytes: bytes):
    """Возвращает штрихкод (строка цифр) с фото или None. Не бросает исключений."""
    try:
        import zxingcpp
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(photo_bytes))).convert("L")
    except Exception as e:
        logger.warning(f"Не удалось открыть фото для штрихкода: {e}")
        return None
    fmt = zxingcpp.BarcodeFormat
    wanted = fmt.EAN13 | fmt.EAN8 | fmt.UPCA | fmt.UPCE
    width, height = image.size
    # Сначала как есть, потом уменьшенное и увеличенное: мелкий код на большом фото читается лучше после сжатия
    for scale in (1.0, 0.5, 0.35, 1.5):
        img = image if scale == 1.0 else image.resize((max(1, int(width * scale)), max(1, int(height * scale))))
        try:
            for result in zxingcpp.read_barcodes(img, formats=wanted, try_rotate=True, try_downscale=True):
                code = clean_code(result.text)
                if code and is_valid(code):
                    return normalize(code)
        except Exception as e:
            logger.warning(f"Ошибка распознавания штрихкода: {e}")
            return None
    return None


# ---------- Open Food Facts ----------

def _num(value):
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def parse_product(code: str, product: dict):
    """Данные Open Food Facts -> {name, brand, kcal, n, serving_g, package_g, warn} на 100 г или (None, причина)."""
    nut = product.get("nutriments") or {}
    name = (product.get("product_name_ru") or product.get("product_name") or product.get("generic_name") or "").strip()
    brand = (product.get("brands") or "").split(",")[0].strip()
    if not name:
        name = f"Товар {code}"
    protein, fat, carb = (_num(nut.get(k)) for k in ("proteins_100g", "fat_100g", "carbohydrates_100g"))

    kcal = _num(nut.get("energy-kcal_100g"))
    if kcal is None:
        kj = _num(nut.get("energy-kj_100g"))
        if kj is None:
            kj = _num(nut.get("energy_100g"))  # в OFF это килоджоули
        if kj is not None:
            kcal = kj / 4.184
    if kcal is None and None not in (protein, fat, carb):
        kcal = protein * 4 + fat * 9 + carb * 4
    if kcal is None:
        return None, STATUS_INCOMPLETE
    kcal = round(kcal)
    if kcal > 900 or (protein or 0) > 100 or (fat or 0) > 100 or (carb or 0) > 100:
        return None, STATUS_INCOMPLETE  # заведомо неверные данные

    n = {}
    for key, value in (("protein", protein), ("fat", fat), ("carb", carb)):
        if value is not None:
            n[key] = value
    for key, off_key, factor in (("fiber", "fiber_100g", 1), ("sugar", "sugars_100g", 1), ("sat", "saturated-fat_100g", 1),
                                 ("mono", "monounsaturated-fat_100g", 1), ("poly", "polyunsaturated-fat_100g", 1),
                                 ("trans", "trans-fat_100g", 1), ("chol", "cholesterol_100g", 1000)):
        value = _num(nut.get(off_key))
        if value is not None:
            n[key] = value * factor
    sodium = _num(nut.get("sodium_100g"))
    if sodium is None:
        salt = _num(nut.get("salt_100g"))
        sodium = salt * 0.4 if salt is not None else None
    if sodium is not None:
        n["sodium"] = sodium * 1000  # г -> мг

    warn = ""
    if None not in (protein, fat, carb):
        calc = protein * 4 + fat * 9 + carb * 4
        if calc > 20 and abs(calc - kcal) > max(40, kcal * 0.35):
            warn = "калории и БЖУ в базе не сходятся — сверьте с упаковкой"
    return {"code": code, "name": name, "brand": brand, "kcal": kcal, "n": n,
            "serving_g": _num(product.get("serving_quantity")) or None,
            "package_g": _num(product.get("product_quantity")) or None, "warn": warn}, STATUS_OK


def _user_agent() -> str:
    try:
        import config
        contact = getattr(config, "SUPPORT_EMAIL", "") or ""
    except ImportError:
        contact = ""
    contact = contact if "@" in contact and "ваша@" not in contact else ""
    return f"PoleznoBot/1.0 ({contact})" if contact else "PoleznoBot/1.0"


def fetch_off(code: str):
    """(статус, данные). unavailable — сеть/сервис не ответили (не кэшируем), not_found — такого товара нет."""
    try:
        response = requests.get(OFF_URL.format(code=code), params={"fields": OFF_FIELDS}, timeout=TIMEOUT,
                                headers={"User-Agent": _user_agent()})
    except requests.RequestException as e:
        logger.warning(f"Open Food Facts недоступен: {e}")
        return STATUS_UNAVAILABLE, None
    if response.status_code == 404:
        return STATUS_NOT_FOUND, None
    if response.status_code != 200:
        logger.warning(f"Open Food Facts ответил {response.status_code}")
        return STATUS_UNAVAILABLE, None
    try:
        payload = response.json()
    except ValueError:
        return STATUS_UNAVAILABLE, None
    if payload.get("status") != 1 or not payload.get("product"):
        return STATUS_NOT_FOUND, None
    product, status = parse_product(code, payload["product"])
    return (status, product) if status == STATUS_OK else (status, {"name": (payload["product"].get("product_name") or "").strip()})


def lookup(code: str):
    """Ищет товар: сначала в кэше бота, затем в Open Food Facts. Возвращает (статус, данные)."""
    from database import get_barcode_cache, save_barcode_cache
    code = normalize(code)
    cached = get_barcode_cache(code)
    if cached:
        age = datetime.now() - datetime.fromisoformat(cached["fetched_at"])
        limit = timedelta(days=CACHE_FOUND_DAYS if cached["status"] == STATUS_OK else CACHE_MISSING_DAYS)
        if age < limit:
            return cached["status"], cached["data"]
    status, data = fetch_off(code)
    if status != STATUS_UNAVAILABLE:
        save_barcode_cache(code, status, data)
    return status, data
