import asyncio
import html
import logging
import os
import re
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import CommandStart, ChatMemberUpdatedFilter, JOIN_TRANSITION, LEAVE_TRANSITION
from aiogram.types import CallbackQuery, ChatMemberUpdated, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
CHANNEL_URL = os.getenv("CHANNEL_URL", "").strip()
DB_NAME = os.getenv("DB_NAME", "music_bot.db")
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID", "0"))
LEAVE_LOG_GROUP_ID = int(os.getenv("LEAVE_LOG_GROUP_ID", "0"))
DOWNLOAD_LOG_GROUP_ID = int(os.getenv("DOWNLOAD_LOG_GROUP_ID", "0"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is not set")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is not set")
if not BOT_USERNAME:
    raise RuntimeError("BOT_USERNAME is not set")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
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
                title TEXT,
                artist TEXT,
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
                file_type TEXT NOT NULL DEFAULT 'audio',
                preview_file_id TEXT NOT NULL,
                preview_type TEXT NOT NULL DEFAULT 'audio',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(song_id, version_no),
                FOREIGN KEY(song_id) REFERENCES songs(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS download_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                song_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(song_id) REFERENCES songs(id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id INTEGER PRIMARY KEY,
                blocked_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Migrate databases created by the older project version.
        song_cols = {r["name"] for r in conn.execute("PRAGMA table_info(songs)").fetchall()}
        if "title" not in song_cols:
            conn.execute("ALTER TABLE songs ADD COLUMN title TEXT")
        if "artist" not in song_cols:
            conn.execute("ALTER TABLE songs ADD COLUMN artist TEXT")
        if "code" not in song_cols:
            conn.execute("ALTER TABLE songs ADD COLUMN code TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_songs_code ON songs(code)")

        version_cols = {r["name"] for r in conn.execute("PRAGMA table_info(versions)").fetchall()}
        if "file_type" not in version_cols:
            conn.execute("ALTER TABLE versions ADD COLUMN file_type TEXT NOT NULL DEFAULT 'audio'")
        if "preview_type" not in version_cols:
            conn.execute("ALTER TABLE versions ADD COLUMN preview_type TEXT NOT NULL DEFAULT 'audio'")

        # Old installations used NOT NULL title/artist. SQLite cannot remove that
        # constraint in place, but existing rows already have values. New rows use
        # empty strings when a field is intentionally left blank.
        conn.commit()


IRAN_TZ = timezone(timedelta(hours=3, minutes=30))


def tehran_time(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IRAN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def song_code(song_id: int, code: str | None = None) -> str:
    return code or f"A{song_id:04d}"


def is_admin(user_id: int | None) -> bool:
    return user_id == ADMIN_ID


def button(text: str, *, callback_data: str | None = None, url: str | None = None, style: str | None = None):
    kwargs = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if style is not None:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("➕ انتشار آهنگ", callback_data="publish", style="success")],
        [button("🔍 جستجو", callback_data="search", style="primary"), button("📊 آمار", callback_data="stats", style="primary")],
        [button("📈 آمار زمانی", callback_data="stats_period", style="primary")],
        [button("📣 پیام همگانی", callback_data="broadcast", style="success")],
        [button("🚫 بلاک کاربر", callback_data="block_user", style="danger"), button("✅ آنبلاک کاربر", callback_data="unblock_user", style="success")],
        [button("💾 بکاپ آهنگ‌ها", callback_data="backup_songs", style="primary")],
    ])


def song_menu(song_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("🔄 جایگزینی نسخه", callback_data=f"replace:{song_id}", style="primary"),
         button("➕ افزودن نسخه", callback_data=f"addversion:{song_id}", style="success")],
        [button("✏️ ویرایش اطلاعات", callback_data=f"edit:{song_id}", style="primary"),
         button("🗑 حذف آهنگ", callback_data=f"delete:{song_id}", style="danger")],
        [button("⬅️ پنل مدیریت", callback_data="home")],
    ])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[button("❌ لغو", callback_data="cancel", style="danger")]])


def skip_keyboard(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("⏭ خالی بگذار", callback_data=callback_data, style="primary")],
        [button("❌ لغو", callback_data="cancel", style="danger")],
    ])


def media_from_message(message: Message) -> tuple[str, str] | None:
    if message.audio:
        return message.audio.file_id, "audio"
    if message.voice:
        return message.voice.file_id, "voice"
    if message.video:
        return message.video.file_id, "video"
    if message.document:
        return message.document.file_id, "document"
    return None


def send_media_kwargs(file_id: str, file_type: str) -> dict:
    key = {
        "audio": "audio",
        "voice": "voice",
        "video": "video",
        "document": "document",
    }.get(file_type, "document")
    return {key: file_id}


def download_link(song_id: int, version_no: int, label: str) -> str:
    return (
        f'<a href="https://t.me/{BOT_USERNAME}?start=s{song_id}v{version_no}">'
        f"{html.escape(label)}</a>"
    )


def build_links(song_id: int) -> str:
    with closing(get_db()) as conn:
        versions = conn.execute(
            "SELECT version_no FROM versions WHERE song_id = ? ORDER BY version_no",
            (song_id,),
        ).fetchall()
    if not versions:
        return ""
    if len(versions) == 1:
        return download_link(song_id, versions[0]["version_no"], "دانلود آهنگ کامل")
    return " | ".join(download_link(song_id, r["version_no"], f"نسخه {r['version_no']}") for r in versions)


def build_caption(song_id: int) -> str:
    with closing(get_db()) as conn:
        song = conn.execute("SELECT title, artist, code FROM songs WHERE id = ?", (song_id,)).fetchone()
    if not song:
        return "دانلود آهنگ کامل"

    lines = []
    title = (song["title"] or "").strip()
    artist = (song["artist"] or "").strip()
    if title:
        lines.append(f"🎵 <b>{html.escape(title)}</b>")
    if artist:
        lines.append(f"👤 {html.escape(artist)}")
    lines.append(f"code: {song_code(song_id, song['code'])}")
    if lines:
        lines.append("")
    lines.append(build_links(song_id))
    return "\n".join(lines)


async def publish_preview(song_id: int, preview_file_id: str, preview_type: str) -> int:
    kwargs = dict(chat_id=CHANNEL_ID, caption=build_caption(song_id), parse_mode="HTML")
    kwargs.update(send_media_kwargs(preview_file_id, preview_type))

    if preview_type == "audio":
        sent = await bot.send_audio(**kwargs)
    elif preview_type == "voice":
        sent = await bot.send_voice(**kwargs)
    elif preview_type == "video":
        sent = await bot.send_video(**kwargs)
    else:
        sent = await bot.send_document(**kwargs)

    with closing(get_db()) as conn:
        conn.execute("UPDATE songs SET channel_message_id = ? WHERE id = ?", (sent.message_id, song_id))
        conn.commit()
    return sent.message_id


async def update_channel_post(song_id: int):
    with closing(get_db()) as conn:
        row = conn.execute("SELECT channel_message_id FROM songs WHERE id = ?", (song_id,)).fetchone()
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
        return conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()


def get_version(song_id: int, version_no: int):
    with closing(get_db()) as conn:
        return conn.execute("SELECT * FROM versions WHERE song_id = ? AND version_no = ?", (song_id, version_no)).fetchone()


def format_song(song_id: int) -> str:
    with closing(get_db()) as conn:
        song = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        versions = conn.execute("SELECT version_no FROM versions WHERE song_id = ? ORDER BY version_no", (song_id,)).fetchall()
        downloads = conn.execute("SELECT COUNT(*) AS c FROM download_stats WHERE song_id = ?", (song_id,)).fetchone()["c"]
    if not song:
        return "آهنگ پیدا نشد."
    title = (song["title"] or "بدون نام").strip() or "بدون نام"
    artist = (song["artist"] or "بدون خواننده").strip() or "بدون خواننده"
    version_text = ", ".join(str(v["version_no"]) for v in versions) or "ندارد"
    return (
        f"🎵 <b>{html.escape(title)}</b>\n"
        f"👤 {html.escape(artist)}\n"
        f"🆔 کد: <code>{song_code(song_id, song['code'])}</code>\n"
        f"🎚 نسخه‌ها: {version_text}\n"
        f"⬇️ درخواست دانلود: {downloads}"
    )


def clear_state():
    admin_state.pop(ADMIN_ID, None)


def remember_user(user_id: int):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO users(user_id) VALUES (?) ON CONFLICT(user_id) DO UPDATE SET last_seen_at=CURRENT_TIMESTAMP",
            (user_id,),
        )
        conn.commit()


def record_download(song_id: int, version_no: int, user_id: int):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO download_stats(song_id, version_no, user_id) VALUES (?, ?, ?)",
            (song_id, version_no, user_id),
        )
        conn.commit()


def welcome_keyboard() -> InlineKeyboardMarkup:
    url = CHANNEL_URL
    if not url:
        url = f"https://t.me/{CHANNEL_URL.lstrip('@')}" if CHANNEL_URL else "https://t.me/"
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("📢 عضویت در کانال", url=url, style="primary")],
        [button("✅ بررسی عضویت", callback_data="check_membership", style="success")],
    ])


async def is_channel_member(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in {"creator", "administrator", "member"} or (
            member.status == "restricted" and getattr(member, "is_member", False)
        )
    except Exception:
        logging.exception("Membership check failed for user %s", user_id)
        return False


async def send_requested_file(chat_id: int, version):
    kwargs = dict(chat_id=chat_id)
    kwargs.update(send_media_kwargs(version["file_id"], version["file_type"]))
    if version["file_type"] == "audio":
        return await bot.send_audio(**kwargs)
    if version["file_type"] == "voice":
        return await bot.send_voice(**kwargs)
    if version["file_type"] == "video":
        return await bot.send_video(**kwargs)
    return await bot.send_document(**kwargs)


async def log_download(user_id: int, song_id: int, version_no: int):
    try:
        try:
            chat = await bot.get_chat(user_id)
            full_name = " ".join(p for p in (chat.first_name, chat.last_name) if p) or "بدون نام"
            username = f"@{chat.username}" if chat.username else "ندارد"
        except Exception:
            full_name, username = "نامشخص", "نامشخص"

        with closing(get_db()) as conn:
            song = conn.execute("SELECT title, artist, code FROM songs WHERE id = ?", (song_id,)).fetchone()
        code = song_code(song_id, song["code"] if song else None)
        title = ((song["title"] if song else "") or "").strip() or "بدون عنوان"
        artist = ((song["artist"] if song else "") or "").strip()
        now = tehran_time()

        logging.info("Download | user_id=%s | username=%s | song=%s | version=%s", user_id, username, code, version_no)
        await bot.send_message(
            DOWNLOAD_LOG_GROUP_ID or LOG_GROUP_ID or ADMIN_ID,
            f"⬇️ <b>دانلود آهنگ</b>\n\n"
            f"🎵 آهنگ: {html.escape(title)}\n"
            + (f"🎤 خواننده: {html.escape(artist)}\n" if artist else "")
            + f"🔖 کد: {code} | نسخه {version_no}\n\n"
            f"👤 نام: {html.escape(full_name)}\n"
            f"🔗 یوزرنیم: {html.escape(username)}\n"
            f"🆔 آیدی: <code>{user_id}</code>\n"
            f"🕒 زمان: {now}",
            parse_mode="HTML",
        )
    except Exception:
        logging.exception("Could not send download log")


async def deliver_pending_song(user_id: int, chat_id: int, song_id: int, version_no: int) -> bool:
    version = get_version(song_id, version_no)
    if not version:
        await bot.send_message(chat_id, "این نسخه دیگر وجود ندارد.")
        return True
    if not await is_channel_member(user_id):
        admin_state[user_id] = {
            "action": "pending_download",
            "song_id": song_id,
            "version_no": version_no,
        }
        await bot.send_message(
            chat_id,
            "برای دریافت فایل کامل، ابتدا عضو کانال شوید و بعد «بررسی عضویت» را بزنید.",
            reply_markup=welcome_keyboard(),
        )
        return False
    await send_requested_file(chat_id, version)
    record_download(song_id, version_no, user_id)
    await log_download(user_id, song_id, version_no)
    return True


@dp.message(CommandStart())
async def start_handler(message: Message):
    remember_user(message.from_user.id)
    if not is_admin(message.from_user.id) and is_blocked(message.from_user.id):
        await message.answer("دسترسی شما به ربات محدود شده است.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) == 1:
        if is_admin(message.from_user.id):
            await message.answer("پنل مدیریت:", reply_markup=admin_menu())
        else:
            await message.answer(
                "🎵 خوش اومدی!\n\nبرای دریافت آهنگ کامل، ابتدا عضو کانال شو و بعد بررسی عضویت را بزن.",
                reply_markup=welcome_keyboard(),
            )
        return

    payload = parts[1].strip()
    match = re.fullmatch(r"s(\d+)v(\d+)", payload)
    if not match:
        return

    song_id = int(match.group(1))
    version_no = int(match.group(2))
    await deliver_pending_song(message.from_user.id, message.chat.id, song_id, version_no)


@dp.callback_query(F.data == "check_membership")
async def check_membership_callback(callback: CallbackQuery):
    remember_user(callback.from_user.id)
    if not is_admin(callback.from_user.id) and is_blocked(callback.from_user.id):
        await callback.answer("دسترسی شما به ربات محدود شده است.", show_alert=True)
        return
    if not await is_channel_member(callback.from_user.id):
        await callback.answer("هنوز عضویت شما تأیید نشد.", show_alert=True)
        return

    await callback.answer("عضویت تأیید شد.")
    # A pending deep-link is stored per user when needed.
    pending = admin_state.get(callback.from_user.id)
    if pending and pending.get("action") == "pending_download":
        song_id = pending["song_id"]
        version_no = pending["version_no"]
        admin_state.pop(callback.from_user.id, None)
        await deliver_pending_song(callback.from_user.id, callback.from_user.id, song_id, version_no)
    else:
        await callback.message.answer("عضویت شما تأیید شد. حالا از لینک آهنگ وارد شوید.")


@dp.callback_query(F.data == "home")
async def home_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    clear_state()
    await callback.message.answer("پنل مدیریت:", reply_markup=admin_menu())
    await callback.answer()


@dp.callback_query(F.data == "cancel")
async def cancel_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    clear_state()
    await callback.message.answer("لغو شد.", reply_markup=admin_menu())
    await callback.answer()


@dp.callback_query(F.data == "publish")
async def publish_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    admin_state[ADMIN_ID] = {"action": "publish", "step": "title", "data": {}}
    await callback.message.answer("نام آهنگ را ارسال کن یا «خالی بگذار» را بزن:", reply_markup=skip_keyboard("publish_skip_title"))
    await callback.answer()


@dp.callback_query(F.data == "publish_skip_title")
async def publish_skip_title(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    state = admin_state.get(ADMIN_ID)
    if not state or state.get("action") != "publish":
        return
    state["data"]["title"] = ""
    state["step"] = "artist"
    await callback.message.answer("نام خواننده را ارسال کن یا «خالی بگذار» را بزن:", reply_markup=skip_keyboard("publish_skip_artist"))
    await callback.answer()


@dp.callback_query(F.data == "publish_skip_artist")
async def publish_skip_artist(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    state = admin_state.get(ADMIN_ID)
    if not state or state.get("action") != "publish":
        return
    state["data"]["artist"] = ""
    state["step"] = "full"
    await callback.message.answer("فایل کامل را ارسال کن. Audio، Voice، Video یا Document قابل قبول است:", reply_markup=cancel_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "search")
async def search_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    admin_state[ADMIN_ID] = {"action": "search", "step": "query", "data": {}}
    await callback.message.answer("کد آهنگ یا نام آهنگ / خواننده را ارسال کن:", reply_markup=cancel_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "stats")
async def stats_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    with closing(get_db()) as conn:
        songs = conn.execute("SELECT COUNT(*) AS c FROM songs").fetchone()["c"]
        versions = conn.execute("SELECT COUNT(*) AS c FROM versions").fetchone()["c"]
        users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        downloads = conn.execute("SELECT COUNT(*) AS c FROM download_stats").fetchone()["c"]
        posts = conn.execute("SELECT COUNT(*) AS c FROM songs WHERE channel_message_id IS NOT NULL").fetchone()["c"]
        top = conn.execute("""
            SELECT s.id, COALESCE(NULLIF(s.title, ''), 'بدون نام') AS title,
                   COUNT(d.id) AS downloads
            FROM songs s LEFT JOIN download_stats d ON d.song_id = s.id
            GROUP BY s.id ORDER BY downloads DESC, s.id DESC LIMIT 5
        """).fetchall()

    try:
        channel_members = await bot.get_chat_member_count(CHANNEL_ID)
        channel_line = f"👥 اعضای فعلی کانال: {channel_members}"
    except Exception:
        channel_line = "👥 اعضای کانال: قابل دریافت نیست"

    top_text = ""
    if top:
        top_text = "\n\n🔥 بیشترین درخواست دانلود:\n" + "\n".join(
            f"{i}. {html.escape(r['title'])} | {r['downloads']}" for i, r in enumerate(top, 1)
        )

    await callback.message.answer(
        f"📊 <b>آمار ربات و کانال</b>\n\n"
        f"🎵 آهنگ‌ها: {songs}\n"
        f"🎚 نسخه‌ها: {versions}\n"
        f"👤 کاربران ربات: {users}\n"
        f"⬇️ درخواست‌های دانلود: {downloads}\n"
        f"📢 پست‌های منتشرشده: {posts}\n"
        f"{channel_line}"
        f"{top_text}",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )
    await callback.answer()


async def show_search_results(message: Message, query: str):
    query = query.strip()
    with closing(get_db()) as conn:
        code_query = query.upper()
        if re.fullmatch(r"[A-Z][0-9]+", code_query):
            rows = conn.execute(
                "SELECT id, title, artist FROM songs WHERE code = ? OR (code IS NULL AND 'A' || printf('%04d', id) = ?) LIMIT 10",
                (code_query, code_query),
            ).fetchall()
        elif query.isdigit():
            rows = conn.execute("SELECT id, title, artist FROM songs WHERE id = ? LIMIT 10", (int(query),)).fetchall()
        else:
            like = f"%{query}%"
            rows = conn.execute("""
                SELECT id, title, artist FROM songs
                WHERE COALESCE(title, '') LIKE ? OR COALESCE(artist, '') LIKE ?
                ORDER BY id DESC LIMIT 10
            """, (like, like)).fetchall()
    if not rows:
        await message.answer("موردی پیدا نشد.", reply_markup=admin_menu())
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        button(f"{row['title'] or 'بدون نام'} | {row['artist'] or 'بدون خواننده'}", callback_data=f"song:{row['id']}", style="primary")
    ] for row in rows])
    await message.answer("نتایج جستجو:", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("song:"))
async def song_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    song_id = int(callback.data.split(":")[1])
    if not get_song(song_id):
        await callback.message.answer("آهنگ پیدا نشد.")
        await callback.answer()
        return
    await callback.message.answer(format_song(song_id), reply_markup=song_menu(song_id), parse_mode="HTML")
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
    admin_state[ADMIN_ID] = {"action": "replace", "step": "version", "data": {"song_id": song_id}}
    await callback.message.answer("شماره نسخه‌ای که می‌خواهی فایل کاملش عوض شود را ارسال کن:", reply_markup=cancel_keyboard())
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
        row = conn.execute("SELECT COALESCE(MAX(version_no), 0) AS max_version FROM versions WHERE song_id = ?", (song_id,)).fetchone()
    next_version = row["max_version"] + 1
    admin_state[ADMIN_ID] = {"action": "add_version", "step": "full", "data": {"song_id": song_id, "version_no": next_version}}
    await callback.message.answer(f"نسخه {next_version}: فایل کامل را ارسال کن. Audio، Voice، Video یا Document:", reply_markup=cancel_keyboard())
    await callback.answer()


@dp.callback_query(F.data.startswith("edit:"))
async def edit_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    song_id = int(callback.data.split(":")[1])
    song = get_song(song_id)
    if not song:
        await callback.message.answer("آهنگ پیدا نشد.")
        await callback.answer()
        return
    admin_state[ADMIN_ID] = {"action": "edit", "step": "title", "data": {"song_id": song_id}}
    await callback.message.answer("نام جدید آهنگ را ارسال کن یا خالی بگذار:", reply_markup=skip_keyboard("edit_skip_title"))
    await callback.answer()


@dp.callback_query(F.data == "edit_skip_title")
async def edit_skip_title(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    state = admin_state.get(ADMIN_ID)
    if not state or state.get("action") != "edit":
        return
    song_id = state["data"]["song_id"]
    song = get_song(song_id)
    state["data"]["title"] = song["title"] if song else ""
    state["step"] = "artist"
    await callback.message.answer("نام جدید خواننده را ارسال کن یا خالی بگذار:", reply_markup=skip_keyboard("edit_skip_artist"))
    await callback.answer()


@dp.callback_query(F.data == "edit_skip_artist")
async def edit_skip_artist(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    state = admin_state.get(ADMIN_ID)
    if not state or state.get("action") != "edit":
        return
    song_id = state["data"]["song_id"]
    state["data"]["artist"] = ""
    with closing(get_db()) as conn:
        conn.execute("UPDATE songs SET title = ?, artist = ? WHERE id = ?", (state["data"].get("title", ""), "", song_id))
        conn.commit()
    clear_state()
    await update_channel_post(song_id)
    await callback.message.answer("اطلاعات آهنگ به‌روزرسانی شد.", reply_markup=song_menu(song_id))
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
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [button("بله، حذف شود", callback_data=f"confirmdelete:{song_id}", style="danger"), button("لغو", callback_data=f"song:{song_id}")]
    ])
    await callback.message.answer("این آهنگ، نسخه‌ها و آمار دانلودش حذف می‌شوند. مطمئنی؟", reply_markup=keyboard)
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
            await bot.delete_message(chat_id=CHANNEL_ID, message_id=song["channel_message_id"])
        except Exception:
            logging.exception("Could not delete channel message")
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM songs WHERE id = ?", (song_id,))
        conn.commit()
    await callback.message.answer("آهنگ حذف شد.", reply_markup=admin_menu())
    await callback.answer()


@dp.message(F.from_user.id == ADMIN_ID)
async def admin_message_handler(message: Message):
    state = admin_state.get(ADMIN_ID)
    if not state:
        await message.answer("پنل مدیریت:", reply_markup=admin_menu())
        return

    action = state["action"]
    step = state["step"]
    data = state["data"]

    if action == "broadcast" and step == "message":
        data["from_chat_id"] = message.chat.id
        data["message_id"] = message.message_id
        state["step"] = "confirm"
        total = len(broadcast_recipients())
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [button(f"✅ ارسال برای {total} نفر", callback_data="broadcast_confirm", style="success")],
            [button("❌ لغو", callback_data="cancel", style="danger")],
        ])
        await message.answer("پیام بالا برای همه کاربران ارسال می‌شود. تأیید می‌کنی؟", reply_markup=keyboard)
        return

    if action in ("block", "unblock") and step == "user":
        text = (message.text or "").strip()
        if not text.isdigit():
            await message.answer("آیدی عددی کاربر را به‌صورت عدد ارسال کن.")
            return
        target = int(text)
        if target == ADMIN_ID:
            await message.answer("نمی‌توانی ادمین را بلاک کنی.")
            return
        clear_state()
        with closing(get_db()) as conn:
            if action == "block":
                conn.execute("INSERT OR IGNORE INTO blocked_users(user_id) VALUES (?)", (target,))
                result = f"کاربر <code>{target}</code> بلاک شد."
            else:
                cur = conn.execute("DELETE FROM blocked_users WHERE user_id = ?", (target,))
                result = f"کاربر <code>{target}</code> آنبلاک شد." if cur.rowcount else "این کاربر در لیست بلاک نبود."
            conn.commit()
        await message.answer(result, parse_mode="HTML", reply_markup=admin_menu())
        return

    if action == "search" and step == "query":
        clear_state()
        await show_search_results(message, message.text or "")
        return

    if action == "publish":
        if step == "title":
            if not message.text or not message.text.strip():
                await message.answer("نام را به‌صورت متن بفرست یا از «خالی بگذار» استفاده کن.")
                return
            data["title"] = message.text.strip()
            state["step"] = "artist"
            await message.answer("نام خواننده را ارسال کن یا خالی بگذار:", reply_markup=skip_keyboard("publish_skip_artist"))
            return
        if step == "artist":
            if not message.text or not message.text.strip():
                await message.answer("نام را به‌صورت متن بفرست یا از «خالی بگذار» استفاده کن.")
                return
            data["artist"] = message.text.strip()
            state["step"] = "full"
            await message.answer("فایل کامل را ارسال کن. Audio، Voice، Video یا Document:")
            return
        if step == "full":
            media = media_from_message(message)
            if not media:
                await message.answer("فایل کامل را به‌صورت Audio، Voice، Video یا Document ارسال کن.")
                return
            data["full_file_id"], data["full_file_type"] = media
            state["step"] = "preview"
            await message.answer("فایل نمایشی کانال را ارسال کن. Audio، Voice، Video یا Document:")
            return
        if step == "preview":
            media = media_from_message(message)
            if not media:
                await message.answer("فایل نمایشی را به‌صورت Audio، Voice، Video یا Document ارسال کن.")
                return
            data["preview_id"], data["preview_type"] = media
            state["step"] = "code"
            await message.answer(
                "کد آهنگ را بفرست. فقط یک حرف بزرگ انگلیسی و بعدش عدد، مثل A25 یا B0007:",
                reply_markup=cancel_keyboard(),
            )
            return
        if step == "code":
            code = (message.text or "").strip()
            if not re.fullmatch(r"[A-Z][0-9]+", code):
                await message.answer("کد نامعتبر است. فقط یک حرف بزرگ انگلیسی و بعدش عدد بنویس، مثل A25 یا B0007.")
                return
            preview_id, preview_type = data["preview_id"], data["preview_type"]
            try:
                with closing(get_db()) as conn:
                    if conn.execute("SELECT 1 FROM songs WHERE code = ?", (code,)).fetchone():
                        raise sqlite3.IntegrityError("duplicate code")
                    cur = conn.execute("INSERT INTO songs(title, artist, code) VALUES (?, ?, ?)", (data.get("title", ""), data.get("artist", ""), code))
                    song_id = cur.lastrowid
                    conn.execute("""
                        INSERT INTO versions(song_id, version_no, file_id, file_type, preview_file_id, preview_type)
                        VALUES (?, 1, ?, ?, ?, ?)
                    """, (song_id, data["full_file_id"], data["full_file_type"], preview_id, preview_type))
                    conn.commit()
            except sqlite3.IntegrityError:
                await message.answer("این کد قبلاً برای آهنگ دیگری استفاده شده. کد دیگری بفرست.")
                return
            clear_state()
            try:
                await publish_preview(song_id, preview_id, preview_type)
            except Exception:
                with closing(get_db()) as conn:
                    conn.execute("DELETE FROM songs WHERE id = ?", (song_id,))
                    conn.commit()
                logging.exception("Publishing failed")
                await message.answer("انتشار در کانال شکست خورد. دسترسی ادمین ربات به کانال را بررسی کن.", reply_markup=admin_menu())
                return
            await message.answer(f"آهنگ منتشر شد.\n\nکد آهنگ: <code>{code}</code>", parse_mode="HTML", reply_markup=song_menu(song_id))
            return

    if action == "replace":
        if step == "version":
            if not message.text or not message.text.strip().isdigit():
                await message.answer("شماره نسخه را به‌صورت عدد ارسال کن.")
                return
            version_no = int(message.text.strip())
            song_id = data["song_id"]
            if not get_version(song_id, version_no):
                await message.answer("این نسخه وجود ندارد.")
                return
            data["version_no"] = version_no
            state["step"] = "file"
            await message.answer("فایل کامل جدید را ارسال کن. Audio، Voice، Video یا Document:")
            return
        if step == "file":
            media = media_from_message(message)
            if not media:
                await message.answer("فایل را به‌صورت Audio، Voice، Video یا Document ارسال کن.")
                return
            file_id, file_type = media
            with closing(get_db()) as conn:
                conn.execute("UPDATE versions SET file_id = ?, file_type = ? WHERE song_id = ? AND version_no = ?", (file_id, file_type, data["song_id"], data["version_no"]))
                conn.commit()
            song_id, version_no = data["song_id"], data["version_no"]
            clear_state()
            await message.answer(f"فایل نسخه {version_no} جایگزین شد. لینک قبلی همچنان معتبر است.", reply_markup=song_menu(song_id))
            return

    if action == "add_version":
        song_id = data["song_id"]
        version_no = data["version_no"]
        if step == "full":
            media = media_from_message(message)
            if not media:
                await message.answer("فایل کامل را ارسال کن.")
                return
            data["full_file_id"], data["full_file_type"] = media
            state["step"] = "preview"
            await message.answer(f"فایل نمایشی نسخه {version_no} را ارسال کن:")
            return
        if step == "preview":
            media = media_from_message(message)
            if not media:
                await message.answer("فایل نمایشی را ارسال کن.")
                return
            preview_id, preview_type = media
            with closing(get_db()) as conn:
                conn.execute("""
                    INSERT INTO versions(song_id, version_no, file_id, file_type, preview_file_id, preview_type)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (song_id, version_no, data["full_file_id"], data["full_file_type"], preview_id, preview_type))
                conn.commit()
            clear_state()
            await update_channel_post(song_id)
            await message.answer(f"نسخه {version_no} اضافه شد و لینک‌های پست کانال به‌روزرسانی شدند.", reply_markup=song_menu(song_id))
            return

    if action == "edit":
        song_id = data["song_id"]
        if step == "title":
            if not message.text or not message.text.strip():
                await message.answer("نام را به‌صورت متن بفرست یا «خالی بگذار» را بزن.")
                return
            data["title"] = message.text.strip()
            state["step"] = "artist"
            await message.answer("نام جدید خواننده را ارسال کن یا خالی بگذار:", reply_markup=skip_keyboard("edit_skip_artist"))
            return
        if step == "artist":
            if not message.text or not message.text.strip():
                await message.answer("نام را به‌صورت متن بفرست یا «خالی بگذار» را بزن.")
                return
            data["artist"] = message.text.strip()
            with closing(get_db()) as conn:
                conn.execute("UPDATE songs SET title = ?, artist = ? WHERE id = ?", (data["title"], data["artist"], song_id))
                conn.commit()
            clear_state()
            await update_channel_post(song_id)
            await message.answer("اطلاعات آهنگ و پست کانال به‌روزرسانی شد.", reply_markup=song_menu(song_id))
            return


@dp.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def channel_join_log_handler(event: ChatMemberUpdated):
    if event.chat.id != CHANNEL_ID:
        return
    user = event.new_chat_member.user
    if user.is_bot:
        return

    full_name = " ".join(p for p in (user.first_name, user.last_name) if p) or "بدون نام"
    username = f"@{user.username}" if user.username else "ندارد"
    invite = getattr(event, "invite_link", None)
    if invite:
        invite_text = invite.name or invite.invite_link
    else:
        invite_text = "نامشخص"
    now = tehran_time(event.date)

    record_channel_event(user.id, "join")
    logging.info("Channel join | user_id=%s | username=%s | name=%s | invite=%s", user.id, username, full_name, invite_text)
    try:
        await bot.send_message(
            LOG_GROUP_ID or ADMIN_ID,
            f"✅ <b>عضو جدید در کانال</b>\n\n"
            f"👤 نام: {html.escape(full_name)}\n"
            f"🔗 یوزرنیم: {html.escape(username)}\n"
            f"🆔 آیدی: <code>{user.id}</code>\n"
            f"📨 لینک دعوت: {html.escape(str(invite_text))}\n"
            f"🕒 زمان: {now}",
            parse_mode="HTML",
        )
    except Exception:
        logging.exception("Could not send join log")


def is_blocked(user_id: int) -> bool:
    with closing(get_db()) as conn:
        return conn.execute("SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,)).fetchone() is not None


def record_channel_event(user_id: int, event: str):
    with closing(get_db()) as conn:
        conn.execute("INSERT INTO channel_events(user_id, event) VALUES (?, ?)", (user_id, event))
        conn.commit()


def broadcast_recipients() -> list[int]:
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE user_id != ? AND user_id NOT IN (SELECT user_id FROM blocked_users)",
            (ADMIN_ID,),
        ).fetchall()
    return [r["user_id"] for r in rows]


@dp.chat_member(ChatMemberUpdatedFilter(LEAVE_TRANSITION))
async def channel_leave_log_handler(event: ChatMemberUpdated):
    if event.chat.id != CHANNEL_ID:
        return
    user = event.new_chat_member.user
    if user.is_bot:
        return

    full_name = " ".join(p for p in (user.first_name, user.last_name) if p) or "بدون نام"
    username = f"@{user.username}" if user.username else "ندارد"
    banned = event.new_chat_member.status == "kicked"
    title = "⛔️ <b>کاربر از کانال بن شد</b>" if banned else "🚪 <b>خروج از کانال</b>"
    now = tehran_time(event.date)

    record_channel_event(user.id, "leave")
    logging.info("Channel leave | user_id=%s | username=%s | name=%s | banned=%s", user.id, username, full_name, banned)
    try:
        await bot.send_message(
            LEAVE_LOG_GROUP_ID or LOG_GROUP_ID or ADMIN_ID,
            f"{title}\n\n"
            f"👤 نام: {html.escape(full_name)}\n"
            f"🔗 یوزرنیم: {html.escape(username)}\n"
            f"🆔 آیدی: <code>{user.id}</code>\n"
            f"🕒 زمان: {now}",
            parse_mode="HTML",
        )
    except Exception:
        logging.exception("Could not send leave log")


@dp.callback_query(F.data == "stats_period")
async def stats_period_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    def period(days: int) -> dict:
        since = f"-{days} days"
        with closing(get_db()) as conn:
            return {
                "users": conn.execute("SELECT COUNT(*) FROM users WHERE first_seen_at >= datetime('now', ?)", (since,)).fetchone()[0],
                "downloads": conn.execute("SELECT COUNT(*) FROM download_stats WHERE created_at >= datetime('now', ?)", (since,)).fetchone()[0],
                "joins": conn.execute("SELECT COUNT(*) FROM channel_events WHERE event = 'join' AND created_at >= datetime('now', ?)", (since,)).fetchone()[0],
                "leaves": conn.execute("SELECT COUNT(*) FROM channel_events WHERE event = 'leave' AND created_at >= datetime('now', ?)", (since,)).fetchone()[0],
            }

    day, week = period(1), period(7)
    with closing(get_db()) as conn:
        blocked = conn.execute("SELECT COUNT(*) FROM blocked_users").fetchone()[0]

    def block(title: str, d: dict) -> str:
        return (
            f"<b>{title}</b>\n"
            f"👤 کاربران جدید ربات: {d['users']}\n"
            f"⬇️ درخواست‌های دانلود: {d['downloads']}\n"
            f"✅ ورود به کانال: {d['joins']}\n"
            f"🚪 خروج از کانال: {d['leaves']}\n"
            f"📊 تغییر خالص اعضا: {d['joins'] - d['leaves']:+d}"
        )

    await callback.message.answer(
        f"📈 <b>آمار زمانی</b>\n\n{block('۲۴ ساعت اخیر', day)}\n\n{block('۷ روز اخیر', week)}\n\n🚫 کاربران بلاک‌شده: {blocked}",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )
    await callback.answer()


@dp.callback_query(F.data == "broadcast")
async def broadcast_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    admin_state[ADMIN_ID] = {"action": "broadcast", "step": "message", "data": {}}
    await callback.message.answer("پیامی که می‌خواهی برای همه کاربران ارسال شود را بفرست (متن، عکس، فایل و ...):", reply_markup=cancel_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "broadcast_confirm")
async def broadcast_confirm_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    state = admin_state.get(ADMIN_ID)
    if not state or state.get("action") != "broadcast" or state.get("step") != "confirm":
        await callback.answer("درخواستی برای ارسال وجود ندارد.", show_alert=True)
        return
    from_chat_id = state["data"]["from_chat_id"]
    message_id = state["data"]["message_id"]
    clear_state()
    await callback.answer("ارسال شروع شد.")
    await callback.message.answer("⏳ ارسال شروع شد. بعد از پایان گزارش می‌دهم.")

    recipients = broadcast_recipients()
    sent = failed = 0
    for uid in recipients:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=from_chat_id, message_id=message_id)
            sent += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=from_chat_id, message_id=message_id)
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    logging.info("Broadcast finished | total=%s | sent=%s | failed=%s", len(recipients), sent, failed)
    await callback.message.answer(
        f"📣 <b>گزارش پیام همگانی</b>\n\n👥 کل: {len(recipients)}\n✅ موفق: {sent}\n❌ ناموفق: {failed}",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )


@dp.callback_query(F.data == "block_user")
async def block_user_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    admin_state[ADMIN_ID] = {"action": "block", "step": "user", "data": {}}
    await callback.message.answer("آیدی عددی کاربری که می‌خواهی بلاک شود را ارسال کن:", reply_markup=cancel_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "unblock_user")
async def unblock_user_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    admin_state[ADMIN_ID] = {"action": "unblock", "step": "user", "data": {}}
    await callback.message.answer("آیدی عددی کاربری که می‌خواهی آنبلاک شود را ارسال کن:", reply_markup=cancel_keyboard())
    await callback.answer()


backup_lock = asyncio.Lock()
BACKUP_PART_LIMIT = 45 * 1024 * 1024
BACKUP_DEFAULT_EXT = {"audio": ".mp3", "voice": ".ogg", "video": ".mp4", "document": ""}


def _backup_safe_name(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", text or "").strip()[:60]


def _backup_write_zip(zip_path: str, files: list[tuple[str, str]]):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for path, arcname in files:
            zf.write(path, arcname)


@dp.callback_query(F.data == "backup_songs")
async def backup_songs_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    if backup_lock.locked():
        await callback.answer("بکاپ در حال انجام است.", show_alert=True)
        return
    await callback.answer()

    async with backup_lock:
        with closing(get_db()) as conn:
            rows = conn.execute("""
                SELECT v.song_id, v.version_no, v.file_id, v.file_type, s.title, s.artist, s.code
                FROM versions v JOIN songs s ON s.id = v.song_id
                ORDER BY v.song_id, v.version_no
            """).fetchall()
        if not rows:
            await callback.message.answer("هنوز آهنگی ثبت نشده است.", reply_markup=admin_menu())
            return
        await callback.message.answer(f"⏳ بکاپ {len(rows)} فایل شروع شد. بعد از آماده شدن، فایل‌های zip پشت‌سرهم ارسال می‌شوند.")

        counts: dict[int, int] = {}
        for r in rows:
            counts[r["song_id"]] = counts.get(r["song_id"], 0) + 1

        date_tag = tehran_time()[:10]
        used_names: set[str] = set()
        batch: list[tuple[str, str]] = []
        batch_size = 0
        part_no = 0
        saved = 0
        failed: list[str] = []
        send_failed: list[int] = []

        with tempfile.TemporaryDirectory() as tmp:

            async def flush():
                nonlocal batch, batch_size, part_no
                if not batch:
                    return
                part_no += 1
                current_part = part_no
                files = batch
                batch, batch_size = [], 0
                zip_path = os.path.join(tmp, f"backup_{date_tag}_part{current_part}.zip")
                await asyncio.to_thread(_backup_write_zip, zip_path, files)
                for path, _ in files:
                    os.remove(path)
                sent = False
                for _attempt in range(2):
                    try:
                        await bot.send_document(
                            ADMIN_ID,
                            FSInputFile(zip_path),
                            caption=f"💾 بکاپ آهنگ‌ها - قسمت {current_part} ({len(files)} فایل)",
                            request_timeout=900,
                        )
                        sent = True
                        break
                    except TelegramRetryAfter as e:
                        await asyncio.sleep(e.retry_after)
                    except Exception:
                        logging.exception("Could not send backup part %s", current_part)
                        break
                if not sent:
                    send_failed.append(current_part)
                os.remove(zip_path)

            for i, r in enumerate(rows):
                code = song_code(r["song_id"], r["code"])
                label = f"{code} (نسخه {r['version_no']})"
                try:
                    tg_file = await bot.get_file(r["file_id"])
                    ext = os.path.splitext(tg_file.file_path or "")[1] or BACKUP_DEFAULT_EXT.get(r["file_type"], "")
                    name = " - ".join(p for p in (code, _backup_safe_name(r["title"]), _backup_safe_name(r["artist"])) if p)
                    if counts[r["song_id"]] > 1:
                        name += f" (v{r['version_no']})"
                    arcname = name + ext
                    n = 2
                    while arcname.lower() in used_names:
                        arcname = f"{name} ({n}){ext}"
                        n += 1
                    local = os.path.join(tmp, f"{i}{ext}")
                    await bot.download_file(tg_file.file_path, destination=local, timeout=300)
                    size = os.path.getsize(local)
                except Exception:
                    logging.exception("Backup download failed for %s", label)
                    failed.append(label)
                    continue
                used_names.add(arcname.lower())
                if batch and batch_size + size > BACKUP_PART_LIMIT:
                    await flush()
                batch.append((local, arcname))
                batch_size += size
                saved += 1
            await flush()

        report = (
            f"💾 <b>گزارش بکاپ</b>\n\n"
            f"✅ ذخیره‌شده: {saved}\n"
            f"❌ ناموفق: {len(failed)}\n"
            f"📦 تعداد فایل zip: {part_no}"
        )
        if failed:
            report += "\n\nناموفق‌ها (معمولاً فایل‌های بالای ۲۰ مگابایت):\n" + "\n".join(failed[:30])
            if len(failed) > 30:
                report += f"\nو {len(failed) - 30} مورد دیگر"
        if send_failed:
            report += "\n\n⚠️ ارسال این قسمت‌ها ناموفق بود: " + ", ".join(str(n) for n in send_failed)
        await callback.message.answer(report, parse_mode="HTML", reply_markup=admin_menu())


async def main():
    init_db()
    logging.info("Music bot started")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
