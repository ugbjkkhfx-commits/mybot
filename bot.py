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

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# تم وضع التوكن مباشرة هنا
BOT_TOKEN = "8131991575:AAG_192aVJOaDiyMeqKsQ-PHR2KE6WWdF9o"
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

seen_users = load_json_set(USERS_FILE)
blocked_users = load_json_set(BLOCKED_FILE)

def notify_admin(text: str) -> None:
    try:
        bot.send_message(ADMIN_ID, text)
    except Exception as exc:
        logger.error("Failed to notify admin: %s", exc)

def send_to_user(chat_id: int, first_name: str, **kwargs) -> None:
    method = kwargs.pop("_method", "send_message")
    try:
        getattr(bot, method)(chat_id, **kwargs)
    except telebot.apihelper.ApiTelegramException as exc:
        if "Forbidden" in str(exc) or exc.error_code == 403:
            if chat_id not in blocked_users:
                blocked_users.add(chat_id)
                save_json_set(BLOCKED_FILE, blocked_users)
                notify_admin(f"🚫 مستخدم حظر البوت!\n🆔 الآيدي: {chat_id}")
        else:
            raise

SUPPORTED_PLATFORMS = {
    "tiktok": re.compile(r"(https?://)?(www\.)?(vm\.|vt\.)?tiktok\.com/\S+", re.IGNORECASE),
    "instagram": re.compile(r"(https?://)?(www\.)?instagram\.com/\S+", re.IGNORECASE),
    "snapchat": re.compile(r"(https?://)?(www\.)?snapchat\.com/\S+", re.IGNORECASE),
    "facebook": re.compile(r"(https?://)?(www\.|m\.|fb\.)?(facebook\.com|fb\.watch)/\S+", re.IGNORECASE),
    "kwai": re.compile(r"(https?://)?(www\.)?kwai\.(com|app)/\S+", re.IGNORECASE),
    "pinterest": re.compile(r"(https?://)?(www\.|pin\.)?pinterest\.(com|co\.\w+)/\S+", re.IGNORECASE),
}

USER_AGENTS = ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"]

BASE_YDL_OPTS = {"merge_output_format": "mp4", "quiet": True, "no_warnings": True, "socket_timeout": 30}

def resolve_url(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": random.choice(USER_AGENTS)})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.url
    except:
        return url

def extract_video_url(text: str) -> tuple[str, str] | tuple[None, None]:
    for platform, pattern in SUPPORTED_PLATFORMS.items():
        match = pattern.search(text)
        if match:
            return match.group(0), platform
    return None, None

def download_video(url: str, output_path: str) -> dict:
    opts = {**BASE_YDL_OPTS, "outtmpl": output_path, "http_headers": {"User-Agent": random.choice(USER_AGENTS)}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)

def find_downloaded_file(tmpdir: str) -> str:
    matches = [f for f in os.listdir(tmpdir) if f.startswith("video.")]
    return os.path.join(tmpdir, matches[0])

@bot.message_handler(commands=["start", "help"])
def handle_start(message: telebot.types.Message) -> None:
    user = message.from_user
    if user.id not in seen_users:
        seen_users.add(user.id)
        save_json_set(USERS_FILE, seen_users)
        notify_admin(f"👾 مستخدم جديد: {user.first_name} | الآيدي: {user.id}")
    
    bot.send_message(message.chat.id, "أهلاً بك! أرسل رابط الفيديو للتحميل.")

@bot.message_handler(commands=["stats"])
def handle_stats(message: telebot.types.Message) -> None:
    if message.from_user.id == ADMIN_ID:
        bot.reply_to(message, f"👥 إجمالي المستخدمين: {len(seen_users)}\n🚫 المحظورين: {len(blocked_users)}")

@bot.message_handler(func=lambda m: not m.text.startswith("/"), content_types=["text"])
def handle_message(message: telebot.types.Message) -> None:
    raw_url, _ = extract_video_url(message.text)
    if not raw_url: return
    status_msg = bot.reply_to(message, "⏳ جارِ التحميل...")
    url = resolve_url(raw_url)
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            download_video(url, os.path.join(tmpdir, "video.%(ext)s"))
            video_file = find_downloaded_file(tmpdir)
            with open(video_file, "rb") as vf:
                bot.send_video(message.chat.id, vf, caption="تم التحميل ✅")
            bot.delete_message(message.chat.id, status_msg.message_id)
        except Exception:
            bot.edit_message_text("حدث خطأ، حاول مجدداً.", message.chat.id, status_msg.message_id)

if __name__ == "__main__":
    logger.info("Bot is running...")
    bot.infinity_polling()
