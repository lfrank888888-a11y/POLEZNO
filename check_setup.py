"""Проверка установки бота. Запуск из папки с ботом:  python check_setup.py

Показывает, чего не хватает, если бот молчит: недостающие файлы, ошибки импорта,
админские ID и работоспособность токена. Бота не запускает и сообщений не отправляет.
"""

import importlib
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Файлы, без которых бот не стартует
REQUIRED_FILES = [
    "main.py", "config.py", "database.py", "reminders.py", "payments.py", "charts.py",
    "recipes.py", "menu_generator.py", "menu_extra.py", "dietfilter.py", "shopping.py",
    "support.py", "tiers.py", "dailyplan.py", "norms.py", "portions.py", "adminstats.py", "timezones.py", "privacy.py", "diary.py", "foodbase.py", "foodparse.py", "nutrients.py", "dishnutrients.py", "menu_nutrition.py", "engagement.py", "alerts.py", "backup.py", "barcode_lookup.py", "offsearch.py", "foods_data.json", "food_analyzer.py",
]

problems = []


def ok(text):
    print(f"✅ {text}")


def bad(text):
    print(f"❌ {text}")
    problems.append(text)


print("— Файлы —")
missing = [f for f in REQUIRED_FILES if not os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), f))]
if missing:
    bad("Нет файлов рядом с main.py: " + ", ".join(missing))
else:
    ok("Все нужные файлы на месте")

print("\n— Импорт модулей —")
for module in [f[:-3] for f in REQUIRED_FILES if f != "main.py"] + ["main"]:
    if f"{module}.py" in missing:
        continue
    try:
        importlib.import_module(module)
        ok(f"{module}")
    except Exception as e:
        bad(f"{module}: {type(e).__name__}: {e}")
        print("   " + traceback.format_exc().strip().splitlines()[-3].strip())

print("\n— Администраторы —")
try:
    import config
    main = sys.modules.get("main")
    admins = getattr(main, "ADMIN_IDS", None) if main else None
    if admins is None:
        admins = {int(str(x).strip()) for x in config.ADMIN_IDS if str(x).strip().lstrip("-").isdigit()}
    if admins:
        ok(f"Администраторы: {sorted(admins)}")
        print("   Свой ID можно узнать командой /myid в боте или у @userinfobot.")
    else:
        bad("ADMIN_IDS пуст или содержит не числа — укажите свой числовой Telegram ID в config.py")
except Exception as e:
    bad(f"ADMIN_IDS: {e}")

print("\n— Токен бота —")
try:
    import config
    import telebot
    from telebot.apihelper import ApiTelegramException
    try:
        me = telebot.TeleBot(config.BOT_TOKEN).get_me()
        ok(f"Токен рабочий: @{me.username}")
    except ApiTelegramException as e:
        if e.error_code in (401, 404):
            bad("Telegram не принимает токен (недействителен). Если вы перевыпускали его в @BotFather, "
                "впишите новый в BOT_TOKEN.")
        else:
            bad(f"Telegram ответил ошибкой: {e}")
    except Exception as e:
        print(f"⚠️ Не удалось проверить токен (нет доступа к интернету?): {type(e).__name__}")
except Exception as e:
    bad(f"Токен: {e}")

print("\n" + ("Всё в порядке." if not problems else f"Найдено проблем: {len(problems)}. Исправьте их и запустите проверку снова."))
