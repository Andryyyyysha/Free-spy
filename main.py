import configparser
import importlib
import os
import sqlite3
from pydantic import BaseModel
import asyncio
from typing import Union
import logging
from aiogram import Router, Bot, Dispatcher, F, types
from aiogram.filters import Command
from html import escape
from datetime import datetime, timezone, timedelta
import pytz


config = configparser.ConfigParser()
config.read("config.ini")

TOKEN = config.get("settings", "TOKEN", fallback="YOUR_BOT_TOKEN_HERE")
try:
    USER_ID = config.getint("settings", "USER_ID")
except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
    USER_ID = 0

TIMEZONE_NAME = config.get("settings", "TIMEZONE_NAME", fallback="Europe/Moscow")
timezone_local = pytz.timezone(TIMEZONE_NAME)

DELETED_MESSAGE_FORMAT = (
    "🗑 <b>Удалено {media_name} от {user_fullname_escaped}</b> (ID: <code>{user_id}</code>)\n"
    "⏰ <b>Время отправки:</b> {timestamp}\n\n"
    "💬 <b>Содержание:</b>\n"
    "<blockquote>{old_text}</blockquote>"
)

EDITED_MESSAGE_FORMAT = (
    "📝 <b>Изменено {media_name} от {user_fullname_escaped}</b> (ID: <code>{user_id}</code>)\n"
    "⏰ <b>Время отправки:</b> {timestamp}\n\n"
    "❌ <b>Было:</b>\n"
    "<blockquote>{old_text}</blockquote>\n\n"
    "✅ <b>Стало:</b>\n"
    "<blockquote>{new_text}</blockquote>"
)


router = Router(name=__name__)
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def dict_factory(cursor, row) -> dict:
    save_dict = {}
    for idx, col in enumerate(cursor.description):
        save_dict[col[0]] = row[idx]
    return save_dict


def update_format(sql, parameters: dict) -> tuple[str, list]:
    values = ", ".join([f"{item} = ?" for item in parameters])
    sql += f" {values}"
    return sql, list(parameters.values())


class MessageRecord(BaseModel):
    user_id: int
    message_id: int
    message_text: Union[str, None] = None
    timestamp: str
    media_type: str = "text"
    file_id: Union[str, None] = None


class Messagesx:
    storage_name = "messages"
    PATH_DATABASE = "messages.db"


    @staticmethod
    def create_db():
        with sqlite3.connect(Messagesx.PATH_DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute('''CREATE TABLE IF NOT EXISTS messages
                              (id INTEGER PRIMARY KEY,
                               user_id INTEGER,
                               message_id INTEGER,
                               message_text TEXT,
                               timestamp TEXT,
                               media_type TEXT DEFAULT 'text',
                               file_id TEXT)''')
            # Migrate old database structure if it exists
            try:
                cursor.execute("ALTER TABLE messages ADD COLUMN media_type TEXT DEFAULT 'text'")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE messages ADD COLUMN file_id TEXT")
            except sqlite3.OperationalError:
                pass


    @staticmethod
    def add(user_id: int, message_id: int, message_text: Union[str, None], timestamp: str, media_type: str = "text", file_id: Union[str, None] = None):
        with sqlite3.connect(Messagesx.PATH_DATABASE) as con:
            con.row_factory = dict_factory
            con.execute(
                f"INSERT INTO {Messagesx.storage_name} (user_id, message_id, message_text, timestamp, media_type, file_id) VALUES (?, ?, ?, ?, ?, ?)",
                [user_id, message_id, message_text, timestamp, media_type, file_id],
            )


    @staticmethod
    def get(user_id: int, message_id: int) -> Union[MessageRecord, None]:
        with sqlite3.connect(Messagesx.PATH_DATABASE) as con:
            con.row_factory = dict_factory
            sql = f"SELECT * FROM {Messagesx.storage_name} WHERE user_id = ? AND message_id = ?"
            response = con.execute(sql, [user_id, message_id]).fetchone()
            if response is not None:
                response = MessageRecord(**response)
            return response


    @staticmethod
    def update(user_id: int, message_id: int, **kwargs):
        with sqlite3.connect(Messagesx.PATH_DATABASE) as con:
            con.row_factory = dict_factory
            sql = f"UPDATE {Messagesx.storage_name} SET"
            sql, parameters = update_format(sql, kwargs)
            parameters.extend([user_id, message_id])
            con.execute(sql + " WHERE user_id = ? AND message_id = ?", parameters)


    @staticmethod
    def delete(user_id: int, message_id: int):
        with sqlite3.connect(Messagesx.PATH_DATABASE) as con:
            con.row_factory = dict_factory
            sql = f"DELETE FROM {Messagesx.storage_name} WHERE user_id = ? AND message_id = ?"
            con.execute(sql, [user_id, message_id])


    @staticmethod
    def delete_old_messages(cutoff_timestamp: str):
        with sqlite3.connect(Messagesx.PATH_DATABASE) as con:
            sql = f"DELETE FROM {Messagesx.storage_name} WHERE timestamp < ?"
            con.execute(sql, [cutoff_timestamp])


class ConnectionsDB:
    storage_name = "connections"
    PATH_DATABASE = "messages.db"

    @staticmethod
    def create_table():
        with sqlite3.connect(ConnectionsDB.PATH_DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute('''CREATE TABLE IF NOT EXISTS connections
                              (connection_id TEXT PRIMARY KEY, user_id INTEGER)''')

    @staticmethod
    def add_or_update(connection_id: str, user_id: int):
        with sqlite3.connect(ConnectionsDB.PATH_DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"INSERT OR REPLACE INTO {ConnectionsDB.storage_name} (connection_id, user_id) VALUES (?, ?)",
                [connection_id, user_id]
            )

    @staticmethod
    def get_user_id(connection_id: str) -> Union[int, None]:
        with sqlite3.connect(ConnectionsDB.PATH_DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT user_id FROM {ConnectionsDB.storage_name} WHERE connection_id = ?",
                [connection_id]
            )
            row = cursor.fetchone()
            return row[0] if row else None

    @staticmethod
    def delete(connection_id: str):
        with sqlite3.connect(ConnectionsDB.PATH_DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"DELETE FROM {ConnectionsDB.storage_name} WHERE connection_id = ?",
                [connection_id]
            )


Messagesx.create_db()
ConnectionsDB.create_table()


async def cleanup_old_messages():
    while True:
        now_local = datetime.now(timezone_local)
        next_run = now_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        sleep_seconds = (next_run - now_local).total_seconds()
        await asyncio.sleep(sleep_seconds)
        cutoff_datetime = datetime.now(timezone.utc) - timedelta(days=30)
        cutoff_timestamp_iso = cutoff_datetime.isoformat()
        Messagesx.delete_old_messages(cutoff_timestamp_iso)


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


async def send_msg(
    message_old: str,
    message_new: Union[str, None],
    user_fullname: str,
    user_id: int,
    timestamp: str,
    recipient_id: int,
    username: Union[str, None] = None,
    media_type: str = "text",
    file_id: Union[str, None] = None,
    bot: Bot = None
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
        if media_type == "sticker":
            old_text_escaped = "<i>[Стикер]</i>"
        elif media_type == "video_note":
            old_text_escaped = "<i>[Кружок]</i>"
        elif media_type == "voice":
            old_text_escaped = "<i>[Голосовое сообщение]</i>"
        else:
            old_text_escaped = "<i>(без описания/текста)</i>"
            
    if message_new is None:
        msg = DELETED_MESSAGE_FORMAT.format(
            media_name=media_name,
            user_fullname_escaped=user_fullname_escaped,
            user_id=user_id,
            timestamp=timestamp,
            old_text=old_text_escaped
        )
        
        if media_type != "text" and file_id:
            try:
                if media_type == "photo":
                    await bot.send_photo(recipient_id, photo=file_id, caption=msg, parse_mode='html')
                elif media_type == "video":
                    await bot.send_video(recipient_id, video=file_id, caption=msg, parse_mode='html')
                elif media_type == "voice":
                    await bot.send_voice(recipient_id, voice=file_id, caption=msg, parse_mode='html')
                elif media_type == "video_note":
                    await bot.send_message(recipient_id, msg, parse_mode='html')
                    await bot.send_video_note(recipient_id, video_note=file_id)
                elif media_type == "document":
                    await bot.send_document(recipient_id, document=file_id, caption=msg, parse_mode='html')
                elif media_type == "audio":
                    await bot.send_audio(recipient_id, audio=file_id, caption=msg, parse_mode='html')
                elif media_type == "sticker":
                    await bot.send_message(recipient_id, msg, parse_mode='html')
                    await bot.send_sticker(recipient_id, sticker=file_id)
                elif media_type == "animation":
                    await bot.send_animation(recipient_id, animation=file_id, caption=msg, parse_mode='html')
                else:
                    await bot.send_message(recipient_id, msg, parse_mode='html')
            except Exception as e:
                logger.error(f"Failed to send deleted media with caption: {e}")
                try:
                    await bot.send_message(recipient_id, msg, parse_mode='html')
                    if media_type == "photo":
                        await bot.send_photo(recipient_id, photo=file_id)
                    elif media_type == "video":
                        await bot.send_video(recipient_id, video=file_id)
                    elif media_type == "voice":
                        await bot.send_voice(recipient_id, voice=file_id)
                    elif media_type == "video_note":
                        await bot.send_video_note(recipient_id, video_note=file_id)
                    elif media_type == "document":
                        await bot.send_document(recipient_id, document=file_id)
                    elif media_type == "audio":
                        await bot.send_audio(recipient_id, audio=file_id)
                    elif media_type == "sticker":
                        await bot.send_sticker(recipient_id, sticker=file_id)
                    elif media_type == "animation":
                        await bot.send_animation(recipient_id, animation=file_id)
                except Exception as e2:
                    logger.error(f"Fallback media sending also failed: {e2}")
        else:
            await bot.send_message(recipient_id, msg, parse_mode='html')
    else:
        new_text_escaped = escape(message_new)
        msg = EDITED_MESSAGE_FORMAT.format(
            media_name=media_name,
            user_fullname_escaped=user_fullname_escaped,
            user_id=user_id,
            timestamp=timestamp,
            old_text=old_text_escaped,
            new_text=new_text_escaped
        )
        await bot.send_message(recipient_id, msg, parse_mode='html')


@router.business_connection()
async def business_connection_handler(con: types.BusinessConnection):
    if con.is_enabled:
        ConnectionsDB.add_or_update(con.id, con.user_chat_id)
        logger.info(f"Business connection established: ID={con.id}, User Chat ID={con.user_chat_id}")
    else:
        ConnectionsDB.delete(con.id)
        logger.info(f"Business connection removed: ID={con.id}, User Chat ID={con.user_chat_id}")


@router.message(Command(commands=["start"]))
async def start_command(message: types.Message):
    await message.answer("Free spy - бесплатная опенсурс замена Dialog spy bot. На стадии тестирования.", parse_mode='html')


@router.edited_business_message()
async def edited_business_message(message: types.Message):
    if message.from_user and message.from_user.id == message.chat.id:
        recipient_id = ConnectionsDB.get_user_id(message.business_connection_id) or USER_ID
        user_msg = Messagesx.get(user_id=message.from_user.id, message_id=message.message_id)
        if user_msg:
            message_timestamp = datetime.fromisoformat(user_msg.timestamp).astimezone(timezone_local)
            timestamp_formatted = message_timestamp.strftime('%d/%m/%y %H:%M')
            await send_msg(
                message_old=user_msg.message_text,
                message_new=message.text or message.caption,
                user_fullname=message.from_user.full_name,
                user_id=message.chat.id,
                timestamp=timestamp_formatted,
                recipient_id=recipient_id,
                username=message.from_user.username,
                media_type=user_msg.media_type,
                file_id=user_msg.file_id,
                bot=message.bot
            )
            Messagesx.update(user_id=message.from_user.id, message_id=message.message_id, message_text=message.text or message.caption)


@router.deleted_business_messages()
async def deleted_business_messages(event: types.BusinessMessagesDeleted, bot: Bot):
    user_id = event.chat.id
    user_fullname = event.chat.full_name
    username = event.chat.username
    recipient_id = ConnectionsDB.get_user_id(event.business_connection_id) or USER_ID
    for msg_id in event.message_ids:
        user_msg = Messagesx.get(user_id=user_id, message_id=msg_id)
        if user_msg:
            message_timestamp = datetime.fromisoformat(user_msg.timestamp).astimezone(timezone_local)
            timestamp_formatted = message_timestamp.strftime('%d/%m/%y %H:%M')
            await send_msg(
                message_old=user_msg.message_text,
                message_new=None,
                user_fullname=user_fullname,
                user_id=user_id,
                timestamp=timestamp_formatted,
                recipient_id=recipient_id,
                username=username,
                media_type=user_msg.media_type,
                file_id=user_msg.file_id,
                bot=bot
            )
            Messagesx.delete(user_id=user_id, message_id=msg_id)


@router.business_message()
async def business_message(message: types.Message):
    if message.from_user and message.from_user.id == message.chat.id:
        user_id = message.from_user.id
        
        media_type = "text"
        file_id = None
        message_text = message.text or message.caption or ""
        
        if message.photo:
            media_type = "photo"
            file_id = message.photo[-1].file_id
        elif message.video:
            media_type = "video"
            file_id = message.video.file_id
        elif message.voice:
            media_type = "voice"
            file_id = message.voice.file_id
        elif message.video_note:
            media_type = "video_note"
            file_id = message.video_note.file_id
        elif message.document:
            media_type = "document"
            file_id = message.document.file_id
        elif message.audio:
            media_type = "audio"
            file_id = message.audio.file_id
        elif message.sticker:
            media_type = "sticker"
            file_id = message.sticker.file_id
        elif message.animation:
            media_type = "animation"
            file_id = message.animation.file_id
            
        message_datetime_utc = message.date.replace(tzinfo=timezone.utc)
        timestamp_iso = message_datetime_utc.isoformat()
        
        Messagesx.add(
            user_id=user_id,
            message_id=message.message_id,
            message_text=message_text,
            timestamp=timestamp_iso,
            media_type=media_type,
            file_id=file_id
        )


async def main() -> None:
    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
