import asyncio
import html
import logging
import os
import re
import sqlite3
from contextlib import closing

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
DB_NAME = os.getenv("DB_NAME", "music_bot.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is not set")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is not set")
if not BOT_USERNAME:
    raise RuntimeError("BOT_USERNAME is not set")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# The bot is intended for one owner/admin, so a small in-memory state machine
# is enough for the management panel. Song/file data itself is persistent in SQLite.
admin_state = {}


def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with closing(get_db()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                channel_message_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                song_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                preview_file_id TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(song_id, version_no),
                FOREIGN KEY(song_id) REFERENCES songs(id) ON DELETE CASCADE
            )
        """)
        conn.commit()


def is_admin(user_id: int | None) -> bool:
    return user_id == ADMIN_ID


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ انتشار آهنگ", callback_data="publish")],
            [InlineKeyboardButton(text="🔍 جستجو", callback_data="search")],
            [InlineKeyboardButton(text="📊 آمار", callback_data="stats")],
        ]
    )


def song_menu(song_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 جایگزینی نسخه",
                    callback_data=f"replace:{song_id}",
                ),
                InlineKeyboardButton(
                    text="➕ افزودن نسخه",
                    callback_data=f"addversion:{song_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ ویرایش اطلاعات",
                    callback_data=f"edit:{song_id}",
                ),
                InlineKeyboardButton(
                    text="🗑 حذف آهنگ",
                    callback_data=f"delete:{song_id}",
                ),
            ],
            [InlineKeyboardButton(text="⬅️ پنل مدیریت", callback_data="home")],
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو", callback_data="cancel")]
        ]
    )


def file_id_from_message(message: Message) -> str | None:
    if message.audio:
        return message.audio.file_id
    if message.document:
        return message.document.file_id
    return None


def download_link(song_id: int, version_no: int, label: str) -> str:
    return (
        f'<a href="https://t.me/{BOT_USERNAME}?start=s{song_id}v{version_no}">'
        f"{html.escape(label)}</a>"
    )


def build_links(song_id: int) -> str:
    with closing(get_db()) as conn:
        versions = conn.execute(
            """
            SELECT version_no
            FROM versions
            WHERE song_id = ?
            ORDER BY version_no
            """,
            (song_id,),
        ).fetchall()

    if not versions:
        return ""

    if len(versions) == 1:
        v = versions[0]["version_no"]
        return download_link(song_id, v, "دانلود آهنگ کامل")

    return " | ".join(
        download_link(song_id, row["version_no"], f"نسخه {row['version_no']}")
        for row in versions
    )


def build_caption(song_id: int) -> str:
    with closing(get_db()) as conn:
        song = conn.execute(
            "SELECT title, artist FROM songs WHERE id = ?",
            (song_id,),
        ).fetchone()

    if not song:
        return ""

    links = build_links(song_id)
    return (
        f"🎵 <b>{html.escape(song['title'])}</b>\n"
        f"👤 {html.escape(song['artist'])}\n\n"
        f"{links}"
    )


async def publish_preview(song_id: int, preview_file_id: str) -> int:
    sent = await bot.send_audio(
        chat_id=CHANNEL_ID,
        audio=preview_file_id,
        caption=build_caption(song_id),
        parse_mode="HTML",
    )

    with closing(get_db()) as conn:
        conn.execute(
            "UPDATE songs SET channel_message_id = ? WHERE id = ?",
            (sent.message_id, song_id),
        )
        conn.commit()

    return sent.message_id


async def update_channel_post(song_id: int):
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT channel_message_id FROM songs WHERE id = ?",
            (song_id,),
        ).fetchone()

    if not row or not row["channel_message_id"]:
        return

    try:
        await bot.edit_message_caption(
            chat_id=CHANNEL_ID,
            message_id=row["channel_message_id"],
            caption=build_caption(song_id),
            parse_mode="HTML",
        )
    except Exception:
        logging.exception("Could not update channel post for song %s", song_id)


def get_song(song_id: int):
    with closing(get_db()) as conn:
        return conn.execute(
            "SELECT * FROM songs WHERE id = ?",
            (song_id,),
        ).fetchone()


def get_version(song_id: int, version_no: int):
    with closing(get_db()) as conn:
        return conn.execute(
            """
            SELECT *
            FROM versions
            WHERE song_id = ? AND version_no = ?
            """,
            (song_id, version_no),
        ).fetchone()


def format_song(song_id: int) -> str:
    with closing(get_db()) as conn:
        song = conn.execute(
            "SELECT * FROM songs WHERE id = ?",
            (song_id,),
        ).fetchone()
        versions = conn.execute(
            """
            SELECT version_no
            FROM versions
            WHERE song_id = ?
            ORDER BY version_no
            """,
            (song_id,),
        ).fetchall()

    if not song:
        return "آهنگ پیدا نشد."

    version_text = ", ".join(str(v["version_no"]) for v in versions) or "ندارد"
    return (
        f"🎵 <b>{html.escape(song['title'])}</b>\n"
        f"👤 {html.escape(song['artist'])}\n"
        f"🆔 کد: <code>{song_id}</code>\n"
        f"🎚 نسخه‌ها: {version_text}"
    )


def clear_state():
    admin_state.pop(ADMIN_ID, None)


async def show_search_results(message: Message, query: str):
    query = query.strip()

    with closing(get_db()) as conn:
        if query.isdigit():
            rows = conn.execute(
                """
                SELECT id, title, artist
                FROM songs
                WHERE id = ?
                LIMIT 10
                """,
                (int(query),),
            ).fetchall()
        else:
            like = f"%{query}%"
            rows = conn.execute(
                """
                SELECT id, title, artist
                FROM songs
                WHERE title LIKE ? OR artist LIKE ?
                ORDER BY id DESC
                LIMIT 10
                """,
                (like, like),
            ).fetchall()

    if not rows:
        await message.answer("موردی پیدا نشد.", reply_markup=admin_menu())
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{row['title']} | {row['artist']}",
                    callback_data=f"song:{row['id']}",
                )
            ]
            for row in rows
        ]
    )

    await message.answer(
        "نتایج جستجو:",
        reply_markup=keyboard,
    )


@dp.message(CommandStart())
async def start_handler(message: Message):
    parts = message.text.split(maxsplit=1)

    if len(parts) == 1:
        if is_admin(message.from_user.id):
            await message.answer(
                "پنل مدیریت:",
                reply_markup=admin_menu(),
            )
        return

    payload = parts[1].strip()
    match = re.fullmatch(r"s(\d+)v(\d+)", payload)

    # Normal users only get the requested song. No user menu, search, or stats.
    if not match:
        return

    song_id = int(match.group(1))
    version_no = int(match.group(2))
    version = get_version(song_id, version_no)

    if not version:
        return

    await bot.send_audio(
        chat_id=message.chat.id,
        audio=version["file_id"],
    )


@dp.callback_query(F.data == "home")
async def home_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    clear_state()
    await callback.message.answer(
        "پنل مدیریت:",
        reply_markup=admin_menu(),
    )
    await callback.answer()


@dp.callback_query(F.data == "cancel")
async def cancel_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    clear_state()
    await callback.message.answer(
        "لغو شد.",
        reply_markup=admin_menu(),
    )
    await callback.answer()


@dp.callback_query(F.data == "publish")
async def publish_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    admin_state[ADMIN_ID] = {
        "action": "publish",
        "step": "title",
        "data": {},
    }

    await callback.message.answer(
        "نام آهنگ را ارسال کن:",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "search")
async def search_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    admin_state[ADMIN_ID] = {
        "action": "search",
        "step": "query",
        "data": {},
    }

    await callback.message.answer(
        "کد آهنگ یا نام آهنگ / خواننده را ارسال کن:",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "stats")
async def stats_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    with closing(get_db()) as conn:
        songs = conn.execute("SELECT COUNT(*) AS c FROM songs").fetchone()["c"]
        versions = conn.execute("SELECT COUNT(*) AS c FROM versions").fetchone()["c"]

    await callback.message.answer(
        f"📊 آمار\n\n"
        f"تعداد آهنگ‌ها: {songs}\n"
        f"تعداد نسخه‌ها: {versions}",
        reply_markup=admin_menu(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("song:"))
async def song_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    song_id = int(callback.data.split(":")[1])
    song = get_song(song_id)

    if not song:
        await callback.message.answer("آهنگ پیدا نشد.")
        await callback.answer()
        return

    await callback.message.answer(
        format_song(song_id),
        reply_markup=song_menu(song_id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("replace:"))
async def replace_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    song_id = int(callback.data.split(":")[1])

    if not get_song(song_id):
        await callback.message.answer("آهنگ پیدا نشد.")
        await callback.answer()
        return

    admin_state[ADMIN_ID] = {
        "action": "replace",
        "step": "version",
        "data": {"song_id": song_id},
    }

    await callback.message.answer(
        "شماره نسخه‌ای که می‌خواهی فایلش عوض شود را ارسال کن:",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("addversion:"))
async def add_version_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    song_id = int(callback.data.split(":")[1])

    if not get_song(song_id):
        await callback.message.answer("آهنگ پیدا نشد.")
        await callback.answer()
        return

    with closing(get_db()) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(MAX(version_no), 0) AS max_version
            FROM versions
            WHERE song_id = ?
            """,
            (song_id,),
        ).fetchone()

    next_version = row["max_version"] + 1

    admin_state[ADMIN_ID] = {
        "action": "add_version",
        "step": "full",
        "data": {
            "song_id": song_id,
            "version_no": next_version,
        },
    }

    await callback.message.answer(
        f"نسخه {next_version}: فایل کامل را ارسال کن.",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("edit:"))
async def edit_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    song_id = int(callback.data.split(":")[1])

    if not get_song(song_id):
        await callback.message.answer("آهنگ پیدا نشد.")
        await callback.answer()
        return

    admin_state[ADMIN_ID] = {
        "action": "edit",
        "step": "title",
        "data": {"song_id": song_id},
    }

    await callback.message.answer(
        "نام جدید آهنگ را ارسال کن.\n"
        "برای بدون تغییر گذاشتن نام، همین نام فعلی را دوباره ارسال کن.",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("delete:"))
async def delete_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    song_id = int(callback.data.split(":")[1])
    song = get_song(song_id)

    if not song:
        await callback.message.answer("آهنگ پیدا نشد.")
        await callback.answer()
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="بله، حذف شود",
                    callback_data=f"confirmdelete:{song_id}",
                ),
                InlineKeyboardButton(
                    text="لغو",
                    callback_data=f"song:{song_id}",
                ),
            ]
        ]
    )

    await callback.message.answer(
        "این آهنگ و تمام نسخه‌هایش حذف می‌شوند. مطمئنی؟",
        reply_markup=keyboard,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("confirmdelete:"))
async def confirm_delete_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    song_id = int(callback.data.split(":")[1])
    song = get_song(song_id)

    if not song:
        await callback.message.answer("آهنگ پیدا نشد.")
        await callback.answer()
        return

    if song["channel_message_id"]:
        try:
            await bot.delete_message(
                chat_id=CHANNEL_ID,
                message_id=song["channel_message_id"],
            )
        except Exception:
            logging.exception("Could not delete channel message")

    with closing(get_db()) as conn:
        conn.execute("DELETE FROM songs WHERE id = ?", (song_id,))
        conn.commit()

    await callback.message.answer(
        "آهنگ حذف شد.",
        reply_markup=admin_menu(),
    )
    await callback.answer()


@dp.message(F.from_user.id == ADMIN_ID)
async def admin_message_handler(message: Message):
    state = admin_state.get(ADMIN_ID)

    if not state:
        await message.answer(
            "پنل مدیریت:",
            reply_markup=admin_menu(),
        )
        return

    action = state["action"]
    step = state["step"]
    data = state["data"]

    if action == "search":
        if step == "query":
            clear_state()
            await show_search_results(message, message.text or "")
        return

    if action == "publish":
        if step == "title":
            if not message.text or not message.text.strip():
                await message.answer("نام آهنگ را به‌صورت متن ارسال کن.")
                return
            data["title"] = message.text.strip()
            state["step"] = "artist"
            await message.answer("نام خواننده را ارسال کن:")
            return

        if step == "artist":
            if not message.text or not message.text.strip():
                await message.answer("نام خواننده را به‌صورت متن ارسال کن.")
                return
            data["artist"] = message.text.strip()
            state["step"] = "full"
            await message.answer("فایل کامل آهنگ را ارسال کن:")
            return

        if step == "full":
            file_id = file_id_from_message(message)
            if not file_id:
                await message.answer("فایل کامل را به‌صورت Audio یا Document ارسال کن.")
                return
            data["full_file_id"] = file_id
            state["step"] = "preview"
            await message.answer("حالا فایل Preview را ارسال کن:")
            return

        if step == "preview":
            preview_id = file_id_from_message(message)
            if not preview_id:
                await message.answer("فایل Preview را به‌صورت Audio یا Document ارسال کن.")
                return

            title = data["title"]
            artist = data["artist"]
            full_id = data["full_file_id"]

            with closing(get_db()) as conn:
                cur = conn.execute(
                    "INSERT INTO songs(title, artist) VALUES (?, ?)",
                    (title, artist),
                )
                song_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO versions(song_id, version_no, file_id, preview_file_id)
                    VALUES (?, 1, ?, ?)
                    """,
                    (song_id, full_id, preview_id),
                )
                conn.commit()

            clear_state()

            try:
                await publish_preview(song_id, preview_id)
            except Exception:
                with closing(get_db()) as conn:
                    conn.execute("DELETE FROM songs WHERE id = ?", (song_id,))
                    conn.commit()
                logging.exception("Publishing failed")
                await message.answer(
                    "ذخیره انجام نشد چون انتشار در کانال شکست خورد. "
                    "دسترسی ربات به کانال و دسترسی ارسال پیام را بررسی کن.",
                    reply_markup=admin_menu(),
                )
                return

            await message.answer(
                f"آهنگ منتشر شد.\n\nکد آهنگ: <code>{song_id}</code>",
                parse_mode="HTML",
                reply_markup=song_menu(song_id),
            )
            return

    if action == "replace":
        if step == "version":
            if not message.text or not message.text.strip().isdigit():
                await message.answer("شماره نسخه را به‌صورت عدد ارسال کن.")
                return

            version_no = int(message.text.strip())
            song_id = data["song_id"]

            if not get_version(song_id, version_no):
                await message.answer("این نسخه وجود ندارد. شماره نسخه را درست وارد کن.")
                return

            data["version_no"] = version_no
            state["step"] = "file"
            await message.answer("فایل کامل جدید را ارسال کن:")
            return

        if step == "file":
            file_id = file_id_from_message(message)
            if not file_id:
                await message.answer("فایل را به‌صورت Audio یا Document ارسال کن.")
                return

            song_id = data["song_id"]
            version_no = data["version_no"]

            with closing(get_db()) as conn:
                conn.execute(
                    """
                    UPDATE versions
                    SET file_id = ?
                    WHERE song_id = ? AND version_no = ?
                    """,
                    (file_id, song_id, version_no),
                )
                conn.commit()

            clear_state()

            await message.answer(
                f"فایل نسخه {version_no} جایگزین شد.\n"
                "لینک قبلی همچنان معتبر است و پست کانال هم تغییر نکرد.",
                reply_markup=song_menu(song_id),
            )
            return

    if action == "add_version":
        song_id = data["song_id"]
        version_no = data["version_no"]

        if step == "full":
            file_id = file_id_from_message(message)
            if not file_id:
                await message.answer("فایل کامل را ارسال کن.")
                return

            data["full_file_id"] = file_id
            state["step"] = "preview"
            await message.answer(
                f"فایل Preview نسخه {version_no} را ارسال کن:"
            )
            return

        if step == "preview":
            preview_id = file_id_from_message(message)
            if not preview_id:
                await message.answer("فایل Preview را ارسال کن.")
                return

            with closing(get_db()) as conn:
                conn.execute(
                    """
                    INSERT INTO versions(song_id, version_no, file_id, preview_file_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        song_id,
                        version_no,
                        data["full_file_id"],
                        preview_id,
                    ),
                )
                conn.commit()

            clear_state()

            try:
                await update_channel_post(song_id)
            except Exception:
                logging.exception("Could not update channel post")

            await message.answer(
                f"نسخه {version_no} اضافه شد و لینک‌های پست کانال به‌روزرسانی شدند.",
                reply_markup=song_menu(song_id),
            )
            return

    if action == "edit":
        song_id = data["song_id"]

        if step == "title":
            if not message.text or not message.text.strip():
                await message.answer("نام آهنگ را ارسال کن.")
                return

            data["title"] = message.text.strip()
            state["step"] = "artist"
            await message.answer("نام جدید خواننده را ارسال کن:")
            return

        if step == "artist":
            if not message.text or not message.text.strip():
                await message.answer("نام خواننده را ارسال کن.")
                return

            data["artist"] = message.text.strip()

            with closing(get_db()) as conn:
                conn.execute(
                    """
                    UPDATE songs
                    SET title = ?, artist = ?
                    WHERE id = ?
                    """,
                    (data["title"], data["artist"], song_id),
                )
                conn.commit()

            clear_state()

            await update_channel_post(song_id)

            await message.answer(
                "اطلاعات آهنگ و متن پست کانال به‌روزرسانی شد.",
                reply_markup=song_menu(song_id),
            )
            return


async def main():
    init_db()
    logging.info("Music bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
