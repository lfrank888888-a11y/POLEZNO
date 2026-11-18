"""Администраторы и уведомления им: сводки и оповещения об ошибках бота.

Все записи уровня ERROR из логов (в том числе ошибки обработчиков и фоновых задач) приходят админам в Telegram.
Чтобы не завалить сообщениями: одинаковая ошибка не чаще раза в 30 минут (с числом повторов), не больше 20 оповещений
в час, а «пользователь заблокировал бота» ошибкой не считается.
"""

import logging
import queue
import re
import threading
import time
import traceback

logger = logging.getLogger(__name__)


def admin_ids() -> list:
    """ADMIN_IDS из config.py -> список чисел. Терпит ID, записанные строкой, и пробелы."""
    try:
        from config import ADMIN_IDS
    except ImportError:
        return []
    if isinstance(ADMIN_IDS, (str, int)):
        ADMIN_IDS = [ADMIN_IDS]
    result = []
    for item in ADMIN_IDS or []:
        try:
            result.append(int(str(item).strip()))
        except ValueError:
            continue
    return result


def notify_admins(bot, text: str, parse_mode=""):
    # parse_mode="" отключает Markdown, чтобы служебный текст с _ и * не ломал отправку
    """Отправляет сообщение всем админам; ошибки доставки не пробрасываются."""
    for admin in admin_ids():
        try:
            bot.send_message(admin, text, parse_mode=parse_mode)
        except Exception as e:
            logger.warning(f"Не удалось отправить сообщение админу {admin}: {e}")


# --- оповещения об ошибках ---

DEDUPE_SECONDS = 30 * 60
MAX_PER_HOUR = 20
_IGNORED_PHRASES = (
    "bot was blocked by the user", "user is deactivated", "chat not found", "bot can't initiate conversation",
    "have no rights to send", "message is not modified",
)


class AdminAlertHandler(logging.Handler):
    """Складывает ERROR-записи в очередь; отправляет отдельный поток, чтобы не тормозить бота и не зациклиться."""

    def __init__(self, bot):
        super().__init__(level=logging.ERROR)
        self.bot = bot
        self.queue = queue.Queue()
        self._last_sent = {}      # ключ ошибки -> время последнего оповещения
        self._suppressed = {}     # ключ ошибки -> сколько раз промолчали
        self._sent_times = []
        self._lock = threading.Lock()
        threading.Thread(target=self._worker, name="admin-alerts", daemon=True).start()

    @staticmethod
    def _key(record, message):
        return record.name + ":" + re.sub(r"\d+", "N", message)[:120]

    def emit(self, record):
        try:
            if record.name.startswith("alerts") or record.name.startswith("urllib3"):
                return
            message = record.getMessage()
            if any(p in message.lower() for p in _IGNORED_PHRASES):
                return
            detail = ""
            if record.exc_info and record.exc_info[0]:
                detail = "".join(traceback.format_exception(*record.exc_info)).strip().splitlines()
                detail = "\n".join(detail[-4:])
            with self._lock:
                key = self._key(record, message)
                now = time.time()
                if now - self._last_sent.get(key, 0) < DEDUPE_SECONDS:
                    self._suppressed[key] = self._suppressed.get(key, 0) + 1
                    return
                self._sent_times = [t for t in self._sent_times if now - t < 3600]
                if len(self._sent_times) >= MAX_PER_HOUR:
                    return
                self._sent_times.append(now)
                self._last_sent[key] = now
                repeats = self._suppressed.pop(key, 0)
            text = f"🚨 Ошибка бота\n{record.name}: {message[:500]}"
            if detail:
                text += f"\n\n{detail[:800]}"
            if repeats:
                text += f"\n\n(такая же ошибка повторялась ещё {repeats} раз)"
            self.queue.put(text)
        except Exception:
            pass  # оповещение не должно само ломать бота

    def _worker(self):
        while True:
            text = self.queue.get()
            try:
                notify_admins(self.bot, text)
            except Exception as e:
                logging.getLogger("alerts").warning(f"Оповещение не отправлено: {e}")
            time.sleep(0.3)


NETWORK_BURST = 5          # столько сетевых сбоев...
NETWORK_WINDOW = 10 * 60   # ...за столько секунд — уже повод написать админу


def is_network_error(exception) -> bool:
    """Обрыв связи или таймаут при обращении к Telegram: не ошибка кода, обычно проходит сам."""
    try:
        import requests
        if isinstance(exception, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return True
    except ImportError:
        pass
    text = repr(exception).lower()
    return any(p in text for p in ("connection aborted", "connection reset", "read timed out", "max retries exceeded", "remotedisconnected"))


class AlertExceptionHandler:
    """Обработчик исключений telebot: пишет их в лог (а значит, в оповещения) и не даёт опросу остановиться.
    Одиночные сетевые сбои только пишутся в лог; админу сообщаем, если их набралось NETWORK_BURST за NETWORK_WINDOW секунд."""

    def __init__(self):
        self._network_times = []

    def handle(self, exception):
        log = logging.getLogger("bot.handler")
        if is_network_error(exception):
            now = time.time()
            self._network_times = [t for t in self._network_times if now - t < NETWORK_WINDOW]
            self._network_times.append(now)
            if len(self._network_times) == NETWORK_BURST:
                log.error(f"Связь с Telegram нестабильна: {NETWORK_BURST} сетевых сбоев за {NETWORK_WINDOW // 60} мин. Последний: {exception!r}")
            else:
                log.warning(f"Сетевой сбой при обращении к Telegram (пропускаем): {exception!r}")
            return True
        log.error(f"Необработанная ошибка в обработчике: {exception!r}", exc_info=exception)
        return True


def install(bot):
    """Подключает оповещения. Вызывать один раз при старте, до запуска планировщика."""
    if not admin_ids():
        logger.warning("ADMIN_IDS пуст — оповещения об ошибках некому отправлять")
        return None
    handler = AdminAlertHandler(bot)
    logging.getLogger().addHandler(handler)
    bot.exception_handler = AlertExceptionHandler()
    return handler


def notify_started(bot, state_file: str):
    """«Бот запущен»: так видно перезапуски и циклы падений. Не чаще раза в 2 минуты."""
    try:
        last = float(open(state_file).read().strip())
    except (OSError, ValueError):
        last = 0
    if time.time() - last < 120:
        return
    try:
        with open(state_file, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass
    notify_admins(bot, "🟢 Бот запущен")
