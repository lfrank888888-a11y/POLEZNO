#!/usr/bin/env bash
# Установка автозапуска бота через systemd.
# Запуск: положите этот файл в папку с ботом (где main.py) и выполните:  sudo bash install_service.sh
set -euo pipefail

SERVICE=poleznobot
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ Запустите с sudo:  sudo bash install_service.sh"; exit 1
fi
if [ ! -f "$DIR/main.py" ]; then
  echo "❌ Не нашёл main.py в $DIR — положите этот файл в папку с ботом."; exit 1
fi

# Python: сначала виртуальное окружение рядом с ботом, иначе системный
if [ -x "$DIR/venv/bin/python" ]; then PY="$DIR/venv/bin/python"
elif [ -x "$DIR/.venv/bin/python" ]; then PY="$DIR/.venv/bin/python"
else PY="$(command -v python3)"; fi
[ -n "${PY:-}" ] || { echo "❌ Python не найден."; exit 1; }

# От чьего имени работает бот: владелец папки (не root, если возможно)
RUN_USER="$(stat -c '%U' "$DIR")"

echo "Папка бота:   $DIR"
echo "Python:       $PY"
echo "Пользователь: $RUN_USER"

# Проверяем, что бот вообще стартует (импорты и конфиг), не подключаясь к Telegram
if [ -f "$DIR/check_setup.py" ]; then
  echo "— Проверка настроек (check_setup.py) —"
  (cd "$DIR" && sudo -u "$RUN_USER" "$PY" check_setup.py) || echo "⚠️  check_setup.py нашёл замечания (см. выше). Установка продолжится."
fi

# Уже запущенные вручную копии бота нужно остановить, иначе будет конфликт (Telegram не даёт двум ботам читать одни обновления)
OLD="$(pgrep -f "python.*main\.py" || true)"
if [ -n "$OLD" ]; then
  echo "Найдены запущенные вручную процессы бота: $(echo $OLD | tr '\n' ' ')"
  read -r -p "Остановить их? [y/N] " ANSWER
  if [[ "$ANSWER" =~ ^[Yy]$ ]]; then kill $OLD || true; sleep 2; else echo "Без этого автозапуск не заработает. Прерываю."; exit 1; fi
fi

cat > "/etc/systemd/system/${SERVICE}.service" <<UNIT
[Unit]
Description=ПолезноБот (Telegram)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${DIR}
ExecStart=${PY} ${DIR}/main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
# Перезапуск без ограничений по частоте: бот сам поднимется после сбоя сети
StartLimitIntervalSec=0

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
sleep 3

echo
systemctl --no-pager --lines=15 status "$SERVICE" || true
echo
echo "✅ Готово. Бот запускается сам при включении сервера и перезапускается после сбоев."
echo "Полезные команды:"
echo "  sudo systemctl status $SERVICE      — состояние"
echo "  sudo systemctl restart $SERVICE     — перезапуск после обновления файлов"
echo "  sudo systemctl stop $SERVICE        — остановить"
echo "  sudo journalctl -u $SERVICE -f      — журнал в реальном времени (Ctrl+C — выйти)"
echo "  sudo journalctl -u $SERVICE -n 50   — последние 50 строк"
