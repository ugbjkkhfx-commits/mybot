import os
import re
import json
import time
import random
import tempfile
import logging
import traceback
import urllib.request
import telebot
import yt_dlp
from keep_alive import keep_alive

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
bot = telebot.TeleBot(BOT_TOKEN)

CHANNEL_ID = -1003872259900
ADMIN_ID = 1520960859
USERS_FILE = "users.json"
BLOCKED_FILE = "blocked.json"


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
RETRY_DELAY = 1  # seconds between retries

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


def _build_main_keyboard() -> telebot.types.InlineKeyboardMarkup:
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📥 تحميل دفعة واحدة", callback_data="batch_download"))
    return markup


def _build_back_keyboard() -> telebot.types.InlineKeyboardMarkup:
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🔙 رجوع", callback_data="go_back"))
    return markup


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


@bot.message_handler(commands=["start", "help"])
def handle_start(message: telebot.types.Message) -> None:
    user = message.from_user

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


def _process_single_url(message: telebot.types.Message, url: str) -> None:
    """Download and send one URL. All errors are contained here — never propagate up."""
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
                    "يرجاء اعادة المحوله حدث خطأ أثناء التحميل...",
                    message.chat.id, status_msg.message_id
                )
            except Exception:
                pass


@bot.message_handler(func=lambda m: not m.text.startswith("/"), content_types=["text"])
def handle_message(message: telebot.types.Message) -> None:
    user_id = message.from_user.id

    if user_id in batch_mode_users:
        # ── Batch mode: split at every https:// so joined URLs are separated ──
        # e.g. "https://vt.tiktok.com/abc/https://vt.tiktok.com/def/" → 2 URLs
        urls = [u.strip() for u in re.split(r'(?=https?://)', message.text)
                if u.strip().startswith('http')]

        if not urls:
            return

        batch_mode_users.discard(user_id)

        for url in urls:
            try:
                _process_single_url(message, url)
            except Exception as e:
                logger.error(
                    "Unexpected error for url=%s: %s\n%s",
                    url, e, traceback.format_exc()
                )

    else:
        # ── Normal mode: original single-link behaviour ────────────────────
        raw_url, platform = extract_video_url(message.text)
        if not raw_url:
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
                    "يرجاء اعادة المحوله حدث خطأ أثناء التحميل...",
                    message.chat.id, status_msg.message_id
                )


if __name__ == "__main__":
    keep_alive()
    bot.set_my_commands([
        telebot.types.BotCommand("start", "رسالة البدء"),
    ])
    logger.info("Bot is running...")
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=10, logger_level=logging.WARNING)
        except Exception as e:
            logger.error("Polling crashed: %s — restarting in 5 seconds...", e)
            time.sleep(5)
