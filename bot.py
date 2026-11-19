from __future__ import annotations

import html
import json
import logging
import os
import random
import re
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import telebot
import yt_dlp
from keep_alive import keep_alive

import state_store

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
bot = telebot.TeleBot(BOT_TOKEN)
CHANNEL_ID = -1003872259900  # Video aggregation destination; not the subscription channel.
ADMIN_ID = 1520960859
USERS_FILE = "users.json"
BLOCKED_FILE = "blocked.json"
MAX_DEPARTURES = 5
_state_lock = threading.RLock()
_data_dir = Path(os.environ.get("BOT_DATA_DIR", "."))


def load_json_set(path: str) -> set[int]:
    return state_store.load_json_set(path)


def save_json_set(path: str, values: set[int]) -> None:
    state_store.save_json_set(path, values)


seen_users: set[int] = load_json_set(USERS_FILE)
blocked_users: set[int] = load_json_set(BLOCKED_FILE)
batch_mode_users: set[int] = set()
admin_states: dict[int, tuple[str, dict]] = {}
appeal_states: set[int] = set()
logger.info("Loaded %d users and %d previously unreachable users", len(seen_users), len(blocked_users))

SUBSCRIPTION_NOTICE = (
    "💌 أهلاً بك في بوت تحميل من السوشيال ميديا!\n\n"
    "⚠️ تنبيه مهم من إدارة البوت\n\n"
    "نحن نبذل وقتًا وجهدًا كبيرين في تطوير البوتات وتوفير الخدمات لكم، ونأمل منكم تقدير هذا الجهد وعدم مغادرة قناة المطوّر بعد الاشتراك.\n\n"
    "🚫 تحذير مهم: في حالة مغادرة قناة المطوّر بعد الاشتراك، سيتم منعك من استخدام البوت وفقًا لنظام المخالفات، وقد يصل الأمر إلى الحظر النهائي بعد استنفاد مرات السماح.\n\n"
    "يرجى احترام مجهود التطوير والالتزام بالاشتراك حتى نستمر في تقديم الأفضل لكم ❤️"
)
PERMANENT_BAN_TEXT = (
    "🚫 تم حظرك من استخدام البوت نهائيًا.\n\n"
    "نأسف لإبلاغك بأنه تم حظرك بسبب مخالفة شروط الاشتراك الإجباري ومغادرة القناة المعتمدة واستنفاد مرات السماح المتاحة لك.\n\n"
    "سبب الحظر: تكرار مغادرة قناة الاشتراك الإجباري 5 مرات.\n\n"
    "إذا كنت ترى أن الحظر غير صحيح أو ترغب في طلب مراجعة الحظر، يمكنك تقديم طعن من خلال الزر أدناه."
)
_URL_RE = re.compile(
    r"(?:(?:https?://)?(?:[A-Za-z0-9-]+\.)*(?:tiktok\.com|instagram\.com|snapchat\.com|facebook\.com|fb\.watch|kwai\.com|kwai\.app|pinterest\.com|pinterest\.co\.[A-Za-z]{2,})/[^\s<>]*)",
    re.IGNORECASE,
)
_ALLOWED_HOSTS = (
    "tiktok.com", "instagram.com", "snapchat.com", "facebook.com", "fb.watch",
    "kwai.com", "kwai.app", "pinterest.com",
)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
]
BASE_YDL_OPTS = {"merge_output_format": "mp4", "quiet": True, "no_warnings": True, "socket_timeout": 30, "retries": 3}
ATTEMPT_PROFILES = [
    {"format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best", "extractor_args": {"tiktok": {"api_hostname": ["api22-normal-c-useast2a.tiktokv.com"]}}},
    {"format": "best[ext=mp4]/best", "extractor_args": {"tiktok": {"api_hostname": ["api19-normal-c-useast1a.tiktokv.com"]}}},
]


def notify_admin(text: str, **kwargs) -> None:
    try:
        bot.send_message(ADMIN_ID, text, **kwargs)
    except Exception as exc:
        logger.error("Failed to notify owner: %s", exc)


def _profile(user) -> dict:
    return {"display_name": user.first_name or "", "username": user.username or "", "user_id": int(user.id)}


def _get_record(user_id: int) -> dict:
    return state_store.get_user_record(user_id)


def _appeal_markup():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📨 تقديم طعن", callback_data="appeal_start"))
    return markup


def _subscription_keyboard(config: dict):
    markup = telebot.types.InlineKeyboardMarkup()
    link = config.get("invite_link") or config.get("public_link")
    if link:
        markup.add(telebot.types.InlineKeyboardButton("🔴 📢 الاشتراك في قناة المطوّر", url=link))
    markup.add(telebot.types.InlineKeyboardButton("🟢 ✅ تحققت من الاشتراك", callback_data="check_subscription"))
    return markup


def _send_permanent_ban(chat_id: int) -> None:
    bot.send_message(chat_id, PERMANENT_BAN_TEXT, reply_markup=_appeal_markup())


def _is_member(member) -> bool:
    status = getattr(member, "status", "")
    return status in ("creator", "administrator", "member") or (status == "restricted" and bool(getattr(member, "is_member", False)))


def _subscription_config() -> dict:
    return state_store.get_subscription()


def _check_access(user, chat_id: int, *, show_prompt: bool = True) -> bool:
    if int(user.id) == ADMIN_ID:
        return True
    state_store.update_user_record(user.id, **_profile(user))
    record = _get_record(user.id)
    if record.get("permanently_banned"):
        _send_permanent_ban(chat_id)
        return False
    config = _subscription_config()
    if not config.get("enabled"):
        return True
    if not config.get("verified") or not config.get("chat_id"):
        bot.send_message(chat_id, "تعذر التحقق من إعداد قناة الاشتراك حاليًا. يرجى المحاولة لاحقًا.")
        return False
    try:
        member = bot.get_chat_member(int(config["chat_id"]), int(user.id))
    except Exception as exc:
        logger.warning("Membership check temporarily failed for user_id=%s: %s", user.id, exc)
        bot.send_message(chat_id, "تعذر فحص الاشتراك مؤقتًا بسبب مشكلة في Telegram. يرجى المحاولة مرة أخرى بعد قليل.")
        return False
    if _is_member(member):
        if record.get("temporarily_restricted"):
            state_store.set_temporary_restriction(user.id, False)
        return True
    if show_prompt:
        bot.send_message(chat_id, SUBSCRIPTION_NOTICE, reply_markup=_subscription_keyboard(config))
    return False


def _admin_main_keyboard():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🔵 ⚙️ إعدادات المالك", callback_data="owner_settings"))
    markup.add(telebot.types.InlineKeyboardButton("🔴 🚫 إدارة المحظورين", callback_data="ban_management"))
    return markup


def _owner_settings_keyboard():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🔵 📢 إعداد الاشتراك الإجباري", callback_data="subscription_settings"))
    markup.add(telebot.types.InlineKeyboardButton("📨 صندوق طعون المستخدمين", callback_data="appeals_inbox"))
    markup.add(telebot.types.InlineKeyboardButton("🔴 🚫 إدارة المحظورين", callback_data="ban_management"))
    markup.add(telebot.types.InlineKeyboardButton("⚪ 🔙 رجوع", callback_data="owner_home"))
    return markup


def _subscription_admin_keyboard():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(telebot.types.InlineKeyboardButton("🟢 تفعيل الاشتراك", callback_data="sub_enable"), telebot.types.InlineKeyboardButton("🟠 إيقاف الاشتراك", callback_data="sub_disable"))
    markup.row(telebot.types.InlineKeyboardButton("🔵 تغيير القناة", callback_data="sub_change"), telebot.types.InlineKeyboardButton("🔵 اختبار الصلاحيات", callback_data="sub_permissions"))
    markup.add(telebot.types.InlineKeyboardButton("⚪ 🔙 رجوع", callback_data="owner_settings"))
    return markup


def _subscription_status_text() -> str:
    config = _subscription_config()
    if not config.get("chat_id"):
        return "📢 إعداد الاشتراك الإجباري\nالحالة: غير مُعدّ\n\nأرسل رابط قناة عامة، أو أعد توجيه منشور من قناة خاصة."
    return ("📢 إعداد الاشتراك الإجباري\n" f"القناة: {html.escape(str(config.get('title') or 'غير معروف'))}\n"
            f"المعرّف: <code>{int(config['chat_id'])}</code>\n"
            f"الرابط: {html.escape(str(config.get('invite_link') or config.get('public_link') or 'غير متاح'))}\n"
            f"الحالة: {'مفعّل' if config.get('enabled') else 'معطّل'}")


def _ban_management_keyboard():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📋 قائمة المحظورين", callback_data="ban_list"))
    markup.add(telebot.types.InlineKeyboardButton("🔍 البحث عن مستخدم", callback_data="ban_search"))
    markup.add(telebot.types.InlineKeyboardButton("🟢 🔓 رفع الحظر", callback_data="ban_unban"))
    markup.add(telebot.types.InlineKeyboardButton("📊 تفاصيل المخالفة", callback_data="ban_details"))
    markup.add(telebot.types.InlineKeyboardButton("⚪ 🔙 رجوع", callback_data="owner_home"))
    return markup


def _format_record(record: dict) -> str:
    username = "@" + record.get("username", "") if record.get("username") else "غير موجود"
    kind = "دائم" if record.get("permanently_banned") else "مؤقت" if record.get("temporarily_restricted") else "غير محظور"
    return (f"الاسم الظاهر: {html.escape(str(record.get('display_name') or 'غير معروف'))}\n"
            f"المستخدم: {html.escape(username)}\nTelegram User ID: <code>{int(record.get('user_id', 0))}</code>\n"
            f"عدد مرات المغادرة: {int(record.get('departure_count', 0))}\nنوع الحظر: {kind}\n"
            f"سبب الحظر: {html.escape(str(record.get('ban_reason') or 'لا يوجد'))}")


def _search_user(query: str) -> list[dict]:
    users = state_store.get_moderation().get("users", {})
    needle = query.strip().lstrip("@").casefold()
    return [record for key, record in users.items() if isinstance(record, dict) and (needle == str(record.get("user_id", key)) or needle in str(record.get("username", "")).casefold() or needle in str(record.get("display_name", "")).casefold())][:20]


def _appeal_inbox_keyboard(appeals):
    markup = telebot.types.InlineKeyboardMarkup()
    for appeal in appeals:
        title = f"📨 #{appeal['id']} — {appeal.get('display_name') or appeal['user_id']}"
        markup.add(telebot.types.InlineKeyboardButton(title[:60], callback_data=f"appeal_view:{appeal['id']}"))
    markup.add(telebot.types.InlineKeyboardButton("⚪ 🔙 رجوع", callback_data="owner_settings"))
    return markup


def _show_appeals_inbox(chat_id: int) -> None:
    appeals = state_store.list_appeals()
    pending = [item for item in appeals if item["status"] == "pending"]
    if not pending:
        bot.send_message(chat_id, "📨 لا توجد طعون قيد المراجعة.", reply_markup=_owner_settings_keyboard())
        return
    bot.send_message(chat_id, f"📨 صندوق الطعون — قيد المراجعة: {len(pending)}", reply_markup=_appeal_inbox_keyboard(pending[-30:]))


def _show_appeal_details(chat_id: int, appeal_id: int) -> None:
    appeal = state_store.get_appeal(appeal_id)
    if not appeal:
        bot.send_message(chat_id, "الطعن غير موجود.")
        return
    username = "@" + appeal["username"] if appeal.get("username") else "غير موجود"
    status = "قيد المراجعة" if appeal["status"] == "pending" else "تمت معالجته"
    record = state_store.get_user_record(appeal["user_id"])
    text = (f"الطعن رقم: {appeal['id']}\nالحالة: {status}\nالاسم: {html.escape(appeal.get('display_name') or 'غير معروف')}\n"
            f"اسم المستخدم: {html.escape(username)}\nTelegram User ID: <code>{appeal['user_id']}</code>\n"
            f"تاريخ التقديم: {html.escape(appeal['created_at'])}\n\nنص الطعن:\n{html.escape(appeal['text'])}\n\n"
            f"سجل المخالفات:\n{_format_record(record)}")
    markup = telebot.types.InlineKeyboardMarkup()
    if appeal["status"] == "pending":
        markup.row(telebot.types.InlineKeyboardButton("🔓 فك الحظر والموافقة", callback_data=f"appeal_approve:{appeal_id}"),
                   telebot.types.InlineKeyboardButton("🚫 رفض الطعن", callback_data=f"appeal_reject:{appeal_id}"))
    markup.add(telebot.types.InlineKeyboardButton("⚪ 🔙 صندوق الطعون", callback_data="appeals_inbox"))
    bot.send_message(chat_id, text[:4000], parse_mode="HTML", reply_markup=markup)


def _start_channel_setup(user_id: int, chat_id: int) -> None:
    admin_states[user_id] = ("channel", {})
    bot.send_message(chat_id, "أرسل رابط قناة عامة أو @اسم_القناة، أو أعد توجيه منشورًا من القناة الخاصة.")


def _verify_and_save_channel(message, data: dict) -> None:
    try:
        chat_id = int(data["chat_id"])
        me = bot.get_me()
        bot_member = bot.get_chat_member(chat_id, me.id)
        if getattr(bot_member, "status", "") not in ("administrator", "creator"):
            bot.reply_to(message, "لم يتم اعتماد القناة: يجب ترقية البوت إلى مشرف فيها ليتمكن من فحص العضوية واستقبال تحديثات المغادرة.")
            return
        bot.get_chat_member(chat_id, ADMIN_ID)
    except Exception as exc:
        logger.warning("Channel permission verification failed: %s", exc)
        bot.reply_to(message, "تعذر التحقق من القناة أو من صلاحية فحص الأعضاء. لم يتم حفظ الإعداد. تحقق من صلاحيات البوت ثم أعد المحاولة.")
        return
    previous = _subscription_config()
    config = {"chat_id": chat_id, "title": str(data.get("title") or "قناة"), "public_link": data.get("public_link"),
              "invite_link": data.get("invite_link"), "enabled": bool(previous.get("enabled", True)), "verified": True}
    state_store.save_subscription(config)
    admin_states.pop(ADMIN_ID, None)
    bot.reply_to(message, "✅ تم التحقق من القناة وحفظها.\n" + _subscription_status_text(), parse_mode="HTML", reply_markup=_subscription_admin_keyboard())


def _parse_channel_link(text: str):
    value = text.strip()
    if value.startswith("@"):
        username = value[1:]
    else:
        parsed = urllib.parse.urlparse(value if "://" in value else "https://" + value)
        if parsed.netloc.lower().split(":")[0] not in ("t.me", "telegram.me"):
            return None
        username = parsed.path.strip("/").split("/")[0]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", username):
        return None
    return username, f"https://t.me/{username}"


def _resolve_channel_setup(message) -> None:
    state, data = admin_states.get(ADMIN_ID, (None, {}))
    if state != "channel":
        return
    forwarded_chat = getattr(message, "forward_from_chat", None)
    if forwarded_chat is None and getattr(message, "forward_origin", None):
        forwarded_chat = getattr(message.forward_origin, "chat", None)
    if forwarded_chat is not None:
        data["chat_id"] = int(forwarded_chat.id)
        data["title"] = getattr(forwarded_chat, "title", "قناة خاصة")
        admin_states[ADMIN_ID] = ("invite", data)
        bot.reply_to(message, "تم التعرف على القناة. أرسل رابط الدعوة t.me/+... أو t.me/joinchat/...")
        return
    channel = _parse_channel_link(message.text or "")
    if not channel:
        bot.reply_to(message, "الرابط غير صالح. أرسل رابط قناة عامة أو @اسم_القناة، أو أعد توجيه منشور من القناة الخاصة.")
        return
    username, public_link = channel
    try:
        chat = bot.get_chat("@" + username)
    except Exception as exc:
        logger.warning("Could not resolve owner-submitted channel: %s", exc)
        bot.reply_to(message, "لم أتمكن من الوصول إلى القناة. تحقق من الرابط ومن إضافة البوت مشرفًا ثم حاول مجددًا.")
        return
    if getattr(chat, "type", None) != "channel":
        bot.reply_to(message, "الرابط لا يشير إلى قناة Telegram.")
        return
    data.update(chat_id=int(chat.id), title=getattr(chat, "title", username), public_link=public_link, invite_link=None)
    _verify_and_save_channel(message, data)


def _handle_admin_text(message) -> bool:
    if message.from_user.id != ADMIN_ID or message.chat.type != "private":
        return False
    current = admin_states.get(ADMIN_ID)
    if not current:
        return False
    state, data = current
    if state in ("channel", "invite"):
        if state == "invite":
            invite = (message.text or "").strip()
            parsed = urllib.parse.urlparse(invite if "://" in invite else "https://" + invite)
            valid = parsed.netloc.lower().split(":")[0] in ("t.me", "telegram.me") and (parsed.path.startswith("/+") or parsed.path.startswith("/joinchat/"))
            if not valid:
                bot.reply_to(message, "صيغة رابط الدعوة غير صحيحة. أرسل t.me/+... أو t.me/joinchat/...")
                return True
            data["invite_link"] = invite if "://" in invite else "https://" + invite
            _verify_and_save_channel(message, data)
        else:
            _resolve_channel_setup(message)
        return True
    if state in ("search", "unban", "details"):
        query = (message.text or "").strip()
        if state == "unban":
            try:
                target_id = int(query)
            except ValueError:
                bot.reply_to(message, "أرسل Telegram User ID رقميًا.")
                return True
            record = state_store.lift_permanent_ban(target_id, ADMIN_ID)
            bot.reply_to(message, "✅ رُفع الحظر. بقي سجل المخالفات محفوظًا.\n" + _format_record(record), parse_mode="HTML")
        else:
            results = _search_user(query)
            bot.reply_to(message, "\n\n────────\n\n".join(_format_record(item) for item in results) if results else "لم يُعثر على مستخدم مطابق.", parse_mode="HTML")
        admin_states.pop(ADMIN_ID, None)
        return True
    return False


def _record_new_user(user) -> None:
    if user.id in seen_users:
        return
    with _state_lock:
        if user.id in seen_users:
            return
        seen_users.add(user.id)
        save_json_set(USERS_FILE, seen_users)
    username = "@" + user.username if user.username else "غير موجود"
    notify_admin(f"👾 مستخدم جديد\nالاسم: {user.first_name}\nالمعرف: {username}\nID: {user.id}\nإجمالي المستخدمين: {len(seen_users)}")


def _welcome_text(first_name: str) -> str:
    platforms = "TikTok، Instagram، Snapchat، Facebook، Kwai، Pinterest"
    return (f"أهلاً بك يا <b>{html.escape(first_name or '')}</b> في بوت تحميل من السوشيال ميديا! 🌹\n"
            "أرسل رابطًا أو أعد توجيهه إلى البوت.\n\n"
            f"المنصات المدعومة: {platforms}.\n\nقد يتوقف البوت أحيانًا للصيانة. شكرًا لكم! ✨")


def _main_keyboard():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📥 تحميل دفعة واحدة", callback_data="batch_download"))
    markup.add(telebot.types.InlineKeyboardButton("❓ المساعدة", callback_data="show_help"))
    return markup


def _back_keyboard():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⚪ 🔙 رجوع", callback_data="go_back"))
    return markup


def _validated_url(candidate: str) -> tuple[str, str] | None:
    candidate = candidate.rstrip(".,);]}>\"'")
    if not candidate.lower().startswith(("http://", "https://")):
        candidate = "https://" + candidate
    try:
        parsed = urllib.parse.urlsplit(candidate)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
        return None
    supported = any(host == domain or host.endswith("." + domain) for domain in _ALLOWED_HOSTS)
    if host.startswith("pinterest.co.") and re.fullmatch(r"pinterest\.co\.[a-z]{2,}", host):
        supported = True
    if not supported or not parsed.path or parsed.path == "/":
        return None
    platform = next((name for name, domain in (
        ("tiktok", "tiktok.com"), ("instagram", "instagram.com"), ("snapchat", "snapchat.com"),
        ("facebook", "facebook.com"), ("facebook", "fb.watch"), ("kwai", "kwai.com"),
        ("kwai", "kwai.app"), ("pinterest", "pinterest.com"),
    ) if host == domain or host.endswith("." + domain)), None)
    if platform is None and host.startswith("pinterest.co."):
        platform = "pinterest"
    return urllib.parse.urlunsplit(parsed), platform


def extract_video_url(text: str):
    for match in _URL_RE.finditer(text or ""):
        candidate = re.split(r"https?://", match.group(0), maxsplit=1, flags=re.IGNORECASE)[0]
        result = _validated_url(candidate)
        if result:
            return result
    return None, None


def extract_all_video_urls(text: str) -> list[tuple[str, str]]:
    found = []
    seen = set()
    for match in _URL_RE.finditer(text or ""):
        candidate = re.split(r"https?://", match.group(0), maxsplit=1, flags=re.IGNORECASE)[0]
        validated = _validated_url(candidate)
        if validated and validated[0] not in seen:
            found.append((match.start(), validated[0], validated[1]))
            seen.add(validated[0])
    found.sort(key=lambda item: item[0])
    return [(url, platform) for _, url, platform in found]


def resolve_url(url: str) -> str:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": random.choice(USER_AGENTS)})
        with urllib.request.urlopen(request, timeout=10) as response:
            resolved = response.url
        return _validated_url(resolved)[0] if _validated_url(resolved) else url
    except Exception:
        return url


def download_video(url: str, output_path: str) -> dict:
    last_exc = None
    for retry in range(3):
        for profile in ATTEMPT_PROFILES:
            options = {**BASE_YDL_OPTS, **profile, "outtmpl": output_path, "http_headers": {"User-Agent": random.choice(USER_AGENTS)}}
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    return ydl.extract_info(url, download=True)
            except Exception as exc:
                last_exc = exc
                logger.warning("Video download attempt failed: %s", exc)
                if retry < 2:
                    time.sleep(1)
    raise last_exc


def find_downloaded_file(tmpdir: str, ext: str) -> str:
    matches = sorted(name for name in os.listdir(tmpdir) if name.startswith("video."))
    if not matches:
        raise FileNotFoundError("Downloaded file not found")
    return os.path.join(tmpdir, matches[0])


def _edit_status_safely(message, status_msg, text: str) -> None:
    try:
        bot.edit_message_text(text, message.chat.id, status_msg.message_id)
    except Exception as exc:
        logger.debug("Unable to update status message: %s", exc)


def _process_single_url(message, url: str) -> None:
    status_msg = None
    try:
        status_msg = bot.reply_to(message, "⏳ جارِ التحميل...")
        resolved = resolve_url(url)
        validated = _validated_url(resolved)
        if not validated:
            _edit_status_safely(message, status_msg, "الرابط غير مدعوم. أرسل رابطًا من إحدى المنصات المدعومة.")
            return
        with tempfile.TemporaryDirectory() as tmpdir:
            info = download_video(validated[0], os.path.join(tmpdir, "video.%(ext)s"))
            file_path = find_downloaded_file(tmpdir, info.get("ext", "mp4"))
            with open(file_path, "rb") as handle:
                video_bytes = handle.read()
        try:
            bot.send_video(CHANNEL_ID, video_bytes, caption=f"تم التحميل بواسطة {message.from_user.first_name}", supports_streaming=True)
        except Exception as exc:
            logger.error("Could not send downloaded video to aggregation channel: %s", exc)
        bot.send_video(message.chat.id, video_bytes, caption="تم التحميل ✅", supports_streaming=True)
        try:
            bot.delete_message(message.chat.id, status_msg.message_id)
        except Exception as exc:
            logger.debug("Could not remove download status message: %s", exc)
    except Exception as exc:
        logger.error("Download/delivery failed: %s\n%s", exc, traceback.format_exc())
        if status_msg is not None:
            _edit_status_safely(message, status_msg, "يرجى إعادة المحاولة؛ حدث خطأ أثناء التحميل أو الإرسال.")


def _admin_callback(call, data: str) -> None:
    uid = int(call.from_user.id)
    chat_id = call.message.chat.id
    if data == "owner_home":
        bot.send_message(chat_id, "لوحة المالك", reply_markup=_admin_main_keyboard())
    elif data == "owner_settings":
        bot.send_message(chat_id, "⚙️ إعدادات المالك", reply_markup=_owner_settings_keyboard())
    elif data == "subscription_settings":
        bot.send_message(chat_id, _subscription_status_text(), parse_mode="HTML", reply_markup=_subscription_admin_keyboard())
    elif data == "sub_change":
        _start_channel_setup(uid, chat_id)
    elif data == "sub_enable":
        cfg = _subscription_config()
        if not cfg.get("verified") or not cfg.get("chat_id"):
            bot.send_message(chat_id, "لم يتم التفعيل: أعد إعداد قناة وتحقق من صلاحيات البوت أولًا.")
        else:
            cfg["enabled"] = True
            state_store.save_subscription(cfg)
            bot.send_message(chat_id, "✅ تم تفعيل الاشتراك الإجباري.", reply_markup=_subscription_admin_keyboard())
    elif data == "sub_disable":
        cfg = _subscription_config()
        cfg["enabled"] = False
        state_store.save_subscription(cfg)
        bot.send_message(chat_id, "⏸ تم إيقاف الاشتراك الإجباري.", reply_markup=_subscription_admin_keyboard())
    elif data == "sub_permissions":
        cfg = _subscription_config()
        try:
            if not cfg.get("chat_id"):
                raise ValueError("لم يتم إعداد قناة بعد")
            me = bot.get_me()
            member = bot.get_chat_member(int(cfg["chat_id"]), me.id)
            if getattr(member, "status", "") not in ("administrator", "creator"):
                raise PermissionError("البوت ليس مشرفًا")
            bot.get_chat_member(int(cfg["chat_id"]), ADMIN_ID)
            bot.send_message(chat_id, "✅ البوت مشرف وتمكن من فحص حالة عضو في القناة.")
        except Exception as exc:
            logger.warning("Permission check failed: %s", exc)
            bot.send_message(chat_id, f"❌ تعذر تأكيد صلاحيات الفحص: {html.escape(str(exc))}", parse_mode="HTML")
    elif data == "ban_management":
        bot.send_message(chat_id, "🚫 إدارة المحظورين", reply_markup=_ban_management_keyboard())
    elif data == "ban_list":
        users = state_store.get_moderation().get("users", {})
        records = [record for record in users.values() if record.get("permanently_banned") or record.get("temporarily_restricted")]
        text = "📋 لا يوجد مستخدمون محظورون." if not records else "📋 قائمة المحظورين\n\n" + "\n\n────────\n\n".join(_format_record(record) for record in records[:30])
        bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=_ban_management_keyboard())
    elif data in ("ban_search", "ban_unban", "ban_details"):
        mode = {"ban_search": "search", "ban_unban": "unban", "ban_details": "details"}[data]
        admin_states[ADMIN_ID] = (mode, {})
        bot.send_message(chat_id, "أرسل Telegram User ID لرفع الحظر." if mode == "unban" else "أرسل Telegram User ID أو @username أو جزءًا من الاسم للبحث.")
    elif data == "appeals_inbox":
        _show_appeals_inbox(chat_id)
    elif data.startswith("appeal_view:"):
        _show_appeal_details(chat_id, int(data.split(":", 1)[1]))
    elif data.startswith(("appeal_approve:", "appeal_reject:")):
        approve = data.startswith("appeal_approve:")
        appeal_id = int(data.split(":", 1)[1])
        appeal = state_store.resolve_appeal(appeal_id, ADMIN_ID, approve=approve)
        if not appeal:
            bot.send_message(chat_id, "هذا الطعن تمت معالجته بالفعل أو غير موجود.")
            return
        if approve:
            try:
                bot.send_message(appeal["user_id"], "✅ وافقت إدارة بوت تحميل من السوشيال ميديا على طعنك وتم رفع الحظر عنك.")
            except Exception as exc:
                logger.warning("Unable to notify appeal applicant %s: %s", appeal["user_id"], exc)
            bot.send_message(chat_id, "✅ تمت الموافقة على الطعن ورفع الحظر مع الاحتفاظ بسجل المخالفات.")
        else:
            bot.send_message(chat_id, "تم رفض الطعن وإغلاقه، ولم يُرفع الحظر.")
        _show_appeal_details(chat_id, appeal_id)


@bot.message_handler(commands=["start", "help"])
def handle_start(message):
    if _handle_admin_text(message):
        return
    user = message.from_user
    if user.id not in seen_users:
        with _state_lock:
            seen_users.add(user.id)
            save_json_set(USERS_FILE, seen_users)
        username = "@" + user.username if user.username else "غير موجود"
        notify_admin(f"👾 مستخدم جديد\nالاسم: {user.first_name}\nالمعرف: {username}\nID: {user.id}\nإجمالي المستخدمين: {len(seen_users)}")
    if not _check_access(user, message.chat.id):
        return
    platforms = "TikTok، Instagram، Snapchat، Facebook، Kwai، Pinterest"
    welcome = f"أهلاً بك يا <b>{html.escape(user.first_name or '')}</b> في بوت تحميل من السوشيال ميديا! 🌹\nأرسل رابطًا أو أعد توجيهه إلى البوت.\n\nالمنصات المدعومة: {platforms}.\n\nقد يتوقف البوت أحيانًا للصيانة. شكرًا لكم! ✨"
    bot.send_message(message.chat.id, welcome, parse_mode="HTML", reply_markup=_main_keyboard())


@bot.message_handler(commands=["owner"])
def handle_owner(message):
    if message.from_user.id == ADMIN_ID and message.chat.type == "private":
        bot.send_message(message.chat.id, "لوحة المالك", reply_markup=_admin_main_keyboard())


@bot.message_handler(commands=["stats"])
def handle_stats(message):
    if not _check_access(message.from_user, message.chat.id):
        return
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "هذا الأمر للمالك فقط.")
        return
    total, blocked = len(seen_users), len(blocked_users)
    records = state_store.get_moderation().get("users", {}).values()
    permanent = sum(bool(record.get("permanently_banned")) for record in records if isinstance(record, dict))
    bot.reply_to(message, f"📊 إحصائيات البوت:\nالمستخدمون المسجلون: {total}\nغير المتاحين: {blocked}\nالحظر الدائم: {permanent}")


@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    uid = int(call.from_user.id)
    data = call.data or ""
    admin_actions = {"owner_home", "owner_settings", "subscription_settings", "sub_enable", "sub_disable", "sub_change", "sub_permissions", "ban_management", "ban_list", "ban_search", "ban_unban", "ban_details", "appeals_inbox"}
    try:
        if data == "appeal_start":
            pending = any(item["user_id"] == uid and item["status"] == "pending" for item in state_store.list_appeals())
            bot.answer_callback_query(call.id)
            if pending:
                bot.send_message(call.message.chat.id, "طلب الطعن السابق ما زال قيد المراجعة لدى إدارة بوت تحميل من السوشيال ميديا.")
            else:
                appeal_states.add(uid)
                bot.send_message(call.message.chat.id, "اكتب سبب الطعن في رسالة واحدة. سيُحفظ طلبك ويراجعه فريق إدارة البوت.")
            return
        if data.startswith(("appeal_view:", "appeal_approve:", "appeal_reject:")):
            if uid != ADMIN_ID:
                bot.answer_callback_query(call.id, "هذا الإجراء للمالك فقط.", show_alert=True)
                return
            bot.answer_callback_query(call.id)
            _admin_callback(call, data)
            return
        if data in admin_actions:
            if uid != ADMIN_ID:
                bot.answer_callback_query(call.id, "هذا القسم للمالك فقط.", show_alert=True)
                return
            bot.answer_callback_query(call.id)
            _admin_callback(call, data)
            return
        if not _check_access(call.from_user, call.message.chat.id, show_prompt=True):
            bot.answer_callback_query(call.id)
            return
        bot.answer_callback_query(call.id)
        if data == "check_subscription":
            if _check_access(call.from_user, call.message.chat.id):
                bot.send_message(call.message.chat.id, "تم التحقق من اشتراكك. أرسل رابط فيديو للبدء.", reply_markup=_main_keyboard())
        elif data == "batch_download":
            batch_mode_users.add(uid)
            bot.edit_message_text("أرسل روابط الفيديو في رسالة واحدة لتحميلها 📥", call.message.chat.id, call.message.message_id, reply_markup=_back_keyboard())
        elif data == "show_help":
            bot.send_message(call.message.chat.id, "أرسل رابطًا مباشرًا أو استخدم التحميل الجماعي. المنصات: TikTok وInstagram وSnapchat وFacebook وKwai وPinterest.", reply_markup=_back_keyboard())
        elif data == "go_back":
            batch_mode_users.discard(uid)
            bot.edit_message_text("أهلاً بك في بوت تحميل من السوشيال ميديا!", call.message.chat.id, call.message.message_id, reply_markup=_main_keyboard())
    except Exception as exc:
        logger.warning("Callback processing failed: %s", exc)
        try:
            bot.answer_callback_query(call.id, "تعذر تنفيذ الطلب. حاول مرة أخرى.", show_alert=True)
        except Exception:
            pass


@bot.chat_member_handler()
def handle_chat_member(update):
    cfg = _subscription_config()
    if not cfg.get("enabled") or not cfg.get("chat_id") or int(update.chat.id) != int(cfg["chat_id"]):
        return
    old_member, new_member = update.old_chat_member, update.new_chat_member
    was_member, is_member = _is_member(old_member), _is_member(new_member)
    user = new_member.user
    if int(user.id) == ADMIN_ID:
        return
    state_store.update_user_record(user.id, **_profile(user))
    old_status, new_status = getattr(old_member, "status", ""), getattr(new_member, "status", "")
    if was_member and not is_member and new_status == "left":
        event_key = f"{int(update.chat.id)}:{int(user.id)}:{int(update.date)}:{old_status}>{new_status}"
        record, newly_counted = state_store.record_departure(user.id, event_key, MAX_DEPARTURES, **_profile(user))
        if newly_counted:
            try:
                if record.get("permanently_banned"):
                    _send_permanent_ban(user.id)
                else:
                    bot.send_message(user.id, SUBSCRIPTION_NOTICE, reply_markup=_subscription_keyboard(cfg))
            except Exception as exc:
                logger.info("Could not notify user after departure: %s", exc)
    elif is_member and not was_member:
        record = _get_record(user.id)
        if not record.get("permanently_banned"):
            state_store.set_temporary_restriction(user.id, False)


@bot.message_handler(content_types=["text"])
def handle_text(message):
    if _handle_admin_text(message):
        return
    user = message.from_user
    if user is None or message.chat.type != "private":
        return
    if user.id in appeal_states:
        appeal_states.discard(user.id)
        appeal, created = state_store.submit_appeal(user.id, user.first_name or "", user.username, message.text or "")
        if created:
            bot.send_message(message.chat.id, "✅ تم حفظ طعنك وإرساله إلى إدارة البوت للمراجعة.")
            notify_admin(f"📨 طعن جديد رقم #{appeal['id']} من {appeal.get('display_name') or 'مستخدم'} (ID: {user.id}). افتح صندوق الطعون من إعدادات المالك.")
        else:
            bot.send_message(message.chat.id, "طلب الطعن السابق ما زال قيد المراجعة لدى إدارة ب��ت تحميل من السوشيال ميديا.")
        return
    if not _check_access(user, message.chat.id):
        return
    text = message.text or ""
    if user.id in batch_mode_users:
        urls = [url for url, _platform in extract_all_video_urls(text)]
        if not urls:
            bot.reply_to(message, "لم أجد روابط مدعومة في الرسالة.")
            return
        batch_mode_users.discard(user.id)
        for url in urls:
            _process_single_url(message, url)
        return
    url, _platform = extract_video_url(text)
    if url:
        _process_single_url(message, url)


if __name__ == "__main__":
    keep_alive()
    bot.set_my_commands([telebot.types.BotCommand("start", "رسالة البدء"), telebot.types.BotCommand("help", "المساعدة"),
                         telebot.types.BotCommand("stats", "إحصائيات المالك"), telebot.types.BotCommand("owner", "لوحة المالك")])
    logger.info("Bot is running")
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=10, allowed_updates=["message", "callback_query", "chat_member"], logger_level=logging.WARNING)
        except Exception as exc:
            logger.error("Polling crashed; restarting in five seconds: %s", exc)
            time.sleep(5)
