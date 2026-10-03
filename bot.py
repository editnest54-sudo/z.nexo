import os
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv


# =========================================================
# CONFIG
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
CHANNEL_URL = os.getenv("CHANNEL_URL", "").strip()
DB_NAME = os.getenv("DB_NAME", "music_bot.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN تنظیم نشده است.")

if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID تنظیم نشده است.")

if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID تنظیم نشده است.")

if not BOT_USERNAME:
    raise RuntimeError("BOT_USERNAME تنظیم نشده است.")

if not CHANNEL_URL:
    raise RuntimeError("CHANNEL_URL تنظیم نشده است.")


bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(DB_NAME, check_same_thread=False)
db.row_factory = sqlite3.Row

db.execute("PRAGMA foreign_keys = ON")


def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def init_db():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS songs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        artist TEXT,
        channel_message_id INTEGER,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        song_id INTEGER NOT NULL,
        version_no INTEGER NOT NULL,
        file_id TEXT NOT NULL,
        file_type TEXT DEFAULT 'document',
        preview_file_id TEXT NOT NULL,
        preview_type TEXT DEFAULT 'document',
        downloads INTEGER DEFAULT 0,
        created_at TEXT,
        FOREIGN KEY(song_id) REFERENCES songs(id) ON DELETE CASCADE,
        UNIQUE(song_id, version_no)
    );

    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY
    );

    CREATE TABLE IF NOT EXISTS download_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        song_id INTEGER,
        version_no INTEGER,
        created_at TEXT
    );
    """)

    # -------- users migration --------
    user_columns = [
        row["name"]
        for row in db.execute("PRAGMA table_info(users)").fetchall()
    ]

    if "first_seen" not in user_columns:
        db.execute(
            "ALTER TABLE users ADD COLUMN first_seen TEXT"
        )

    # -------- versions migration --------
    version_columns = [
        row["name"]
        for row in db.execute("PRAGMA table_info(versions)").fetchall()
    ]

    if "file_type" not in version_columns:
        db.execute(
            "ALTER TABLE versions ADD COLUMN file_type TEXT DEFAULT 'document'"
        )

    if "preview_type" not in version_columns:
        db.execute(
            "ALTER TABLE versions ADD COLUMN preview_type TEXT DEFAULT 'document'"
        )

    if "downloads" not in version_columns:
        db.execute(
            "ALTER TABLE versions ADD COLUMN downloads INTEGER DEFAULT 0"
        )

    # -------- songs migration --------
    song_columns = [
        row["name"]
        for row in db.execute("PRAGMA table_info(songs)").fetchall()
    ]

    if "channel_message_id" not in song_columns:
        db.execute(
            "ALTER TABLE songs ADD COLUMN channel_message_id INTEGER"
        )

    if "created_at" not in song_columns:
        db.execute(
            "ALTER TABLE songs ADD COLUMN created_at TEXT"
        )

    # -------- download_logs migration --------
    log_columns = [
        row["name"]
        for row in db.execute("PRAGMA table_info(download_logs)").fetchall()
    ]

    if "created_at" not in log_columns:
        db.execute(
            "ALTER TABLE download_logs ADD COLUMN created_at TEXT"
        )

    db.commit()


init_db()


# =========================================================
# HELPERS
# =========================================================

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def remember_user(user_id: int):
    db.execute(
        """
        INSERT OR IGNORE INTO users(user_id, first_seen)
        VALUES (?, ?)
        """,
        (user_id, now()),
    )
    db.commit()


async def check_membership(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(
            chat_id=CHANNEL_ID,
            user_id=user_id
        )

        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }

    except Exception:
        return False


def media_from_message(message: Message):
    """
    تشخیص خودکار نوع فایل
    """

    if message.audio:
        return message.audio.file_id, "audio"

    if message.voice:
        return message.voice.file_id, "voice"

    if message.video:
        return message.video.file_id, "video"

    if message.document:
        return message.document.file_id, "document"

    return None, None


async def send_media(
    chat_id: int,
    file_id: str,
    file_type: str,
    caption: str | None = None,
    reply_markup=None,
):
    if file_type == "audio":
        return await bot.send_audio(
            chat_id=chat_id,
            audio=file_id,
            caption=caption,
            reply_markup=reply_markup,
        )

    if file_type == "voice":
        return await bot.send_voice(
            chat_id=chat_id,
            voice=file_id,
            caption=caption,
            reply_markup=reply_markup,
        )

    if file_type == "video":
        return await bot.send_video(
            chat_id=chat_id,
            video=file_id,
            caption=caption,
            reply_markup=reply_markup,
        )

    return await bot.send_document(
        chat_id=chat_id,
        document=file_id,
        caption=caption,
        reply_markup=reply_markup,
    )


def song_name(song):
    title = (song["title"] or "").strip()
    artist = (song["artist"] or "").strip()

    if title and artist:
        return f"🎵 {title} - {artist}"

    if title:
        return f"🎵 {title}"

    if artist:
        return f"🎵 {artist}"

    return "🎵 آهنگ"


def deep_link(song_id: int, version_no: int):
    return f"https://t.me/{BOT_USERNAME}?start=s{song_id}v{version_no}"


def song_caption(song_id: int):
    song = db.execute(
        "SELECT * FROM songs WHERE id = ?",
        (song_id,)
    ).fetchone()

    if not song:
        return "آهنگ پیدا نشد."

    versions = db.execute(
        """
        SELECT version_no
        FROM versions
        WHERE song_id = ?
        ORDER BY version_no
        """,
        (song_id,)
    ).fetchall()

    text = f"{song_name(song)}\n\n"
    text += f"🔢 کد: {song_id}\n\n"

    if len(versions) == 1:
        version_no = versions[0]["version_no"]
        text += f"🔗 [دانلود آهنگ کامل]({deep_link(song_id, version_no)})"
    else:
        links = []

        for version in versions:
            number = version["version_no"]
            links.append(
                f"[نسخه {number}]({deep_link(song_id, number)})"
            )

        text += " | ".join(links)

    return text


async def refresh_channel_caption(song_id: int):
    song = db.execute(
        "SELECT channel_message_id FROM songs WHERE id = ?",
        (song_id,)
    ).fetchone()

    if not song or not song["channel_message_id"]:
        return

    try:
        await bot.edit_message_caption(
            chat_id=CHANNEL_ID,
            message_id=song["channel_message_id"],
            caption=song_caption(song_id),
        )
    except Exception:
        pass


# =========================================================
# KEYBOARDS
# =========================================================

def admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ انتشار آهنگ",
                    callback_data="admin_publish",
                    style="success",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔍 جستجو",
                    callback_data="admin_search",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 آمار",
                    callback_data="admin_stats",
                    style="primary",
                ),
            ],
        ]
    )


def join_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 عضویت در کانال",
                    url=CHANNEL_URL,
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ بررسی عضویت",
                    callback_data="check_join",
                    style="success",
                )
            ],
        ]
    )


def search_result_keyboard(song_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 جایگزینی نسخه",
                    callback_data=f"replace:{song_id}",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="➕ افزودن نسخه",
                    callback_data=f"addversion:{song_id}",
                    style="success",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ ویرایش اطلاعات",
                    callback_data=f"edit:{song_id}",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="🗑 حذف آهنگ",
                    callback_data=f"delete:{song_id}",
                    style="danger",
                ),
            ],
        ]
    )


def skip_keyboard(callback_data: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏭ رد کردن",
                    callback_data=callback_data,
                )
            ]
        ]
    )


# =========================================================
# STATES
# =========================================================

class PublishStates(StatesGroup):
    title = State()
    artist = State()
    full_file = State()
    preview = State()


class ReplaceStates(StatesGroup):
    version = State()
    file = State()


class AddVersionStates(StatesGroup):
    file = State()
    preview = State()


class EditStates(StatesGroup):
    title = State()
    artist = State()


class SearchStates(StatesGroup):
    query = State()


# =========================================================
# START
# =========================================================

@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()

    user_id = message.from_user.id
    remember_user(user_id)

    args = message.text.split(maxsplit=1)

    # -----------------------------------------
    # Deep link
    # -----------------------------------------

    if len(args) > 1:
        payload = args[1].strip()

        if payload.startswith("s") and "v" in payload:
            try:
                song_part, version_part = payload[1:].split("v", 1)

                song_id = int(song_part)
                version_no = int(version_part)

                await deliver_song(
                    message,
                    song_id,
                    version_no,
                )

                return

            except (ValueError, IndexError):
                pass

    # -----------------------------------------
    # Admin
    # -----------------------------------------

    if is_admin(user_id):
        await message.answer(
            "پنل مدیریت:",
            reply_markup=admin_keyboard(),
        )
        return

    # -----------------------------------------
    # User
    # -----------------------------------------

    is_member = await check_membership(user_id)

    if is_member:
        # عضو است، فقط خوش‌آمدگویی
        await message.answer(
            "خوش اومدی.\n"
            "برای دریافت آهنگ از لینک دانلود داخل کانال استفاده کن."
        )
    else:
        await message.answer(
            "برای دریافت آهنگ کامل ابتدا عضو کانال شو.",
            reply_markup=join_keyboard(),
        )


# =========================================================
# DELIVERY
# =========================================================

async def deliver_song(
    message: Message,
    song_id: int,
    version_no: int,
):
    is_member = await check_membership(message.from_user.id)

    if not is_member:
        await message.answer(
            "برای دریافت آهنگ کامل ابتدا عضو کانال شو.",
            reply_markup=join_keyboard(),
        )
        return

    version = db.execute(
        """
        SELECT *
        FROM versions
        WHERE song_id = ? AND version_no = ?
        """,
        (song_id, version_no),
    ).fetchone()

    if not version:
        await message.answer("این آهنگ یا نسخه دیگر وجود ندارد.")
        return

    song = db.execute(
        "SELECT * FROM songs WHERE id = ?",
        (song_id,)
    ).fetchone()

    if not song:
        await message.answer("آهنگ پیدا نشد.")
        return

    # ثبت دانلود
    db.execute(
        """
        UPDATE versions
        SET downloads = downloads + 1
        WHERE id = ?
        """,
        (version["id"],)
    )

    db.execute(
        """
        INSERT INTO download_logs(
            user_id,
            song_id,
            version_no,
            created_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            message.from_user.id,
            song_id,
            version_no,
            now(),
        ),
    )

    db.commit()

    await send_media(
        chat_id=message.chat.id,
        file_id=version["file_id"],
        file_type=version["file_type"],
        caption=song_name(song),
    )


# =========================================================
# CHECK JOIN
# =========================================================

@dp.callback_query(F.data == "check_join")
async def check_join(callback: CallbackQuery):
    user_id = callback.from_user.id

    is_member = await check_membership(user_id)

    if is_member:
        await callback.message.edit_text(
            "عضویتت تأیید شد.\n"
            "حالا می‌تونی از لینک آهنگ داخل کانال استفاده کنی."
        )

        await callback.answer("عضویت تأیید شد.")

    else:
        await callback.answer(
            "هنوز عضو کانال نیستی.",
            show_alert=True,
        )


# =========================================================
# ADMIN PUBLISH
# =========================================================

@dp.callback_query(F.data == "admin_publish")
async def admin_publish(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    await state.clear()
    await state.set_state(PublishStates.title)

    await callback.message.answer(
        "نام آهنگ را بفرست.\n"
        "اگر نمی‌خواهی نام ثبت شود، رد کردن را بزن.",
        reply_markup=skip_keyboard("publish_skip_title"),
    )

    await callback.answer()


@dp.callback_query(F.data == "publish_skip_title")
async def publish_skip_title(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    await state.update_data(title="")
    await state.set_state(PublishStates.artist)

    await callback.message.answer(
        "نام خواننده را بفرست.\n"
        "اگر نمی‌خواهی ثبت شود، رد کردن را بزن.",
        reply_markup=skip_keyboard("publish_skip_artist"),
    )

    await callback.answer()


@dp.message(PublishStates.title)
async def publish_title(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.update_data(title=message.text.strip())
    await state.set_state(PublishStates.artist)

    await message.answer(
        "نام خواننده را بفرست.\n"
        "اگر نمی‌خواهی ثبت شود، رد کردن را بزن.",
        reply_markup=skip_keyboard("publish_skip_artist"),
    )


@dp.callback_query(F.data == "publish_skip_artist")
async def publish_skip_artist(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    await state.update_data(artist="")
    await state.set_state(PublishStates.full_file)

    await callback.message.answer(
        "فایل کامل آهنگ را بفرست.\n\n"
        "Audio، Voice، Video یا Document قابل قبول است."
    )

    await callback.answer()


@dp.message(PublishStates.artist)
async def publish_artist(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.update_data(artist=message.text.strip())
    await state.set_state(PublishStates.full_file)

    await message.answer(
        "حالا فایل کامل آهنگ را بفرست.\n\n"
        "Audio، Voice، Video یا Document قابل قبول است."
    )


@dp.message(PublishStates.full_file)
async def publish_full_file(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    file_id, file_type = media_from_message(message)

    if not file_id:
        await message.answer(
            "فایل نامعتبر است.\n"
            "Audio، Voice، Video یا Document بفرست."
        )
        return

    await state.update_data(
        file_id=file_id,
        file_type=file_type,
    )

    await state.set_state(PublishStates.preview)

    await message.answer(
        "حالا فایل پیش‌نمایش را بفرست.\n\n"
        "Audio، Voice، Video یا Document قابل قبول است."
    )


@dp.message(PublishStates.preview)
async def publish_preview(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    preview_id, preview_type = media_from_message(message)

    if not preview_id:
        await message.answer(
            "فایل نامعتبر است.\n"
            "Audio، Voice، Video یا Document بفرست."
        )
        return

    data = await state.get_data()

    title = data.get("title", "")
    artist = data.get("artist", "")
    file_id = data["file_id"]
    file_type = data["file_type"]

    cursor = db.execute(
        """
        INSERT INTO songs(
            title,
            artist,
            channel_message_id,
            created_at
        )
        VALUES (?, ?, NULL, ?)
        """,
        (
            title,
            artist,
            now(),
        ),
    )

    song_id = cursor.lastrowid

    db.execute(
        """
        INSERT INTO versions(
            song_id,
            version_no,
            file_id,
            file_type,
            preview_file_id,
            preview_type,
            downloads,
            created_at
        )
        VALUES (?, 1, ?, ?, ?, ?, 0, ?)
        """,
        (
            song_id,
            file_id,
            file_type,
            preview_id,
            preview_type,
            now(),
        ),
    )

    db.commit()

    try:
        sent = await send_media(
            chat_id=CHANNEL_ID,
            file_id=preview_id,
            file_type=preview_type,
            caption=song_caption(song_id),
        )

        db.execute(
            """
            UPDATE songs
            SET channel_message_id = ?
            WHERE id = ?
            """,
            (
                sent.message_id,
                song_id,
            ),
        )

        db.commit()

    except Exception as e:
        db.execute(
            "DELETE FROM songs WHERE id = ?",
            (song_id,)
        )
        db.commit()

        await message.answer(
            "انتشار در کانال انجام نشد.\n"
            "مطمئن شو ربات ادمین کانال است و دسترسی ارسال پیام دارد."
        )
        return

    await state.clear()

    await message.answer(
        f"آهنگ با موفقیت منتشر شد.\n\n"
        f"🔢 کد آهنگ: {song_id}\n"
        f"🎵 {song_name({'title': title, 'artist': artist})}",
        reply_markup=admin_keyboard(),
    )


# =========================================================
# SEARCH
# =========================================================

@dp.callback_query(F.data == "admin_search")
async def admin_search(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    await state.clear()
    await state.set_state(SearchStates.query)

    await callback.message.answer(
        "کد آهنگ، نام آهنگ یا نام خواننده را بفرست."
    )

    await callback.answer()


@dp.message(SearchStates.query)
async def search_song(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    query = message.text.strip()

    if query.isdigit():
        rows = db.execute(
            """
            SELECT *
            FROM songs
            WHERE id = ?
            """,
            (int(query),)
        ).fetchall()

    else:
        like = f"%{query}%"

        rows = db.execute(
            """
            SELECT *
            FROM songs
            WHERE title LIKE ?
               OR artist LIKE ?
            ORDER BY id DESC
            """,
            (like, like)
        ).fetchall()

    await state.clear()

    if not rows:
        await message.answer(
            "هیچ آهنگی پیدا نشد.",
            reply_markup=admin_keyboard(),
        )
        return

    for song in rows[:20]:
        versions = db.execute(
            """
            SELECT version_no, downloads
            FROM versions
            WHERE song_id = ?
            ORDER BY version_no
            """,
            (song["id"],)
        ).fetchall()

        versions_text = "\n".join(
            f"نسخه {v['version_no']} | دانلود: {v['downloads']}"
            for v in versions
        )

        text = (
            f"{song_name(song)}\n\n"
            f"🔢 کد: {song['id']}\n"
            f"📦 تعداد نسخه: {len(versions)}\n\n"
            f"{versions_text}"
        )

        await message.answer(
            text,
            reply_markup=search_result_keyboard(song["id"]),
        )


# =========================================================
# REPLACE VERSION
# =========================================================

@dp.callback_query(F.data.startswith("replace:"))
async def replace_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    song_id = int(callback.data.split(":")[1])

    versions = db.execute(
        """
        SELECT version_no
        FROM versions
        WHERE song_id = ?
        ORDER BY version_no
        """,
        (song_id,)
    ).fetchall()

    if not versions:
        await callback.answer("نسخه‌ای پیدا نشد.", show_alert=True)
        return

    version_list = ", ".join(
        str(v["version_no"])
        for v in versions
    )

    await state.clear()
    await state.update_data(song_id=song_id)
    await state.set_state(ReplaceStates.version)

    await callback.message.answer(
        f"شماره نسخه‌ای که می‌خواهی جایگزین شود را بفرست.\n\n"
        f"نسخه‌های موجود: {version_list}"
    )

    await callback.answer()


@dp.message(ReplaceStates.version)
async def replace_version_number(
    message: Message,
    state: FSMContext
):
    if not is_admin(message.from_user.id):
        return

    if not message.text.isdigit():
        await message.answer("فقط شماره نسخه را بفرست.")
        return

    version_no = int(message.text)
    data = await state.get_data()
    song_id = data["song_id"]

    exists = db.execute(
        """
        SELECT id
        FROM versions
        WHERE song_id = ? AND version_no = ?
        """,
        (song_id, version_no)
    ).fetchone()

    if not exists:
        await message.answer("این نسخه وجود ندارد.")
        return

    await state.update_data(version_no=version_no)
    await state.set_state(ReplaceStates.file)

    await message.answer(
        "فایل جدید را بفرست.\n\n"
        "Audio، Voice، Video یا Document قابل قبول است."
    )


@dp.message(ReplaceStates.file)
async def replace_file(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    file_id, file_type = media_from_message(message)

    if not file_id:
        await message.answer(
            "فایل نامعتبر است.\n"
            "Audio، Voice، Video یا Document بفرست."
        )
        return

    data = await state.get_data()

    db.execute(
        """
        UPDATE versions
        SET file_id = ?,
            file_type = ?
        WHERE song_id = ?
          AND version_no = ?
        """,
        (
            file_id,
            file_type,
            data["song_id"],
            data["version_no"],
        ),
    )

    db.commit()

    await state.clear()

    await message.answer(
        f"نسخه {data['version_no']} با موفقیت جایگزین شد.\n"
        f"کد آهنگ همچنان {data['song_id']} است.",
        reply_markup=admin_keyboard(),
    )


# =========================================================
# ADD VERSION
# =========================================================

@dp.callback_query(F.data.startswith("addversion:"))
async def add_version_start(
    callback: CallbackQuery,
    state: FSMContext
):
    if not is_admin(callback.from_user.id):
        return

    song_id = int(callback.data.split(":")[1])

    await state.clear()
    await state.update_data(song_id=song_id)
    await state.set_state(AddVersionStates.file)

    await callback.message.answer(
        "فایل کامل نسخه جدید را بفرست.\n\n"
        "Audio، Voice، Video یا Document قابل قبول است."
    )

    await callback.answer()


@dp.message(AddVersionStates.file)
async def add_version_file(
    message: Message,
    state: FSMContext
):
    if not is_admin(message.from_user.id):
        return

    file_id, file_type = media_from_message(message)

    if not file_id:
        await message.answer(
            "فایل نامعتبر است."
        )
        return

    await state.update_data(
        file_id=file_id,
        file_type=file_type,
    )

    await state.set_state(AddVersionStates.preview)

    await message.answer(
        "حالا فایل پیش‌نمایش این نسخه را بفرست."
    )


@dp.message(AddVersionStates.preview)
async def add_version_preview(
    message: Message,
    state: FSMContext
):
    if not is_admin(message.from_user.id):
        return

    preview_id, preview_type = media_from_message(message)

    if not preview_id:
        await message.answer(
            "فایل نامعتبر است."
        )
        return

    data = await state.get_data()

    song_id = data["song_id"]

    row = db.execute(
        """
        SELECT MAX(version_no) AS max_version
        FROM versions
        WHERE song_id = ?
        """,
        (song_id,)
    ).fetchone()

    version_no = (row["max_version"] or 0) + 1

    db.execute(
        """
        INSERT INTO versions(
            song_id,
            version_no,
            file_id,
            file_type,
            preview_file_id,
            preview_type,
            downloads,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            song_id,
            version_no,
            data["file_id"],
            data["file_type"],
            preview_id,
            preview_type,
            now(),
        ),
    )

    db.commit()

    # لینک‌های نسخه‌ها در پست کانال به‌روز می‌شوند
    await refresh_channel_caption(song_id)

    await state.clear()

    await message.answer(
        f"نسخه {version_no} اضافه شد.\n"
        f"کد آهنگ: {song_id}",
        reply_markup=admin_keyboard(),
    )


# =========================================================
# EDIT INFO
# =========================================================

@dp.callback_query(F.data.startswith("edit:"))
async def edit_start(
    callback: CallbackQuery,
    state: FSMContext
):
    if not is_admin(callback.from_user.id):
        return

    song_id = int(callback.data.split(":")[1])

    await state.clear()
    await state.update_data(song_id=song_id)
    await state.set_state(EditStates.title)

    await callback.message.answer(
        "نام جدید آهنگ را بفرست.\n"
        "برای خالی گذاشتن، رد کردن را بزن.",
        reply_markup=skip_keyboard("edit_skip_title"),
    )

    await callback.answer()


@dp.callback_query(F.data == "edit_skip_title")
async def edit_skip_title(
    callback: CallbackQuery,
    state: FSMContext
):
    if not is_admin(callback.from_user.id):
        return

    await state.update_data(title="")
    await state.set_state(EditStates.artist)

    await callback.message.answer(
        "نام خواننده را بفرست.\n"
        "برای خالی گذاشتن، رد کردن را بزن.",
        reply_markup=skip_keyboard("edit_skip_artist"),
    )

    await callback.answer()


@dp.message(EditStates.title)
async def edit_title(
    message: Message,
    state: FSMContext
):
    if not is_admin(message.from_user.id):
        return

    await state.update_data(title=message.text.strip())
    await state.set_state(EditStates.artist)

    await message.answer(
        "نام خواننده را بفرست.\n"
        "برای خالی گذاشتن، رد کردن را بزن.",
        reply_markup=skip_keyboard("edit_skip_artist"),
    )


@dp.callback_query(F.data == "edit_skip_artist")
async def edit_skip_artist(
    callback: CallbackQuery,
    state: FSMContext
):
    if not is_admin(callback.from_user.id):
        return

    data = await state.get_data()

    db.execute(
        """
        UPDATE songs
        SET title = ?,
            artist = ?
        WHERE id = ?
        """,
        (
            data.get("title", ""),
            "",
            data["song_id"],
        ),
    )

    db.commit()

    await refresh_channel_caption(data["song_id"])

    await state.clear()

    await callback.message.answer(
        "اطلاعات آهنگ ویرایش شد.",
        reply_markup=admin_keyboard(),
    )

    await callback.answer()


@dp.message(EditStates.artist)
async def edit_artist(
    message: Message,
    state: FSMContext
):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()

    db.execute(
        """
        UPDATE songs
        SET title = ?,
            artist = ?
        WHERE id = ?
        """,
        (
            data.get("title", ""),
            message.text.strip(),
            data["song_id"],
        ),
    )

    db.commit()

    await refresh_channel_caption(data["song_id"])

    await state.clear()

    await message.answer(
        "اطلاعات آهنگ ویرایش شد.",
        reply_markup=admin_keyboard(),
    )


# =========================================================
# DELETE
# =========================================================

@dp.callback_query(F.data.startswith("delete:"))
async def delete_song(
    callback: CallbackQuery
):
    if not is_admin(callback.from_user.id):
        return

    song_id = int(callback.data.split(":")[1])

    song = db.execute(
        """
        SELECT channel_message_id
        FROM songs
        WHERE id = ?
        """,
        (song_id,)
    ).fetchone()

    if not song:
        await callback.answer(
            "آهنگ پیدا نشد.",
            show_alert=True,
        )
        return

    # حذف پست کانال
    if song["channel_message_id"]:
        try:
            await bot.delete_message(
                chat_id=CHANNEL_ID,
                message_id=song["channel_message_id"],
            )
        except Exception:
            pass

    db.execute(
        "DELETE FROM download_logs WHERE song_id = ?",
        (song_id,)
    )

    db.execute(
        "DELETE FROM versions WHERE song_id = ?",
        (song_id,)
    )

    db.execute(
        "DELETE FROM songs WHERE id = ?",
        (song_id,)
    )

    db.commit()

    await callback.message.answer(
        f"آهنگ با کد {song_id} حذف شد.",
        reply_markup=admin_keyboard(),
    )

    await callback.answer("حذف شد.")


# =========================================================
# STATS
# =========================================================

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    songs = db.execute(
        "SELECT COUNT(*) AS c FROM songs"
    ).fetchone()["c"]

    versions = db.execute(
        "SELECT COUNT(*) AS c FROM versions"
    ).fetchone()["c"]

    users = db.execute(
        "SELECT COUNT(*) AS c FROM users"
    ).fetchone()["c"]

    downloads = db.execute(
        "SELECT COUNT(*) AS c FROM download_logs"
    ).fetchone()["c"]

    posts = db.execute(
        """
        SELECT COUNT(*)
        FROM songs
        WHERE channel_message_id IS NOT NULL
        """
    ).fetchone()[0]

    try:
        channel_members = await bot.get_chat_member_count(
            CHANNEL_ID
        )
    except Exception:
        channel_members = "نامشخص"

    text = (
        "📊 آمار ربات و کانال\n\n"
        f"🎵 تعداد آهنگ‌ها: {songs}\n"
        f"📦 تعداد نسخه‌ها: {versions}\n"
        f"👤 کاربران ثبت‌شده: {users}\n"
        f"⬇️ تعداد دانلودها: {downloads}\n"
        f"📢 تعداد پست‌های منتشرشده: {posts}\n"
        f"👥 اعضای فعلی کانال: {channel_members}\n\n"
        "نکته: تعداد بازدید تاریخی پست‌های کانال از طریق Bot API "
        "به شکل قابل اتکا در دسترس نیست."
    )

    await callback.message.answer(text)
    await callback.answer()


# =========================================================
# FALLBACK
# =========================================================

@dp.message()
async def fallback(message: Message):
    if is_admin(message.from_user.id):
        await message.answer(
            "از پنل مدیریت استفاده کن.",
            reply_markup=admin_keyboard(),
        )


# =========================================================
# RUN
# =========================================================

async def main():
    print("Bot is running...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
