"""
Анализ фото еды через AI
Поддерживает: Google Gemini (прямой) и OpenRouter (GPT-4o-mini и др.)
"""

import logging
import io
import re
import os
from PIL import Image
from config import GEMINI_API_KEY, GEMINI_ENABLED

logger = logging.getLogger(__name__)

# === OpenRouter настройки ===
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
OPENROUTER_ENABLED = bool(OPENROUTER_API_KEY)

# Функция анализа фото доступна, если настроен ХОТЯ БЫ ОДИН из провайдеров.
# main.py должен проверять именно этот флаг, а не GEMINI_ENABLED напрямую —
# иначе бот будет считать фичу выключенной, даже если работает OpenRouter.
FOOD_ANALYSIS_ENABLED = OPENROUTER_ENABLED or GEMINI_ENABLED


def _extract_calories(text: str) -> int:
    """Вытащить число ккал из текста AI."""
    match = re.search(r'(\d{2,4})\s*(ккал|калорий|cal|kcal)', text.lower())
    if match:
        return int(match.group(1))
    # Ищем просто число перед "ккал"
    match = re.search(r'(\d{2,4})\s*ккал', text.lower())
    if match:
        return int(match.group(1))
    return 0


def _analyze_with_gemini(photo_bytes: bytes) -> dict:
    """Анализ через Google Gemini (прямой API)."""
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.0-flash")

        image = Image.open(io.BytesIO(photo_bytes))

        prompt = (
            """Ты — нутрициолог. Проанализируй фото еды и ответь строго в формате:
1. Что на фото (кратко, 1 предложение)
2. Примерная калорийность порции (число)
3. Состав (белки/жиры/углеводы примерно)
4. Короткая рекомендация (1 предложение)

Ответь на русском языке."""
        )

        response = model.generate_content([prompt, image])
        text = response.text.strip()
        calories = _extract_calories(text)

        return {"text": text, "calories": calories, "error": None}
    except Exception as e:
        logger.error(f"Ошибка Gemini: {e}")
        return {"text": None, "calories": None, "error": f"Ошибка Gemini: {str(e)}"}


def _analyze_with_openrouter(photo_bytes: bytes) -> dict:
    """Анализ через OpenRouter (GPT-4o-mini и др.)."""
    import base64
    import requests

    try:
        # Конвертируем фото в base64
        image = Image.open(io.BytesIO(photo_bytes))
        # Уменьшаем, если слишком большое (OpenRouter лимит ~5MB)
        max_size = (1024, 1024)
        image.thumbnail(max_size, Image.LANCZOS)

        buf = io.BytesIO()
        image_format = image.format or "JPEG"
        if image_format.upper() not in ("JPEG", "PNG", "WEBP", "GIF"):
            image_format = "JPEG"
        image.save(buf, format=image_format)
        img_base64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        mime = f"image/{image_format.lower()}"

        prompt = (
            """Ты — нутрициолог. Проанализируй фото еды и ответь строго в формате:
1. Что на фото (кратко, 1 предложение)
2. Примерная калорийность порции (число)
3. Состав (белки/жиры/углеводы примерно)
4. Короткая рекомендация (1 предложение)

Ответь на русском языке."""
        )

        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://t.me/poleznobot",  # требуется OpenRouter
            "X-Title": "ПолезноБот",
        }

        payload = {
            "model": OPENROUTER_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_base64}"}}
                    ]
                }
            ],
            "max_tokens": 500,
        }

        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=60
        )

        data = response.json()

        if response.status_code != 200:
            error_msg = data.get("error", {}).get("message", str(data))
            return {"text": None, "calories": None, "error": f"OpenRouter ошибка: {error_msg}"}

        text = data["choices"][0]["message"]["content"].strip()
        calories = _extract_calories(text)

        return {"text": text, "calories": calories, "error": None}

    except Exception as e:
        logger.error(f"Ошибка OpenRouter: {e}")
        return {"text": None, "calories": None, "error": f"Ошибка OpenRouter: {str(e)}"}


def analyze_food_photo(photo_bytes: bytes) -> dict:
    """
    Универсальный анализ фото еды.
    Приоритет: OpenRouter → Gemini → Заглушка
    """
    # 1. Пробуем OpenRouter (работает из любой страны)
    if OPENROUTER_ENABLED:
        return _analyze_with_openrouter(photo_bytes)

    # 2. Пробуем Gemini (прямой, если доступен)
    if GEMINI_ENABLED:
        return _analyze_with_gemini(photo_bytes)

    # 3. Ничего не настроено
    return {
        "text": (
            """🔒 *Фото-анализ отключён*

Чтобы включить анализ еды по фото, добавьте API-ключ в файл `.env`:

**Вариант 1 — OpenRouter (рекомендуется):**
1. Зарегистрируйтесь на openrouter.ai
2. Пополните баланс (от $5)
3. Создайте ключ и добавьте в .env:
`OPENROUTER_API_KEY=your_key_here`

**Вариант 2 — Google Gemini:**
`GEMINI_API_KEY=your_key_here`

GPT-4o-mini через OpenRouter стоит ~$0.005 за фото."""
        ),
        "calories": 0,
        "error": None
    }
