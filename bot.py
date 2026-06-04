import os
import re
import json
import time
import random
import tempfile
import logging
import traceback
import urllib.request
import threading
import sqlite3
from contextlib import contextmanager

import telebot
from telebot import types
import yt_dlp
from keep_alive import keep_alive

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

CHANNEL_ID = -1003872259900
ADMIN_ID = 1520960859
USERS_FILE = "users.json"
BLOCKED_FILE = "blocked.json"
DB_PATH = "users.db"
BATCH_SIZE = 20

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
            CREATE TABLE IF NOT EXISTS users_db (
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

def register_user_db(user: types.User) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users_db (id, username, first_name)
            VALUES (:id, :u, :n)
            ON CONFLICT(id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name,
                last_seen  = datetime('now'),
                blocked    = 0
            """,
            {"id": user.id, "u": user.username or "", "n": user.first_name or ""},
        )

def mark_blocked_db(uid: int) -> None:
    with db() as conn:
        conn.execute("UPDATE users_db SET blocked = 1 WHERE id = ?", (uid,))

def all_active_ids() -> list:
    with db() as conn:
        rows = conn.execute(
            "SELECT id FROM users_db WHERE blocked = 0"
        ).fetchall()
    return [r["id"] for r in rows]

def get_stats() -> dict:
    with db() as conn:
        total   = conn.execute("SELECT COUNT(*) FROM users_db").fetchone()[0]
        blocked = conn.execute(
            "SELECT COUNT(*) FROM users_db WHERE blocked = 1"
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

# ━━━━━━━━━━━━━━━━━━━━━━━━ JSON FILES ━━━━━━━━━━���━━━━━━━━━━━━━

def load_json_set(path: str) -> set[int]:
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()

def save_json_set(path: str, data: set[int]) -> None:
    with open(path, "w") as f:
        json.dump(list(data), f)

seen_users: set[int] = load_json_set(USERS_FILE)
blocked_users: set[int] = load_json_set(BLOCKED_FILE)
batch_mode_users: set[int] = set()
logger.info("Loaded %s users, %s blocked", len(seen_users), len(blocked_users))

def notify_admin(text: str) -> None:
    try:
        bot.send_message(ADMIN_ID, text)
    except Exception as exc:
        logger.error("Failed to notify admin: %s", exc)

def send_to_user(chat_id: int, first_name: str, **kwargs) -> None:
    """Send a message/video to user, detect Forbidden (bot blocked) and notify admin."""
    method = kwargs.pop("_method", "send_message")
    try:
        getattr(bot, method)(chat_id, **kwargs)
    except telebot.apihelper.ApiTelegramException as exc:
        if "Forbidden" in str(exc) or exc.error_code == 403:
            if chat_id not in blocked_users:
                blocked_users.add(chat_id)
                save_json_set(BLOCKED_FILE, blocked_users)
                mark_blocked_db(chat_id)
                logger.warning("User blocked the bot: %s", chat_id)
                notify_admin(
                    f"🚫 مستخدم حظر البوت!\n"
                    f"👤 الاسم: {first_name}\n"
                    f"🆔 الآيدي: {chat_id}\n"
                    f"📊 إجمالي المحظورين: {len(blocked_users)}"
                )
        else:
            raise

SUPPORTED_PLATFORMS = {
    "tiktok":    re.compile(r"(https?://)?(www\.)?(vm\.|vt\.)?tiktok\.com/\S+", re.IGNORECASE),
    "instagram": re.compile(r"(https?://)?(www\.)?instagram\.com/\S+", re.IGNORECASE),
    "snapchat":  re.compile(r"(https?://)?(www\.)?snapchat\.com/\S+", re.IGNORECASE),
    "facebook":  re.compile(r"(https?://)?(www\.|m\.|fb\.)?(facebook\.com|fb\.watch)/\S+", re.IGNORECASE),
    "kwai":      re.compile(r"(https?://)?(www\.)?kwai\.(com|app)/\S+", re.IGNORECASE),
    "pinterest": re.compile(r"(https?://)?(www\.|pin\.)?pinterest\.(com|co\.\w+)/\S+", re.IGNORECASE),
}

PLATFORM_EMOJI = {
    "tiktok":    "🎵",
    "instagram": "📸",
    "snapchat":  "👻",
    "facebook":  "📘",
    "kwai":      "🎬",
    "pinterest": "📌",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36",
    "Mozilla/5.0 (iPad; CPU OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1",
]

BASE_YDL_OPTS = {
    "merge_output_format": "mp4",
    "quiet": False,
    "no_warnings": False,
    "socket_timeout": 30,
    "retries": 3,
    "cookiefile": None,
}

ATTEMPT_PROFILES = [
    {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "extractor_args": {"tiktok": {"api_hostname": ["api22-normal-c-useast2a.tiktokv.com"]}},
    },
    {
        "format": "best[ext=mp4]/best",
        "extractor_args": {"tiktok": {"api_hostname": ["api19-normal-c-useast1a.tiktokv.com"]}},
    },
]

def resolve_url(url: str) -> str:
    """Follow redirects to expand short links (vt.tiktok.com, vm.tiktok.com, etc.)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": random.choice(USER_AGENTS)})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.url
    except Exception:
        return url

def extract_video_url(text: str) -> tuple[str, str] | tuple[None, None]:
    for platform, pattern in SUPPORTED_PLATFORMS.items():
        match = pattern.search(text)
        if match:
            return match.group(0), platform
    return None, None

def extract_all_video_urls(text: str) -> list[tuple[str, str]]:
    """Extract all video URLs from text, preserving the order they appear."""
    found: list[tuple[int, str, str]] = []
    seen_urls: set[str] = set()
    for platform, pattern in SUPPORTED_PLATFORMS.items():
        for match in pattern.finditer(text):
            url = match.group(0)
            if url not in seen_urls:
                seen_urls.add(url)
                found.append((match.start(), url, platform))
    found.sort(key=lambda x: x[0])
    return [(url, platform) for _, url, platform in found]

MAX_RETRIES = 3
RETRY_DELAY = 1

def download_video(url: str, output_path: str) -> dict:
    last_exc: Exception | None = None
    total_attempts = MAX_RETRIES * len(ATTEMPT_PROFILES)
    attempt_num = 0

    for retry in range(MAX_RETRIES):
        for profile in ATTEMPT_PROFILES:
            attempt_num += 1
            opts = {**BASE_YDL_OPTS, **profile}
            opts["outtmpl"] = output_path
            opts["http_headers"] = {"User-Agent": random.choice(USER_AGENTS)}
            logger.info(
                "Download attempt %d/%d (retry=%d) — url=%s",
                attempt_num, total_attempts, retry, url
            )
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                logger.info("Download succeeded on attempt %d", attempt_num)
                return info
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Attempt %d/%d failed: %s\n%s",
                    attempt_num, total_attempts, exc, traceback.format_exc()
                )
                if attempt_num < total_attempts:
                    time.sleep(RETRY_DELAY)
    raise last_exc

def find_downloaded_file(tmpdir: str, ext: str) -> str:
    matches = [f for f in os.listdir(tmpdir) if f.startswith("video.")]
    if not matches:
        raise FileNotFoundError("Downloaded file not found in tmpdir")
    return os.path.join(tmpdir, matches[0])

# ━━━━━━━━━━━━━━━━━━━━━━━━ KEYBOARDS ━━━━━━━━━━━━━━━━━━━━━━━━

def _build_main_keyboard(chat_id: int = None) -> types.ReplyKeyboardMarkup:
    if chat_id and is_admin(chat_id):
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        kb.add(
            types.KeyboardButton("📥 تحميل دفعة واحدة"),
            types.KeyboardButton("🚀 استنساخ كامل"),
        )
        kb.add(
            types.KeyboardButton("📊 لوحة التحكم"),
            types.KeyboardButton("📡 إذاعة رسالة"),
        )
        return kb
    else:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📥 تحميل دفعة واحدة", callback_data="batch_download"))
        return markup

def _build_back_keyboard() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="go_back"))
    return markup

def batch_done_inline(has_more: bool) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    if has_more:
        kb.add(
            types.InlineKeyboardButton("✅ تكملة",  callback_data="batch_continue"),
            types.InlineKeyboardButton("❌ إيقاف", callback_data="batch_stop"),
        )
    else:
        kb.add(
            types.InlineKeyboardButton("🔄 تغيير الحسا��", callback_data="change_profile")
        )
    return kb

def panel_inline() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📡 إذاعة رسالة", callback_data="admin_broadcast"),
        types.InlineKeyboardButton("🔄 تحديث",       callback_data="admin_refresh"),
    )
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

def _welcome_text(first_name: str) -> str:
    return (
        f"أهلاً بك يا <b>{first_name}</b> في بوت تحميل من السوشيال ميديا! 🌹\n"
        "بـوتـنـا سـهـل الاسـتـخـدام..\n"
        "كـل مـا عـلـيـك فـعـلـه هـو إرسـال الـرابط أو إعـادة تـوجـيـهـه إلـيـنـا.\n\n"
        "نـحـن لا نـضـع اشـتـراكـاً إجـبـاريـاً في الـوقـت الـحـالـي.. لـكـن قـد نـضـعـه في الـمـسـتـقـبـل.\n\n"
        "الـبـوت لا يـحـتـوي عـلى رسـائـل مـزعـجـة أو إعـلانـات ومـا شـابـه.\n\n"
        "يـمـكـنـك الـتـحـمـيـل مـن:\n"
        "• تـيـك تـوك\n"
        "• إنـسـتـغـرام\n"
        "• فـيـسـبـوك\n"
        "• بـيـنـتـرسـت\n"
        "بـأفـضـل جـودة مـوجـودة.\n\n"
        "الـبـوت قـد يـتـوقـف أحـيـانـاً بـسـبـب الـصـيـانـة أو الـتـعـديل..\n"
        "لـكـن في الأيـام الـمـقـبـلـة، لـن يـتـوقـف بـإذن الله.\n\n"
        "شـكـراً لـكـم! ✨"
    )

# ━━━��━━━━━━━━━━━━━━━━━━━━ STREAMING ━━━━━━━━━━━━━━━━━━━━━━━━

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

    opts: dict = {
        "format": "best[ext=mp4]/best",
        "outtmpl": os.path.join("downloads", f"{chat_id}_%(id)s.%(ext)s"),
        "noplaylist": False,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "http_headers": {"User-Agent": random.choice(USER_AGENTS)},
        "progress_hooks": [progress_hook],
    }
    if playlist_items:
        opts["playlist_items"] = playlist_items

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([profile_url])
    except Exception as exc:
        logger.error("Streaming error: %s", exc)

    return counters

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
                         reply_markup=_build_main_keyboard(chat_id))
    elif processed == 0:
        safe_edit(chat_id, status_msg_id, "تم ارسال جميع مقاطع الحساب بالفعل!")
        bot.send_message(chat_id, "اختر الاجراء التالي:",
                         reply_markup=_build_main_keyboard(chat_id))
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
                             reply_markup=_build_main_keyboard(chat_id))

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
                     reply_markup=_build_main_keyboard(chat_id))

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
                mark_blocked_db(uid)
                blocked_users.add(uid)
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

def start_worker(chat_id: int, target, initial_text: str) -> None:
    session = get_session(chat_id)
    cancel_event = threading.Event()
    session["state"]        = "fetching"
    session["cancel_event"] = cancel_event
    status_msg = bot.send_message(chat_id, initial_text,
                                  reply_markup=types.ReplyKeyboardRemove())
    threading.Thread(
        target=target,
        args=(chat_id, cancel_event, status_msg.message_id),
        daemon=True,
    ).start()

# ━━━━━━━━━━━━━━━━━━━━━━━━ HANDLERS ━━━━━━━━━━━━━━━━━━━━━━━━

@bot.message_handler(commands=["start", "help"])
def handle_start(message: telebot.types.Message) -> None:
    user = message.from_user
    register_user_db(user)

    if user.id not in seen_users:
        seen_users.add(user.id)
        save_json_set(USERS_FILE, seen_users)
        username = f"@{user.username}" if user.username else "N/A"
        language = user.language_code if user.language_code else "غير معروف"
        notification = (
            f"👾 شخص جديد دخل البوت\n\n"
            f"👤 معلومات العضو الجديد:\n"
            f"• الاسم: {user.first_name}\n"
            f"• المعرف: {username}\n"
            f"• الآيدي: {user.id}\n"
            f"• اللغة: {language}\n\n"
            f"📊 إجمالي المستخدمين: {len(seen_users)}"
        )
        notify_admin(notification)
        logger.info("New user saved: user_id=%s total=%s", user.id, len(seen_users))

    reset_session(message.chat.id)
    
    if is_admin(message.chat.id):
        bot.send_message(
            message.chat.id,
            _welcome_text(user.first_name),
            parse_mode="HTML",
            reply_markup=_build_main_keyboard(message.chat.id),
        )
    else:
        bot.send_message(
            message.chat.id,
            _welcome_text(user.first_name),
            parse_mode="HTML",
            reply_markup=_build_main_keyboard(),
        )

@bot.callback_query_handler(func=lambda call: call.data == "batch_download")
def handle_batch_download_callback(call: telebot.types.CallbackQuery) -> None:
    batch_mode_users.add(call.from_user.id)
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        "ارسال الروابط مره وحده وانا سوفا اقوم بتحملها لك 📥",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=_build_back_keyboard(),
    )

@bot.callback_query_handler(func=lambda call: call.data == "go_back")
def handle_go_back_callback(call: telebot.types.CallbackQuery) -> None:
    batch_mode_users.discard(call.from_user.id)
    bot.answer_callback_query(call.id)
    
    if is_admin(call.message.chat.id):
        bot.edit_message_text(
            _welcome_text(call.from_user.first_name),
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=_build_main_keyboard(call.message.chat.id),
        )
    else:
        bot.edit_message_text(
            _welcome_text(call.from_user.first_name),
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=_build_main_keyboard(),
        )

@bot.message_handler(commands=["stats"])
def handle_stats(message: telebot.types.Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    total = len(seen_users)
    blocked = len(blocked_users)
    active = total - blocked
    bot.reply_to(
        message,
        f"📊 إحصائيات البوت:\n\n"
        f"👥 إجمالي المستخدمين: {total}\n"
        f"✅ المستخدمون النشطون: {active}\n"
        f"🚫 المستخدمون المحظورون: {blocked}",
    )

@bot.message_handler(commands=["panel"])
def handle_panel_cmd(message: types.Message) -> None:
    if not is_admin(message.chat.id):
        return
    bot.send_message(
        message.chat.id,
        panel_text(),
        parse_mode="Markdown",
        reply_markup=panel_inline(),
    )

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
        "ارسل الرسالة التي تريد اذاعتها.\nيمكنها نص، صورة، فيديو، أو أي نوع آخر.\nارسل /cancel للإلغاء.",
        reply_markup=types.ReplyKeyboardRemove(),
    )

@bot.message_handler(func=lambda m: m.text == "📥 تحميل دفعة واحدة")
def handle_batch_btn(message: types.Message) -> None:
    chat_id = message.chat.id
    session = get_session(chat_id)
    register_user_db(message.from_user)
    if session["state"] == "fetching":
        bot.send_message(chat_id, "هناك عملية جارية. جرب لاحقاً.")
        return
    if not session["profile_url"]:
        bot.send_message(chat_id, "ارسل رابط الحساب أولاً.")
        return
    start_worker(chat_id, batch_worker, "جاري بدء التحميل...")

@bot.message_handler(func=lambda m: m.text == "🚀 استنساخ كامل")
def handle_full_clone(message: types.Message) -> None:
    chat_id = message.chat.id
    session = get_session(chat_id)
    register_user_db(message.from_user)
    if session["state"] == "fetching":
        bot.send_message(chat_id, "هناك عملية جارية. جرب لاحقاً.")
        return
    if not session["profile_url"]:
        bot.send_message(chat_id, "ارسل رابط الحساب أولاً.")
        return
    start_worker(chat_id, full_clone_worker, "بدء الاستنساخ الكامل!")

@bot.callback_query_handler(func=lambda c: True)
def handle_inline(call: types.CallbackQuery) -> None:
    chat_id = call.message.chat.id
    session = get_session(chat_id)
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    data = call.data

    if data == "batch_continue":
        if session["state"] == "fetching":
            return
        if not session["profile_url"]:
            bot.send_message(chat_id, "ارسل رابط الحساب أولاً.")
            return
        start_worker(chat_id, batch_worker, "جاري بدء التحميل للدفعة التالية...")

    elif data == "batch_stop":
        session["state"] = "profile_loaded"
        bot.send_message(chat_id, "تم الإيقاف.",
                         reply_markup=_build_main_keyboard(chat_id))

    elif data == "change_profile":
        reset_session(chat_id)
        bot.send_message(chat_id, "ارسل رابط الحساب الجديد.",
                         reply_markup=types.ReplyKeyboardRemove())

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
            "ارسل الرسالة.",
            reply_markup=types.ReplyKeyboardRemove(),
        )

    elif data == "bc_confirm":
        if not is_admin(chat_id):
            return
        msg_to_copy = session.get("broadcast_msg")
        if not msg_to_copy:
            bot.send_message(chat_id, "لم يتم العثور على رسالة.")
            return
        session["state"]         = "fetching"
        session["broadcast_msg"] = None
        status = bot.send_message(chat_id, "جاري الإرسال...")
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
        bot.send_message(chat_id, "تم الإلغاء.",
                         reply_markup=_build_main_keyboard(chat_id))

def _process_single_url(message: telebot.types.Message, url: str) -> None:
    """Download and send one URL."""
    status_msg = bot.reply_to(message, "⏳ جارِ التحميل...")

    resolved_url = resolve_url(url)
    if resolved_url != url:
        logger.info("Resolved short URL: %s → %s", url, resolved_url)

    _, platform = extract_video_url(resolved_url)

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            info = download_video(resolved_url, os.path.join(tmpdir, "video.%(ext)s"))
            video_file = find_downloaded_file(tmpdir, info.get("ext", "mp4"))
            with open(video_file, "rb") as vf:
                video_bytes = vf.read()
            bot.send_video(
                CHANNEL_ID, video_bytes,
                caption=f"تم التحميل بواسطة {message.from_user.first_name}",
                supports_streaming=True,
            )
            send_to_user(
                message.chat.id, message.from_user.first_name,
                _method="send_video",
                video=video_bytes,
                caption="تم التحميل ✅",
                supports_streaming=True,
            )
            bot.delete_message(message.chat.id, status_msg.message_id)
        except Exception as e:
            logger.error(
                "Download failed for url=%s platform=%s error=%s\n%s",
                resolved_url, platform, e, traceback.format_exc()
            )
            try:
                bot.edit_message_text(
                    "يرجاء إعادة المحاولة حدث خطأ أثناء التحميل...",
                    message.chat.id, status_msg.message_id
                )
            except Exception:
                pass

@bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"), content_types=["text"])
def handle_message(message: telebot.types.Message) -> None:
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = message.text.strip()
    session = get_session(chat_id)
    register_user_db(message.from_user)

    # Admin broadcast mode
    if is_admin(chat_id) and session["state"] == "waiting_broadcast":
        users = all_active_ids()
        session["broadcast_msg"] = message
        session["state"] = "confirm_broadcast"
        bot.send_message(
            chat_id,
            f"هل تريد إرسالها إلى *{len(users)}* مستخدم؟",
            parse_mode="Markdown",
            reply_markup=broadcast_confirm_inline(len(users)),
        )
        return

    # Batch mode
    if user_id in batch_mode_users:
        urls = [u.strip() for u in re.split(r'(?=https?://)', text)
                if u.strip().startswith('http')]

        if not urls:
            return

        batch_mode_users.discard(user_id)

        for url in urls:
            try:
                _process_single_url(message, url)
            except Exception as e:
                logger.error("Unexpected error for url=%s: %s\n%s",
                            url, e, traceback.format_exc())

    else:
        # Normal mode
        raw_url, platform = extract_video_url(text)
        if not raw_url:
            # Check for TikTok profile
            profile_url = normalise_profile_input(text)
            if profile_url:
                session["profile_url"] = profile_url
                session["offset"]      = 0
                session["state"]       = "profile_loaded"

                username = re.search(r"@([\w.]+)", profile_url)
                display  = f"@{username.group(1)}" if username else profile_url

                bot.send_message(
                    chat_id,
                    f"تم تسجيل الحساب: *{display}*\n\nاختر الاجراء:",
                    parse_mode="Markdown",
                    reply_markup=_build_main_keyboard(chat_id),
                )
            return

        status_msg = bot.reply_to(message, "⏳ جارِ التحميل...")

        url = resolve_url(raw_url)
        if url != raw_url:
            logger.info("Resolved short URL: %s → %s", raw_url, url)

        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                info = download_video(url, os.path.join(tmpdir, "video.%(ext)s"))
                video_file = find_downloaded_file(tmpdir, info.get("ext", "mp4"))
                with open(video_file, "rb") as vf:
                    video_bytes = vf.read()
                bot.send_video(
                    CHANNEL_ID, video_bytes,
                    caption=f"تم التحميل بواسطة {message.from_user.first_name}",
                    supports_streaming=True,
                )
                send_to_user(
                    message.chat.id, message.from_user.first_name,
                    _method="send_video",
                    video=video_bytes,
                    caption="تم التحميل ✅",
                    supports_streaming=True,
                )
                bot.delete_message(message.chat.id, status_msg.message_id)
            except Exception as e:
                logger.error(
                    "Download failed for url=%s platform=%s error=%s\n%s",
                    url, platform, e, traceback.format_exc()
                )
                bot.edit_message_text(
                    "يرجاء إعادة المحاولة حدث خطأ أثناء التحميل...",
                    message.chat.id, status_msg.message_id
                )

@bot.message_handler(
    content_types=["photo", "video", "audio", "document",
                   "sticker", "voice", "video_note"],
)
def handle_media(message: types.Message) -> None:
    chat_id = message.chat.id
    session = get_session(chat_id)
    register_user_db(message.from_user)

    if is_admin(chat_id) and session["state"] == "waiting_broadcast":
        users = all_active_ids()
        session["broadcast_msg"] = message
        session["state"] = "confirm_broadcast"
        bot.send_message(
            chat_id,
            f"هل تريد إرسالها إلى *{len(users)}* مستخدم؟",
            parse_mode="Markdown",
            reply_markup=broadcast_confirm_inline(len(users)),
        )

if __name__ == "__main__":
    os.makedirs("downloads", exist_ok=True)
    init_db()
    keep_alive()
    bot.set_my_commands([
        telebot.types.BotCommand("start", "رسالة البدء"),
        telebot.types.BotCommand("panel", "لوحة التحكم"),
        telebot.types.BotCommand("stats", "إحصائيات"),
    ])
    logger.info("Bot is running...")
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=10, logger_level=logging.WARNING)
        except Exception as e:
            logger.error("Polling crashed: %s — restarting in 5 seconds...", e)
            time.sleep(5)
