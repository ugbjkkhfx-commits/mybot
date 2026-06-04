"""
TikTok Profile Manager Bot
Features:
  - Instant profile registration (no pre-scan)
  - Streaming batch download (20 videos at a time)
  - Full profile clone (streaming)
  - User tracking (SQLite)
  - Admin broadcast (any message type to all users)
  - Admin stats dashboard

Environment variables:
  TELEGRAM_BOT_TOKEN  - required
  ADMIN_ID            - Telegram user-id of the admin
"""

import os
import re
import logging
import threading
import sqlite3
import time
from contextlib import contextmanager

import telebot
from telebot import types
import yt_dlp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID: int = int(os.environ.get("ADMIN_ID", "1520960859"))

DOWNLOAD_DIR = "downloads"
DB_PATH = "users.db"
BATCH_SIZE = 20

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

YT_DLP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━ DATABASE ━━━━━━━━━━━━━━━━━━━━━━━━

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


@contextmanager
def db():
    conn = _conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY,
                username   TEXT    DEFAULT '',
                first_name TEXT    DEFAULT '',
                first_seen TEXT    DEFAULT (datetime('now')),
                last_seen  TEXT    DEFAULT (datetime('now')),
                blocked    INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS broadcasts (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at   TEXT    DEFAULT (datetime('now')),
                message   TEXT    DEFAULT '',
                delivered INTEGER DEFAULT 0,
                blocked   INTEGER DEFAULT 0,
                failed    INTEGER DEFAULT 0
            );
        """)


def register_user(user: types.User) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users (id, username, first_name)
            VALUES (:id, :u, :n)
            ON CONFLICT(id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name,
                last_seen  = datetime('now'),
                blocked    = 0
            """,
            {"id": user.id, "u": user.username or "", "n": user.first_name or ""},
        )


def mark_blocked(uid: int) -> None:
    with db() as conn:
        conn.execute("UPDATE users SET blocked = 1 WHERE id = ?", (uid,))


def all_active_ids() -> list:
    with db() as conn:
        rows = conn.execute(
            "SELECT id FROM users WHERE blocked = 0"
        ).fetchall()
    return [r["id"] for r in rows]


def get_stats() -> dict:
    with db() as conn:
        total   = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        blocked = conn.execute(
            "SELECT COUNT(*) FROM users WHERE blocked = 1"
        ).fetchone()[0]
        last_bc = conn.execute(
            "SELECT sent_at, delivered, blocked, failed "
            "FROM broadcasts ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {
        "total":   total,
        "active":  total - blocked,
        "blocked": blocked,
        "last_bc": dict(last_bc) if last_bc else None,
    }


def save_broadcast(msg: str, delivered: int, blocked: int, failed: int) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO broadcasts (message, delivered, blocked, failed) "
            "VALUES (?, ?, ?, ?)",
            (msg[:200], delivered, blocked, failed),
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━ SESSIONS ━━━━━━━━━━━━━━━━━━━━━━━━

sessions: dict = {}


def get_session(chat_id: int) -> dict:
    if chat_id not in sessions:
        sessions[chat_id] = {
            "profile_url":   None,
            "offset":        0,
            "state":         "waiting_profile",
            "cancel_event":  None,
            "broadcast_msg": None,
        }
    return sessions[chat_id]


def reset_session(chat_id: int) -> None:
    sessions[chat_id] = {
        "profile_url":   None,
        "offset":        0,
        "state":         "waiting_profile",
        "cancel_event":  None,
        "broadcast_msg": None,
    }


def is_admin(chat_id: int) -> bool:
    return ADMIN_ID != 0 and chat_id == ADMIN_ID


# ━━━━━━━━━━━━━━━━━━━━━━━━ KEYBOARDS ━━━━━━━━━━━━━━━━━━━━━━━━

def main_keyboard(chat_id: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        types.KeyboardButton("📥 جلب 20 فيديو"),
        types.KeyboardButton("🚀 استنساخ كامل"),
    )
    kb.add(types.KeyboardButton("🔄 تغيير الحساب"))
    if is_admin(chat_id):
        kb.add(
            types.KeyboardButton("📊 لوحة التحكم"),
            types.KeyboardButton("📡 إذاعة رسالة"),
        )
    return kb


def cancel_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("❌ إلغاء"))
    return kb


def broadcast_confirm_inline(user_count: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(
            f"✅ إرسال لـ {user_count} مستخدم", callback_data="bc_confirm"
        ),
        types.InlineKeyboardButton("❌ إلغاء", callback_data="bc_cancel"),
    )
    return kb


def batch_done_inline(has_more: bool) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    if has_more:
        kb.add(
            types.InlineKeyboardButton("✅ تكملة",  callback_data="batch_continue"),
            types.InlineKeyboardButton("❌ إيقاف", callback_data="batch_stop"),
        )
    else:
        kb.add(
            types.InlineKeyboardButton("🔄 تغيير الحساب", callback_data="change_profile")
        )
    return kb


def panel_inline() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📡 إذاعة رسالة", callback_data="admin_broadcast"),
        types.InlineKeyboardButton("🔄 تحديث",       callback_data="admin_refresh"),
    )
    return kb


# ━━━━━━━━━━━━━━━━━━━━━━━━ HELPERS ━━━━━━━━━━━━━━━━━━━━━━━━

def safe_edit(chat_id: int, msg_id: int, text: str,
              parse_mode=None, reply_markup=None) -> None:
    try:
        bot.edit_message_text(text, chat_id, msg_id,
                              parse_mode=parse_mode,
                              reply_markup=reply_markup)
    except Exception as exc:
        err = str(exc)
        if "message is not modified" in err:
            return
        try:
            bot.send_message(chat_id, text,
                             parse_mode=parse_mode,
                             reply_markup=reply_markup)
        except Exception as se:
            logger.error("send_message fallback failed: %s", se)


def send_and_delete(chat_id: int, filepath: str, caption: str = "") -> bool:
    try:
        ext = filepath.lower().rsplit(".", 1)[-1]
        with open(filepath, "rb") as f:
            if ext in ("jpg", "jpeg", "png", "webp"):
                bot.send_photo(chat_id, f, caption=caption)
            else:
                bot.send_video(chat_id, f, caption=caption,
                               supports_streaming=True)
        return True
    except Exception as exc:
        logger.warning("Failed to send %s: %s", filepath, exc)
        return False
    finally:
        try:
            os.remove(filepath)
        except OSError:
            pass


def normalise_profile_input(text: str):
    text = text.strip()
    if "tiktok.com" in text:
        m = re.search(r"(https?://(?:www\.)?tiktok\.com/@[\w.]+)", text)
        return m.group(1) if m else None
    if text.startswith("@"):
        u = text.lstrip("@").split("/")[0]
        return f"https://www.tiktok.com/@{u}" if u else None
    if re.match(r"^[\w.]{1,30}$", text):
        return f"https://www.tiktok.com/@{text}"
    return None


def is_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ["429", "rate", "too many", "blocked", "403"])


# ━━━━━━━━━━━━━━━━━━━━━━━━ STREAMING ENGINE ━━━━━━━━━━━━━━━━━━━━━━━━

def run_streaming_download(
    chat_id: int,
    profile_url: str,
    status_msg_id: int,
    cancel_event: threading.Event,
    start_offset: int = 0,
    playlist_items=None,
) -> dict:
    counters = {"sent": 0, "skipped": 0, "processed": 0}
    seen_ids = set()
    lock = threading.Lock()
    last_ui = [0.0]

    def throttle_edit(text: str) -> None:
        now = time.monotonic()
        if now - last_ui[0] >= 1.0:
            last_ui[0] = now
            safe_edit(chat_id, status_msg_id, text)

    def on_file_ready(filepath: str, video_id: str) -> None:
        with lock:
            if video_id and video_id in seen_ids:
                return
            if video_id:
                seen_ids.add(video_id)
            counters["processed"] += 1
            n = start_offset + counters["processed"]

        label = str(n)
        safe_edit(chat_id, status_msg_id, f"📤 جاري إرسال الفيديو {label}...")
        ok = send_and_delete(chat_id, filepath, caption=f"🎬 {label}")
        with lock:
            if ok:
                counters["sent"] += 1
            else:
                counters["skipped"] += 1

    def progress_hook(d: dict) -> None:
        if cancel_event.is_set():
            raise yt_dlp.utils.DownloadError("Cancelled by user")
        status = d["status"]
        if status == "downloading":
            n = start_offset + counters["processed"] + 1
            pct = d.get("_percent_str", "").strip()
            suffix = f" {pct}" if pct else ""
            throttle_edit(f"⏳ جاري تحميل الفيديو {n}...{suffix}")
        elif status == "finished":
            info = d.get("info_dict", {})
            video_id = info.get("id", "")
            filename = d.get("filename", "")
            if not filename or not os.path.exists(filename):
                return
            if not filename.endswith(".mp4"):
                return
            acodec = info.get("acodec") or "none"
            vcodec = info.get("vcodec") or "none"
            if acodec != "none" and vcodec != "none":
                threading.Thread(
                    target=on_file_ready, args=(filename, video_id), daemon=True
                ).start()
        elif status == "error":
            with lock:
                counters["skipped"] += 1

    def postprocessor_hook(d: dict) -> None:
        if d["status"] != "finished" or cancel_event.is_set():
            return
        info = d.get("info_dict", {})
        video_id = info.get("id", "")
        filepath = info.get("filepath") or ""
        if not filepath:
            return
        if not filepath.endswith(".mp4"):
            mp4 = os.path.splitext(filepath)[0] + ".mp4"
            filepath = mp4 if os.path.exists(mp4) else ""
        if filepath and os.path.exists(filepath):
            threading.Thread(
                target=on_file_ready, args=(filepath, video_id), daemon=True
            ).start()

    opts: dict = {
        "format": "best[ext=mp4]/best",
        "outtmpl": os.path.join(DOWNLOAD_DIR, f"{chat_id}_%(id)s.%(ext)s"),
        "noplaylist": False,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "http_headers": YT_DLP_HEADERS,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "sleep_interval_requests": 1,
    }
    if playlist_items:
        opts["playlist_items"] = playlist_items

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([profile_url])
    except yt_dlp.utils.DownloadError as exc:
        err = str(exc)
        if "Cancelled" not in err:
            if is_rate_limit(exc):
                bot.send_message(chat_id, "❌ خطأ: تيك توك يحظر الطلبات، سأحاول مجدداً..")
            else:
                logger.error("DownloadError: %s", exc)
    except Exception as exc:
        logger.error("Streaming error: %s", exc)

    return counters


# ━━━━━━━━━━━━━━━━━━━━━━━━ WORKERS ━━━━━━━━━━━━━━━━━━━━━━━━

def batch_worker(chat_id: int, cancel_event: threading.Event,
                 status_msg_id: int) -> None:
    session = get_session(chat_id)
    profile_url = session["profile_url"]
    offset = session["offset"]

    pl_start = offset + 1
    pl_end   = offset + BATCH_SIZE
    playlist_items = f"{pl_start}-{pl_end}"

    safe_edit(chat_id, status_msg_id,
              f"⏳ جاري بدء التحميل ({pl_start}-{pl_end})...")

    result = run_streaming_download(
        chat_id=chat_id,
        profile_url=profile_url,
        status_msg_id=status_msg_id,
        cancel_event=cancel_event,
        start_offset=offset,
        playlist_items=playlist_items,
    )

    sent      = result["sent"]
    skipped   = result["skipped"]
    processed = result["processed"]
    new_offset = offset + processed
    reached_end = processed < BATCH_SIZE

    session["offset"]       = new_offset
    session["state"]        = "profile_loaded"
    session["cancel_event"] = None

    if cancel_event.is_set():
        safe_edit(chat_id, status_msg_id,
                  f"تم الايقاف.\nارسل: {sent} | تخطي: {skipped}")
        bot.send_message(chat_id, "اختر الاجراء التالي:",
                         reply_markup=main_keyboard(chat_id))
    elif processed == 0:
        safe_edit(chat_id, status_msg_id, "تم ارسال جميع مقاطع الحساب بالفعل!")
        bot.send_message(chat_id, "اختر الاجراء التالي:",
                         reply_markup=main_keyboard(chat_id))
    else:
        summary = (
            f"اكتملت الدفعة!\n"
            f"ارسل: {sent} | تخطي: {skipped}\n"
            f"الاجمالي حتى الان: {new_offset}"
        )
        safe_edit(chat_id, status_msg_id, summary,
                  reply_markup=batch_done_inline(has_more=not reached_end))
        if reached_end:
            bot.send_message(chat_id, "تم استنساخ الحساب بالكامل!",
                             reply_markup=main_keyboard(chat_id))


def full_clone_worker(chat_id: int, cancel_event: threading.Event,
                      status_msg_id: int) -> None:
    session = get_session(chat_id)
    profile_url = session["profile_url"]
    offset = session["offset"]

    playlist_items = f"{offset + 1}:" if offset > 0 else None
    safe_edit(chat_id, status_msg_id, "جاري بدء الاستنساخ الكامل...")

    result = run_streaming_download(
        chat_id=chat_id,
        profile_url=profile_url,
        status_msg_id=status_msg_id,
        cancel_event=cancel_event,
        start_offset=offset,
        playlist_items=playlist_items,
    )

    sent      = result["sent"]
    skipped   = result["skipped"]
    processed = result["processed"]

    session["offset"]       = offset + processed
    session["state"]        = "profile_loaded"
    session["cancel_event"] = None

    if cancel_event.is_set():
        summary = (
            f"تم الايقاف عند الفيديو {offset + processed}.\n"
            f"ارسل: {sent} | تخطي: {skipped}"
        )
    else:
        summary = (
            f"اكتمل الاستنساخ الكامل!\n"
            f"ارسل: {sent} | تخطي: {skipped}"
        )
    safe_edit(chat_id, status_msg_id, summary)
    bot.send_message(chat_id, "اختر الاجراء التالي:",
                     reply_markup=main_keyboard(chat_id))


def broadcast_worker(admin_id: int, msg_to_copy: types.Message,
                     status_msg_id: int) -> None:
    users     = all_active_ids()
    total     = len(users)
    delivered = 0
    bk_count  = 0
    failed    = 0
    last_ui   = [0.0]

    def ui(text: str) -> None:
        now = time.monotonic()
        if now - last_ui[0] >= 2.0:
            last_ui[0] = now
            safe_edit(admin_id, status_msg_id, text)

    for i, uid in enumerate(users, 1):
        try:
            bot.copy_message(
                chat_id=uid,
                from_chat_id=msg_to_copy.chat.id,
                message_id=msg_to_copy.message_id,
            )
            delivered += 1
        except telebot.apihelper.ApiTelegramException as exc:
            err = str(exc).lower()
            if "forbidden" in err or "403" in err or "blocked" in err:
                mark_blocked(uid)
                bk_count += 1
            else:
                failed += 1
        except Exception:
            failed += 1

        ui(f"جاري الارسال... {i}/{total}\n"
           f"{delivered} وصل | {bk_count} حظر | {failed} فشل")
        time.sleep(0.05)

    save_broadcast(
        msg=msg_to_copy.text or "[media]",
        delivered=delivered,
        blocked=bk_count,
        failed=failed,
    )

    summary = (
        f"اكتملت الاذاعة!\n\n"
        f"وصلت الى: *{delivered}* مستخدم\n"
        f"حظروا البوت: *{bk_count}*\n"
        f"فشل في الارسال: *{failed}*\n"
        f"الاجمالي: {total}"
    )
    safe_edit(admin_id, status_msg_id, summary, parse_mode="Markdown")


def panel_text() -> str:
    s = get_stats()
    lines = [
        "*لوحة التحكم*\n",
        f"اجمالي المستخدمين: *{s['total']}*",
        f"نشطون: *{s['active']}*",
        f"حظروا البوت: *{s['blocked']}*",
    ]
    if s["last_bc"]:
        bc = s["last_bc"]
        lines.append(
            f"\n*اخر اذاعة* ({bc['sent_at'][:16]})\n"
            f"  وصل: {bc['delivered']} | "
            f"حظر: {bc['blocked']} | "
            f"فشل: {bc['failed']}"
        )
    return "\n".join(lines)


def start_worker(chat_id: int, target, initial_text: str) -> None:
    session = get_session(chat_id)
    cancel_event = threading.Event()
    session["state"]        = "fetching"
    session["cancel_event"] = cancel_event
    status_msg = bot.send_message(chat_id, initial_text,
                                  reply_markup=cancel_keyboard())
    threading.Thread(
        target=target,
        args=(chat_id, cancel_event, status_msg.message_id),
        daemon=True,
    ).start()


# ━━━━━━━━━━━━━━━━━━━━━━━━ HANDLERS ━━━━━━━━━━━━━━━━━━━━━━━━

@bot.message_handler(commands=["start", "help"])
def handle_start(message: types.Message) -> None:
    register_user(message.from_user)
    reset_session(message.chat.id)
    bot.send_message(
        message.chat.id,
        "مرحبا! ارسل لي رابط حساب تيك توك او اسم المستخدم.",
        reply_markup=types.ReplyKeyboardRemove(),
    )


@bot.message_handler(commands=["panel", "stats"])
def handle_panel_cmd(message: types.Message) -> None:
    if not is_admin(message.chat.id):
        return
    bot.send_message(
        message.chat.id,
        panel_text(),
        parse_mode="Markdown",
        reply_markup=panel_inline(),
    )


@bot.message_handler(func=lambda m: m.text == "❌ إلغاء")
def handle_cancel(message: types.Message) -> None:
    chat_id = message.chat.id
    session = get_session(chat_id)
    ev = session.get("cancel_event")
    if ev:
        ev.set()
        bot.send_message(chat_id, "جاري الايقاف...")
    else:
        reset_session(chat_id)
        bot.send_message(
            chat_id,
            "تم الالغاء. ارسل لي رابط الحساب او اسم المستخدم.",
            reply_markup=types.ReplyKeyboardRemove(),
        )


@bot.message_handler(commands=["cancel"])
def handle_cancel_cmd(message: types.Message) -> None:
    chat_id = message.chat.id
    session = get_session(chat_id)
    ev = session.get("cancel_event")
    if ev:
        ev.set()
    reset_session(chat_id)
    bot.send_message(chat_id, "تم الالغاء.",
                     reply_markup=main_keyboard(chat_id)
                     if session["profile_url"] else types.ReplyKeyboardRemove())


@bot.message_handler(func=lambda m: m.text == "🔄 تغيير الحساب")
def handle_change_profile(message: types.Message) -> None:
    chat_id = message.chat.id
    ev = get_session(chat_id).get("cancel_event")
    if ev:
        ev.set()
    reset_session(chat_id)
    bot.send_message(
        chat_id,
        "ارسل لي رابط الحساب او اسم المستخدم الجديد.",
        reply_markup=types.ReplyKeyboardRemove(),
    )


@bot.message_handler(func=lambda m: m.text == "📥 جلب 20 فيديو")
def handle_batch(message: types.Message) -> None:
    chat_id = message.chat.id
    session = get_session(chat_id)
    register_user(message.from_user)
    if session["state"] == "fetching":
        bot.send_message(chat_id, "هناك عملية جارية. اضغط الغاء لايقافها.",
                         reply_markup=cancel_keyboard())
        return
    if not session["profile_url"]:
        bot.send_message(chat_id, "ارسل رابط الحساب اولا.")
        return
    start_worker(chat_id, batch_worker, "جاري بدء التحميل...")


@bot.message_handler(func=lambda m: m.text == "🚀 استنساخ كامل")
def handle_full_clone(message: types.Message) -> None:
    chat_id = message.chat.id
    session = get_session(chat_id)
    register_user(message.from_user)
    if session["state"] == "fetching":
        bot.send_message(chat_id, "هناك عملية جارية. اضغط الغاء لايقافها.",
                         reply_markup=cancel_keyboard())
        return
    if not session["profile_url"]:
        bot.send_message(chat_id, "ارسل رابط الحساب اولا.")
        return
    start_worker(chat_id, full_clone_worker, "بدا الاستنساخ الكامل! اضغط الغاء لايقافه.")


@bot.message_handler(func=lambda m: m.text == "📊 لوحة التحكم")
def handle_panel_btn(message: types.Message) -> None:
    if not is_admin(message.chat.id):
        return
    bot.send_message(
        message.chat.id,
        panel_text(),
        parse_mode="Markdown",
        reply_markup=panel_inline(),
    )


@bot.message_handler(func=lambda m: m.text == "📡 إذاعة رسالة")
def handle_broadcast_btn(message: types.Message) -> None:
    chat_id = message.chat.id
    if not is_admin(chat_id):
        return
    session = get_session(chat_id)
    session["state"] = "waiting_broadcast"
    bot.send_message(
        chat_id,
        "ارسل الرسالة التي تريد اذاعتها لجميع المستخدمين.\n"
        "يمكنها نص، صورة، فيديو، او اي نوع اخر.\n"
        "ارسل /cancel للالغاء.",
        reply_markup=types.ReplyKeyboardRemove(),
    )


@bot.callback_query_handler(func=lambda c: True)
def handle_inline(call: types.CallbackQuery) -> None:
    chat_id = call.message.chat.id
    session = get_session(chat_id)
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    data = call.data

    if data == "change_profile":
        ev = session.get("cancel_event")
        if ev:
            ev.set()
        reset_session(chat_id)
        bot.send_message(chat_id, "ارسل لي رابط الحساب او اسم المستخدم الجديد.",
                         reply_markup=types.ReplyKeyboardRemove())

    elif data == "batch_stop":
        session["state"] = "profile_loaded"
        bot.send_message(chat_id, "تم الايقاف.",
                         reply_markup=main_keyboard(chat_id))

    elif data == "batch_continue":
        if session["state"] == "fetching":
            return
        if not session["profile_url"]:
            bot.send_message(chat_id, "ارسل رابط الحساب اولا.")
            return
        start_worker(chat_id, batch_worker, "جاري بدء التحميل للدفعة التالية...")

    elif data == "admin_refresh":
        if not is_admin(chat_id):
            return
        try:
            bot.edit_message_text(
                panel_text(), chat_id, call.message.message_id,
                parse_mode="Markdown", reply_markup=panel_inline()
            )
        except Exception:
            pass

    elif data == "admin_broadcast":
        if not is_admin(chat_id):
            return
        session["state"] = "waiting_broadcast"
        bot.send_message(
            chat_id,
            "ارسل الرسالة التي تريد اذاعتها.",
            reply_markup=types.ReplyKeyboardRemove(),
        )

    elif data == "bc_confirm":
        if not is_admin(chat_id):
            return
        msg_to_copy = session.get("broadcast_msg")
        if not msg_to_copy:
            bot.send_message(chat_id, "لم يتم العثور على رسالة للاذاعة.")
            return
        session["state"]         = "fetching"
        session["broadcast_msg"] = None
        status = bot.send_message(chat_id, "جاري ارسال الرسالة...")
        threading.Thread(
            target=broadcast_worker,
            args=(chat_id, msg_to_copy, status.message_id),
            daemon=True,
        ).start()

    elif data == "bc_cancel":
        if not is_admin(chat_id):
            return
        session["state"]         = "profile_loaded" if session["profile_url"] else "waiting_profile"
        session["broadcast_msg"] = None
        bot.send_message(chat_id, "تم الغاء الاذاعة.",
                         reply_markup=main_keyboard(chat_id))


@bot.message_handler(
    func=lambda m: m.text and not m.text.startswith("/"),
    content_types=["text"],
)
def handle_text(message: types.Message) -> None:
    chat_id = message.chat.id
    text = message.text.strip()
    session = get_session(chat_id)
    register_user(message.from_user)

    if is_admin(chat_id) and session["state"] == "waiting_broadcast":
        users = all_active_ids()
        session["broadcast_msg"] = message
        session["state"] = "confirm_broadcast"
        bot.send_message(
            chat_id,
            f"هل تريد اذاعتها الى *{len(users)}* مستخدم؟",
            parse_mode="Markdown",
            reply_markup=broadcast_confirm_inline(len(users)),
        )
        return

    if session["state"] == "fetching":
        bot.send_message(chat_id, "هناك عملية جارية. اضغط الغاء لايقافها.",
                         reply_markup=cancel_keyboard())
        return

    profile_url = normalise_profile_input(text)
    if not profile_url:
        bot.send_message(
            chat_id,
            "لم اتعرف على هذا الرابط او اسم المستخدم.\n"
            "ارسل رابطا مثل: https://www.tiktok.com/@username\n"
            "او اسم المستخدم: @username",
        )
        return

    session["profile_url"] = profile_url
    session["offset"]      = 0
    session["state"]       = "profile_loaded"

    username = re.search(r"@([\w.]+)", profile_url)
    display  = f"@{username.group(1)}" if username else profile_url

    bot.send_message(
        chat_id,
        f"تم تسجيل الحساب: *{display}*\n\nاختر الاجراء:",
        parse_mode="Markdown",
        reply_markup=main_keyboard(chat_id),
    )


@bot.message_handler(
    content_types=["photo", "video", "audio", "document",
                   "sticker", "voice", "video_note"],
)
def handle_media(message: types.Message) -> None:
    chat_id = message.chat.id
    session = get_session(chat_id)
    register_user(message.from_user)

    if is_admin(chat_id) and session["state"] == "waiting_broadcast":
        users = all_active_ids()
        session["broadcast_msg"] = message
        session["state"] = "confirm_broadcast"
        bot.send_message(
            chat_id,
            f"هل تريد اذاعتها الى *{len(users)}* مستخدم؟",
            parse_mode="Markdown",
            reply_markup=broadcast_confirm_inline(len(users)),
        )


if __name__ == "__main__":
    init_db()
    logger.info("TikTok Profile Manager Bot started.")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
