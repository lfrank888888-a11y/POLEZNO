"""
Модуль оплаты: ЮKassa + CryptoCloud
"""

import uuid
import requests
from datetime import datetime

from config import (
    YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, YOOKASSA_ENABLED,
    CRYPTOCLOUD_API_KEY, CRYPTOCLOUD_SHOP_ID, CRYPTOCLOUD_ENABLED,
    SUBSCRIPTION_PRICE
)


# === ЮKASSA ===

def create_yookassa_payment(user_id: int, amount: int = None, tier_name: str = None) -> dict:
    """
    Создать платёж через ЮKassa.
    Возвращает: {"payment_id": str, "confirmation_url": str, "error": str or None}
    """
    if not YOOKASSA_ENABLED:
        return {"error": "ЮKassa не настроена. Укажите SHOP_ID и SECRET_KEY в config.py"}

    try:
        from yookassa import Configuration, Payment
        Configuration.account_id = YOOKASSA_SHOP_ID
        Configuration.secret_key = YOOKASSA_SECRET_KEY

        amount = amount or SUBSCRIPTION_PRICE
        idempotence_key = str(uuid.uuid4())

        payment = Payment.create({
            "amount": {
                "value": f"{amount}.00",
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": "https://t.me/"
            },
            "capture": True,
            "description": f"Подписка ПолезноБот{f', тариф «{tier_name}»' if tier_name else ''} (пользователь {user_id})",
            "metadata": {
                "user_id": str(user_id),
                "tier": tier_name or ""
            }
        }, idempotence_key)

        return {
            "payment_id": payment.id,
            "confirmation_url": payment.confirmation.confirmation_url,
            "error": None
        }
    except Exception as e:
        return {"error": f"Ошибка ЮKassa: {str(e)}"}


def check_yookassa_payment(payment_id: str) -> dict:
    """
    Проверить статус платежа ЮKassa.
    Возвращает: {"status": str, "paid": bool, "error": str or None}
    """
    if not YOOKASSA_ENABLED:
        return {"error": "ЮKassa не настроена"}

    try:
        from yookassa import Payment
        payment = Payment.find_one(payment_id)

        return {
            "status": payment.status,
            "paid": payment.paid,
            "error": None
        }
    except Exception as e:
        return {"error": f"Ошибка проверки: {str(e)}"}


# === CRYPTOCLOUD ===

CRYPTOCLOUD_API_URL = "https://api.cryptocloud.plus/v2"

def create_cryptocloud_payment(user_id: int, amount: int = None, tier_name: str = None) -> dict:
    """
    Создать счёт через CryptoCloud.
    Возвращает: {"invoice_id": str, "pay_url": str, "error": str or None}
    """
    if not CRYPTOCLOUD_ENABLED:
        return {"error": "CryptoCloud не настроен. Укажите API_KEY и SHOP_ID в config.py"}

    try:
        amount = amount or SUBSCRIPTION_PRICE
        order_id = f"poleznobot_{user_id}_{int(datetime.now().timestamp())}"

        headers = {
            "Authorization": f"Token {CRYPTOCLOUD_API_KEY}",
            "Content-Type": "application/json"
        }

        payload = {
            "amount": amount,
            "shop_id": CRYPTOCLOUD_SHOP_ID,
            "currency": "RUB",
            "order_id": order_id,
            "add_fields": {
                "telegram_user_id": str(user_id),
                "tier": tier_name or ""
            }
        }

        response = requests.post(
            f"{CRYPTOCLOUD_API_URL}/invoice/create",
            headers=headers,
            json=payload,
            timeout=30
        )

        data = response.json()

        if response.status_code == 200 and data.get("status") == "success":
            return {
                "invoice_id": data.get("invoice_id") or order_id,
                "pay_url": data.get("pay_url"),
                "error": None
            }
        else:
            return {"error": f"CryptoCloud ошибка: {data}"}

    except Exception as e:
        return {"error": f"Ошибка CryptoCloud: {str(e)}"}


def check_cryptocloud_payment(invoice_id: str) -> dict:
    """
    Проверить статус счёта CryptoCloud.
    Возвращает: {"status": str, "paid": bool, "error": str or None}
    """
    if not CRYPTOCLOUD_ENABLED:
        return {"error": "CryptoCloud не настроен"}

    try:
        headers = {
            "Authorization": f"Token {CRYPTOCLOUD_API_KEY}"
        }

        response = requests.get(
            f"{CRYPTOCLOUD_API_URL}/invoice/status",
            headers=headers,
            params={"invoice_id": invoice_id},
            timeout=30
        )

        data = response.json()

        if response.status_code == 200:
            status = data.get("status", "unknown")
            paid = status in ("paid", "success", "completed")
            return {
                "status": status,
                "paid": paid,
                "error": None
            }
        else:
            return {"error": f"CryptoCloud ошибка: {data}"}

    except Exception as e:
        return {"error": f"Ошибка проверки: {str(e)}"}


# === УНИВЕРСАЛЬНЫЙ ИНТЕРФЕЙС ===

def create_payment(user_id: int, provider: str, amount: int = None, tier_name: str = None) -> dict:
    """Создать платёж через указанного провайдера. tier_name попадает в описание платежа
    (в личном кабинете ЮKassa видно, за какой тариф заплатили)."""
    if provider == "yookassa":
        return create_yookassa_payment(user_id, amount, tier_name)
    elif provider == "cryptocloud":
        return create_cryptocloud_payment(user_id, amount, tier_name)
    else:
        return {"error": f"Неизвестный провайдер: {provider}"}


def check_payment(provider: str, payment_id: str) -> dict:
    """Проверить статус платежа"""
    if provider == "yookassa":
        return check_yookassa_payment(payment_id)
    elif provider == "cryptocloud":
        return check_cryptocloud_payment(payment_id)
    else:
        return {"error": f"Неизвестный провайдер: {provider}"}
