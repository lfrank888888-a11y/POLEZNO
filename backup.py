"""Ежесуточная резервная копия базы бота.

Копия делается штатным механизмом SQLite (безопасно, пока бот работает), проверяется на целостность, сжимается
и кладётся в папку backups рядом с базой. Хранится последние BACKUP_KEEP копий (по умолчанию 14).

Настройки в config.py (все необязательные):
    BACKUP_DIR = "/путь/к/папке"     # куда складывать; по умолчанию data/backups рядом с базой
    BACKUP_KEEP = 14                  # сколько последних копий хранить
    BACKUP_SEND_TO_ADMIN = False      # True — раз в неделю (воскресенье) присылать копию админу в Telegram

Важно: копия на том же сервере не спасёт, если сервер пропадёт целиком. Периодически забирайте файлы из папки
backups к себе (scp/rsync) или включите BACKUP_SEND_TO_ADMIN. Проверить копию: gunzip файл.gz, затем check_backup.py.
"""

import glob
import gzip
import logging
import os
import shutil
import sqlite3
from datetime import datetime

import pytz

import config

logger = logging.getLogger(__name__)

PREFIX = "users-"


def backup_dir() -> str:
    default = os.path.join(os.path.dirname(os.path.abspath(config.DB_PATH)), "backups")
    path = getattr(config, "BACKUP_DIR", None) or default
    os.makedirs(path, exist_ok=True)
    return path


def list_backups() -> list:
    return sorted(glob.glob(os.path.join(backup_dir(), PREFIX + "*.db.gz")))


def make_backup() -> str:
    """Создаёт копию и возвращает путь к .db.gz. При любой проблеме (в том числе битая копия) бросает исключение."""
    if not os.path.exists(config.DB_PATH):
        raise FileNotFoundError(f"база не найдена: {config.DB_PATH}")
    stamp = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%Y-%m-%d_%H%M%S")
    raw = os.path.join(backup_dir(), f"{PREFIX}{stamp}.db")
    target = raw + ".gz"
    src = sqlite3.connect(config.DB_PATH)
    try:
        dst = sqlite3.connect(raw)
        try:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"копия не прошла проверку целостности: {result[:80]}")
            if dst.execute("SELECT COUNT(*) FROM users").fetchone()[0] != src.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                # пользователи могли прийти за доли секунды копирования — тогда это не ошибка, но проверка полезна
                logger.info("Число пользователей в копии и в базе отличается (база менялась во время копирования)")
        finally:
            dst.close()
    except Exception:
        if os.path.exists(raw):
            os.remove(raw)  # не оставляем недоделанную копию
        raise
    finally:
        src.close()
    try:
        with open(raw, "rb") as f_in, gzip.open(target, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)
    finally:
        os.remove(raw)
    os.chmod(target, 0o600)  # в базе личные данные: читать копию может только владелец
    _rotate()
    return target


def _rotate():
    keep = max(1, int(getattr(config, "BACKUP_KEEP", 14)))
    for old in list_backups()[:-keep]:
        try:
            os.remove(old)
        except OSError as e:
            logger.warning(f"Не удалось удалить старую копию {old}: {e}")


def run_scheduled(bot):
    """Задача планировщика: копия + оповещение админа при сбое; по воскресеньям — отправка копии, если включено."""
    from alerts import notify_admins, admin_ids
    try:
        path = make_backup()
        logger.info(f"💾 Резервная копия создана: {path} ({os.path.getsize(path) // 1024} КБ)")
    except Exception as e:
        logger.error(f"Ошибка резервного копирования: {e}")
        notify_admins(bot, f"⚠️ Не удалось сделать резервную копию базы: {e}")
        return
    if getattr(config, "BACKUP_SEND_TO_ADMIN", False) and datetime.now(pytz.timezone("Europe/Moscow")).weekday() == 6:
        for admin in admin_ids():
            try:
                with open(path, "rb") as f:
                    bot.send_document(admin, f, caption="💾 Еженедельная копия базы (внутри личные данные пользователей — храните аккуратно)")
            except Exception as e:
                logger.error(f"Не удалось отправить копию админу {admin}: {e}")
