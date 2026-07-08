import configparser
import os
import sqlite3
import gc
import sys
import signal
import glob
try:
    import resource
except ImportError:
    resource = None
from pydantic import BaseModel
import asyncio
from typing import Union, Any
import logging
from aiogram import Router, Bot, Dispatcher, F, types
from aiogram.filters import Command
from html import escape
from datetime import datetime, timezone, timedelta
import pytz
from contextlib import contextmanager
from aiohttp import web
from cryptography.fernet import Fernet
import random

# Импортируем Pyrogram для Пикми-режима
from pyrogram import Client, filters as py_filters

try:
    import psycopg2
    from psycopg2.pool import ThreadedConnectionPool
    from psycopg2.extras import RealDictCursor
    psycopg2_available = True
except ImportError:
    psycopg2_available = False

config = configparser.ConfigParser()
config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
config.read(config_path)

# Encryption Key setup
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    ENCRYPTION_KEY = config.get("settings", "ENCRYPTION_KEY", fallback="")

if not ENCRYPTION_KEY:
    raise ValueError("ENCRYPTION_KEY is not configured! Please generate a key with generate_key.py and set it in config.ini or environment variables.")

try:
    Fernet(ENCRYPTION_KEY.encode())
except Exception as e:
    raise ValueError(f"Invalid ENCRYPTION_KEY format: {e}. The key must be a valid 32-byte url-safe base64-encoded key.")

cipher_suite = Fernet(ENCRYPTION_KEY.encode())


def encrypt_text(text: str) -> str:
    if not text:
        return text
    return cipher_suite.encrypt(text.encode()).decode()


def decrypt_text(encrypted_text: str) -> str:
    if not encrypted_text:
        return encrypted_text
    try:
        return cipher_suite.decrypt(encrypted_text.encode()).decode()
    except Exception as e:
        logging.error(f"Failed to decrypt message: {e}")
        return "[Зашифрованное сообщение - Ошибка дешифрования]"

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    TOKEN = config.get("settings", "TOKEN", fallback="YOUR_BOT_TOKEN_HERE")

user_id_env = os.environ.get("USER_ID")
if user_id_env:
    try:
        USER_ID = int(user_id_env)
    except ValueError as e:
        raise ValueError(f"USER_ID environment variable must be a valid integer, got '{user_id_env}'") from e
else:
    try:
        USER_ID = config.getint("settings", "USER_ID")
    except (configparser.NoSectionError, configparser.NoOptionError, ValueError) as e:
        raise ValueError("USER_ID is not configured! Please set USER_ID in config.ini or environment variables.") from e

if USER_ID <= 0:
    raise ValueError(f"USER_ID must be a positive integer, got {USER_ID}")

# === НАСТРОЙКИ ДЛЯ ПИКМИ-ЮЗЕРБОТА ===
API_ID = os.environ.get("API_ID")
if not API_ID:
    API_ID = config.get("settings", "API_ID", fallback="")
if API_ID:
    API_ID = int(API_ID)

API_HASH = os.environ.get("API_HASH")
if not API_HASH:
    API_HASH = config.get("settings", "API_HASH", fallback="")

# Глобальный флаг режима
PICKME_MODE = os.environ.get("PICKME_MODE", "1") == "1"

# Шаблоны фраз для пикми-трансформации
PICKME_PREFIXES = [
    "Ой, извини, что вообще пишу... 🥺👉👈 ",
    "Я знаю, что я ужасный и неидеальный человек, но: ",
    "Наверное, я тебя дико раздражаю и надоедаю своим присутствием, но ",
    "Пока все нормальные люди заняты важными делами, я как дура пишу: ",
    "Я не такая как все эти инста-модели, я простая, поэтому скажу прямо: "
]
PICKME_POSTFIXES = [
    " ...только не злись на меня, ладно? 😭",
    " (прости, я просто очень ранимая девочка) 💔",
    " ...хотя кому вообще интересно мое мнение.",
    " (наверное, ты опять проигнорируешь, ну и ладно) 🚶‍♀️",
    " 🥺"
]

TIMEZONE_NAME = os.environ.get("TIMEZONE_NAME")
if not TIMEZONE_NAME:
    TIMEZONE_NAME = config.get("settings", "TIMEZONE_NAME", fallback="Europe/Moscow")
timezone_local = pytz.timezone(TIMEZONE_NAME)

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    DATABASE_URL = config.get("settings", "DATABASE_URL", fallback="")
PLACEHOLDER = "%s" if DATABASE_URL else "?"

db_pool = None

DELETED_MESSAGE_FORMAT = (
    "🗑 <b>Удалено {media_name} от {user_fullname_escaped}</b> (ID: <code>{user_id}</code>)\n"
    "⏰ <b>Время отправки:</b> {timestamp}\n\n"
    "💬 <b>Содержание:</b>\n"
    "<blockquote>{old_text}</blockquote>"
)

DELETED_MESSAGE_NO_CONTENT_FORMAT = (
    "🗑 <b>Удалено {media_name} от {user_fullname_escaped}</b> (ID: <code>{user_id}</code>)\n"
    "⏰ <b>Время отправки:</b> {timestamp}"
)

EDITED_MESSAGE_FORMAT = (
    "📝 <b>Изменено {media_name} от {user_fullname_escaped}</b> (ID: <code>{user_id}</code>)\n"
    "⏰ <b>Время отправки:</b> {timestamp}\n\n"
    "<b>Было:</b>\n"
    "<blockquote>{old_text}</blockquote>\n\n"
    "<b>Стало:</b>\n"
    "<blockquote>{new_text}</blockquote>"
)

router = Router(name=__name__)
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, force=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def get_db_cursor(conn):
    if DATABASE_URL:
        return conn.cursor(cursor_factory=RealDictCursor)
    else:
        conn.row_factory = sqlite3.Row
        return conn.cursor()


@contextmanager
def db_session():
    if DATABASE_URL:
        if not psycopg2_available:
            raise RuntimeError("psycopg2 is not installed but DATABASE_URL is set.")
        if not db_pool:
            raise RuntimeError("Database connection pool is not initialized.")
        conn = db_pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            db_pool.putconn(conn)
    else:
        db_path = "messages.db"
        if os.path.basename(BASE_DIR) == "test":
            db_path = os.path.join(os.path.dirname(BASE_DIR), "messages.db")
        conn = sqlite3.connect(db_path)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


ALLOWED_COLUMNS = {
    "user_id", "message_id", "message_text", "timestamp", "media_type", "file_id",
    "connection_id", "key", "value", "notify_updates", "notify_startup", "delete_reply"
}


def update_format(sql, parameters: dict) -> tuple[str, list]:
    for key in parameters:
        if key not in ALLOWED_COLUMNS:
            raise ValueError(f"Unsanitized database column name detected: {key}")
    values = ", ".join([f"{item} = {PLACEHOLDER}" for item in parameters])
    sql += f" {values}"
    return sql, list(parameters.values())


class MessageRecord(BaseModel):
    user_id: int
    message_id: int
    message_text: Union[str, None] = None
    timestamp: str
    media_type: str = "text"
    file_id: Union[str, None] = None


class MessageStore:
    storage_name = "messages"

    @staticmethod
    async def create_db():
        def _sync():
            with db_session() as conn:
                cursor = conn.cursor()
                if DATABASE_URL:
                    cursor.execute('''CREATE TABLE IF NOT EXISTS messages
                                      (id SERIAL PRIMARY KEY,
                                       user_id BIGINT,
                                       message_id BIGINT,
                                       message_text TEXT,
                                       timestamp TEXT,
                                       media_type TEXT DEFAULT 'text',
                                       file_id TEXT)''')
                else:
                    cursor.execute('''CREATE TABLE IF NOT EXISTS messages
                                      (id INTEGER PRIMARY KEY,
                                       user_id INTEGER,
                                       message_id INTEGER,
                                       message_text TEXT,
                                       timestamp TEXT,
                                       media_type TEXT DEFAULT 'text',
                                       file_id TEXT)''')
                    try:
                        cursor.execute("ALTER TABLE messages ADD COLUMN media_type TEXT DEFAULT 'text'")
                    except Exception:
                        pass
                    try:
                        cursor.execute("ALTER TABLE messages ADD COLUMN file_id TEXT")
                    except Exception:
                        pass
        await asyncio.to_thread(_sync)

    @staticmethod
    async def add(user_id: int, message_id: int, message_text: Union[str, None], timestamp: str, media_type: str = "text", file_id: Union[str, None] = None):
        encrypted_text = encrypt_text(message_text) if message_text else None
        def _sync():
            with db_session() as con:
                cursor = con.cursor()
                cursor.execute(
                    f"INSERT INTO messages (user_id, message_id, message_text, timestamp, media_type, file_id) VALUES ({PLACEHOLDER}, {PLACEHOLDER}, {PLACEHOLDER}, {PLACEHOLDER}, {PLACEHOLDER}, {PLACEHOLDER})",
                    [user_id, message_id, encrypted_text, timestamp, media_type, file_id],
                )
        await asyncio.to_thread(_sync)

    @staticmethod
    async def get(user_id: int, message_id: int) -> Union[MessageRecord, None]:
        def _sync():
            with db_session() as con:
                cursor = get_db_cursor(con)
                sql = f"SELECT * FROM messages WHERE user_id = {PLACEHOLDER} AND message_id = {PLACEHOLDER}"
                cursor.execute(sql, [user_id, message_id])
                return cursor.fetchone()
        response = await asyncio.to_thread(_sync)
        if response is not None:
            response = dict(response)
            if response.get("message_text"):
                response["message_text"] = decrypt_text(response["message_text"])
            response = MessageRecord(**response)
        return response

    @staticmethod
    async def update(user_id: int, message_id: int, **kwargs):
        if "message_text" in kwargs and kwargs["message_text"]:
            kwargs["message_text"] = encrypt_text(kwargs["message_text"])
        def _sync():
            with db_session() as con:
                cursor = con.cursor()
                sql = f"UPDATE messages SET"
                sql, parameters = update_format(sql, kwargs)
                parameters.extend([user_id, message_id])
                cursor.execute(sql + f" WHERE user_id = {PLACEHOLDER} AND message_id = {PLACEHOLDER}", parameters)
        await asyncio.to_thread(_sync)

    @staticmethod
    async def delete(user_id: int, message_id: int):
        def _sync():
            with db_session() as con:
                cursor = con.cursor()
                sql = f"DELETE FROM messages WHERE user_id = {PLACEHOLDER} AND message_id = {PLACEHOLDER}"
                cursor.execute(sql, [user_id, message_id])
        await asyncio.to_thread(_sync)

    @staticmethod
    async def delete_old_messages(cutoff_timestamp: str):
        def _sync():
            with db_session() as con:
                cursor = get_db_cursor(con)
                cursor.execute(
                    f"SELECT file_id FROM messages WHERE timestamp < {PLACEHOLDER} AND file_id IS NOT NULL",
                    [cutoff_timestamp]
                )
                rows = cursor.fetchall()
                file_ids = [row['file_id'] for row in rows]
                
                cursor = con.cursor()
                sql = f"DELETE FROM messages WHERE timestamp < {PLACEHOLDER}"
                cursor.execute(sql, [cutoff_timestamp])
                return file_ids
                
        file_ids = await asyncio.to_thread(_sync)
        if file_ids:
            downloads_dir = os.path.join(BASE_DIR, "downloads")
            for fid in file_ids:
                files = glob.glob(os.path.join(downloads_dir, f"{fid}.*"))
                for f in files:
                    try:
                        os.remove(f)
                        logger.info(f"Cleanup: deleted local file {f}")
                    except Exception as e:
                        logger.warning(f"Cleanup: failed to delete local file {f}: {e}")


class ConnectionsDB:
    storage_name = "connections"

    @staticmethod
    async def create_table():
        def _sync():
            with db_session() as conn:
                cursor = conn.cursor()
                if DATABASE_URL:
                    cursor.execute('''CREATE TABLE IF NOT EXISTS connections
                                      (connection_id TEXT PRIMARY KEY, user_id BIGINT)''')
                else:
                    cursor.execute('''CREATE TABLE IF NOT EXISTS connections
                                      (connection_id TEXT PRIMARY KEY, user_id INTEGER)''')
        await asyncio.to_thread(_sync)

    @staticmethod
    async def add_or_update(connection_id: str, user_id: int):
        def _sync():
            with db_session() as conn:
                cursor = conn.cursor()
                if DATABASE_URL:
                    cursor.execute(
                        f"INSERT INTO connections (connection_id, user_id) VALUES (%s, %s) ON CONFLICT (connection_id) DO UPDATE SET user_id = EXCLUDED.user_id",
                        [connection_id, user_id]
                    )
                else:
                    cursor.execute(
                        f"INSERT OR REPLACE INTO connections (connection_id, user_id) VALUES (?, ?)",
                        [connection_id, user_id]
                    )
        await asyncio.to_thread(_sync)

    @staticmethod
    async def get_user_id(connection_id: str) -> Union[int, None]:
        def _sync():
            with db_session() as conn:
                cursor = get_db_cursor(conn)
                cursor.execute(
                    f"SELECT user_id FROM connections WHERE connection_id = {PLACEHOLDER}",
                    [connection_id]
                )
                row = cursor.fetchone()
                return row['user_id'] if row else None
        return await asyncio.to_thread(_sync)

    @staticmethod
    async def delete(connection_id: str):
        def _sync():
            with db_session() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"DELETE FROM connections WHERE connection_id = {PLACEHOLDER}",
                    [connection_id]
                )
        await asyncio.to_thread(_sync)

    @staticmethod
    async def get_all_user_ids() -> list[int]:
        def _sync():
            with db_session() as conn:
                cursor = get_db_cursor(conn)
                cursor.execute("SELECT DISTINCT user_id FROM connections")
                rows = cursor.fetchall()
                return [row['user_id'] for row in rows]
        return await asyncio.to_thread(_sync)


class SystemStateDB:
    storage_name = "system_state"

    @staticmethod
    async def create_table():
        def _sync():
            with db_session() as conn:
                cursor = conn.cursor()
                cursor.execute('''CREATE TABLE IF NOT EXISTS system_state
                                  (key TEXT PRIMARY KEY, value TEXT)''')
        await asyncio.to_thread(_sync)

    @staticmethod
    async def get_value(key: str) -> Union[str, None]:
        def _sync():
            with db_session() as conn:
                cursor = get_db_cursor(conn)
                cursor.execute(
                    f"SELECT value FROM system_state WHERE key = {PLACEHOLDER}",
                    [key]
                )
                row = cursor.fetchone()
                return row['value'] if row else None
        return await asyncio.to_thread(_sync)

    @staticmethod
    async def set_value(key: str, value: str):
        def _sync():
            with db_session() as conn:
                cursor = conn.cursor()
                if DATABASE_URL:
                    cursor.execute(
                        f"INSERT INTO system_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                        [key, value]
                    )
                else:
                    cursor.execute(
                        f"INSERT OR REPLACE INTO system_state (key, value) VALUES (?, ?)",
                        [key, value]
                    )
        await asyncio.to_thread(_sync)


class UserSettingsDB:
    storage_name = "user_settings"

    @staticmethod
    async def create_table():
        def _sync():
            with db_session() as conn:
                cursor = conn.cursor()
                if DATABASE_URL:
                    cursor.execute('''CREATE TABLE IF NOT EXISTS user_settings
                                      (user_id BIGINT PRIMARY KEY,
                                       notify_updates INTEGER DEFAULT 1,
                                       notify_startup INTEGER DEFAULT 1,
                                       delete_reply INTEGER DEFAULT 1)''')
                else:
                    cursor.execute('''CREATE TABLE IF NOT EXISTS user_settings
                                      (user_id INTEGER PRIMARY KEY,
                                       notify_updates INTEGER DEFAULT 1,
                                       notify_startup INTEGER DEFAULT 1,
                                       delete_reply INTEGER DEFAULT 1)''')
                try:
                    cursor.execute("ALTER TABLE user_settings ADD COLUMN delete_reply INTEGER DEFAULT 1")
                except Exception:
                    pass
        await asyncio.to_thread(_sync)

    @staticmethod
    async def get_settings(user_id: int) -> dict:
        def _sync():
            with db_session() as conn:
                cursor = get_db_cursor(conn)
                cursor.execute(f"SELECT * FROM user_settings WHERE user_id = {PLACEHOLDER}", [user_id])
                row = cursor.fetchone()
                if row:
                    res = dict(row)
                    if "delete_reply" not in res:
                        res["delete_reply"] = 1
                    return res
                else:
                    cursor = conn.cursor()
                    if DATABASE_URL:
                        cursor.execute(
                            "INSERT INTO user_settings (user_id, notify_updates, notify_startup, delete_reply) "
                            "VALUES (%s, 1, 1, 1) ON CONFLICT (user_id) DO NOTHING",
                            [user_id]
                        )
                    else:
                        cursor.execute(
                            "INSERT OR IGNORE INTO user_settings (user_id, notify_updates, notify_startup, delete_reply) "
                            "VALUES (?, 1, 1, 1)",
                            [user_id]
                        )
                    return {"user_id": user_id, "notify_updates": 1, "notify_startup": 1, "delete_reply": 1}
        return await asyncio.to_thread(_sync)

    @staticmethod
    async def set_setting(user_id: int, key: str, value: int):
        if key not in ALLOWED_COLUMNS:
            raise ValueError(f"Unsanitized database column: {key}")
        def _sync():
            with db_session() as conn:
                cursor = conn.cursor()
                if DATABASE_URL:
                    cursor.execute(
                        f"INSERT INTO user_settings (user_id, {key}) VALUES (%s, %s) "
                        f"ON CONFLICT (user_id) DO UPDATE SET {key} = EXCLUDED.{key}",
                        [user_id, value]
                    )
                else:
                    cursor.execute(
                        f"INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)",
                        [user_id]
                    )
                    cursor.execute(
                        f"UPDATE user_settings SET {key} = ? WHERE user_id = ?",
                        [value, user_id]
                    )
        await asyncio.to_thread(_sync)

    @staticmethod
    async def get_all_user_ids() -> list[int]:
        def _sync():
            with db_session() as conn:
                cursor = get_db_cursor(conn)
                cursor.execute("SELECT user_id FROM user_settings")
                rows = cursor.fetchall()
                return [row['user_id'] for row in rows]
        return await asyncio.to_thread(_sync)


async def cleanup_old_messages():
    while True:
        now_local = datetime.now(timezone_local)
        next_run = now_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        sleep_seconds = (next_run - now_local).total_seconds()
        await asyncio.sleep(sleep_seconds)
        cutoff_datetime = datetime.now(timezone.utc) - timedelta(days=30)
        cutoff_timestamp_iso = cutoff_datetime.isoformat()
        await MessageStore.delete_old_messages(cutoff_timestamp_iso)


BOT_VERSION = "1.7.0"


async def monitor_memory():
    while True:
        await asyncio.sleep(300)
        if not resource:
            continue
        try:
            usage_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if sys.platform == "darwin":
                usage_mb = usage_kb / 1024.0 / 1024.0
            else:
                usage_mb = usage_kb / 1024.0
            
            logger.info(f"Current memory usage: {usage_mb:.2f} MB")
            if usage_mb > 150:
                collected = gc.collect()
                logger.info(f"Memory cleanup: current usage is {usage_mb:.2f} MB. gc.collect() cleared {collected} objects.")
            if usage_mb > 400:
                logger.critical(f"Memory usage critical ({usage_mb:.2f} MB). Triggering graceful shutdown.")
                os.kill(os.getpid(), signal.SIGTERM)
        except Exception as e:
            logger.error(f"Error during memory monitoring: {e}")


async def update_last_active():
    while True:
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            await SystemStateDB.set_value("last_active_timestamp", now_iso)
        except Exception as e:
            logger.warning(f"Failed to update last active timestamp: {e}")
        await asyncio.sleep(60)


async def check_and_broadcast_changelog(bot: Bot):
    last_active_str = await SystemStateDB.get_value("last_active_timestamp")
    is_restart = False
    if last_active_str:
        try:
            last_active_dt = datetime.fromisoformat(last_active_str)
            now_utc = datetime.now(timezone.utc)
            if now_utc - last_active_dt < timedelta(minutes=15):
                is_restart = True
        except Exception:
            pass

    admin_settings = await UserSettingsDB.get_settings(USER_ID)
    if admin_settings.get("notify_startup", 1) == 1:
        try:
            status_text = "перезапущен" if is_restart else "запущен"
            await bot.send_message(USER_ID, f"🤖 Бот успешно {status_text}!")
        except Exception as e:
            logger.warning(f"Failed to send startup notification to admin: {e}")

    if not is_restart:
        logger.info("Cold start detected. Notifying users about bot recovery...")
        user_ids = await UserSettingsDB.get_all_user_ids()
        if user_ids:
            for uid in user_ids:
                if uid == USER_ID:
                    continue
                user_settings = await UserSettingsDB.get_settings(uid)
                if user_settings.get("notify_startup", 1) == 1:
                    try:
                        await bot.send_message(uid, "🤖 Бот снова в сети и готов к работе после технического перерыва!")
                        await asyncio.sleep(0.05)
                    except Exception as e:
                        logger.warning(f"Failed to send recovery notification to {uid}: {e}")
    pass


MEDIA_NAMES = {
    "text": "сообщение",
    "photo": "фото",
    "video": "видео",
    "voice": "голосовое сообщение (ГС)",
    "video_note": "видео-сообщение (кружок)",
    "document": "документ",
    "audio": "аудио",
    "sticker": "стикер",
    "animation": "анимация (GIF)"
}


async def _send_media_with_fallback(
    bot: Bot,
    recipient_id: int,
    media_type: str,
    media_val: Union[types.FSInputFile, str],
    msg: str
):
    try:
        if media_type == "photo":
            await bot.send_photo(recipient_id, photo=media_val, caption=msg, parse_mode='html')
        elif media_type == "video":
            await bot.send_video(recipient_id, video=media_val, caption=msg, parse_mode='html')
        elif media_type == "voice":
            await bot.send_voice(recipient_id, voice=media_val, caption=msg, parse_mode='html')
        elif media_type == "video_note":
            await bot.send_message(recipient_id, msg, parse_mode='html')
            await bot.send_video_note(recipient_id, video_note=media_val)
        elif media_type == "document":
            await bot.send_document(recipient_id, document=media_val, caption=msg, parse_mode='html')
        elif media_type == "audio":
            await bot.send_audio(recipient_id, audio=media_val, caption=msg, parse_mode='html')
        elif media_type == "sticker":
            await bot.send_message(recipient_id, msg, parse_mode='html')
            await bot.send_sticker(recipient_id, sticker=media_val)
        elif media_type == "animation":
            await bot.send_animation(recipient_id, animation=media_val, caption=msg, parse_mode='html')
        else:
            await bot.send_message(recipient_id, msg, parse_mode='html')
    except Exception as e:
        logger.error(f"Failed to send media with caption: {e}")
        try:
            await bot.send_message(recipient_id, msg, parse_mode='html')
            if media_type == "photo":
                await bot.send_photo(recipient_id, photo=media_val)
            elif media_type == "video":
                await bot.send_video(recipient_id, video=media_val)
            elif media_type == "voice":
                await bot.send_voice(recipient_id, voice=media_val)
            elif media_type == "video_note":
                await bot.send_video_note(recipient_id, video_note=media_val)
            elif media_type == "document":
                await bot.send_document(recipient_id, document=media_val)
            elif media_type == "audio":
                await bot.send_audio(recipient_id, audio=media_val)
            elif media_type == "sticker":
                await bot.send_sticker(recipient_id, sticker=media_val)
            elif media_type == "animation":
                await bot.send_animation(recipient_id, animation=media_val)
        except Exception as e2:
            logger.error(f"Fallback media sending also failed: {e2}")


async def send_msg(
    message_old: str,
    message_new: Union[str, None],
    user_fullname: str,
    user_id: int,
    timestamp: str,
    recipient_id: int,
    bot: Bot,
    username: Union[str, None] = None,
    media_type: str = "text",
    file_id: Union[str, None] = None
):
    if username:
        user_display = f"{user_fullname} (@{username})"
    else:
        user_display = user_fullname
    user_fullname_escaped = escape(user_display)
    media_name = MEDIA_NAMES.get(media_type, "сообщение")
    
    if message_old:
        old_text_escaped = escape(message_old)
    else:
        old_text_escaped = "<i>(без описания/текста)</i>"
            
    local_path = None
    if file_id:
        downloads_dir = os.path.join(BASE_DIR, "downloads")
        files = glob.glob(os.path.join(downloads_dir, f"{file_id}.*"))
        if files:
            local_path = files[0]

    media_val = types.FSInputFile(local_path) if local_path else file_id

    if message_new is None:
        if media_type != "text" and not message_old:
            msg = DELETED_MESSAGE_NO_CONTENT_FORMAT.format(
                media_name=media_name,
                user_fullname_escaped=user_fullname_escaped,
                user_id=user_id,
                timestamp=timestamp
            )
        else:
            msg = DELETED_MESSAGE_FORMAT.format(
                media_name=media_name,
                user_fullname_escaped=user_fullname_escaped,
                user_id=user_id,
                timestamp=timestamp,
                old_text=old_text_escaped
            )
        if media_type != "text" and file_id:
            await _send_media_with_fallback(bot, recipient_id, media_type, media_val, msg)
        else:
            await bot.send_message(recipient_id, msg, parse_mode='html')
    else:
        new_text_escaped = escape(message_new) if message_new else "<i>(без описания/текста)</i>"
        msg = EDITED_MESSAGE_FORMAT.format(
            media_name=media_name,
            user_fullname_escaped=user_fullname_escaped,
            user_id=user_id,
            timestamp=timestamp,
            old_text=old_text_escaped,
            new_text=new_text_escaped
        )
        if media_type != "text" and file_id:
            await _send_media_with_fallback(bot, recipient_id, media_type, media_val, msg)
        else:
            await bot.send_message(recipient_id, msg, parse_mode='html')


@router.business_connection()
async def business_connection_handler(con: types.BusinessConnection, bot: Bot):
    if con.is_enabled:
        await ConnectionsDB.add_or_update(con.id, con.user_chat_id)
        logger.info(f"Business connection established: ID={con.id}, User Chat ID={con.user_chat_id}")
        try:
            await bot.send_message(
                chat_id=con.user_chat_id,
                text="🔌 <b>Бот успешно подключен!</b>\n\nТеперь измененные и удаленные сообщения в ваших чатах будут дублироваться сюда.",
                parse_mode="html"
            )
        except Exception as e:
            logger.warning(f"Failed to send connection notification to user {con.user_chat_id}: {e}")
    else:
        await ConnectionsDB.delete(con.id)
        logger.info(f"Business connection removed: ID={con.id}, User Chat ID={con.user_chat_id}")
        try:
            await bot.send_message(
                chat_id=con.user_chat_id,
                text="🔌 <b>Бот отключен от вашего аккаунта.</b>\n\nУведомления больше приходить не будут.",
                parse_mode="html"
            )
        except Exception as e:
            logger.warning(f"Failed to send disconnection notification to user {con.user_chat_id}: {e}")


@router.message(Command(commands=["broadcast", "sendall"]))
async def broadcast_command(message: types.Message, bot: Bot):
    if message.from_user.id != USER_ID:
        return
    reply = message.reply_to_message
    broadcast_text = None
    if not reply:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.answer(
                "📢 <b>Рассылка объявлений:</b>\n\n"
                "1. Отправьте команду <code>/broadcast Текст</code>.\n"
                "2. Или сделайте <b>Ответ (Reply)</b> с текстом <code>/broadcast</code>.",
                parse_mode="html"
            )
            return
        broadcast_text = args[1]

    user_ids = await UserSettingsDB.get_all_user_ids()
    if not user_ids:
        await message.answer("В базе данных нет зарегистрированных пользователей для рассылки.")
        return

    status_msg = await message.answer(f"Начинаю рассылку для {len(user_ids)} пользователей...")
    success_count = 0
    fail_count = 0

    for uid in user_ids:
        try:
            if reply:
                await bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=reply.message_id)
            else:
                await bot.send_message(uid, broadcast_text)
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning(f"Failed to send broadcast to {uid}: {e}")
            fail_count += 1

    await status_msg.edit_text(
        f"📢 <b>Рассылка завершена!</b>\n\n"
        f"✅ Успешно отправлено: <code>{success_count}</code>\n"
        f"❌ Ошибок: <code>{fail_count}</code>",
        parse_mode="html"
    )


def get_settings_keyboard(user_id: int, settings: dict) -> types.InlineKeyboardMarkup:
    buttons = []
    
    # 1. Updates notification toggle
    updates_val = settings.get("notify_updates", 1)
    updates_icon = "🔔 Вкл" if updates_val == 1 else "🔕 Выкл"
    buttons.append([
        types.InlineKeyboardButton(
            text=f"Оповещения об обновлениях: {updates_icon}",
            callback_data="toggle_notify_updates"
        )
    ])
    
    # 2. Startup/restart notification toggle
    startup_val = settings.get("notify_startup", 1)
    startup_icon = "🔔 Вкл" if startup_val == 1 else "🔕 Выкл"
    label = "Запуск/перезапуск бота" if user_id == USER_ID else "Оповещения о работе бота"
    buttons.append([
        types.InlineKeyboardButton(
            text=f"{label}: {startup_icon}",
            callback_data="toggle_notify_startup"
        )
    ])
    
    # 3. Delete dot-reply toggle
    delete_reply_val = settings.get("delete_reply", 1)
    delete_reply_icon = "🗑 Удалять" if delete_reply_val == 1 else "💾 Оставлять"
    buttons.append([
        types.InlineKeyboardButton(
            text=f"Удаление точки на реплай: {delete_reply_icon}",
            callback_data="toggle_delete_reply"
        )
    ])

    # КНОПКА ДЛЯ УПРАВЛЕНИЯ ПИКМИ-РЕЖИМОМ (Доступна админу)
    if user_id == USER_ID:
        pk_icon = "🥺 Вкл" if PICKME_MODE else "😎 Выкл"
        buttons.append([
            types.InlineKeyboardButton(
                text=f"Пикми-режим юзербота: {pk_icon}",
                callback_data="toggle_pickme_mode"
            )
        ])
        
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command(commands=["start"]))
async def start_command(message: types.Message):
    kb = [
        [types.KeyboardButton(text="🪞 Создать зеркало"), types.KeyboardButton(text="🛡️ Безопасность и хостинг")],
        [types.KeyboardButton(text="⚙️ Настройки")]
    ]
    keyboard = types.ReplyKeyboardMarkup(
        keyboard=kb,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие..."
    )
    start_text = (
        "Free spy — бесплатная опенсурс замена Dialog spy bot. На стадии тестирования.\n\n"
        "✨ <b>В этот экземпляр также встроен Пикми-Юзербот!</b> Управляйте им через настройки или пишите команду <code>.пикми</code> в чатах."
    )
    await UserSettingsDB.get_settings(message.from_user.id)
    await message.answer(start_text, parse_mode='html', reply_markup=keyboard, disable_web_page_preview=True)


@router.message(F.text == "⚙️ Настройки")
async def settings_menu_handler(message: types.Message):
    settings = await UserSettingsDB.get_settings(message.from_user.id)
    keyboard = get_settings_keyboard(message.from_user.id, settings)
    await message.answer("⚙️ <b>Настройки оповещений и режимов</b>", parse_mode="html", reply_markup=keyboard)


@router.callback_query(F.data.startswith("toggle_"))
async def toggle_notification_callback(callback_query: types.CallbackQuery):
    global PICKME_MODE
    user_id = callback_query.from_user.id
    action = callback_query.data
    
    settings = await UserSettingsDB.get_settings(user_id)
    
    if action == "toggle_notify_updates":
        current_val = settings.get("notify_updates", 1)
        new_val = 0 if current_val == 1 else 1
        await UserSettingsDB.set_setting(user_id, "notify_updates", new_val)
        settings["notify_updates"] = new_val
    elif action == "toggle_notify_startup":
        current_val = settings.get("notify_startup", 1)
        new_val = 0 if current_val == 1 else 1
        await UserSettingsDB.set_setting(user_id, "notify_startup", new_val)
        settings["notify_startup"] = new_val
    elif action == "toggle_delete_reply":
        current_val = settings.get("delete_reply", 1)
        new_val = 0 if current_val == 1 else 1
        await UserSettingsDB.set_setting(user_id, "delete_reply", new_val)
        settings["delete_reply"] = new_val
    elif action == "toggle_pickme_mode":
        if user_id == USER_ID:
            PICKME_MODE = not PICKME_MODE
        
    keyboard = get_settings_keyboard(user_id, settings)
    try:
        await callback_query.message.edit_reply_markup(reply_markup=keyboard)
    except Exception:
        pass
    await callback_query.answer("Настройки обновлены!")


@router.message(F.text == "🛡️ Безопасность и хостинг")
async def security_info_handler(message: types.Message):
    info_text = "🛡️ <b>Вопросы безопасности и конфиденциальности</b>\n\nКод проекта полностью открыт."
    await message.answer(info_text, parse_mode="html", disable_web_page_preview=True)


@router.message(F.text == "🪞 Создать зеркало")
async def create_mirror_handler(message: types.Message):
    mirror_text = "🪞 <b>Создание личного зеркала бота</b>"
    await message.answer(mirror_text, parse_mode="html")


IS_BOOTING = True
MISSED_UPDATES_BUFFER = []
PENDING_STARTUP_MEDIA = {}
LAST_STARTUP_UPDATE_TIME = 0.0


def safe_escape(text: Union[str, None]) -> str:
    if not text:
        return "<i>(без описания/текста)</i>"
    return escape(str(text))


async def process_startup_digest(bot: Bot):
    global IS_BOOTING
    await asyncio.sleep(3)
    while True:
        now = asyncio.get_event_loop().time()
        silence_duration = now - LAST_STARTUP_UPDATE_TIME
        if silence_duration >= 2.0:
            break
        await asyncio.sleep(0.5)
    IS_BOOTING = False
    logger.info(f"Startup buffering complete. Processing {len(MISSED_UPDATES_BUFFER)} missed updates...")
    
    grouped = {}
    for item in MISSED_UPDATES_BUFFER:
        r_id = item["recipient_id"]
        if r_id not in grouped:
            grouped[r_id] = []
        grouped[r_id].append(item)
        
    for r_id, items in grouped.items():
        text_lines = []
        media_items = []
        for item in items:
            user_display = item["user_fullname"]
            if item.get("username"):
                user_display += f" (@{item['username']})"
            user_display = escape(user_display)
            user_id = item["user_id"]
            timestamp = item["timestamp"]
            media_type = item["media_type"]
            media_name = MEDIA_NAMES.get(media_type, "сообщение")
            
            if item["type"] == "delete":
                if media_type == "text":
                    text_lines.append(f"🗑 <b>Удалено сообщение</b> от {user_display}... <blockquote>{safe_escape(item['old_text'])}</blockquote>")
                else:
                    caption_part = f"\n<blockquote>Описание: {escape(item['old_text'])}</blockquote>" if item.get("old_text") else ""
                    text_lines.append(f"🗑 <b>Удалено {media_name}</b> от {user_display}... {caption_part}")
                    if item.get("file_id"): media_items.append(item)
            elif item["type"] == "edit":
                if media_type == "text":
                    text_lines.append(f"📝 <b>Изменено сообщение</b>...\n<b>Было:</b> <blockquote>{safe_escape(item['old_text'])}</blockquote>\n<b>Стало:</b> <blockquote>{safe_escape(item['new_text'])}</blockquote>")
                else:
                    text_lines.append(f"📝 <b>Изменено описание {media_name}</b>...\n<b>Было:</b> <blockquote>{safe_escape(item['old_text'])}</blockquote>\n<b>Стало:</b> <blockquote>{safe_escape(item['new_text'])}</blockquote>")
                    if item.get("file_id"): media_items.append(item)
                        
        if text_lines:
            header = "📋 <b>Отчет о сообщениях, пропущенных за время отсутствия сети:</b>\n\n"
            current_msg = header
            for line in text_lines:
                if len(current_msg) + len(line) + 2 > 4000:
                    try: await bot.send_message(r_id, current_msg, parse_mode="html")
                    except Exception: pass
                    current_msg = line + "\n\n"
                else:
                    current_msg += line + "\n\n"
            if current_msg:
                try: await bot.send_message(r_id, current_msg, parse_mode="html")
                except Exception: pass
                    
        if media_items:
            PENDING_STARTUP_MEDIA[r_id] = media_items
            kb = [[types.InlineKeyboardButton(text="📥 Да, прислать", callback_data="startup_media_yes"), types.InlineKeyboardButton(text="❌ Нет, не нужно", callback_data="startup_media_no")]]
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=kb)
            try: await bot.send_message(r_id, f"📷 Удалены/изменены медиафайлы ({len(media_items)} шт.). Отправить?", reply_markup=keyboard)
            except Exception: pass
    MISSED_UPDATES_BUFFER.clear()


@router.callback_query(F.data.startswith("startup_media_"))
async def startup_media_callback(callback_query: types.CallbackQuery, bot: Bot):
    user_id = callback_query.from_user.id
    action = callback_query.data
    
    if action == "startup_media_yes":
        media_items = PENDING_STARTUP_MEDIA.get(user_id, [])
        if not media_items:
            await callback_query.message.edit_text("Медиафайлы не найдены.")
            return
        await callback_query.message.edit_text(f"Начинаю отправку медиафайлов ({len(media_items)} шт.)...")
        
        for item in media_items:
            media_type = item["media_type"]
            file_id = item["file_id"]
            user_display = item["user_fullname"] + (f" (@{item['username']})" if item.get("username") else "")
            caption = f"💾 <b>Медиа ({MEDIA_NAMES.get(media_type)}) от {escape(user_display)}</b>"
            
            local_path = None
            downloads_dir = os.path.join(BASE_DIR, "downloads")
            files = glob.glob(os.path.join(downloads_dir, f"{file_id}.*"))
            if files: local_path = files[0]
            media_val = types.FSInputFile(local_path) if local_path else file_id
            
            try:
                if media_type == "photo": await bot.send_photo(user_id, photo=media_val, caption=caption, parse_mode='html')
                elif media_type == "video": await bot.send_video(user_id, video=media_val, caption=caption, parse_mode='html')
                elif media_type == "voice": await bot.send_voice(user_id, voice=media_val, caption=caption, parse_mode='html')
                elif media_type == "document": await bot.send_document(user_id, document=media_val, caption=caption, parse_mode='html')
                if local_path: os.remove(local_path)
                await asyncio.sleep(0.1)
            except Exception: pass
        await callback_query.message.edit_text("Отправка завершена!")
        PENDING_STARTUP_MEDIA.pop(user_id, None)
    elif action == "startup_media_no":
        PENDING_STARTUP_MEDIA.pop(user_id, None)
        await callback_query.message.edit_text("Медиафайлы удалены из буфера.")


@router.edited_business_message()
async def edited_business_message(message: types.Message):
    if message.from_user and message.from_user.id == message.chat.id:
        recipient_id = await ConnectionsDB.get_user_id(message.business_connection_id)
        if not recipient_id: return
        user_msg = await MessageStore.get(user_id=message.from_user.id, message_id=message.message_id)
        if not user_msg:
            for _ in range(10):
                await asyncio.sleep(0.1)
                user_msg = await MessageStore.get(user_id=message.from_user.id, message_id=message.message_id)
                if user_msg: break
        if user_msg:
            message_timestamp = datetime.fromisoformat(user_msg.timestamp).astimezone(timezone_local)
            timestamp_formatted = message_timestamp.strftime('%d/%m/%y %H:%M')
            if IS_BOOTING:
                MISSED_UPDATES_BUFFER.append({
                    "type": "edit", "recipient_id": recipient_id, "user_fullname": message.from_user.full_name,
                    "user_id": message.chat.id, "username": message.from_user.username, "timestamp": timestamp_formatted,
                    "media_type": user_msg.media_type, "file_id": user_msg.file_id, "old_text": user_msg.message_text, "new_text": message.text or message.caption or ""
                })
            else:
                await send_msg(user_msg.message_text, message.text or message.caption or "", message.from_user.full_name, message.chat.id, timestamp_formatted, recipient_id, message.bot, message.from_user.username, user_msg.media_type, user_msg.file_id)
            await MessageStore.update(user_id=message.from_user.id, message_id=message.message_id, message_text=message.text or message.caption)


@router.deleted_business_messages()
async def deleted_business_messages(event: types.BusinessMessagesDeleted, bot: Bot):
    user_id = event.chat.id
    user_fullname = event.chat.full_name
    username = event.chat.username
    recipient_id = await ConnectionsDB.get_user_id(event.business_connection_id)
    if not recipient_id: return
    for msg_id in event.message_ids:
        user_msg = await MessageStore.get(user_id=user_id, message_id=msg_id)
        if not user_msg:
            for _ in range(10):
                await asyncio.sleep(0.1)
                user_msg = await MessageStore.get(user_id=user_id, message_id=msg_id)
                if user_msg: break
        if user_msg:
            message_timestamp = datetime.fromisoformat(user_msg.timestamp).astimezone(timezone_local)
            timestamp_formatted = message_timestamp.strftime('%d/%m/%y %H:%M')
            if IS_BOOTING:
                MISSED_UPDATES_BUFFER.append({
                    "type": "delete", "recipient_id": recipient_id, "user_fullname": user_fullname,
                    "user_id": user_id, "username": username, "timestamp": timestamp_formatted, "media_type": user_msg.media_type, "file_id": user_msg.file_id, "old_text": user_msg.message_text
                })
            else:
                await send_msg(user_msg.message_text, None, user_fullname, user_id, timestamp_formatted, recipient_id, bot, username, user_msg.media_type, user_msg.file_id)
            await MessageStore.delete(user_id=user_id, message_id=msg_id)


async def download_media(bot: Bot, file_id: str) -> Union[str, None]:
    try:
        file_info = await bot.get_file(file_id)
        ext = os.path.splitext(file_info.file_path)[1] or ""
        downloads_dir = os.path.join(BASE_DIR, "downloads")
        os.makedirs(downloads_dir, exist_ok=True)
        local_path = os.path.join(downloads_dir, f"{file_id}{ext}")
        if os.path.exists(local_path): return local_path
        await bot.download_file(file_info.file_path, local_path)
        return local_path
    except Exception: return None


@router.business_message()
async def business_message(message: types.Message):
    is_outgoing = message.from_user and message.from_user.id != message.chat.id
    if is_outgoing:
        if message.reply_to_message:
            replied_msg = message.reply_to_message
            media_type = "text"
            file_id = None
            if replied_msg.photo: media_type = "photo"; file_id = replied_msg.photo[-1].file_id
            elif replied_msg.video: media_type = "video"; file_id = replied_msg.video.file_id
            elif replied_msg.voice: media_type = "voice"; file_id = replied_msg.voice.file_id
            elif replied_msg.video_note: media_type = "video_note"; file_id = replied_msg.video_note.file_id
            
            if file_id and replied_msg.has_protected_content:
                recipient_id = await ConnectionsDB.get_user_id(message.business_connection_id)
                if recipient_id:
                    local_path = await download_media(message.bot, file_id)
                    if local_path:
                        caption = f"💾 <b>Сохранено {MEDIA_NAMES.get(media_type)}</b>"
                        media_val = types.FSInputFile(local_path)
                        try:
                            if media_type == "photo": await message.bot.send_photo(recipient_id, photo=media_val, caption=caption, parse_mode='html')
                            elif media_type == "video": await message.bot.send_video(recipient_id, video=media_val, caption=caption, parse_mode='html')
                        except Exception: pass
                        finally: os.remove(local_path)
            settings = await UserSettingsDB.get_settings(recipient_id or USER_ID)
            if message.text == "." and settings.get("delete_reply", 1) == 1:
                try: await message.bot.delete_business_messages(business_connection_id=message.business_connection_id, message_ids=[message.message_id])
                except Exception: pass
        return

    if message.from_user and message.from_user.id == message.chat.id:
        user_id = message.from_user.id
        media_type = "text"
        file_id = None
        message_text = message.text or message.caption or ""
        
        if message.photo: media_type = "photo"; file_id = message.photo[-1].file_id
        elif message.video: media_type = "video"; file_id = message.video.file_id
        elif message.voice: media_type = "voice"; file_id = message.voice.file_id
            
        if file_id: asyncio.create_task(download_media(message.bot, file_id))
        timestamp_iso = message.date.replace(tzinfo=timezone.utc).isoformat()
        await MessageStore.add(user_id=user_id, message_id=message.message_id, message_text=message_text, timestamp=timestamp_iso, media_type=media_type, file_id=file_id)


async def handle(request):
    return web.Response(text="Bot is running!")

web_runner = None
background_tasks = set()


async def start_web_server():
    global web_runner
    app = web.Application()
    app.router.add_get("/", handle)
    web_runner = web.AppRunner(app)
    await web_runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(web_runner, "0.0.0.0", port)
    await site.start()


async def stop_web_server():
    global web_runner
    if web_runner: await web_runner.cleanup()


async def self_ping():
    import aiohttp
    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not external_url: return
    while True:
        try:
            await asyncio.sleep(5 * 60)
            async with aiohttp.ClientSession() as session:
                async with session.get(external_url) as resp: logger.info(f"Self-ping: {resp.status}")
        except asyncio.CancelledError: break
        except Exception: pass


async def on_startup(bot: Bot):
    global LAST_STARTUP_UPDATE_TIME
    LAST_STARTUP_UPDATE_TIME = asyncio.get_event_loop().time()
    await MessageStore.create_db()
    await ConnectionsDB.create_table()
    await SystemStateDB.create_table()
    await UserSettingsDB.create_table()

    for task_func in [process_startup_digest(bot), check_and_broadcast_changelog(bot), update_last_active(), monitor_memory(), cleanup_old_messages(), self_ping()]:
        task = asyncio.create_task(task_func)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)


async def on_shutdown(bot: Bot):
    for task in background_tasks: task.cancel()
    if background_tasks: await asyncio.gather(*background_tasks, return_exceptions=True)
    await stop_web_server()
    global db_pool
    if db_pool: db_pool.closeall(); db_pool = None


async def raw_update_middleware(handler, event: types.Update, data):
    global LAST_STARTUP_UPDATE_TIME
    if IS_BOOTING: LAST_STARTUP_UPDATE_TIME = asyncio.get_event_loop().time()
    return await handler(event, data)


# =======================================================
# КОД ДЛЯ ПИКМИ-ЮЗЕРБОТА (PYROGRAM)
# =======================================================
userbot = None
if API_ID and API_HASH:
    userbot = Client("my_account_session", api_id=API_ID, api_hash=API_HASH)

    # 1. Хэндлер для быстрой триггер-команды в любом чате
    @userbot.on_message(py_filters.me & py_filters.command("пикми", prefixes="."))
    async def toggle_via_chat(_, message):
        global PICKME_MODE
        PICKME_MODE = not PICKME_MODE
        status = "ВКЛЮЧЕН 🥺" if PICKME_MODE else "ВЫКЛЮЧЕН 😎"
        
        # Редактируем сообщение, чтобы показать статус, и через 2 секунды удаляем скрытно
        await message.edit_text(f"<b>[Пикми-режим {status}]</b>")
        await asyncio.sleep(2)
        await message.delete()

    # 2. Хэндлер для изменения твоих сообщений
    @userbot.on_message(py_filters.me & py_filters.text)
    async def pickme_transformer(_, message):
        global PICKME_MODE
        # Игнорируем управляющие команды
        if message.text.startswith(".") or message.text.startswith("/"):
            return

        if PICKME_MODE:
            original_text = message.text
            new_text = f"{random.choice(PICKME_PREFIXES)}{original_text}{random.choice(PICKME_POSTFIXES)}"
            try:
                await message.edit_text(new_text)
            except Exception as e:
                logger.error(f"Ошибка изменения сообщения юзерботом: {e}")


async def main() -> None:
    global db_pool
    if DATABASE_URL:
        if not psycopg2_available: raise RuntimeError("psycopg2 is not installed.")
        db_pool = ThreadedConnectionPool(1, 20, dsn=DATABASE_URL)

    await start_web_server()

    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.update.outer_middleware(raw_update_middleware)
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    await bot.delete_webhook(drop_pending_updates=False)

    if userbot:
        logger.info("🤖 Запуск тандема: Шпионский бот (Aiogram) + Пикми-Юзербот (Pyrogram)...")
        await asyncio.gather(
            dp.start_polling(bot),
            userbot.start()
        )
        await asyncio.Event().wait()
    else:
        logger.warning("⚠️ API_ID и API_HASH не настроены. Пикми-Юзербот не будет запущен. Работает только логгер.")
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
